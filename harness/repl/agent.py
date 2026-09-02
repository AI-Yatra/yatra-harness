"""The turn loop.

One user message starts a turn. A turn is: send the whole thread, read what
comes back, run whatever tools it asked for, append the results, and send
again. It ends when the model replies without asking for a tool -- or when a
bound is hit, which is the difference between an agent and a runaway loop.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harness.core.contracts import ToolSpec
from harness.core.errors import HarnessError, PermanentProviderError, TransientProviderError
from harness.repl.tools import ReplToolset

from .approvals import Gate
from .conversation import AssistantTurn, Conversation, ToolCall
from .model import ChatModel

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import HarnessConfig


class Interrupted(Exception):
    """The operator asked for the current turn to stop."""


@dataclass
class Limits:
    """What bounds one turn, so a loop cannot run forever unattended."""

    max_steps: int = 40
    max_tool_calls: int = 60
    #: Consecutive tool errors before the loop gives up on its own.
    max_consecutive_errors: int = 6


@dataclass
class TurnStats:
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0


@dataclass
class Events:
    """Everything the loop tells the outside world.

    A structure of callbacks rather than direct printing, so the loop can be
    driven by a test with no terminal attached and asserted on exactly.
    """

    on_text: Callable[[str], None] = lambda _text: None
    on_delta: Callable[[str], None] | None = None
    on_tool_start: Callable[[ToolCall, ToolSpec], None] = lambda _call, _spec: None
    on_tool_end: Callable[[ToolCall, str, bool], None] = lambda _call, _detail, _ok: None
    on_tool_denied: Callable[[ToolCall, str], None] = lambda _call, _reason: None
    on_notice: Callable[[str], None] = lambda _text: None
    on_thinking: Callable[[bool], None] = lambda _busy: None


class Agent:
    """One session: a thread, a working directory, a model and a gate."""

    def __init__(
        self,
        *,
        model: ChatModel,
        conversation: Conversation,
        toolset: ReplToolset,
        gate: Gate,
        config: HarnessConfig,
        events: Events | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.model = model
        self.conversation = conversation
        self.toolset = toolset
        self.gate = gate
        self.config = config
        self.events = events or Events()
        self.limits = limits or Limits()
        self.specs: dict[str, ToolSpec] = {spec.name: spec for spec in toolset.specs()}
        self._cancel = threading.Event()

    # -------------------------------------------------------------- interrupt

    def cancel(self) -> None:
        """Ask the running turn to stop at the next safe point."""
        self._cancel.set()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise Interrupted

    # ------------------------------------------------------------------- turn

    def send(self, message: str) -> TurnStats:
        """Run one user message to completion. Returns what it cost."""
        self._cancel.clear()
        self.conversation.add_user(message)
        return self._drive()

    def resume_after_interrupt(self, note: str) -> TurnStats:
        """Continue the thread after the operator cut a turn short."""
        self._cancel.clear()
        self.conversation.add_system_note(note)
        return self._drive()

    def _drive(self) -> TurnStats:
        stats = TurnStats()
        consecutive_errors = 0

        while True:
            self._check_cancelled()
            if stats.steps >= self.limits.max_steps:
                self.events.on_notice(
                    f"Stopped after {stats.steps} steps. Say 'continue' to keep going."
                )
                self.conversation.add_system_note(
                    "You reached this turn's step limit and were stopped. Summarise where "
                    "you got to and what remains."
                )
                return stats
            if stats.tool_calls >= self.limits.max_tool_calls:
                self.events.on_notice(f"Stopped after {stats.tool_calls} tool calls.")
                return stats

            self._compact_if_needed()
            stats.steps += 1
            turn = self._infer()
            self.conversation.add_assistant(turn)
            _account(stats, turn)

            if turn.text.strip() and self.events.on_delta is None:
                self.events.on_text(turn.text)

            if not turn.wants_tools:
                return stats

            errored_this_step = False
            for call in turn.tool_calls:
                self._check_cancelled()
                stats.tool_calls += 1
                ok = self._run_tool(call)
                if not ok:
                    stats.errors += 1
                    errored_this_step = True

            consecutive_errors = consecutive_errors + 1 if errored_this_step else 0
            if consecutive_errors >= self.limits.max_consecutive_errors:
                self.events.on_notice(
                    f"{consecutive_errors} steps in a row failed; stopping so it does not spin."
                )
                self.conversation.add_system_note(
                    "Several tool calls failed in a row and the loop was stopped. Explain "
                    "what is going wrong rather than trying again."
                )
                return stats

    # ------------------------------------------------------------- inference

    def _infer(self) -> AssistantTurn:
        """One model call, with the spinner and streaming wired up."""
        specs = tuple(self.specs.values())
        streaming = self.model.streams and self.events.on_delta is not None
        self.events.on_thinking(True)
        try:
            return self.model.converse(
                self.conversation.wire_messages(),
                specs,
                on_delta=self.events.on_delta if streaming else None,
            )
        except (TransientProviderError, PermanentProviderError) as exc:
            # Surfaced as a turn rather than raised: the thread stays usable
            # and the operator can retry, change model, or ask something else.
            raise ModelUnavailable(str(exc)) from exc
        finally:
            self.events.on_thinking(False)

    # ------------------------------------------------------------------ tools

    def _run_tool(self, call: ToolCall) -> bool:
        spec = self.specs.get(call.name)
        if spec is None:
            known = ", ".join(sorted(self.specs))
            self.events.on_tool_denied(call, f"no such tool: {call.name}")
            self._record(call, f"No tool named {call.name!r}. Available tools: {known}.")
            return False

        self.events.on_tool_start(call, spec)

        decision = self.gate.check(spec, call.arguments)
        if not decision.allowed:
            self.events.on_tool_denied(call, decision.reason)
            self._record(call, decision.reason)
            return False

        outcome = self.toolset.dispatch(call.name, call.arguments)
        detail = outcome.detail or ("done" if outcome.ok else outcome.content)
        self.events.on_tool_end(call, detail, outcome.ok)
        self._record(call, outcome.content if outcome.ok else f"Error: {outcome.content}")
        return outcome.ok

    def _record(self, call: ToolCall, content: str) -> None:
        self.conversation.add_tool_result(call.id, call.name, content)

    # ------------------------------------------------------------- compaction

    def _compact_if_needed(self) -> None:
        """Summarise the old part of the thread before the window fills.

        The summary is written by the same model, from the same thread, and
        the request is deliberately made without tools: a summariser that can
        call tools starts doing the task again.
        """
        if not self.conversation.needs_compaction():
            return
        self.events.on_notice("Context is nearly full; compacting the earlier conversation.")
        try:
            freed = self.compact()
        except (HarnessError, ModelUnavailable) as exc:
            self.events.on_notice(f"Could not compact ({exc}); continuing.")
            return
        self.events.on_notice(f"Compacted, freeing roughly {freed} tokens.")

    def compact(self) -> int:
        """Summarise and replace the earlier part of the thread."""
        transcript = self.conversation.transcript()
        request = [
            {
                "role": "system",
                "content": (
                    "Summarise this coding session for an agent that will continue it. "
                    "Keep: what was asked, decisions made, files read or changed and why, "
                    "commands run and their results, and anything still outstanding. "
                    "Drop pleasantries and duplicated file contents. Be specific about "
                    "paths and names. Write it as notes, not prose."
                ),
            },
            {"role": "user", "content": transcript[-60_000:]},
        ]
        summary = self.model.converse(request, ()).text.strip()
        if not summary:
            raise PermanentProviderError("the summariser returned nothing")
        return self.conversation.compact(summary)

    # ------------------------------------------------------------ persistence

    def save(self, path: Path) -> None:
        self.conversation.save(path)


class ModelUnavailable(HarnessError):
    """The route could not be reached, or refused the request."""


def _account(stats: TurnStats, turn: AssistantTurn) -> None:
    usage = turn.usage or {}
    stats.input_tokens += int(
        usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    )
    stats.output_tokens += int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )


def describe_arguments(call: ToolCall, limit: int = 60) -> str:
    """A one-line rendering of a call's arguments, for a tool card."""
    if call.name == "run_command":
        command = call.arguments.get("command")
        if isinstance(command, list):
            return " ".join(str(p) for p in command)[:limit]
        return str(command)[:limit]
    for key in ("path", "pattern", "query"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value:
            return value[:limit]
    if not call.arguments:
        return ""
    return json.dumps(call.arguments)[:limit]
