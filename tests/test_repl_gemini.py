"""Gemini's thought signatures, and the route switching around them.

Gemini 3 returns an encrypted `thought_signature` on every function call and
rejects the *next* request with a 400 if it does not come back. The batch
path never hit this because it sends one action at a time; a conversation
hits it on the second turn of the first tool call, which is to say
immediately. These tests pin the passthrough end to end: blocking response,
streamed response, and the message that goes back on the wire.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import load_config
from harness.execution.workspace import Workspace
from harness.models.streaming import StreamAccumulator
from harness.repl.agent import Agent, Events
from harness.repl.approvals import Gate, Mode
from harness.repl.conversation import AssistantTurn, Conversation, ToolCall
from harness.repl.model import ChatModel, RouteChain, _read_openai
from harness.repl.shell import Options, Shell
from harness.repl.tools import ReplToolset
from tests.test_repl_end_to_end import ScriptedServer, completion, sse, tool_call

ROOT = Path(__file__).resolve().parents[1]
AY_CONFIG = ROOT / "configs" / "ay.yaml"

#: Shortened, but the same shape Gemini actually returns.
SIGNATURE = "EuoBCucBARFNMg/1Awgj4lGhkzD0sBKcbh39i+Enpug100st7QV"
EXTRA = {"google": {"thought_signature": SIGNATURE}}


def gemini_call(index: int, name: str, arguments: dict, *, signed: bool = True) -> dict:
    call = tool_call(index, name, arguments)
    if signed:
        call["extra_content"] = EXTRA
    return call


class ReadingTests(unittest.TestCase):
    def test_a_signature_is_captured_off_a_blocking_response(self) -> None:
        payload = completion("", [gemini_call(1, "read_file", {"path": "a.py"})])
        turn = _read_openai(payload)
        self.assertEqual(turn.tool_calls[0].extra, {"extra_content": EXTRA})

    def test_a_call_without_one_carries_nothing(self) -> None:
        """Every other provider sends no extra fields, and must stay clean."""
        payload = completion("", [gemini_call(1, "read_file", {"path": "a"}, signed=False)])
        self.assertEqual(_read_openai(payload).tool_calls[0].extra, {})

    def test_a_signature_survives_streaming(self) -> None:
        """It arrives once, on the chunk that opens the call."""
        accumulator = StreamAccumulator()
        accumulator.feed(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "extra_content": EXTRA,
                                    "function": {"name": "read_file", "arguments": '{"pa'},
                                }
                            ]
                        }
                    }
                ]
            }
        )
        accumulator.feed(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a"}'}}]}}]}
        )
        turn = _read_openai(accumulator.as_payload())
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "a"})
        self.assertEqual(turn.tool_calls[0].extra, {"extra_content": EXTRA})

    def test_a_later_chunk_does_not_erase_the_signature(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.feed(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "extra_content": EXTRA,
                 "function": {"name": "grep", "arguments": "{}"}}]}}]}
        )
        accumulator.feed(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ""}}]}}]}
        )
        self.assertEqual(accumulator.tool_calls()[0]["extra_content"], EXTRA)


class ConversationTests(unittest.TestCase):
    def test_the_signature_goes_back_on_the_assistant_message(self) -> None:
        thread = Conversation("s")
        thread.add_assistant(
            AssistantTurn(
                text="",
                tool_calls=(
                    ToolCall("c1", "read_file", {"path": "a"}, extra={"extra_content": EXTRA}),
                ),
            )
        )
        wire = thread.messages[0]["tool_calls"][0]
        self.assertEqual(wire["extra_content"], EXTRA)
        # The ordinary fields must be untouched by the passthrough.
        self.assertEqual(wire["id"], "c1")
        self.assertEqual(wire["function"]["name"], "read_file")

    def test_a_call_with_no_extras_produces_no_extra_keys(self) -> None:
        thread = Conversation("s")
        thread.add_assistant(
            AssistantTurn(text="", tool_calls=(ToolCall("c1", "read_file", {"path": "a"}),))
        )
        self.assertEqual(
            set(thread.messages[0]["tool_calls"][0]), {"id", "type", "function"}
        )

    def test_the_signature_survives_saving_and_reopening(self) -> None:
        """A resumed session would otherwise fail on its first tool call."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            thread = Conversation("s")
            thread.add_user("go")
            thread.add_assistant(
                AssistantTurn(
                    text="",
                    tool_calls=(ToolCall("c1", "grep", {"pattern": "x"}, extra={"extra_content": EXTRA}),),
                )
            )
            thread.add_tool_result("c1", "grep", "no matches")
            thread.save(path)
            reopened = Conversation.load(path, system="s")
        self.assertEqual(reopened.messages[1]["tool_calls"][0]["extra_content"], EXTRA)


class WireTests(unittest.TestCase):
    """The whole loop, over HTTP, asserting on what the second request sent."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(AY_CONFIG)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def agent_for(self, server: ScriptedServer, *, stream: bool = False, deltas=None) -> Agent:
        route = replace(
            self.config.router.routes["gemini"],
            base_url=server.base_url,
            api_key_env="",
            stream=stream,
            timeout_seconds=20,
        )
        return Agent(
            model=ChatModel(route, retries=0),
            conversation=Conversation("You are a test agent."),
            toolset=ReplToolset(Workspace(self.root, ()), self.config),
            gate=Gate(self.config.policy, mode=Mode.FULL_AUTO),
            config=self.config,
            events=Events(on_delta=deltas.append if deltas is not None else None),
        )

    def test_the_second_request_carries_the_signature_back(self) -> None:
        script = [
            completion("Looking.", [gemini_call(1, "read_file", {"path": "a.py"})]),
            completion("It sets x to 1."),
        ]
        with ScriptedServer(script) as server:
            self.agent_for(server).send("what is in a.py?")
        assistant = [
            m for m in server.requests[1]["messages"] if m.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant), 1)
        sent = assistant[0]["tool_calls"][0]
        self.assertEqual(
            sent["extra_content"]["google"]["thought_signature"], SIGNATURE
        )

    def test_it_survives_the_streaming_path_too(self) -> None:
        stream = sse(
            [
                {"choices": [{"delta": {"content": "Reading."}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "extra_content": EXTRA,
                     "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        deltas: list[str] = []
        with ScriptedServer([stream, completion("done")]) as server:
            self.agent_for(server, stream=True, deltas=deltas).send("read it")
        assistant = [m for m in server.requests[1]["messages"] if m.get("role") == "assistant"]
        self.assertEqual(
            assistant[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            SIGNATURE,
        )

    def test_several_signed_calls_in_one_turn_all_keep_theirs(self) -> None:
        (self.root / "b.py").write_text("y = 2\n", encoding="utf-8")
        first = gemini_call(1, "read_file", {"path": "a.py"})
        second = gemini_call(2, "read_file", {"path": "b.py"})
        second["extra_content"] = {"google": {"thought_signature": "SECOND"}}
        with ScriptedServer([completion("", [first, second]), completion("done")]) as server:
            self.agent_for(server).send("read both")
        sent = [
            m for m in server.requests[1]["messages"] if m.get("role") == "assistant"
        ][0]["tool_calls"]
        self.assertEqual(sent[0]["extra_content"]["google"]["thought_signature"], SIGNATURE)
        self.assertEqual(sent[1]["extra_content"]["google"]["thought_signature"], "SECOND")


class RetryTests(unittest.TestCase):
    """Gemini returns 503 freely under load; one must not end a turn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(AY_CONFIG)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def model_for(self, server: ScriptedServer, retries: int) -> ChatModel:
        route = replace(
            self.config.router.routes["gemini"],
            base_url=server.base_url,
            api_key_env="",
            stream=False,
            timeout_seconds=20,
        )
        return ChatModel(route, retries=retries, backoff_seconds=0.01)

    def test_a_transient_failure_is_retried(self) -> None:
        with ScriptedServer([503, completion("recovered")]) as server:
            turn = self.model_for(server, retries=2).converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(turn.text, "recovered")
        self.assertEqual(len(server.requests), 2)

    def test_retries_are_bounded(self) -> None:
        from harness.core.errors import TransientProviderError

        with ScriptedServer([503, 503, 503, 503]) as server:
            with self.assertRaises(TransientProviderError):
                self.model_for(server, retries=2).converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(len(server.requests), 3)

    def test_a_permanent_failure_is_not_retried(self) -> None:
        """A 400 will be a 400 again; retrying only wastes the operator's time."""
        from harness.core.errors import PermanentProviderError

        with ScriptedServer([400, completion("never reached")]) as server:
            with self.assertRaises(PermanentProviderError):
                self.model_for(server, retries=3).converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(len(server.requests), 1)

    def test_the_shell_takes_its_retry_budget_from_the_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = Shell(
                Options(config_path=AY_CONFIG, root=Path(tmp), sessions_dir=Path(tmp) / ".ay")
            )
        self.assertEqual(shell.model.current.retries, self.config.router.retries_per_route)
        self.assertEqual(
            shell.model.current.backoff_seconds, self.config.router.backoff_seconds
        )


class RouteSwitchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.shell = Shell(
            Options(config_path=AY_CONFIG, root=self.root, sessions_dir=self.root / ".ay")
        )
        import io

        from harness.repl.render import Console, Renderer

        self.shell.console = Console(io.StringIO(), colour=False)
        self.shell.render = Renderer(self.shell.console)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def output(self) -> str:
        return self.shell.console.stream.getvalue()

    def test_model_with_no_argument_lists_routes_not_bare_model_ids(self) -> None:
        """Switching is by route name, so route names have to be visible."""
        self.shell._command("/model")
        for name in ("qwen", "gemini", "opencode", "commandcode", "local"):
            self.assertIn(name, self.output)
        self.assertIn("/model <name>", self.output)

    def test_the_listing_marks_routes_with_no_credential(self) -> None:
        self.shell._command("/model")
        self.assertIn("no key", self.output)

    def test_switching_to_a_route_changes_the_endpoint_and_window(self) -> None:
        self.shell._command("/model gemini")
        self.assertEqual(self.shell.route.name, "gemini")
        self.assertIn("generativelanguage", self.shell.route.base_url)
        self.assertEqual(self.shell.conversation.max_tokens, self.shell.route.context_window)
        self.assertIs(self.shell.agent.model, self.shell.model)

    def test_an_unknown_name_stays_on_the_current_route_and_says_so(self) -> None:
        """It used to silently jump back to the primary route's endpoint."""
        self.shell._command("/model gemini")
        self.shell._command("/model some-preview-model")
        self.assertEqual(self.shell.route.model, "some-preview-model")
        self.assertIn("generativelanguage", self.shell.route.base_url)
        self.assertIn("using it as a model id", self.output)

    def test_a_route_without_a_credential_is_called_out_on_switch(self) -> None:
        self.shell._command("/model opencode")
        self.assertIn("no credential", self.output)

    def test_switching_by_model_id_finds_the_route(self) -> None:
        self.shell._command("/model gemini-3.7-flash")
        self.assertEqual(self.shell.route.name, "gemini")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class QuotaMessageTests(unittest.TestCase):
    """Google buries the two useful numbers at the end of a long paragraph."""

    def quota_error(self, *, quota_id: str, value: str, model: str, retry: str = "41s") -> str:
        import json as _json

        return _json.dumps(
            [
                {
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "You exceeded your current quota, please check your plan "
                        "and billing details. For more information on this error, head to: "
                        "https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your "
                        "current usage, head to: https://ai.dev/rate-limit. \n* Quota "
                        f"exceeded for metric: x, limit: {value}, model: {model}\n"
                        f"Please retry in {retry}.",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [
                                    {
                                        "quotaId": quota_id,
                                        "quotaValue": value,
                                        "quotaDimensions": {"model": model},
                                    }
                                ],
                            },
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": retry,
                            },
                        ],
                    }
                }
            ]
        )

    def test_the_limit_and_the_wait_both_survive(self) -> None:
        from harness.core.util import provider_error_message

        message = provider_error_message(
            self.quota_error(
                quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                value="20",
                model="gemini-3.7-flash",
            )
        )
        self.assertIn("20", message)
        self.assertIn("per day", message)
        self.assertIn("gemini-3.7-flash", message)
        self.assertIn("41s", message)
        self.assertNotIn("http", message.lower())

    def test_a_per_minute_quota_says_per_minute(self) -> None:
        from harness.core.util import provider_error_message

        message = provider_error_message(
            self.quota_error(
                quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                value="5",
                model="gemini-3.5-flash",
            )
        )
        self.assertIn("per minute", message)

    def test_an_error_without_details_still_reads_normally(self) -> None:
        import json as _json

        from harness.core.util import provider_error_message

        body = _json.dumps([{"error": {"code": 400, "message": "Please pass a valid API key"}}])
        self.assertEqual(provider_error_message(body), "Please pass a valid API key")


class SiblingModelTests(unittest.TestCase):
    def test_non_chat_models_are_never_suggested(self) -> None:
        from harness.core.util import is_chat_model

        for name in (
            "gemini-embedding-001",
            "gemini-2.5-flash-image",
            "gemini-3.1-flash-tts-preview",
            "gemini-3.5-transcribe",
            "gemini-2.5-computer-use-preview-10-2025",
            "gemini-3.5-live-translate-preview",
        ):
            self.assertFalse(is_chat_model(name), name)
        for name in ("gemini-3.7-flash", "gemini-3.5-flash", "gemini-pro-latest"):
            self.assertTrue(is_chat_model(name), name)

    def test_newer_versions_rank_first(self) -> None:
        """An older listed model is often retired on the chat endpoint."""
        from harness.core.util import model_version

        self.assertGreater(model_version("gemini-3.5-flash"), model_version("gemini-2.5-flash"))
        self.assertGreater(model_version("gemini-flash-latest"), model_version("gemini-3.7-flash"))
        self.assertEqual(model_version("no-version-here"), 0.0)


class ModelResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def shell(self, override: str = "") -> Shell:
        return Shell(
            Options(
                config_path=AY_CONFIG,
                root=self.root,
                sessions_dir=self.root / ".ay",
                model_override=override,
            )
        )

    def test_a_route_name_selects_that_route(self) -> None:
        shell = self.shell("gemini")
        self.assertEqual(shell.route.name, "gemini")
        self.assertEqual(shell.guessed_route, "")

    def test_route_colon_model_pins_both(self) -> None:
        shell = self.shell("gemini:gemini-3.5-flash")
        self.assertEqual(shell.route.name, "gemini")
        self.assertEqual(shell.route.model, "gemini-3.5-flash")
        self.assertIn("generativelanguage", shell.route.base_url)
        self.assertEqual(shell.guessed_route, "")

    def test_an_unknown_route_in_that_syntax_is_an_error(self) -> None:
        from harness.repl.shell import UnknownRoute

        with self.assertRaises(UnknownRoute):
            self.shell("nope:whatever")

    def test_a_bare_unknown_model_records_that_it_guessed(self) -> None:
        """It used to attach silently to the primary provider's endpoint."""
        shell = self.shell("some-unknown-model")
        self.assertEqual(shell.route.model, "some-unknown-model")
        self.assertEqual(shell.guessed_route, "qwen")

    def test_the_guess_is_announced_in_the_banner(self) -> None:
        import io

        from harness.repl.render import Console, Renderer

        shell = self.shell("some-unknown-model")
        shell.console = Console(io.StringIO(), colour=False)
        shell.render = Renderer(shell.console)
        shell._banner()
        output = shell.console.stream.getvalue()
        self.assertIn("not a configured route or model", output)
        self.assertIn("qwen", output)
        self.assertIn("--model <route>:<model>", output)


class RouteChainTests(unittest.TestCase):
    """One exhausted free tier must not end a conversation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(AY_CONFIG)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def chain(self, servers: list[ScriptedServer], **kwargs) -> RouteChain:
        models = [
            ChatModel(
                replace(
                    self.config.router.routes["gemini"],
                    name=f"route{index}",
                    base_url=server.base_url,
                    api_key_env="",
                    stream=False,
                    timeout_seconds=20,
                ),
                retries=0,
            )
            for index, server in enumerate(servers)
        ]
        return RouteChain(models, **kwargs)

    def test_a_quota_failure_moves_to_the_next_route(self) -> None:
        with ScriptedServer([429]) as dead, ScriptedServer([completion("from the spare")]) as spare:
            switches: list[tuple[str, str, str]] = []
            chain = self.chain([dead, spare], on_switch=lambda *a: switches.append(a))
            turn = chain.converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(turn.text, "from the spare")
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0][0], "route0")
        self.assertEqual(switches[0][1], "route1")

    def test_a_dead_key_also_moves_on(self) -> None:
        with ScriptedServer([401]) as dead, ScriptedServer([completion("ok")]) as spare:
            turn = self.chain([dead, spare]).converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(turn.text, "ok")

    def test_a_bad_request_does_not_burn_every_key(self) -> None:
        """A 400 is our fault and fails identically everywhere."""
        from harness.core.errors import PermanentProviderError

        with ScriptedServer([400]) as first, ScriptedServer([completion("never")]) as second:
            chain = self.chain([first, second])
            with self.assertRaises(PermanentProviderError):
                chain.converse([{"role": "user", "content": "hi"}], ())
            self.assertEqual(len(second.requests), 0, "the second route must not be tried")

    def test_the_switch_is_sticky_across_turns(self) -> None:
        """Otherwise every later turn pays the dead route's timeout again."""
        with ScriptedServer([429, 429]) as dead, ScriptedServer(
            [completion("one"), completion("two")]
        ) as spare:
            chain = self.chain([dead, spare])
            chain.converse([{"role": "user", "content": "a"}], ())
            chain.converse([{"role": "user", "content": "b"}], ())
        self.assertEqual(len(dead.requests), 1)
        self.assertEqual(len(spare.requests), 2)

    def test_the_last_route_raises_rather_than_looping(self) -> None:
        from harness.core.errors import TransientProviderError

        with ScriptedServer([429]) as only:
            with self.assertRaises(TransientProviderError):
                self.chain([only]).converse([{"role": "user", "content": "hi"}], ())

    def test_the_shell_builds_a_chain_of_credentialled_routes_only(self) -> None:
        shell = Shell(
            Options(config_path=AY_CONFIG, root=self.root, sessions_dir=self.root / ".ay")
        )
        names = [m.route.name for m in shell.model.models]
        self.assertEqual(names[0], shell.route.name)
        for name in names[1:]:
            route = shell.config.router.routes[name]
            self.assertTrue(shell._has_credential(route), name)
        self.assertEqual(len(names), len(set(names)), "a route must not appear twice")
