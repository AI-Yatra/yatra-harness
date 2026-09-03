"""Reassembling a streamed completion into the response the adapter expects.

A turn against a remote model is a blocking request. The operator sees
nothing for however long it takes and then sees everything at once, which
reads as a hang -- and a run that looks hung is a run someone kills, often
just before it would have succeeded.

Streaming fixes the appearance and creates a parsing problem. Server-sent
events split a completion at arbitrary boundaries: content arrives in
fragments, tool-call arguments arrive as string pieces that are only valid
JSON once concatenated, and the stream ends with a sentinel that is not JSON
at all. This module is that reassembly, and nothing else.

The design constraint is at the bottom: `as_payload` produces exactly the
shape a non-streamed response has, so every adapter, normalizer and test
downstream cannot tell the difference. Streaming is a transport detail and it
should not leak into the layer that decides what an action is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

DONE = "[DONE]"


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """The `data:` payloads of a server-sent event stream, in order.

    Blank lines, `:` comments used as keep-alives, and non-`data` fields are
    skipped. So is the terminating `[DONE]`, which is not JSON -- handing it
    to a parser is the classic way a streaming client dies on the last line
    of a perfectly successful response.
    """
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == DONE:
            continue
        yield payload


class StreamAccumulator:
    """Folds streamed chunks back into one completion."""

    def __init__(self, on_delta: Callable[[str], None] | None = None) -> None:
        self.on_delta = on_delta
        self._content: list[str] = []
        self._calls: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, Any] = {}
        self._finish_reason: str = ""
        self._extra: dict[str, Any] = {}
        self._error: str = ""

    @property
    def content(self) -> str:
        return "".join(self._content)

    @property
    def error(self) -> str:
        """A failure the provider announced inside the stream, if it did.

        A stream that dies after output has started still ends with a 200 and
        a plausible-looking partial completion. Without this the caller cannot
        tell that answer apart from a short one, and a truncated turn is
        accepted as a finished one.
        """
        return self._error

    #: Top-level chunk keys that are not part of the OpenAI completion shape
    #: but that the caller needs. GMI's router reports which model it picked
    #: this way, in a frame of its own after the content.
    CARRIED = ("routing_metadata",)

    def feed(self, chunk: dict[str, Any]) -> None:
        """Absorb one decoded chunk. Anything unexpected in it is ignored.

        Providers interleave usage-only and keep-alive chunks with the real
        ones, so a missing `choices` is normal rather than an error.
        """
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        for key in self.CARRIED:
            value = chunk.get(key)
            if isinstance(value, dict):
                self._extra[key] = value
        failure = chunk.get("error")
        if failure:
            self._error = (
                str(failure.get("message") or failure) if isinstance(failure, dict) else str(failure)
            )
        choices = chunk.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0]
        if not isinstance(choice, dict):
            return
        if choice.get("finish_reason"):
            self._finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._content.append(text)
            if self.on_delta is not None:
                self.on_delta(text)
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                self._absorb_tool_call(call)

    #: Keys on a streamed tool call that are neither the identity nor the
    #: function, and that the provider needs handed back verbatim. Gemini
    #: puts its `thought_signature` here, and rejects the next request with a
    #: 400 if it does not come back.
    PASSTHROUGH = ("extra_content",)

    def _absorb_tool_call(self, call: dict[str, Any]) -> None:
        try:
            index = int(call.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        entry = self._calls.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if call.get("id"):
            entry["id"] = str(call["id"])
        for name in self.PASSTHROUGH:
            # Arrives once, on whichever chunk opens the call, so it is kept
            # the first time it is seen rather than overwritten with nothing.
            if call.get(name) is not None:
                entry[name] = call[name]
        function = call.get("function")
        if not isinstance(function, dict):
            return
        if function.get("name"):
            # The name can arrive after the first argument fragment, so it is
            # set whenever it appears rather than only on the opening chunk.
            entry["function"]["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            # Concatenated, never parsed per fragment: a fragment is a slice
            # of a JSON document and is almost never valid JSON on its own.
            entry["function"]["arguments"] += arguments

    def tool_calls(self) -> list[dict[str, Any]]:
        """The reassembled calls, ordered by the index the provider gave them."""
        return [self._calls[key] for key in sorted(self._calls)]

    def as_payload(self) -> dict[str, Any]:
        """The completion, in the shape a non-streamed response has."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        calls = self.tool_calls()
        if calls:
            message["tool_calls"] = calls
        return {
            "choices": [
                {"message": message, "finish_reason": self._finish_reason or "stop"}
            ],
            "usage": self._usage,
            **self._extra,
        }
