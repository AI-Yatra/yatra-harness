"""Unit tests for the provider adapters and the normalized action contract."""

from __future__ import annotations

import json
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from harness.config import RouteConfig, load_config
from harness.contracts import ActionKind, ModelRequest, RiskLevel, ToolSpec
from harness.errors import (
    ConfigurationError,
    PermanentProviderError,
    TransientProviderError,
)
from harness.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ReplayProvider,
    provider_for,
)

ROOT = Path(__file__).resolve().parents[1]


def _request() -> ModelRequest:
    return ModelRequest(
        run_id="test-run",
        turn=3,
        messages=(
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ),
        tools=(
            ToolSpec("finish", "Finish.", {"type": "object"}, RiskLevel.CONTROL),
        ),
        max_output_chars=1000,
    )


class ReplayProviderTests(unittest.TestCase):
    def test_replay_replays_scripted_actions_in_order(self) -> None:
        route = RouteConfig(
            name="teaching",
            kind="replay",
            model="demo",
            script=ROOT / "scenarios" / "repair_demo.yaml",
        )
        provider = ReplayProvider(route)
        first = provider.complete(_request(), cursor=0)
        self.assertEqual(first.action.kind, ActionKind.TOOL)
        self.assertEqual(first.action.name, "repo_stats")
        self.assertEqual(first.next_cursor, 1)

    def test_replay_exhaustion_is_permanent(self) -> None:
        route = RouteConfig(
            name="teaching",
            kind="replay",
            model="demo",
            script=ROOT / "scenarios" / "repair_demo.yaml",
        )
        provider = ReplayProvider(route)
        with self.assertRaises(PermanentProviderError):
            provider.complete(_request(), cursor=10_000)

    def test_replay_scripted_transient_error(self) -> None:
        route = RouteConfig(
            name="broken",
            kind="replay",
            model="failure",
            script=ROOT / "scenarios" / "provider_failure.yaml",
        )
        provider = ReplayProvider(route)
        with self.assertRaises(TransientProviderError):
            provider.complete(_request(), cursor=0)

    def test_replay_requires_a_script(self) -> None:
        route = RouteConfig(name="r", kind="replay", model="m")
        with self.assertRaises(ConfigurationError):
            ReplayProvider(route)


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        del limit
        return json.dumps(self.payload).encode("utf-8")


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str = "") -> None:
        # HTTPError requires a real response-like object; a minimal stub works
        # because the adapter only calls .read() and .code.
        super().__init__("https://example.test", code, f"error {code}", None, None)
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        del limit
        return self._body.encode("utf-8")


def _openai_route() -> RouteConfig:
    return RouteConfig(
        name="remote",
        kind="openai_compatible",
        model="test-model",
        base_url="https://example.test/v1",
        timeout_seconds=5,
    )


