"""Talking to a model in conversation shape.

`providers.complete` normalizes a response down to one `ActionProposal`,
because that is what the batch loop consumes. A conversation needs the
opposite: the prose and every tool call, with nothing discarded and no JSON
envelope demanded of a model that simply wants to answer.

Transport is not reimplemented here. `_HTTPProvider.send` owns timeouts,
status classification and the error vocabulary; this module owns the request
body and the reading of the reply.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from harness.core.contracts import ToolSpec
from harness.core.errors import PermanentProviderError, ProviderError, TransientProviderError
from harness.models.providers import AnthropicProvider, OpenAICompatibleProvider, provider_for

from .conversation import AssistantTurn, ToolCall

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import RouteConfig

DeltaCallback = Callable[[str], None]

#: Fields that arrive on a tool call and must be handed back with it. Their
#: contents belong to the provider; this layer only refuses to lose them.
PASSTHROUGH_KEYS = ("extra_content",)


class ChatModel:
    """One route, spoken to in whole conversations."""

    def __init__(
        self,
        route: RouteConfig,
        *,
        max_output_tokens: int = 8192,
        retries: int = 2,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.route = route
        self.max_output_tokens = max_output_tokens
        self.provider = provider_for(route)
        self.name = route.model
        self.route_name = route.name
        self.retries = max(0, retries)
        self.backoff_seconds = max(0.0, backoff_seconds)

    @property
    def streams(self) -> bool:
        return isinstance(self.provider, OpenAICompatibleProvider)

    def converse(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        on_delta: DeltaCallback | None = None,
    ) -> AssistantTurn:
        """One inference over the whole thread, retried while it is worth it.

        The batch path gets its retries from the model router. The REPL talks
        to a route directly, so without this a single 503 -- which Gemini
        returns freely under load -- ends the turn and throws away the work
        that led up to it.
        """
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                return self._once(messages, tools, on_delta=on_delta)
            except TransientProviderError:
                if attempt == attempts - 1:
                    raise
                time.sleep(self.backoff_seconds * (2**attempt))
        raise PermanentProviderError("unreachable")  # pragma: no cover

    def _once(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        on_delta: DeltaCallback | None = None,
    ) -> AssistantTurn:
        if isinstance(self.provider, AnthropicProvider):
            body = _anthropic_body(self.route, messages, tools, self.max_output_tokens)
            payload = self.provider.send(body)
            return _read_anthropic(payload)
        if isinstance(self.provider, OpenAICompatibleProvider):
            body = _openai_body(self.route, messages, tools, self.max_output_tokens)
            stream = bool(getattr(self.route, "stream", False)) and on_delta is not None
            payload = self.provider.send(body, stream=stream, on_delta=on_delta)
            return _read_openai(payload)
        raise PermanentProviderError(
            f"route {self.route.name!r} uses provider kind {self.route.kind!r}, which has no "
            "conversational adapter; the REPL needs an openai_compatible, ollama, vllm or "
            "anthropic route"
        )


# ── OpenAI-compatible wire format ──────────────────────────────────────────


def _openai_body(
    route: RouteConfig,
    messages: list[dict[str, Any]],
    tools: tuple[ToolSpec, ...],
    max_output_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": route.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_output_tokens,
    }
    if tools:
        body["tools"] = [tool.as_model_tool() for tool in tools]
        body["tool_choice"] = "auto"
    return body


def _read_openai(payload: dict[str, Any]) -> AssistantTurn:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PermanentProviderError("provider response has no assistant message") from exc
    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            # A nameless call cannot be dispatched. Dropping it is better than
            # failing the turn: the rest of the response is usually fine.
            continue
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call-{index}"),
                name=name,
                arguments=_parse_arguments(function.get("arguments")),
                extra={k: raw[k] for k in PASSTHROUGH_KEYS if raw.get(k) is not None},
            )
        )
    usage = payload.get("usage")
    return AssistantTurn(
        text=str(message.get("content") or ""),
        tool_calls=tuple(calls),
        usage=dict(usage) if isinstance(usage, dict) else {},
        stop_reason=str(choice.get("finish_reason") or ""),
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments as an object, whatever the model actually sent.

    A malformed argument string is returned as a parse error in the arguments
    rather than raised: the dispatcher turns it into a tool result the model
    can read and retry from, which is far more useful than killing the turn.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"__parse_error__": f"arguments were not valid JSON: {exc}"}
    return parsed if isinstance(parsed, dict) else {"__parse_error__": "arguments were not an object"}


# ── Anthropic wire format ──────────────────────────────────────────────────


def _anthropic_body(
    route: RouteConfig,
    messages: list[dict[str, Any]],
    tools: tuple[ToolSpec, ...],
    max_output_tokens: int,
) -> dict[str, Any]:
    system, converted = _to_anthropic_messages(messages)
    body: dict[str, Any] = {
        "model": route.model,
        "system": system,
        "messages": converted,
        "max_tokens": max_output_tokens,
        "temperature": 0,
    }
    if tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]
    return body


def _to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert the canonical OpenAI-shaped history to Anthropic blocks.

    Two shape differences do real work here. Tool results are user-role
    content blocks rather than their own role, and consecutive results must be
    merged into a single user message: Anthropic rejects two user messages in
    a row, which is exactly what a turn with three parallel tool calls
    produces if each is emitted separately.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
            continue
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        flush_results()
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = str(message.get("content") or "")
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for raw in message.get("tool_calls") or []:
                function = raw.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(raw.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": _parse_arguments(function.get("arguments")),
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": "user", "content": str(message.get("content") or "")})

    flush_results()
    return "\n\n".join(part for part in system_parts if part), out


def _read_anthropic(payload: dict[str, Any]) -> AssistantTurn:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise PermanentProviderError("anthropic response has no content blocks")
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            arguments = block.get("input")
            calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
    usage = payload.get("usage")
    return AssistantTurn(
        text="".join(text_parts),
        tool_calls=tuple(calls),
        usage=dict(usage) if isinstance(usage, dict) else {},
        stop_reason=str(payload.get("stop_reason") or ""),
    )


class RouteChain:
    """Several routes tried in order, so one exhausted quota is not the end.

    The batch path gets this from the model router. The REPL talks to a route
    directly, which is fine until a free tier runs out mid-conversation and
    the session is simply over.

    Two rules keep it honest. It only moves on for failures the *route* owns
    -- a quota, a dead key, an outage -- never for a 400, which would fail the
    same way everywhere and would silently burn every configured key. And a
    switch is sticky: once a route is known to be out, later turns start from
    the working one rather than paying the failure again each time.
    """

    def __init__(
        self,
        models: list[ChatModel],
        *,
        on_switch: Callable[[str, str, str], None] | None = None,
    ) -> None:
        if not models:
            raise ValueError("a route chain needs at least one route")
        self.models = models
        self.index = 0
        self.on_switch = on_switch

    @property
    def current(self) -> ChatModel:
        return self.models[self.index]

    @property
    def route(self) -> RouteConfig:
        return self.current.route

    @property
    def name(self) -> str:
        return self.current.name

    @property
    def streams(self) -> bool:
        return self.current.streams

    @property
    def alternatives(self) -> list[str]:
        return [m.route.name for m in self.models[self.index + 1 :]]

    def converse(
        self,
        messages: list[dict[str, Any]],
        tools: tuple[ToolSpec, ...],
        *,
        on_delta: DeltaCallback | None = None,
    ) -> AssistantTurn:
        last: ProviderError | None = None
        while self.index < len(self.models):
            model = self.current
            try:
                return model.converse(messages, tools, on_delta=on_delta)
            except ProviderError as exc:
                if not exc.route_is_exhausted or self.index == len(self.models) - 1:
                    raise
                last = exc
                self.index += 1
                if self.on_switch is not None:
                    self.on_switch(model.route.name, self.current.route.name, str(exc))
        raise last or PermanentProviderError("no routes left")  # pragma: no cover
