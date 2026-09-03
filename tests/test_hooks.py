"""Operator commands that run when the agent acts.

The design decision worth testing is what a hook cannot do. It observes and it
cannot veto, because `Gate` already answers that question and two authorities
over one decision is how a permission system stops being trustworthy. The tests
here assert that a hook never changes an outcome, alongside the ordinary things
about matching, failure and configuration.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.core.errors import ConfigurationError
from harness.execution.hooks import EVENTS, Hook, HookRunner, parse_hooks
from harness.execution.workspace import Workspace
from harness.repl.agent import Agent, Events
from harness.repl.approvals import Gate, Mode
from harness.repl.conversation import Conversation, ToolCall
from harness.repl.model import ChatModel
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


def writer(target: Path, text: str = "fired") -> tuple[str, ...]:
    return (sys.executable, "-c", f"import pathlib;pathlib.Path(r{str(target)!r}).write_text({text!r})")


class ParseTests(unittest.TestCase):
    def parse(self, entry: dict):
        return parse_hooks([entry], "hooks")

    def test_a_minimal_hook(self) -> None:
        hooks = self.parse({"event": "tool_end", "run": ["echo", "hi"]})
        self.assertEqual(hooks[0].event, "tool_end")
        self.assertEqual(hooks[0].command, ("echo", "hi"))

    def test_no_hooks_is_fine(self) -> None:
        self.assertEqual(parse_hooks([], "hooks"), ())
        self.assertEqual(parse_hooks(None, "hooks"), ())

    def test_an_unknown_event_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            self.parse({"event": "whenever", "run": ["echo"]})
        for event in EVENTS:
            self.assertIn(event, str(caught.exception))

    def test_a_missing_command_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.parse({"event": "tool_end"})

    def test_an_empty_command_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.parse({"event": "tool_end", "run": []})

    def test_an_unknown_key_is_refused(self) -> None:
        """A typo that would silently never fire is worse than a failure."""
        with self.assertRaises(ConfigurationError):
            self.parse({"event": "tool_end", "run": ["echo"], "mach": "edit_file"})

    def test_a_nonpositive_timeout_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.parse({"event": "tool_end", "run": ["echo"], "timeout": 0})

    def test_hooks_load_from_a_real_config(self) -> None:
        text = (ROOT / "configs" / "ay.yaml").read_text(encoding="utf-8")
        text += "\nhooks:\n  - event: tool_end\n    match: edit_file\n    run: [echo, done]\n"
        path = Path(tempfile.mkdtemp()) / "c.yaml"
        path.write_text(text, encoding="utf-8")
        config = load_config(path)
        self.assertEqual(len(config.hooks), 1)
        self.assertEqual(config.hooks[0].match, "edit_file")


class MatchTests(unittest.TestCase):
    def hook(self, **kwargs) -> Hook:
        return Hook(event=kwargs.pop("event", "tool_end"), command=("echo",), **kwargs)

    def test_the_event_has_to_match(self) -> None:
        self.assertTrue(self.hook().applies("tool_end", "edit_file"))
        self.assertFalse(self.hook().applies("tool_start", "edit_file"))

    def test_no_match_means_every_tool(self) -> None:
        for tool in ("edit_file", "run_command", "read_file"):
            self.assertTrue(self.hook().applies("tool_end", tool))

    def test_a_match_is_a_glob(self) -> None:
        hook = self.hook(match="*_file")
        self.assertTrue(hook.applies("tool_end", "edit_file"))
        self.assertTrue(hook.applies("tool_end", "write_file"))
        self.assertFalse(hook.applies("tool_end", "run_command"))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.marker = self.root / "fired.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_hook_runs(self) -> None:
        runner = HookRunner((Hook("tool_end", writer(self.marker)),), root=self.root)
        reports = runner.fire("tool_end", tool="edit_file")
        self.assertTrue(reports[0].ok, reports[0].output)
        self.assertTrue(self.marker.exists())

    def test_a_hook_that_does_not_match_does_not_run(self) -> None:
        runner = HookRunner(
            (Hook("tool_end", writer(self.marker), match="edit_file"),), root=self.root
        )
        runner.fire("tool_end", tool="read_file")
        self.assertFalse(self.marker.exists())

    def test_a_missing_binary_is_reported_not_raised(self) -> None:
        runner = HookRunner((Hook("tool_end", ("no-such-binary-xyz",), name="ghost"),), root=self.root)
        reports = runner.fire("tool_end", tool="x")
        self.assertFalse(reports[0].ok)
        self.assertTrue(reports[0].output)

    def test_a_broken_hook_is_disabled_for_the_session(self) -> None:
        """The tenth identical error tells nobody anything."""
        runner = HookRunner((Hook("tool_end", ("no-such-binary-xyz",), name="ghost"),), root=self.root)
        self.assertEqual(len(runner.fire("tool_end", tool="x")), 1)
        self.assertEqual(runner.fire("tool_end", tool="x"), [])

    def test_a_non_zero_exit_keeps_running(self) -> None:
        """A lint hook failing is the hook working."""
        hook = Hook("tool_end", (sys.executable, "-c", "import sys;sys.exit(3)"), name="lint")
        runner = HookRunner((hook,), root=self.root)
        self.assertFalse(runner.fire("tool_end", tool="x")[0].ok)
        self.assertEqual(len(runner.fire("tool_end", tool="x")), 1)

    def test_a_slow_hook_is_cut_off_and_disabled(self) -> None:
        hook = Hook(
            "tool_end", (sys.executable, "-c", "import time;time.sleep(30)"), timeout=2, name="slow"
        )
        runner = HookRunner((hook,), root=self.root)
        reports = runner.fire("tool_end", tool="x")
        self.assertFalse(reports[0].ok)
        self.assertIn("timed out", reports[0].output)
        self.assertEqual(runner.fire("tool_end", tool="x"), [])

    def test_the_context_reaches_the_hook(self) -> None:
        script = (
            "import os,pathlib;"
            f"pathlib.Path(r{str(self.marker)!r})"
            ".write_text(os.environ['HARNESS_EVENT'] + ':' + os.environ['HARNESS_TOOL'])"
        )
        runner = HookRunner((Hook("tool_end", (sys.executable, "-c", script)),), root=self.root)
        runner.fire("tool_end", tool="edit_file")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "tool_end:edit_file")

    def test_no_hooks_is_a_no_op(self) -> None:
        self.assertEqual(HookRunner().fire("tool_end", tool="x"), [])


class ObserveOnlyTests(unittest.TestCase):
    """A hook must not be able to change what happened."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.config = load_config(ROOT / "configs" / "ay.yaml")
        self.notices: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def agent(self, hooks: HookRunner) -> Agent:
        return Agent(
            model=ChatModel(self.config.router.routes["inception"]),
            conversation=Conversation("test"),
            toolset=ReplToolset(Workspace(self.root, ()), self.config),
            gate=Gate(self.config.policy, mode=Mode.FULL_AUTO),
            config=self.config,
            events=Events(on_notice=self.notices.append),
            hooks=hooks,
        )

    def edit(self, agent: Agent) -> bool:
        return agent._run_tool(  # noqa: SLF001
            ToolCall(
                id="1",
                name="edit_file",
                arguments={"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            )
        )

    def test_a_failing_hook_does_not_undo_the_tool(self) -> None:
        hook = Hook("tool_end", (sys.executable, "-c", "import sys;sys.exit(1)"), name="angry")
        ok = self.edit(self.agent(HookRunner((hook,), root=self.root)))
        self.assertTrue(ok, "a hook changed the outcome of the call")
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "x = 2\n")

    def test_a_failing_hook_reaches_the_operator(self) -> None:
        hook = Hook("tool_end", ("no-such-binary-xyz",), name="ghost")
        self.edit(self.agent(HookRunner((hook,), root=self.root)))
        self.assertTrue(any("ghost" in notice for notice in self.notices))

    def test_a_failing_hook_does_not_reach_the_model(self) -> None:
        """Otherwise it tries to fix the operator's formatter."""
        hook = Hook("tool_end", ("no-such-binary-xyz",), name="ghost")
        agent = self.agent(HookRunner((hook,), root=self.root))
        self.edit(agent)
        thread = " ".join(str(message) for message in agent.conversation.messages)
        self.assertNotIn("ghost", thread)

    def test_both_events_fire_around_a_call(self) -> None:
        started = self.root / "start.txt"
        ended = self.root / "end.txt"
        runner = HookRunner(
            (Hook("tool_start", writer(started)), Hook("tool_end", writer(ended))),
            root=self.root,
        )
        self.edit(self.agent(runner))
        self.assertTrue(started.exists())
        self.assertTrue(ended.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
