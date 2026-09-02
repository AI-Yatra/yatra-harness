"""Tests for the shell around the loop: arguments, commands, rendering.

The shell is where a mistake is visible rather than merely wrong, so the
cases here are mostly about what reaches the screen and what a slash command
does to the session.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ay
from harness.core.contracts import RiskLevel, ToolSpec
from harness.repl.approvals import Mode, Request, Verdict
from harness.repl.conversation import AssistantTurn, Conversation, ToolCall
from harness.repl.render import Console, Renderer
from harness.repl.shell import Options, Shell, _answer_dangling_calls

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "ay.yaml"


def quiet_console() -> Console:
    return Console(io.StringIO(), colour=False)


class ArgumentTests(unittest.TestCase):
    def test_harness_verbs_are_delegated_untouched(self) -> None:
        with mock.patch("harness.cli.main", return_value=0) as cli:
            self.assertEqual(ay.main_with_argv(["auth", "status"]), 0)
        cli.assert_called_once_with(["auth", "status"])

    def test_run_still_reaches_the_batch_harness(self) -> None:
        with mock.patch("harness.cli.main", return_value=0) as cli:
            ay.main_with_argv(["run", "task.yaml", "--config", "c.yaml"])
        cli.assert_called_once_with(["run", "task.yaml", "--config", "c.yaml"])

    def test_a_message_is_not_mistaken_for_a_verb(self) -> None:
        parser = ay.build_parser()
        arguments = parser.parse_args(["why", "is", "this", "slow"])
        self.assertEqual(arguments.message, ["why", "is", "this", "slow"])

    def test_print_mode_needs_a_message(self) -> None:
        with self.assertRaises(SystemExit):
            ay.main_with_argv(["-p"])

    def test_a_missing_directory_is_reported(self) -> None:
        code = ay.main_with_argv(["-C", str(ROOT / "definitely-not-here")])
        self.assertEqual(code, 2)

    def test_a_missing_config_is_reported(self) -> None:
        code = ay.main_with_argv(["--config", str(ROOT / "configs" / "nope.yaml")])
        self.assertEqual(code, 2)

    def test_the_default_config_exists(self) -> None:
        self.assertTrue(ay.DEFAULT_CONFIG.is_file(), ay.DEFAULT_CONFIG)

    def test_the_modes_offered_match_the_gate(self) -> None:
        parser = ay.build_parser()
        action = next(a for a in parser._actions if a.dest == "mode")
        self.assertEqual(set(action.choices), {m.value for m in Mode})


class ShellTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        options = Options(
            config_path=CONFIG,
            root=self.root,
            mode=Mode.SUGGEST,
            sessions_dir=self.root / ".ay",
        )
        self.shell = Shell(options)
        self.shell.console = quiet_console()
        self.shell.render = Renderer(self.shell.console)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def output(self) -> str:
        return self.shell.console.stream.getvalue()


class CommandTests(ShellTestCase):
    def test_exit_ends_the_session(self) -> None:
        self.assertFalse(self.shell._command("/exit"))

    def test_an_unknown_command_is_reported_and_continues(self) -> None:
        self.assertTrue(self.shell._command("/teleport"))
        self.assertIn("unknown command", self.output)

    def test_help_lists_the_commands(self) -> None:
        self.shell._command("/help")
        for expected in ("/model", "/mode", "/compact", "@path", "!command"):
            self.assertIn(expected, self.output)

    def test_mode_switches_the_gate_too(self) -> None:
        self.shell._command("/mode full-auto")
        self.assertIs(self.shell.mode, Mode.FULL_AUTO)
        self.assertIs(self.shell.gate.mode, Mode.FULL_AUTO)

    def test_an_invalid_mode_is_rejected_without_changing_anything(self) -> None:
        self.shell._command("/mode reckless")
        self.assertIs(self.shell.mode, Mode.SUGGEST)
        self.assertIn("modes are", self.output)

    def test_clear_forgets_the_conversation_only(self) -> None:
        self.shell.conversation.add_user("something")
        (self.root / "keep.txt").write_text("still here", encoding="utf-8")
        self.shell._command("/clear")
        self.assertEqual(self.shell.conversation.messages, [])
        self.assertTrue((self.root / "keep.txt").exists())

    def test_tools_lists_what_the_model_can_call(self) -> None:
        self.shell._command("/tools")
        for name in ("read_file", "edit_file", "run_command"):
            self.assertIn(name, self.output)

    def test_context_shows_a_meter(self) -> None:
        self.shell._command("/context")
        self.assertIn("tokens", self.output)

    def test_approvals_reports_nothing_standing_by_default(self) -> None:
        self.shell._command("/approvals")
        self.assertIn("Nothing is blanket-approved", self.output)

    def test_approvals_lists_what_was_granted(self) -> None:
        self.shell.gate.remember("run_command:git")
        self.shell._command("/approvals")
        self.assertIn("run_command:git", self.output)

    def test_model_without_an_argument_reports_the_current_one(self) -> None:
        self.shell._command("/model")
        self.assertIn(self.shell.route.model, self.output)

    def test_model_switches_the_route_the_agent_uses(self) -> None:
        self.shell._command("/model some-other-model")
        self.assertEqual(self.shell.route.model, "some-other-model")
        self.assertIs(self.shell.agent.model, self.shell.model)

    def test_config_reports_where_everything_came_from(self) -> None:
        self.shell._command("/config")
        self.assertIn(str(CONFIG), self.output)
        self.assertIn(str(self.root), self.output)


class ExpansionTests(ShellTestCase):
    def test_an_at_reference_inlines_the_file(self) -> None:
        (self.root / "notes.txt").write_text("the contents\n", encoding="utf-8")
        expanded = self.shell._expand("explain @notes.txt please")
        self.assertIn("the contents", expanded)
        self.assertIn("explain @notes.txt please", expanded)

    def test_a_missing_reference_is_reported_and_the_message_survives(self) -> None:
        expanded = self.shell._expand("look at @gone.txt")
        self.assertEqual(expanded, "look at @gone.txt")
        self.assertIn("gone.txt", self.output)

    def test_an_email_address_is_not_treated_as_a_file(self) -> None:
        text = "mail someone@example.com about it"
        self.assertEqual(self.shell._expand(text), text)

    def test_the_same_reference_twice_is_included_once(self) -> None:
        (self.root / "a.txt").write_text("body\n", encoding="utf-8")
        expanded = self.shell._expand("@a.txt and @a.txt")
        self.assertEqual(expanded.count("--- a.txt ---"), 1)


class SessionTests(ShellTestCase):
    def test_a_session_is_written_after_a_turn(self) -> None:
        self.shell.conversation.add_user("remember me")
        self.shell._persist()
        self.assertTrue(self.shell._session_path().is_file())

    def test_resuming_reopens_the_saved_thread(self) -> None:
        self.shell.conversation.add_user("earlier message")
        self.shell._persist()
        reopened = Shell(
            Options(
                config_path=CONFIG,
                root=self.root,
                session_id=self.shell.session_id,
                resume=True,
                sessions_dir=self.root / ".ay",
            )
        )
        self.assertEqual(reopened.conversation.messages[0]["content"], "earlier message")

    def test_resume_without_a_name_finds_the_most_recent(self) -> None:
        self.shell.conversation.add_user("the only one")
        self.shell._persist()
        reopened = Shell(
            Options(config_path=CONFIG, root=self.root, resume=True, sessions_dir=self.root / ".ay")
        )
        self.assertEqual(reopened.session_id, self.shell.session_id)

    def test_a_fresh_session_starts_empty(self) -> None:
        self.assertEqual(self.shell.conversation.messages, [])


class InterruptTests(unittest.TestCase):
    def test_dangling_tool_calls_are_answered(self) -> None:
        """An interrupted turn leaves calls with no results; providers reject that."""
        thread = Conversation("s")
        thread.add_user("go")
        thread.add_assistant(
            AssistantTurn(
                text="",
                tool_calls=(
                    ToolCall("c1", "read_file", {"path": "a"}),
                    ToolCall("c2", "read_file", {"path": "b"}),
                ),
            )
        )
        thread.add_tool_result("c1", "read_file", "done")
        _answer_dangling_calls(thread, "interrupted")
        answered = {m["tool_call_id"] for m in thread.messages if m.get("role") == "tool"}
        self.assertEqual(answered, {"c1", "c2"})

    def test_answering_is_a_no_op_when_nothing_dangles(self) -> None:
        thread = Conversation("s")
        thread.add_user("hi")
        before = len(thread.messages)
        _answer_dangling_calls(thread, "interrupted")
        self.assertEqual(len(thread.messages), before)


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = quiet_console()
        self.render = Renderer(self.console)

    @property
    def output(self) -> str:
        return self.console.stream.getvalue()

    def test_a_tool_card_names_the_tool_and_its_target(self) -> None:
        self.render.tool_start("read_file", "src/a.py")
        self.assertIn("Read", self.output)
        self.assertIn("src/a.py", self.output)

    def test_a_result_line_is_indented_under_the_card(self) -> None:
        self.render.tool_result("42 lines")
        self.assertIn("42 lines", self.output)

    def test_a_long_result_is_truncated_rather_than_flooding(self) -> None:
        self.render.tool_result("\n".join(f"line {n}" for n in range(200)))
        self.assertLessEqual(len(self.output.splitlines()), 12)

    def test_prose_is_wrapped(self) -> None:
        self.render.assistant_text("word " * 200)
        for line in self.output.splitlines():
            self.assertLessEqual(len(line), 121)

    def test_a_code_fence_is_left_intact(self) -> None:
        self.render.assistant_text("Here:\n```python\ndef f(x):    return x\n```\ndone")
        self.assertIn("def f(x):    return x", self.output)

    def test_headings_and_bullets_survive(self) -> None:
        self.render.assistant_text("# Title\n\n- first\n- second")
        self.assertIn("Title", self.output)
        self.assertIn("first", self.output)

    def test_a_diff_is_printed_with_its_markers(self) -> None:
        self.render.diff("@@ -1 +1 @@\n-old\n+new")
        self.assertIn("-old", self.output)
        self.assertIn("+new", self.output)

    def test_the_console_falls_back_when_a_glyph_cannot_be_encoded(self) -> None:
        """A cp1252 console must lose the glyphs, not the session."""

        class Cp1252Stream(io.StringIO):
            encoding = "cp1252"

        console = Console(Cp1252Stream(), colour=False)
        self.assertEqual(console.glyphs.bullet, "*")
        self.assertEqual(console.glyphs.branch, "`-")

    def test_a_utf8_console_keeps_the_glyphs(self) -> None:
        class Utf8Stream(io.StringIO):
            encoding = "utf-8"

        self.assertEqual(Console(Utf8Stream(), colour=False).glyphs.bullet, "⏺")

    def test_writing_an_unencodable_glyph_does_not_raise(self) -> None:
        class Strict(io.StringIO):
            encoding = "cp1252"

            def write(self, text: str) -> int:
                text.encode("cp1252")  # raises exactly as a real console does
                return super().write(text)

        console = Console(Strict(), colour=False)
        console.line("héllo ⏺ world")  # must not propagate
        self.assertIn("world", console.stream.getvalue())


class PromptTests(unittest.TestCase):
    def _request(self) -> Request:
        return Request(
            tool=ToolSpec("run_command", "", {}, RiskLevel.EXECUTE),
            arguments={"command": ["pytest"]},
            target="pytest",
            question="Run pytest?",
            preview="pytest",
            always_means="run any pytest command",
        )

    def _answer(self, reply: str) -> Verdict:
        render = Renderer(quiet_console())
        with mock.patch("builtins.input", return_value=reply):
            return render.ask(self._request())

    def test_yes_allows_once(self) -> None:
        self.assertIs(self._answer("1"), Verdict.ALLOW)

    def test_two_allows_for_the_session(self) -> None:
        self.assertIs(self._answer("2"), Verdict.ALLOW_ALWAYS)

    def test_three_denies(self) -> None:
        self.assertIs(self._answer("3"), Verdict.DENY)

    def test_empty_input_denies(self) -> None:
        """The safe answer must be the one a stray Enter gives."""
        self.assertIs(self._answer(""), Verdict.DENY)

    def test_interrupting_the_prompt_denies(self) -> None:
        render = Renderer(quiet_console())
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertIs(render.ask(self._request()), Verdict.DENY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
