"""The project's checker, run over a file the agent just changed.

One rule shapes every test here: **a diagnostic is not a failed edit.**
Another agent shipped exactly that bug -- its model read the report attached
to a successful edit, concluded the edit had not applied, and wrote the same
change again. So the assertions are about separation: the edit's own result
comes first, the report is labelled as a separate thing, and `ok` never moves
because a checker had something to say.

The second theme is what the model is *not* told. A checker that is missing or
misconfigured is the operator's problem; a model told its linter is absent
tries to install one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import ConfigurationError
from harness.execution.diagnostics import (
    DiagnosticsConfig,
    Report,
    attach,
    build_command,
    check,
    diagnostics_config_from_dict,
)

#: A checker written as a Python one-liner, so these tests need no tool
#: installed and behave the same on every machine.
COMPLAINS = (sys.executable, "-c", "import sys; print('problem in', sys.argv[1]); sys.exit(1)")
HAPPY = (sys.executable, "-c", "print('All checks passed!')")


class ConfigTests(unittest.TestCase):
    def test_a_sensor_is_on_by_default(self) -> None:
        """A harness with every sensor switched off is feed-forward only.

        It encodes rules and never finds out whether they worked. The default
        is the floor rather than a preference: `py_compile` is standard
        library, needs no configuration, and catches an edit that left the
        file unparseable at the moment it was made.
        """
        config = DiagnosticsConfig()
        self.assertTrue(config.enabled)
        self.assertIn("py_compile", " ".join(config.command))
        self.assertEqual(config.suffixes, (".py",))

    def test_the_default_needs_nothing_installed(self) -> None:
        """Anything that could be absent is not a default."""
        self.assertIn(sys.executable, DiagnosticsConfig().command)

    def test_an_empty_command_turns_it_off(self) -> None:
        """The operator has to be able to say no."""
        self.assertFalse(diagnostics_config_from_dict({"command": []}).enabled)

    def test_a_command_written_as_a_string_is_accepted(self) -> None:
        """It is what an operator writes first."""
        config = diagnostics_config_from_dict({"command": "ruff check"})
        self.assertEqual(config.command, ("ruff", "check"))

    def test_a_command_written_as_a_list_is_accepted(self) -> None:
        self.assertEqual(
            diagnostics_config_from_dict({"command": ["ruff", "check"]}).command,
            ("ruff", "check"),
        )

    def test_an_unknown_key_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            diagnostics_config_from_dict({"comand": "ruff"})

    def test_a_command_of_the_wrong_shape_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            diagnostics_config_from_dict({"command": {"run": "ruff"}})

    def test_suffixes_decide_which_files_are_checked(self) -> None:
        """A Python type checker should not run over a Markdown edit."""
        config = diagnostics_config_from_dict({"command": "x", "suffixes": [".py"]})
        self.assertTrue(config.applies_to("a/b.py"))
        self.assertFalse(config.applies_to("README.md"))

    def test_no_suffixes_means_every_file(self) -> None:
        config = diagnostics_config_from_dict({"command": "x", "suffixes": []})
        self.assertTrue(config.applies_to("README.md"))


class CommandTests(unittest.TestCase):
    def test_the_file_is_appended_when_the_token_is_absent(self) -> None:
        config = DiagnosticsConfig(command=("ruff", "check"))
        self.assertEqual(build_command(config, "a.py"), ["ruff", "check", "a.py"])

    def test_the_token_is_substituted_where_it_appears(self) -> None:
        config = DiagnosticsConfig(command=("check", "{file}", "--strict"))
        self.assertEqual(build_command(config, "a.py"), ["check", "a.py", "--strict"])


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_happy_checker_reports_nothing(self) -> None:
        """The exit code is the signal, not the output.

        Checkers announce success in prose -- "All checks passed!", "Success:
        no issues found" -- and reading that as a finding would attach a report
        to every clean edit and teach the model to ignore the section.
        """
        report = check(DiagnosticsConfig(command=HAPPY), self.root, "a.py")
        self.assertTrue(report.clean)

    def test_a_complaining_checker_reports_what_it_said(self) -> None:
        report = check(DiagnosticsConfig(command=COMPLAINS), self.root, "a.py")
        self.assertFalse(report.clean)
        self.assertIn("problem in", report.output)

    def test_a_file_outside_the_suffixes_is_not_checked(self) -> None:
        config = DiagnosticsConfig(command=COMPLAINS, suffixes=(".ts",))
        self.assertTrue(check(config, self.root, "a.py").clean)

    def test_a_missing_checker_is_reported_as_broken_not_as_a_finding(self) -> None:
        report = check(DiagnosticsConfig(command=("no-such-checker-xyz",)), self.root, "a.py")
        self.assertTrue(report.broken)

    def test_a_slow_checker_is_cut_off(self) -> None:
        slow = (sys.executable, "-c", "import time; time.sleep(30)")
        report = check(DiagnosticsConfig(command=slow, timeout=1), self.root, "a.py")
        self.assertTrue(report.broken)

    def test_a_flood_of_findings_is_trimmed(self) -> None:
        """Fifty problems is a broken setup, and pasting it buries the edit."""
        noisy = (sys.executable, "-c", "import sys\nfor n in range(100): print(n)\nsys.exit(1)")
        report = check(DiagnosticsConfig(command=noisy, max_lines=5), self.root, "a.py")
        self.assertLessEqual(len(report.output.splitlines()), 6)
        self.assertIn("more", report.output)

    def test_checking_never_raises(self) -> None:
        for command in ((), ("no-such-checker-xyz",), COMPLAINS):
            check(DiagnosticsConfig(command=command), self.root, "a.py")


class AttachTests(unittest.TestCase):
    """The bug this whole module is shaped around."""

    def report(self) -> Report:
        return Report("a.py:1:1 F401 unused import")

    def test_the_edit_result_comes_first_and_intact(self) -> None:
        combined = attach("edited a.py in 1 place (+1 -1)", self.report())
        self.assertTrue(combined.startswith("edited a.py in 1 place (+1 -1)"))

    def test_the_model_is_told_the_edit_was_applied(self) -> None:
        """Otherwise it reads the report as a failure and edits again."""
        self.assertIn("was applied", attach("edited", self.report()))

    def test_the_model_is_told_not_to_repeat_the_edit(self) -> None:
        self.assertIn("do not repeat", attach("edited", self.report()).lower())

    def test_the_report_is_separated_from_the_result(self) -> None:
        self.assertIn("---", attach("edited", self.report()))

    def test_the_findings_are_included(self) -> None:
        self.assertIn("F401", attach("edited", self.report()))

    def test_a_clean_report_adds_nothing_at_all(self) -> None:
        self.assertEqual(attach("edited", Report("")), "edited")

    def test_a_broken_checker_never_reaches_the_model(self) -> None:
        """A missing linter is the operator's problem, not the model's."""
        broken = Report("no such tool: ruff", broken=True)
        self.assertEqual(attach("edited", broken), "edited")


class ToolsetTests(unittest.TestCase):
    """Wired into the tools that write, and only those."""

    def setUp(self) -> None:
        from harness.config import load_config
        from harness.execution.workspace import Workspace
        from harness.repl.tools import ReplToolset

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "a.py").write_text("first line\nsecond line\n", encoding="utf-8")
        base = load_config(Path(__file__).resolve().parents[1] / "configs" / "ay.yaml")
        import dataclasses

        self.config = dataclasses.replace(
            base, diagnostics=DiagnosticsConfig(command=COMPLAINS, suffixes=(".py",))
        )
        self.toolset = ReplToolset(Workspace(self.root, ()), self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_edit_carries_the_report(self) -> None:
        outcome = self.toolset.edit_file(
            {"path": "a.py", "old_string": "first line", "new_string": "changed"}
        )
        self.assertIn("problem in", outcome.content)

    def test_the_edit_still_succeeds(self) -> None:
        """The flag the other agent's bug turned on."""
        outcome = self.toolset.edit_file(
            {"path": "a.py", "old_string": "first line", "new_string": "changed"}
        )
        self.assertTrue(outcome.ok)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8").splitlines()[0], "changed")

    def test_a_write_carries_it_too(self) -> None:
        outcome = self.toolset.write_file({"path": "b.py", "content": "x = 1\n"})
        self.assertIn("problem in", outcome.content)

    def test_a_read_does_not_run_the_checker(self) -> None:
        outcome = self.toolset.read_file({"path": "a.py"})
        self.assertNotIn("problem in", outcome.content)

    def test_a_broken_checker_becomes_an_operator_notice(self) -> None:
        import dataclasses

        from harness.execution.workspace import Workspace
        from harness.repl.tools import ReplToolset

        config = dataclasses.replace(
            self.config, diagnostics=DiagnosticsConfig(command=("no-such-xyz",), suffixes=(".py",))
        )
        toolset = ReplToolset(Workspace(self.root, ()), config)
        outcome = toolset.edit_file(
            {"path": "a.py", "old_string": "first line", "new_string": "changed"}
        )
        self.assertNotIn("no-such-xyz", outcome.content)
        self.assertTrue(toolset.notices)

    def test_the_default_checker_says_nothing_about_a_valid_file(self) -> None:
        from harness.config import load_config
        from harness.execution.workspace import Workspace
        from harness.repl.tools import ReplToolset

        base = load_config(Path(__file__).resolve().parents[1] / "configs" / "ay.yaml")
        (self.root / "valid.py").write_text("value = 1\n", encoding="utf-8")
        toolset = ReplToolset(Workspace(self.root, ()), base)
        outcome = toolset.edit_file(
            {"path": "valid.py", "old_string": "value = 1", "new_string": "value = 2"}
        )
        self.assertNotIn("---", outcome.content)

    def test_the_default_checker_catches_an_unparseable_edit(self) -> None:
        """The mistake an agent actually makes, caught where it is made."""
        from harness.config import load_config
        from harness.execution.workspace import Workspace
        from harness.repl.tools import ReplToolset

        base = load_config(Path(__file__).resolve().parents[1] / "configs" / "ay.yaml")
        (self.root / "broken.py").write_text("value = 1\n", encoding="utf-8")
        toolset = ReplToolset(Workspace(self.root, ()), base)
        outcome = toolset.edit_file(
            {"path": "broken.py", "old_string": "value = 1", "new_string": "value = = 1"}
        )
        self.assertTrue(outcome.ok, "a diagnostic turned a real edit into a failure")
        self.assertIn("was applied", outcome.content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
