"""Reading a codebase with no way to change it.

Plan mode is a refusal rather than a prompt. Offering to approve each change
one at a time would be the same as not being in the mode, so the gate says no
and says how to leave, and the system prompt tells the model to spend the turn
producing a plan instead of probing for what it is allowed to do.

Verified live against mercury-2 on the demo repository: it tried `run_command`,
was refused, tried `edit_file`, was refused, and then wrote out the two changes
it would make with the command that would check them. Nothing on disk moved.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.core.contracts import RiskLevel
from harness.execution.workspace import Workspace
from harness.repl import prompt
from harness.repl.approvals import Gate, Mode
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class ModeTests(unittest.TestCase):
    def test_plan_is_a_mode(self) -> None:
        self.assertEqual(Mode("plan"), Mode.PLAN)

    def test_it_has_a_label_for_the_prompt_and_the_banner(self) -> None:
        self.assertTrue(Mode.PLAN.label)
        self.assertIn("changes nothing", Mode.PLAN.label)

    def test_every_mode_still_has_a_label(self) -> None:
        for mode in Mode:
            self.assertTrue(mode.label, mode)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "ay.yaml")
        toolset = ReplToolset(Workspace(self.root, ()), self.config)
        self.specs = {spec.name: spec for spec in toolset.specs()}
        # A prompt that would say yes to anything, to prove plan mode never
        # reaches it.
        self.asked: list[str] = []
        self.gate = Gate(self.config.policy, mode=Mode.PLAN, prompt=self._approve)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _approve(self, request: object) -> object:
        from harness.repl.approvals import Verdict

        self.asked.append(getattr(request, "question", ""))
        return Verdict.ALLOW

    def arguments(self) -> dict:
        return {
            "path": "a.py",
            "command": ["ls"],
            "content": "x",
            "old_string": "a",
            "new_string": "b",
            "pattern": "x",
            "query": "x",
        }

    def test_reads_are_allowed(self) -> None:
        for name in ("read_file", "list_dir", "glob", "grep"):
            decision = self.gate.check(self.specs[name], self.arguments())
            self.assertTrue(decision.allowed, name)

    def test_everything_that_changes_something_is_refused(self) -> None:
        for name in ("write_file", "edit_file", "run_command"):
            decision = self.gate.check(self.specs[name], self.arguments())
            self.assertFalse(decision.allowed, name)

    def test_the_operator_is_never_asked(self) -> None:
        """A prompt would make the mode pointless."""
        for name in ("write_file", "edit_file", "run_command"):
            self.gate.check(self.specs[name], self.arguments())
        self.assertEqual(self.asked, [])

    def test_the_refusal_says_how_to_leave(self) -> None:
        """Otherwise the model spends the turn asking for permission."""
        decision = self.gate.check(self.specs["edit_file"], self.arguments())
        self.assertIn("plan mode", decision.reason)
        self.assertIn("/mode", decision.reason)

    def test_a_standing_approval_does_not_unlock_it(self) -> None:
        """Approvals from an earlier mode must not leak into this one."""
        self.gate.remember("edit_file")
        decision = self.gate.check(self.specs["edit_file"], self.arguments())
        self.assertFalse(decision.allowed)

    def test_the_deny_list_still_comes_first(self) -> None:
        decision = self.gate.check(
            self.specs["run_command"], {"command": ["rm", "-rf", "/"]}
        )
        self.assertFalse(decision.allowed)
        self.assertIn("deny-list", decision.reason)

    def test_must_ask_does_not_report_plan_mode_as_permissive(self) -> None:
        """Unreachable via check, but it must not read as 'allows'."""
        self.assertTrue(self.gate._must_ask(RiskLevel.WRITE))  # noqa: SLF001


class OtherModesUnchangedTests(unittest.TestCase):
    """Adding a mode must not move the other three."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = load_config(ROOT / "configs" / "ay.yaml")
        toolset = ReplToolset(Workspace(Path(self._tmp.name).resolve(), ()), self.config)
        self.specs = {spec.name: spec for spec in toolset.specs()}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_auto_still_runs_commands(self) -> None:
        gate = Gate(self.config.policy, mode=Mode.FULL_AUTO)
        self.assertTrue(gate.check(self.specs["run_command"], {"command": ["ls"]}).allowed)

    def test_auto_edit_still_edits_and_still_asks_before_commands(self) -> None:
        gate = Gate(self.config.policy, mode=Mode.AUTO_EDIT)
        self.assertTrue(
            gate.check(
                self.specs["edit_file"],
                {"path": "a", "old_string": "a", "new_string": "b"},
            ).allowed
        )
        self.assertFalse(gate.check(self.specs["run_command"], {"command": ["ls"]}).allowed)


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "configs" / "ay.yaml")

    def render(self, mode: Mode) -> str:
        return prompt.build(self.config, ROOT, mode=mode)

    def test_plan_mode_tells_the_model_not_to_probe(self) -> None:
        text = self.render(Mode.PLAN)
        self.assertIn("nothing you do can change anything", text)

    def test_it_asks_for_a_plan_specific_enough_to_judge(self) -> None:
        text = self.render(Mode.PLAN)
        self.assertIn("the files, what changes in each", text)

    def test_the_guidance_is_absent_in_every_other_mode(self) -> None:
        for mode in (Mode.SUGGEST, Mode.AUTO_EDIT, Mode.FULL_AUTO):
            self.assertNotIn("nothing you do can change anything", self.render(mode), mode)

    def test_the_mode_is_always_stated(self) -> None:
        for mode in Mode:
            self.assertIn(f"Approval mode: {mode.value}", self.render(mode), mode)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
