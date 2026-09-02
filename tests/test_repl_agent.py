"""Tests for the conversation loop, the approval gate and the thread.

The model is scripted rather than called: every case here is about what the
loop does with a response, which is exactly the part a live model would make
untestable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import load_config
from harness.core.contracts import RiskLevel, ToolSpec
from harness.core.errors import PermanentProviderError, TransientProviderError
from harness.execution.workspace import Workspace
from harness.repl.agent import Agent, Events, Limits, ModelUnavailable, describe_arguments
from harness.repl.approvals import Gate, Mode, Verdict
from harness.repl.conversation import AssistantTurn, Conversation, ToolCall
from harness.repl.model import (
    _read_anthropic,
    _read_openai,
    _to_anthropic_messages,
)
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class ScriptedModel:
    """Returns prepared turns, and records what it was asked."""

    streams = False
    name = "scripted"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)
        self.requests: list[list[dict]] = []

    def converse(self, messages, tools, *, on_delta=None):  # noqa: ANN001, ARG002
        self.requests.append([dict(m) for m in messages])
        if not self.turns:
            return AssistantTurn(text="(nothing left to say)")
        return self.turns.pop(0)


class FailingModel:
    streams = False

    def __init__(self, error: Exception) -> None:
        self.error = error

    def converse(self, messages, tools, *, on_delta=None):  # noqa: ANN001, ARG002
        raise self.error


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "teaching.yaml")
        self.toolset = ReplToolset(Workspace(self.root, ()), self.config)
        self.notices: list[str] = []
        self.texts: list[str] = []
        self.started: list[str] = []
        self.denied: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(
        self,
        turns: list[AssistantTurn],
        *,
        mode: Mode = Mode.FULL_AUTO,
        prompt=None,
        limits: Limits | None = None,
    ) -> Agent:
        gate = Gate(self.config.policy, mode=mode, prompt=prompt)
        events = Events(
            on_text=self.texts.append,
            on_tool_start=lambda call, spec: self.started.append(call.name),
            on_tool_denied=lambda call, reason: self.denied.append(reason),
            on_notice=self.notices.append,
        )
        self.model = ScriptedModel(turns)
        return Agent(
            model=self.model,
            conversation=Conversation("system prompt"),
            toolset=self.toolset,
            gate=gate,
            config=self.config,
            events=events,
            limits=limits or Limits(),
        )


class LoopTests(AgentTestCase):
    def test_a_plain_answer_ends_the_turn(self) -> None:
        agent = self.build([AssistantTurn(text="42")])
        stats = agent.send("what is six times seven?")
        self.assertEqual(stats.steps, 1)
        self.assertEqual(stats.tool_calls, 0)
        self.assertEqual(self.texts, ["42"])

    def test_a_tool_call_is_executed_and_fed_back(self) -> None:
        (self.root / "a.txt").write_text("contents\n", encoding="utf-8")
        agent = self.build(
            [
                AssistantTurn(
                    text="reading",
                    tool_calls=(ToolCall("c1", "read_file", {"path": "a.txt"}),),
                ),
                AssistantTurn(text="it says contents"),
            ]
        )
        stats = agent.send("what is in a.txt?")
        self.assertEqual(stats.steps, 2)
        self.assertEqual(stats.tool_calls, 1)
        # The second request must carry the tool result back to the model.
        second = self.model.requests[1]
        tool_messages = [m for m in second if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("contents", tool_messages[0]["content"])
        self.assertEqual(tool_messages[0]["tool_call_id"], "c1")

    def test_several_tool_calls_in_one_turn_all_run(self) -> None:
        (self.root / "a.txt").write_text("A\n", encoding="utf-8")
        (self.root / "b.txt").write_text("B\n", encoding="utf-8")
        agent = self.build(
            [
                AssistantTurn(
                    text="",
                    tool_calls=(
                        ToolCall("c1", "read_file", {"path": "a.txt"}),
                        ToolCall("c2", "read_file", {"path": "b.txt"}),
                    ),
                ),
                AssistantTurn(text="done"),
            ]
        )
        stats = agent.send("read both")
        self.assertEqual(stats.tool_calls, 2)
        results = [m for m in self.model.requests[1] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["c1", "c2"])

    def test_an_unknown_tool_is_answered_with_the_real_tool_list(self) -> None:
        agent = self.build(
            [
                AssistantTurn(text="", tool_calls=(ToolCall("c1", "teleport", {}),)),
                AssistantTurn(text="sorry"),
            ]
        )
        agent.send("teleport")
        result = [m for m in self.model.requests[1] if m.get("role") == "tool"][0]
        self.assertIn("No tool named", result["content"])
        self.assertIn("read_file", result["content"])

    def test_a_failing_tool_does_not_end_the_turn(self) -> None:
        agent = self.build(
            [
                AssistantTurn(text="", tool_calls=(ToolCall("c1", "read_file", {"path": "gone"}),)),
                AssistantTurn(text="that file does not exist"),
            ]
        )
        stats = agent.send("read gone")
        self.assertEqual(stats.steps, 2)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(self.texts[-1], "that file does not exist")

    def test_the_step_limit_stops_a_runaway_loop(self) -> None:
        forever = [
            AssistantTurn(text="", tool_calls=(ToolCall(f"c{n}", "list_dir", {}),))
            for n in range(20)
        ]
        agent = self.build(forever, limits=Limits(max_steps=4))
        stats = agent.send("loop forever")
        self.assertEqual(stats.steps, 4)
        self.assertTrue(any("Stopped after 4 steps" in n for n in self.notices))

    def test_repeated_failures_stop_the_loop(self) -> None:
        broken = [
            AssistantTurn(text="", tool_calls=(ToolCall(f"c{n}", "read_file", {"path": "gone"}),))
            for n in range(20)
        ]
        agent = self.build(broken, limits=Limits(max_consecutive_errors=3))
        agent.send("keep failing")
        self.assertTrue(any("in a row failed" in n for n in self.notices))

    def test_a_provider_failure_becomes_a_typed_error(self) -> None:
        agent = self.build([])
        agent.model = FailingModel(TransientProviderError("connection reset"))
        with self.assertRaises(ModelUnavailable):
            agent.send("hello")

    def test_the_thread_survives_a_provider_failure(self) -> None:
        agent = self.build([])
        agent.model = FailingModel(PermanentProviderError("bad key"))
        with self.assertRaises(ModelUnavailable):
            agent.send("hello")
        # The user message is still there, so a retry does not lose it.
        self.assertEqual(agent.conversation.messages[0]["content"], "hello")


class ApprovalTests(AgentTestCase):
    def test_reads_never_ask(self) -> None:
        asked: list[str] = []
        (self.root / "a.txt").write_text("x\n", encoding="utf-8")
        agent = self.build(
            [
                AssistantTurn(text="", tool_calls=(ToolCall("c1", "read_file", {"path": "a.txt"}),)),
                AssistantTurn(text="done"),
            ],
            mode=Mode.SUGGEST,
            prompt=lambda request: asked.append(request.question) or Verdict.ALLOW,
        )
        agent.send("read it")
        self.assertEqual(asked, [])

    def test_an_edit_asks_before_touching_the_file(self) -> None:
        asked: list[str] = []
        (self.root / "a.txt").write_text("old\n", encoding="utf-8")

        def prompt(request):
            asked.append(request.question)
            return Verdict.DENY

        agent = self.build(
            [
                AssistantTurn(
                    text="",
                    tool_calls=(
                        ToolCall("c1", "edit_file", {"path": "a.txt", "old_string": "old", "new_string": "new"}),
                    ),
                ),
                AssistantTurn(text="you said no"),
            ],
            mode=Mode.SUGGEST,
            prompt=prompt,
        )
        agent.send("change it")
        self.assertEqual(asked, ["Edit a.txt?"])
        # A denial must leave the file exactly as it was.
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "old\n")

    def test_a_denial_tells_the_model_not_to_retry(self) -> None:
        (self.root / "a.txt").write_text("old\n", encoding="utf-8")
        agent = self.build(
            [
                AssistantTurn(
                    text="",
                    tool_calls=(ToolCall("c1", "write_file", {"path": "a.txt", "content": "new"}),),
                ),
                AssistantTurn(text="ok"),
            ],
            mode=Mode.SUGGEST,
            prompt=lambda _request: Verdict.DENY,
        )
        agent.send("write it")
        result = [m for m in self.model.requests[1] if m.get("role") == "tool"][0]
        self.assertIn("declined", result["content"])
        self.assertIn("Do not retry", result["content"])

    def test_allow_always_stops_asking_a_second_time(self) -> None:
        asked: list[str] = []
        (self.root / "a.txt").write_text("1\n", encoding="utf-8")

        def prompt(request):
            asked.append(request.question)
            return Verdict.ALLOW_ALWAYS

        agent = self.build(
            [
                AssistantTurn(text="", tool_calls=(ToolCall("c1", "write_file", {"path": "a.txt", "content": "2"}),)),
                AssistantTurn(text="", tool_calls=(ToolCall("c2", "write_file", {"path": "b.txt", "content": "3"}),)),
                AssistantTurn(text="done"),
            ],
            mode=Mode.SUGGEST,
            prompt=prompt,
        )
        agent.send("write both")
        self.assertEqual(len(asked), 1)
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "3")

    def test_auto_edit_writes_freely_but_still_asks_to_run(self) -> None:
        asked: list[str] = []
        agent = self.build(
            [
                AssistantTurn(text="", tool_calls=(ToolCall("c1", "write_file", {"path": "a.txt", "content": "x"}),)),
                AssistantTurn(text="", tool_calls=(ToolCall("c2", "run_command", {"command": ["python", "--version"]}),)),
                AssistantTurn(text="done"),
            ],
            mode=Mode.AUTO_EDIT,
            prompt=lambda request: asked.append(request.question) or Verdict.ALLOW,
        )
        agent.send("do both")
        self.assertEqual(len(asked), 1)
        self.assertIn("Run python --version", asked[0])

    def test_the_deny_list_is_never_offered_for_approval(self) -> None:
        asked: list[str] = []
        policy = replace(self.config.policy, denied_commands=(("rm", "-rf"),))
        gate = Gate(
            policy,
            mode=Mode.SUGGEST,
            prompt=lambda request: asked.append(request.question) or Verdict.ALLOW,
        )
        spec = ToolSpec("run_command", "", {}, RiskLevel.EXECUTE)
        decision = gate.check(spec, {"command": ["rm", "-rf", "/"]})
        self.assertFalse(decision.allowed)
        self.assertIn("deny-list", decision.reason)
        self.assertEqual(asked, [], "a denied command must never reach a human")

    def test_a_deny_pattern_matches_inside_the_command_not_only_at_the_front(self) -> None:
        """One inserted flag must not dodge the rule."""
        policy = replace(self.config.policy, denied_commands=(("rm", "-rf"),))
        gate = Gate(policy, mode=Mode.FULL_AUTO, prompt=None)
        spec = ToolSpec("run_command", "", {}, RiskLevel.EXECUTE)
        decision = gate.check(spec, {"command": ["sudo", "rm", "-rf", "/"]})
        self.assertFalse(decision.allowed, "full-auto must not bypass the deny-list")

    def test_the_shipped_repl_config_denies_the_obvious_disasters(self) -> None:
        policy = load_config(ROOT / "configs" / "ay.yaml").policy
        gate = Gate(policy, mode=Mode.FULL_AUTO, prompt=None)
        spec = ToolSpec("run_command", "", {}, RiskLevel.EXECUTE)
        for command in (
            ["rm", "-rf", "/"],
            ["git", "push", "--force"],
            ["git", "reset", "--hard"],
            ["sudo", "rm", "-rf", "."],
        ):
            self.assertFalse(
                gate.check(spec, {"command": command}).allowed,
                f"{' '.join(command)} should be refused outright",
            )

    def test_full_auto_asks_nothing(self) -> None:
        gate = Gate(self.config.policy, mode=Mode.FULL_AUTO, prompt=None)
        spec = ToolSpec("write_file", "", {}, RiskLevel.WRITE)
        self.assertTrue(gate.check(spec, {"path": "a"}).allowed)

    def test_without_an_approver_the_refusal_says_asking_will_not_help(self) -> None:
        gate = Gate(self.config.policy, mode=Mode.SUGGEST, prompt=None)
        spec = ToolSpec("write_file", "", {}, RiskLevel.WRITE)
        decision = gate.check(spec, {"path": "a"})
        self.assertFalse(decision.allowed)
        self.assertIn("will not help", decision.reason)

    def test_standing_approvals_are_listed_for_the_operator(self) -> None:
        gate = Gate(self.config.policy, mode=Mode.SUGGEST, prompt=lambda _r: Verdict.ALLOW_ALWAYS)
        gate.check(ToolSpec("run_command", "", {}, RiskLevel.EXECUTE), {"command": ["git", "status"]})
        self.assertIn("run_command:git", gate.standing_approvals)
        gate.forget_all()
        self.assertEqual(gate.standing_approvals, ())


class ConversationTests(unittest.TestCase):
    def test_the_system_prompt_is_always_first(self) -> None:
        thread = Conversation("SYSTEM")
        thread.add_user("hi")
        wire = thread.wire_messages()
        self.assertEqual(wire[0], {"role": "system", "content": "SYSTEM"})

    def test_an_assistant_turn_round_trips_its_tool_calls(self) -> None:
        thread = Conversation("s")
        thread.add_assistant(
            AssistantTurn(text="t", tool_calls=(ToolCall("c1", "read_file", {"path": "a"}),))
        )
        message = thread.messages[0]
        self.assertEqual(message["tool_calls"][0]["id"], "c1")
        self.assertEqual(
            json.loads(message["tool_calls"][0]["function"]["arguments"]), {"path": "a"}
        )

    def test_an_empty_turn_is_not_stored(self) -> None:
        thread = Conversation("s")
        thread.add_assistant(AssistantTurn(text="", tool_calls=()))
        self.assertEqual(thread.messages, [])

    def test_compaction_keeps_the_tail_and_a_summary(self) -> None:
        thread = Conversation("s")
        for n in range(20):
            thread.add_user(f"message {n}")
        freed = thread.compact("a summary", keep_recent=4)
        self.assertGreater(freed, 0)
        self.assertEqual(len(thread.messages), 5)
        self.assertIn("a summary", thread.messages[0]["content"])
        self.assertEqual(thread.messages[-1]["content"], "message 19")

    def test_compaction_never_leaves_a_dangling_tool_result(self) -> None:
        """A tool result whose call was summarised away makes providers reject."""
        thread = Conversation("s")
        thread.add_user("go")
        thread.add_assistant(
            AssistantTurn(text="", tool_calls=(ToolCall("c1", "read_file", {"path": "a"}),))
        )
        thread.add_tool_result("c1", "read_file", "contents")
        thread.add_user("thanks")
        thread.compact("summary", keep_recent=2)
        self.assertNotEqual(thread.messages[1]["role"], "tool")

    def test_a_thread_saves_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            thread = Conversation("original system")
            thread.add_user("remember this")
            thread.save(path)
            reopened = Conversation.load(path, system="fresh system")
            self.assertEqual(reopened.messages[0]["content"], "remember this")
            # The system prompt is rebuilt, never restored: it names the
            # current directory and tool set.
            self.assertEqual(reopened.system, "fresh system")

    def test_an_unreadable_session_file_yields_an_empty_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(Conversation.load(path, system="s").messages, [])

    def test_the_context_meter_grows_with_the_thread(self) -> None:
        thread = Conversation("s")
        before = thread.token_estimate()
        thread.add_user("x" * 4_000)
        self.assertGreater(thread.token_estimate(), before + 500)

    def test_compaction_triggers_before_the_window_is_full(self) -> None:
        thread = Conversation("s", max_tokens=1_000)
        self.assertFalse(thread.needs_compaction())
        thread.add_user("x" * 4_000)
        self.assertTrue(thread.needs_compaction())


class WireFormatTests(unittest.TestCase):
    def test_openai_text_and_several_tool_calls_are_all_kept(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "I'll check two files.",
                        "tool_calls": [
                            {"id": "a", "function": {"name": "read_file", "arguments": '{"path":"x"}'}},
                            {"id": "b", "function": {"name": "read_file", "arguments": '{"path":"y"}'}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
        turn = _read_openai(payload)
        self.assertEqual(turn.text, "I'll check two files.")
        self.assertEqual([c.name for c in turn.tool_calls], ["read_file", "read_file"])
        self.assertEqual(turn.tool_calls[1].arguments, {"path": "y"})
        self.assertEqual(turn.usage["prompt_tokens"], 10)

    def test_plain_prose_needs_no_json_envelope(self) -> None:
        """The batch path demands a JSON action here; a conversation must not."""
        turn = _read_openai({"choices": [{"message": {"content": "Because it caches."}}]})
        self.assertEqual(turn.text, "Because it caches.")
        self.assertEqual(turn.tool_calls, ())

    def test_malformed_arguments_become_a_readable_error(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{oops"}}],
                    }
                }
            ]
        }
        turn = _read_openai(payload)
        self.assertIn("__parse_error__", turn.tool_calls[0].arguments)

    def test_a_nameless_tool_call_is_dropped_not_fatal(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "tool_calls": [{"id": "a", "function": {"name": "", "arguments": "{}"}}],
                    }
                }
            ]
        }
        turn = _read_openai(payload)
        self.assertEqual(turn.tool_calls, ())
        self.assertEqual(turn.text, "hi")

    def test_anthropic_conversion_moves_system_to_the_top(self) -> None:
        system, messages = _to_anthropic_messages(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
        )
        self.assertEqual(system, "S")
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])

    def test_anthropic_merges_consecutive_tool_results(self) -> None:
        """Two parallel calls must come back as one user message, not two."""
        _system, messages = _to_anthropic_messages(
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
                        {"id": "b", "function": {"name": "read_file", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "a", "content": "one"},
                {"role": "tool", "tool_call_id": "b", "content": "two"},
            ]
        )
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(len(messages[-1]["content"]), 2)
        self.assertEqual(messages[-1]["content"][0]["type"], "tool_result")

    def test_anthropic_response_blocks_are_split(self) -> None:
        turn = _read_anthropic(
            {
                "content": [
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "x"}},
                ],
                "stop_reason": "tool_use",
            }
        )
        self.assertEqual(turn.text, "Looking.")
        self.assertEqual(turn.tool_calls[0].name, "grep")


class DescribeTests(unittest.TestCase):
    def test_a_command_is_described_by_its_command(self) -> None:
        call = ToolCall("c", "run_command", {"command": ["pytest", "-q"]})
        self.assertEqual(describe_arguments(call), "pytest -q")

    def test_a_file_tool_is_described_by_its_path(self) -> None:
        call = ToolCall("c", "read_file", {"path": "src/a.py", "offset": 3})
        self.assertEqual(describe_arguments(call), "src/a.py")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