class OpenAICompatibleProviderTests(unittest.TestCase):
    def _provider(self) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(_openai_route())

    def test_normalizes_tool_call(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "counter.py"}),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key"}):
            with mock.patch(
                "urllib.request.urlopen", return_value=FakeHTTPResponse(payload)
            ) as urlopen:
                response = self._provider().complete(_request())
        self.assertEqual(response.action.kind, ActionKind.TOOL)
        self.assertEqual(response.action.name, "read_file")
        self.assertEqual(response.action.arguments, {"path": "counter.py"})
        self.assertEqual(response.usage["prompt_tokens"], 10)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["temperature"], 0)
        # Request title-cases header names; compare case-insensitively.
        authorization = next(value for key, value in request.headers.items() if key.lower() == "authorization")
        self.assertTrue(authorization.startswith("Bearer "))

    def test_parses_json_finish_from_text(self) -> None:
        payload = {
            "choices": [{"message": {"content": '{"type":"finish","summary":"done"}'}}]
        }
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = self._provider().complete(_request())
        self.assertEqual(response.action.kind, ActionKind.FINISH)
        self.assertEqual(response.action.summary, "done")

    def test_parses_json_envelope_surrounded_by_prose(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "Here is my analysis.\n"
                            '```json\n{"type":"finish","summary":"all good"}\n```\n'
                            "Hope that helps."
                        )
                    }
                }
            ]
        }
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = self._provider().complete(_request())
        self.assertEqual(response.action.kind, ActionKind.FINISH)
        self.assertEqual(response.action.summary, "all good")

    def test_429_is_transient(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=FakeHTTPError(429, "rate limited")
        ):
            with self.assertRaises(TransientProviderError):
                self._provider().complete(_request())

    def test_500_is_transient(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=FakeHTTPError(500, "boom")
        ):
            with self.assertRaises(TransientProviderError):
                self._provider().complete(_request())

    def test_400_is_permanent(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=FakeHTTPError(400, "bad request")
        ):
            with self.assertRaises(PermanentProviderError):
                self._provider().complete(_request())

    def test_network_error_is_transient(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(TransientProviderError):
                self._provider().complete(_request())

    def test_missing_api_key_is_tolerated_when_unconfigured(self) -> None:
        route = RouteConfig(
            name="local",
            kind="ollama",
            model="m",
            base_url="http://127.0.0.1:11434/v1",
        )
        provider = OpenAICompatibleProvider(route)
        payload = {"choices": [{"message": {"content": '{"type":"clarify","question":"?"}'}}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.complete(_request())
        self.assertEqual(response.action.kind, ActionKind.CLARIFY)

    def test_configured_but_missing_api_key_raises(self) -> None:
        route = RouteConfig(
            name="remote",
            kind="openai_compatible",
            model="m",
            base_url="https://example.test/v1",
            api_key_env="HARNESS_TEST_MISSING_KEY_XYZ",
        )
        with self.assertRaises(ConfigurationError):
            OpenAICompatibleProvider(route).complete(_request())

    def test_endpoint_suffix_is_appended(self) -> None:
        provider = OpenAICompatibleProvider(_openai_route())
        self.assertEqual(
            provider._endpoint(), "https://example.test/v1/chat/completions"
        )


class AnthropicProviderTests(unittest.TestCase):
    def _provider(self) -> AnthropicProvider:
        route = RouteConfig(
            name="claude",
            kind="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            timeout_seconds=5,
        )
        return AnthropicProvider(route)

    def test_normalizes_tool_use_block(self) -> None:
        payload = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "apply_patch",
                    "input": {"patch": "diff --git a/x b/x"},
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with mock.patch(
                "urllib.request.urlopen", return_value=FakeHTTPResponse(payload)
            ) as urlopen:
                response = self._provider().complete(_request())
        self.assertEqual(response.action.kind, ActionKind.TOOL)
        self.assertEqual(response.action.name, "apply_patch")
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "claude-test")
        self.assertEqual(body["system"], "system prompt")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(body["tools"][0]["name"], "finish")
        self.assertIn("input_schema", body["tools"][0])
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_uses_api_key_env_when_set(self) -> None:
        route = RouteConfig(
            name="claude",
            kind="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            api_key_env="HARNESS_TEST_ANTHROPIC_KEY",
        )
        payload = {"content": [{"type": "text", "text": '{"type":"finish","summary":"ok"}'}]}
        with mock.patch.dict("os.environ", {"HARNESS_TEST_ANTHROPIC_KEY": "env-key"}):
            with mock.patch(
                "urllib.request.urlopen", return_value=FakeHTTPResponse(payload)
            ) as urlopen:
                AnthropicProvider(route).complete(_request())
        request = urlopen.call_args.args[0]
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers["x-api-key"], "env-key")

    def test_plain_text_without_json_is_permanent(self) -> None:
        payload = {"content": [{"type": "text", "text": "no json here"}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            with self.assertRaises(PermanentProviderError):
                self._provider().complete(_request())

    def test_missing_content_blocks_is_permanent(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeHTTPResponse({"content": "nope"})
        ):
            with self.assertRaises(PermanentProviderError):
                self._provider().complete(_request())


class ProviderFactoryTests(unittest.TestCase):
    def test_kind_mapping(self) -> None:
        self.assertIsInstance(
            provider_for(RouteConfig(name="r", kind="replay", model="m", script=ROOT / "scenarios" / "repair_demo.yaml")),
            ReplayProvider,
        )
        self.assertIsInstance(
            provider_for(RouteConfig(name="r", kind="openai_compatible", model="m", base_url="http://x")),
            OpenAICompatibleProvider,
        )
        self.assertIsInstance(
            provider_for(RouteConfig(name="r", kind="ollama", model="m", base_url="http://x")),
            OpenAICompatibleProvider,
        )
        self.assertIsInstance(
            provider_for(RouteConfig(name="r", kind="vllm", model="m", base_url="http://x")),
            OpenAICompatibleProvider,
        )
        self.assertIsInstance(
            provider_for(RouteConfig(name="r", kind="anthropic", model="m", base_url="http://x")),
            AnthropicProvider,
        )

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            provider_for(RouteConfig(name="r", kind="aliens", model="m"))


class ConfigLoadingTests(unittest.TestCase):
    def test_teaching_config_loads_and_disables_llm_light(self) -> None:
        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertFalse(config.llm_light.enabled)
        self.assertEqual(config.router.primary, "teaching")

    def test_llm_light_config_loads_profiles(self) -> None:
        config = load_config(ROOT / "configs" / "llm_light.yaml")
        self.assertTrue(config.llm_light.enabled)
        self.assertEqual(
            set(config.llm_light.profiles),
            {"offline", "budget", "quality", "teaching", "balanced", "long-context"},
        )
        route = config.router.routes["remote-frontier"]
        self.assertEqual(route.kind, "anthropic")
        self.assertEqual(route.quality, 5.0)
        self.assertFalse(route.local)


if __name__ == "__main__":
    unittest.main()
