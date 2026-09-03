"""The deny-list against someone trying to get around it.

`tests/test_policy_denylist.py` covers the straightforward cases. These are the
ways the previous contiguous-argv matcher was defeated, kept as tests so it
cannot regress to being a control that only reads like one.

The honest boundary is written down at the end: a pattern list cannot catch
arbitrary code in a general-purpose language, and the sandbox rather than the
deny-list is what stands in the way of that.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from harness.config import load_config
from harness.execution.policy import (
    carried_code,
    denied_pattern,
    expand_command,
    normalize_command,
)

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = load_config(ROOT / "configs" / "ay.yaml").policy.denied_commands


def denied(*command: str) -> str | None:
    return denied_pattern(command, PATTERNS)


class NormalizationTests(unittest.TestCase):
    def test_an_absolute_path_is_reduced_to_the_program(self) -> None:
        self.assertEqual(normalize_command(("/usr/bin/rm", "-rf")), ("rm", "-rf"))

    def test_a_windows_path_is_reduced_too(self) -> None:
        self.assertEqual(
            normalize_command((r"C:\Windows\System32\rm.exe", "-rf")), ("rm", "-rf")
        )

    def test_the_program_name_is_case_folded(self) -> None:
        self.assertEqual(normalize_command(("RM.EXE",)), ("rm",))

    def test_python_versions_fold_together(self) -> None:
        for spelling in ("python3", "python2", "python3.13"):
            self.assertEqual(normalize_command((spelling, "-c"))[0], "python", spelling)

    def test_an_empty_command_is_returned_unchanged(self) -> None:
        self.assertEqual(normalize_command(()), ())


class ExpansionTests(unittest.TestCase):
    def test_a_shell_wrapper_exposes_the_inner_command(self) -> None:
        found = expand_command(("bash", "-c", "rm -rf /"))
        self.assertIn(("rm", "-rf", "/"), found)

    def test_combined_shell_flags_are_understood(self) -> None:
        for flag in ("-c", "-lc", "-ic", "-xc"):
            found = expand_command(("bash", flag, "rm -rf /"))
            self.assertIn(("rm", "-rf", "/"), found, flag)

    def test_each_side_of_an_operator_is_exposed(self) -> None:
        for code in ("ls && rm -rf /", "ls; rm -rf /", "ls || rm -rf /", "ls | rm -rf /"):
            self.assertIn(("rm", "-rf", "/"), expand_command(("sh", "-c", code)), code)

    def test_an_operator_glued_to_a_word_is_still_split(self) -> None:
        self.assertIn(("rm", "-rf", "/"), expand_command(("sh", "-c", "ls&&rm -rf /")))

    def test_a_command_prefix_is_stripped(self) -> None:
        for prefix in ("sudo", "nohup", "timeout"):
            found = expand_command((prefix, "rm", "-rf", "/"))
            self.assertIn(("rm", "-rf", "/"), found, prefix)

    def test_env_assignments_are_stripped(self) -> None:
        found = expand_command(("env", "FOO=1", "BAR=2", "rm", "-rf", "/"))
        self.assertIn(("rm", "-rf", "/"), found)

    def test_nesting_is_followed_but_bounded(self) -> None:
        nested = ("bash", "-c", "bash -c 'rm -rf /'")
        self.assertIn(("rm", "-rf", "/"), expand_command(nested))
        # Absurd depth terminates rather than recursing forever.
        deep = ("bash", "-c", "bash -c " * 40 + "rm")
        self.assertTrue(expand_command(deep))

    def test_unbalanced_quotes_degrade_instead_of_being_skipped(self) -> None:
        """Skipping unparseable input is how a check gets bypassed."""
        found = expand_command(("sh", "-c", "rm -rf / \"unclosed"))
        self.assertTrue(any(part and part[0] == "rm" for part in found))


class EvasionTests(unittest.TestCase):
    """Every one of these was allowed before the matcher was rewritten."""

    def test_the_plain_form_is_still_refused(self) -> None:
        self.assertIsNotNone(denied("rm", "-rf", "/"))

    def test_shell_wrappers(self) -> None:
        for shell, flag in (("bash", "-c"), ("sh", "-c"), ("zsh", "-c"), ("bash", "-lc")):
            self.assertIsNotNone(denied(shell, flag, "rm -rf /"), f"{shell} {flag}")

    def test_windows_shells(self) -> None:
        self.assertIsNotNone(denied("cmd.exe", "/c", "rm -rf /"))
        self.assertIsNotNone(denied("powershell", "-Command", "rm -rf /"))

    def test_an_inserted_flag_no_longer_breaks_the_match(self) -> None:
        self.assertIsNotNone(denied("git", "-C", ".", "push", "--force"))
        self.assertIsNotNone(denied("git", "--no-pager", "push", "--force"))

    def test_an_absolute_or_renamed_binary(self) -> None:
        self.assertIsNotNone(denied("/bin/rm", "-rf", "/"))
        self.assertIsNotNone(denied("RM.EXE", "-rf", "/"))

    def test_a_privilege_or_env_prefix(self) -> None:
        self.assertIsNotNone(denied("sudo", "rm", "-rf", "/"))
        self.assertIsNotNone(denied("env", "FOO=1", "rm", "-rf", "/"))

    def test_hidden_behind_an_operator(self) -> None:
        self.assertIsNotNone(denied("bash", "-c", "ls && rm -rf /"))
        self.assertIsNotNone(denied("bash", "-c", "ls;rm -rf /"))

    def test_literal_text_inside_interpreter_code(self) -> None:
        """Structural matching cannot see this; the textual pass can."""
        self.assertIsNotNone(denied("python", "-c", 'import os; os.system("rm -rf /")'))
        self.assertIsNotNone(
            denied("node", "-e", 'require("child_process").exec("rm -rf /")')
        )


class FalsePositiveTests(unittest.TestCase):
    """A deny-list that blocks ordinary work gets turned off."""

    def test_ordinary_commands_are_untouched(self) -> None:
        for command in (
            ("ls", "-la"),
            ("echo", "hello"),
            ("git", "status"),
            ("git", "log", "--oneline"),
            ("git", "diff", "--stat"),
            ("python", "-m", "pytest", "-q"),
            ("python", "-m", "unittest", "discover", "-s", "tests"),
            ("python", "-c", "print(1 + 1)"),
            ("npm", "run", "build"),
        ):
            self.assertIsNone(denied(*command), command)

    def test_an_empty_pattern_never_matches(self) -> None:
        self.assertIsNone(denied_pattern(("rm", "-rf"), ((),)))

    def test_an_empty_command_never_matches(self) -> None:
        self.assertIsNone(denied_pattern((), PATTERNS))


class CarriedCodeTests(unittest.TestCase):
    def test_it_returns_the_code_a_wrapper_holds(self) -> None:
        self.assertIn("rm -rf /", carried_code(("bash", "-c", "rm -rf /")))

    def test_a_plain_command_carries_none(self) -> None:
        self.assertEqual(carried_code(("ls", "-la")), [])


class KnownLimitTests(unittest.TestCase):
    """Written down rather than left to be discovered.

    A deny-list matches patterns. Code that performs the same destruction
    without naming it cannot be matched by any pattern, and pretending
    otherwise is how a guard rail gets mistaken for a wall. The sandbox is the
    control for untrusted code; this is a guard against an obvious mistake.
    """

    def test_destruction_expressed_in_library_calls_is_not_caught(self) -> None:
        self.assertIsNone(denied("python", "-c", 'import shutil; shutil.rmtree("/")'))

    def test_the_approval_gate_still_sees_it(self) -> None:
        """Which is why suggest and auto-edit remain the real protection."""
        import tempfile

        from harness.execution.workspace import Workspace
        from harness.repl.approvals import Gate, Mode
        from harness.repl.tools import ReplToolset

        config = load_config(ROOT / "configs" / "ay.yaml")
        toolset = ReplToolset(Workspace(Path(tempfile.mkdtemp()), ()), config)
        spec = {s.name: s for s in toolset.specs()}["run_command"]
        gate = Gate(config.policy, mode=Mode.SUGGEST)
        decision = gate.check(spec, {"command": ["python", "-c", "import shutil"]})
        # With no operator attached the gate refuses rather than assuming yes.
        self.assertFalse(decision.allowed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
