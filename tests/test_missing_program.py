"""A name that is not a program is information, not a crash.

From a real session on Windows. An agent finished its work, tried to tidy up
after itself with `del`, and got back:

    unexpected tool failure: FileNotFoundError: [WinError 2] The system cannot
    find the file specified

It named no program, called routine a thing that happens whenever a model
guesses a name, and read like the harness had broken rather than like the
command had. The agent retried with absolute paths, got the same, and gave up.
Five scratch files it had created were left in the operator's repository.

`del` is not a file. Windows runs it inside cmd, and this harness executes
commands directly with no shell, so there is nothing to interpret it. That is
worth saying plainly, because a model on Windows reaches for `del`, `copy` and
`move` constantly.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from harness.execution.process import NOT_FOUND, run_process

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str):
    return run_process(command, cwd=ROOT, timeout=30, max_output_chars=4000)


class MissingProgramTests(unittest.TestCase):
    def test_an_unknown_program_does_not_raise(self) -> None:
        """It used to reach the loop's catch-all as an unexpected failure."""
        run("nosuchprogram-a1b2c3")

    def test_an_unknown_program_exits_with_the_conventional_code(self) -> None:
        self.assertEqual(run("nosuchprogram-a1b2c3").returncode, NOT_FOUND)

    def test_an_unknown_program_is_named(self) -> None:
        self.assertIn("nosuchprogram-a1b2c3", run("nosuchprogram-a1b2c3").output)

    def test_an_unknown_program_says_what_is_wrong(self) -> None:
        self.assertIn("not a program", run("nosuchprogram-a1b2c3").output)

    def test_nothing_is_reported_as_timed_out(self) -> None:
        self.assertFalse(run("nosuchprogram-a1b2c3").timed_out)

    def test_the_command_is_carried_back(self) -> None:
        self.assertEqual(run("nosuchprogram-a1b2c3", "-x").command, ("nosuchprogram-a1b2c3", "-x"))

    def test_an_empty_command_does_not_crash_the_message(self) -> None:
        """`normalized[0]` on an empty tuple would raise inside the handler."""
        result = run_process((), cwd=ROOT, timeout=30, max_output_chars=4000)
        self.assertEqual(result.returncode, NOT_FOUND)


@unittest.skipUnless(os.name == "nt", "cmd builtins are a Windows problem")
class CmdBuiltinTests(unittest.TestCase):
    def test_a_builtin_explains_that_there_is_no_shell(self) -> None:
        self.assertIn("no shell", run("del", "x.txt").output)

    def test_a_builtin_offers_the_way_to_run_it(self) -> None:
        self.assertIn("cmd /c del", run("del", "x.txt").output)

    def test_the_advice_is_not_given_for_an_ordinary_name(self) -> None:
        self.assertNotIn("cmd /c", run("nosuchprogram-a1b2c3").output)


class RealProgramTests(unittest.TestCase):
    """The ordinary path is untouched."""

    def test_a_real_program_still_runs(self) -> None:
        import sys

        result = run_process(
            (sys.executable, "-c", "print('ran')"),
            cwd=ROOT, timeout=60, max_output_chars=4000,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("ran", result.output)

    def test_a_real_program_that_fails_keeps_its_own_exit_code(self) -> None:
        import sys

        result = run_process(
            (sys.executable, "-c", "raise SystemExit(3)"),
            cwd=ROOT, timeout=60, max_output_chars=4000,
        )
        self.assertEqual(result.returncode, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
