"""The message history a session accumulates, and what to do when it fills.

Messages are held in the OpenAI chat shape because it is the one every route
in this repository speaks natively except Anthropic, which `model.py`
converts on the way out. Holding one canonical shape means compaction,
persistence and token accounting are written once.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the model asked for, with its arguments already parsed."""

    id: str
    name: str
    arguments: dict[str, Any]
    #: Provider-specific fields that came with the call and have to go back
    #: with it, carried opaquely because their meaning belongs to the
    #: provider. Gemini 3 puts an encrypted `thought_signature` here and
    #: rejects the next request with a 400 if it is missing.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """What one inference produced: some prose, and zero or more tool calls.

    Both at once is normal and is the thing the batch path cannot express:
    `ActionProposal` is exactly one action, so a model that says "I'll read
    the config first" alongside a `read_file` call loses one of the two.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def estimate_tokens(text: str) -> int:
    """A deliberately rough token count.

    Four characters per token is wrong for code and wrong for prose, in
    opposite directions. It is used only to decide when to compact and to
    draw a context meter, and being wrong by a third does not change either
    decision. Calling a real tokenizer per keystroke would.
    """
    return max(1, len(text) // 4)


def message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    total = estimate_tokens(content) if isinstance(content, str) else 0
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += estimate_tokens(str(function.get("name", "")))
        total += estimate_tokens(str(function.get("arguments", "")))
    # Every message carries role and delimiter overhead on the wire.
    return total + 4


class Conversation:
    """One thread: a fixed system prompt and a growing list of turns.

    The system prompt is held apart from the history and always re-emitted
    first. Providers cache on prompt prefixes, so anything stable belongs at
    the front and must not move; rebuilding the list with the system message
    somewhere else costs a cache miss on every turn.
    """

    def __init__(self, system: str, *, max_tokens: int = 120_000) -> None:
        self.system = system
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []
        self.compactions = 0

    # ------------------------------------------------------------- appending

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, turn: AssistantTurn) -> None:
        """Record an assistant turn exactly as the wire format expects it.

        A turn with no text and no calls is dropped rather than stored: some
        providers emit an empty final chunk, and an assistant message with
        neither content nor tool_calls is rejected by others on the next
        request. Storing it poisons the rest of the session.
        """
        if not turn.text and not turn.tool_calls:
            return
        message: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                    # Handed straight back. Dropping it is what makes Gemini
                    # reject every turn after the first tool call.
                    **call.extra,
                }
                for call in turn.tool_calls
            ]
        self.messages.append(message)

    def add_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
        )

    def add_system_note(self, text: str) -> None:
        """An out-of-band note to the model, phrased as a user message.

        Providers reject a second system message mid-history, and an
        assistant message the model did not write teaches it that it says
        things it did not say. A user-role note is the honest option.
        """
        self.messages.append({"role": "user", "content": text})

    # ------------------------------------------------------------- accounting

    def token_estimate(self) -> int:
        return estimate_tokens(self.system) + sum(message_tokens(m) for m in self.messages)

    def wire_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system}, *self.messages]

    # ------------------------------------------------------------- compaction

    def needs_compaction(self, headroom: float = 0.8) -> bool:
        return self.token_estimate() > self.max_tokens * headroom

    def compact(self, summary: str, *, keep_recent: int = 6) -> int:
        """Replace everything but the tail with *summary*. Returns tokens freed.

        The tail is trimmed to a safe boundary before it is kept: a `tool`
        message whose matching assistant call has been summarised away is a
        dangling reference, and providers reject the request outright rather
        than ignoring it. This is the failure that makes naive compaction
        break a session on exactly the long conversations it exists to save.
        """
        before = self.token_estimate()
        tail = self.messages[-keep_recent:] if keep_recent > 0 else []
        tail = _trim_to_safe_start(tail)
        self.messages = [
            {
                "role": "user",
                "content": (
                    "Summary of the earlier part of this conversation, which has been "
                    f"compacted to save context:\n\n{summary}"
                ),
            },
            *tail,
        ]
        self.compactions += 1
        return max(0, before - self.token_estimate())

    def transcript(self, limit: int = 0) -> str:
        """A plain-text rendering of the history, for the summariser."""
        chosen = self.messages[:limit] if limit else self.messages
        lines: list[str] = []
        for message in chosen:
            role = str(message.get("role", "?"))
            content = str(message.get("content") or "").strip()
            calls = message.get("tool_calls") or []
            if calls:
                named = ", ".join(
                    str((c.get("function") or {}).get("name", "?")) for c in calls
                )
                content = f"{content}\n[called: {named}]".strip()
            if role == "tool":
                content = f"[{message.get('name', 'tool')} result] {content}"
            if content:
                lines.append(f"{role}: {content[:2_000]}")
        return "\n\n".join(lines)

    # ------------------------------------------------------------ persistence

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "system": self.system,
            "messages": self.messages,
            "compactions": self.compactions,
        }
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, *, system: str, max_tokens: int = 120_000) -> Conversation:
        """Reopen a saved thread, falling back to a fresh one if unreadable.

        The system prompt is taken from the caller rather than the file: it
        names the current working directory and the current tool set, and a
        stale one would describe a session that no longer exists.
        """
        conversation = cls(system, max_tokens=max_tokens)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return conversation
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            return conversation
        messages = raw.get("messages")
        if isinstance(messages, list):
            conversation.messages = [m for m in messages if isinstance(m, dict)]
            conversation.messages = _trim_to_safe_start(conversation.messages)
        conversation.compactions = int(raw.get("compactions") or 0)
        return conversation


def _trim_to_safe_start(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading messages until the list starts somewhere a request may.

    A history that opens with a `tool` result refers to an assistant tool call
    that is no longer present. Providers reject that, so the orphans are
    dropped rather than sent.
    """
    kept = list(messages)
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept
