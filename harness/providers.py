"""Provider adapters behind one normalized contract.

Every provider returns the same :class:`ActionProposal` shape, so the agent loop
never learns which vendor it is talking to. Adapters own wire format; the loop
owns orchestration.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import auth
from .config import RouteConfig
from .contracts import ActionKind, ActionProposal, ModelRequest, ModelResponse
from .errors import ConfigurationError, PermanentProviderError, TransientProviderError


class Provider(Protocol):
    name: str

    def complete(self, request: ModelRequest, cursor: int = 0) -> ModelResponse: ...


class ReplayProvider:
    name = "replay"

    def __init__(self, route: RouteConfig) -> None:
        if route.script is None:
            raise ConfigurationError(f"replay route {route.name!r} requires a script")
        self.route = route
        self.actions = self._load(route.script)

    @staticmethod
    def _load(path: Path) -> tuple[dict[str, Any], ...]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"could not load replay script {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("actions"), list):
            raise ConfigurationError(f"replay script {path} must have version: 1 and an actions list")
        actions = []
        for index, action in enumerate(raw["actions"]):
            if not isinstance(action, dict) or action.get("type") not in {"tool", "finish", "clarify", "error"}:
                raise ConfigurationError(f"invalid replay action at {path}:{index + 1}")
            actions.append(dict(action))
        return tuple(actions)

    def complete(self, request: ModelRequest, cursor: int = 0) -> ModelResponse:
        if cursor >= len(self.actions):
            raise PermanentProviderError(
                f"replay route {self.route.name!r} exhausted its {len(self.actions)} scripted actions"
            )
        raw = self.actions[cursor]
        action_type = raw["type"]
        if action_type == "error":
            message = str(raw.get("message", "scripted provider failure"))
            if raw.get("transient", True):
                raise TransientProviderError(message)
            raise PermanentProviderError(message)
        kind = ActionKind(action_type)
        proposal = ActionProposal(
            kind=kind,
            call_id=str(raw.get("call_id", f"replay-{cursor + 1}")),
            name=str(raw["name"]) if kind is ActionKind.TOOL else None,
            arguments=dict(raw.get("arguments", {})),
            summary=str(raw.get("summary", "")),
        )
        return ModelResponse(
            route=self.route.name,
            provider=self.name,
            action=proposal,
            raw_summary=f"scripted {kind.value} action {cursor + 1}",
            next_cursor=cursor + 1,
        )


USER_AGENT = "yatra-harness/1.0"
ANTHROPIC_VERSION = "2023-06-01"


class _HTTPProvider:
    """Shared transport for HTTP providers.

    Subclasses supply the endpoint, body, headers, and response normalization.
    Transport concerns -- timeouts, status classification, and error mapping --
    live here so every provider fails in the same vocabulary.
    """

    name = "http"
    default_api_key_env = ""

    def complete(self, request: ModelRequest, cursor: int = 0) -> ModelResponse:
        del cursor
        secret = self._secret()
        http_request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(self._body(request)).encode("utf-8"),
            headers=self._headers(secret),
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.route.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2_000).decode("utf-8", errors="replace")
            # 408/429 and 5xx are worth another attempt; 4xx means the request
            # itself is wrong and will be wrong again.
            if exc.code == 429 or exc.code >= 500:
                raise TransientProviderError(f"provider HTTP {exc.code}: {detail}") from exc
            raise PermanentProviderError(f"provider HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransientProviderError(f"provider request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TransientProviderError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentProviderError("provider response was not a JSON object")
        return self._normalize(payload, request)

    def _secret(self) -> str:
        """Resolve the credential, tolerating providers that need none.

        A route may name an explicit ``api_key_env``; otherwise the provider's
        conventional variable (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``) is
        consulted. Local servers such as Ollama run unauthenticated, so a
        missing key is only an error once a variable is actually configured.
        """
        explicit = self.route.api_key_env
        candidates = (explicit,) if explicit else (self.default_api_key_env,)
        for name in candidates:
            # The endpoint is passed so a route naming a non-standard
            # variable can still reach a stored key; otherwise the error
            # below tells the operator to run `auth add` when doing so
            # could not have helped.
            credential = auth.resolve_route(name, self.route.base_url)
            if credential.available:
                return credential.key
        if explicit:
            raise ConfigurationError(
                f"route {self.route.name!r} has no credential for {explicit}. "
                f"export it, or run: harness auth add <key>"
            )
        return ""

    def _endpoint(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _body(self, request: ModelRequest) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _headers(self, secret: str) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _normalize(self, payload: dict[str, Any], request: ModelRequest) -> ModelResponse:
        raise NotImplementedError  # pragma: no cover - overridden

    @staticmethod
    def _parse_text_action(content: str, turn: int) -> ActionProposal:
        """Accept a JSON finish/clarify envelope, tolerating prose around it.

        Small local models frequently wrap the envelope in explanation, so the
        outermost JSON object is recovered rather than requiring a bare reply.
        """
        if not isinstance(content, str) or not content.strip():
            raise PermanentProviderError(
                "provider returned neither a tool call nor usable text"
            )
        raw = _extract_json_object(content)
        if raw is None:
            raise PermanentProviderError(
                "assistant text must contain a JSON finish/clarify action when no tool call is returned"
            )
        if raw.get("type") not in {"finish", "clarify"}:
            raise PermanentProviderError("assistant JSON action must have type finish or clarify")
        kind = ActionKind(raw["type"])
        return ActionProposal(
            kind=kind,
            call_id=str(raw.get("call_id", f"turn-{turn}-{kind.value}")),
            summary=str(raw.get("summary", raw.get("question", ""))),
        )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the first complete top-level JSON object found in *text*.

    Scans for balanced braces while respecting string literals, so braces inside
    a summary do not truncate the match.
    """
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    start = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        candidate = json.loads(stripped[start : index + 1])
                    except json.JSONDecodeError:
                        start = None
                        continue
                    if isinstance(candidate, dict):
                        return candidate
                    start = None
    return None


class OpenAICompatibleProvider(_HTTPProvider):
    name = "openai_compatible"

    def __init__(self, route: RouteConfig) -> None:
        if not route.base_url:
            raise ConfigurationError(f"route {route.name!r} requires base_url")
        self.route = route
        self.default_api_key_env = "OPENAI_API_KEY"

    def _endpoint(self) -> str:
        endpoint = self.route.base_url.rstrip("/")
        return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model": self.route.model,
            "messages": list(request.messages),
            "tools": [tool.as_model_tool() for tool in request.tools],
            "tool_choice": "auto",
            "temperature": 0,
        }

    def _headers(self, secret: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _normalize(self, payload: dict[str, Any], request: ModelRequest) -> ModelResponse:
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentProviderError("provider response has no assistant message") from exc
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            tool_call = tool_calls[0]
            try:
                function = tool_call["function"]
                arguments_raw = function.get("arguments", "{}")
                arguments = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else dict(arguments_raw)
                )
                proposal = ActionProposal(
                    kind=ActionKind.TOOL,
                    call_id=str(tool_call.get("id") or f"turn-{request.turn}-tool"),
                    name=str(function["name"]),
                    arguments=arguments,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PermanentProviderError("provider returned a malformed tool call") from exc
        else:
            proposal = self._parse_text_action(message.get("content"), request.turn)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return ModelResponse(
            route=self.route.name,
            provider=self.name,
            action=proposal,
            raw_summary=str(message.get("content") or "tool call")[:500],
            usage=dict(usage),
        )


class AnthropicProvider(_HTTPProvider):
    """Native Anthropic Messages API adapter.

    Kept separate from the OpenAI-shaped adapter because the wire contract
    genuinely differs: ``system`` is a top-level field, tools carry a bare
    ``input_schema``, and a tool use arrives as a content block rather than a
    sibling of the message. Forcing both through one shape is exactly the
    abstraction leak a provider port exists to prevent.
    """

    name = "anthropic"

    def __init__(self, route: RouteConfig) -> None:
        if not route.base_url:
            raise ConfigurationError(f"route {route.name!r} requires base_url")
        self.route = route
        self.default_api_key_env = "ANTHROPIC_API_KEY"

    def _endpoint(self) -> str:
        endpoint = self.route.base_url.rstrip("/")
        return endpoint if endpoint.endswith("/messages") else endpoint + "/messages"

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        system, messages = self._split_system(request.messages)
        return {
            "model": self.route.model,
            "system": system,
            "messages": messages,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ],
            "max_tokens": 4096,
            "temperature": 0,
        }

    def _headers(self, secret: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-api-key": secret,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    @staticmethod
    def _split_system(messages: tuple[dict[str, Any], ...]) -> tuple[str, list[dict[str, Any]]]:
        system_parts = [
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        ]
        rest = [message for message in messages if message.get("role") != "system"]
        return "\n\n".join(system_parts), rest

    def _normalize(self, payload: dict[str, Any], request: ModelRequest) -> ModelResponse:
        if not isinstance(payload.get("content"), list):
            raise PermanentProviderError("anthropic response has no content blocks")
        text_parts = []
        tool_block = None
        for block in payload["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_block = block
            elif block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in text_parts if part).strip()
        if tool_block is not None:
            arguments = tool_block.get("input")
            if not isinstance(arguments, dict):
                raise PermanentProviderError("anthropic tool_use block has no object input")
            proposal = ActionProposal(
                kind=ActionKind.TOOL,
                call_id=str(tool_block.get("id") or f"turn-{request.turn}-tool"),
                name=str(tool_block.get("name") or ""),
                arguments=arguments,
            )
        else:
            proposal = self._parse_text_action(text, request.turn)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return ModelResponse(
            route=self.route.name,
            provider=self.name,
            action=proposal,
            raw_summary=(text or "tool call")[:500],
            usage=dict(usage),
        )


def provider_for(route: RouteConfig) -> Provider:
    if route.kind == "replay":
        return ReplayProvider(route)
    # ollama and vllm serve the OpenAI chat-completions wire format.
    if route.kind in {"openai_compatible", "ollama", "vllm"}:
        return OpenAICompatibleProvider(route)
    if route.kind == "anthropic":
        return AnthropicProvider(route)
    raise ConfigurationError(f"unsupported provider kind: {route.kind}")

