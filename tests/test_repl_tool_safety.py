"""The two tool inputs that used to end a session or a file.

Both were found by review rather than by use, and both are the same shape of
bug: an input the tool accepted and should not have. `grep` accepted a pattern
that never returns, and `edit_file` accepted an empty `old_string` and reported
success on a file it had scrambled.

The limits of the regex screen are asserted here too, in `KnownLimitTests`.
A guard whose boundary is written down can be reasoned about; one whose
boundary is discovered in use cannot.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from harness.config import load_config
from harness.execution.workspace import Workspace
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]

#: Long enough that a catastrophic pattern would never finish on it.
HOSTILE_LINE = "ab" * 60 + "X"


class ToolSafetyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "a.txt").write_text(HOSTILE_LINE, encoding="utf-8")
        (self.root / "code.py").write_text(
            "def hello():\n    return 1\n\n\nclass Boom(Exception):\n    pass\n", encoding="utf-8"
        )
        self.tools = ReplToolset(
            Workspace(self.root, ()), load_config(ROOT / "configs" / "ay.yaml")
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()


class BacktrackingTests(ToolSafetyTestCase):
    """A pattern that cannot finish must not be started."""

    CATASTROPHIC = (
        r"(a+)+$",
        r"(a*)*b",
        r"([a-z]+)*$",
        r"((a)*)*$",
        r"(a|a)*$",
        r"(\w+\s?)*$",
    )

    def test_none_of_them_run(self) -> None:
        for pattern in self.CATASTROPHIC:
            outcome = self.tools.dispatch("grep", {"pattern": pattern})
            self.assertFalse(outcome.ok, pattern)

    def test_each_is_refused_promptly(self) -> None:
        """The screen's own cost is bounded; that is why the probe is short."""
        for pattern in self.CATASTROPHIC:
            started = time.perf_counter()
            self.tools.dispatch("grep", {"pattern": pattern})
            self.assertLess(time.perf_counter() - started, 5.0, pattern)

    def test_the_refusal_explains_itself(self) -> None:
        """Two screens run, so accept either explanation."""
        for pattern, expected in ((r"(a+)+$", "quantifier"), (r"(a|a)*$", "backtrack")):
            outcome = self.tools.dispatch("grep", {"pattern": pattern})
            self.assertIn(expected, outcome.content.lower(), pattern)

    def test_a_session_survives_one(self) -> None:
        """The point of the whole guard: the next call still works."""
        self.tools.dispatch("grep", {"pattern": r"(a+)+$"})
        outcome = self.tools.dispatch("grep", {"pattern": "def hello"})
        self.assertTrue(outcome.ok)
        self.assertIn("code.py", outcome.content)


class HonestPatternTests(ToolSafetyTestCase):
    """A screen that blocks real searches would just get removed."""

    ORDINARY = (
        r"def \w+",
        r"class \w+\(",
        r"^def .*:$",
        r"\bself\.\w+",
        r"\d{4}-\d{2}",
        r"(GET|POST|PUT)\s",
        r"(foo|bar)+",
        r"[a-z]+",
        "TODO|FIXME",
        "return",
        "a+",
    )

    def test_they_all_still_run(self) -> None:
        for pattern in self.ORDINARY:
            outcome = self.tools.dispatch("grep", {"pattern": pattern})
            self.assertTrue(outcome.ok, f"{pattern}: {outcome.content[:80]}")

    def test_the_screen_costs_almost_nothing(self) -> None:
        started = time.perf_counter()
        for pattern in self.ORDINARY:
            self.tools.dispatch("grep", {"pattern": pattern})
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_results_are_unchanged_by_the_screen(self) -> None:
        outcome = self.tools.dispatch("grep", {"pattern": r"def \w+"})
        self.assertIn("code.py:1", outcome.content)

    def test_an_invalid_pattern_still_reports_clearly(self) -> None:
        outcome = self.tools.dispatch("grep", {"pattern": "([unclosed"})
        self.assertFalse(outcome.ok)
        self.assertIn("regular expression", outcome.content)


class EmptyOldStringTests(ToolSafetyTestCase):
    """`"".count()` is one per character, so this used to interleave."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.root / "m.py"
        self.target.write_text("x = 1\n", encoding="utf-8")

    def test_an_empty_old_string_is_refused(self) -> None:
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "", "new_string": "Z"}
        )
        self.assertFalse(outcome.ok)

    def test_it_is_refused_with_replace_all_too(self) -> None:
        """The combination that actually destroyed the file."""
        outcome = self.tools.dispatch(
            "edit_file",
            {"path": "m.py", "old_string": "", "new_string": "Z", "replace_all": True},
        )
        self.assertFalse(outcome.ok)

    def test_the_file_is_untouched(self) -> None:
        self.tools.dispatch(
            "edit_file",
            {"path": "m.py", "old_string": "", "new_string": "Z", "replace_all": True},
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "x = 1\n")

    def test_the_message_points_at_the_right_tool(self) -> None:
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "", "new_string": "Z"}
        )
        self.assertIn("write_file", outcome.content)

    def test_deleting_text_still_works(self) -> None:
        """An empty new_string is legitimate; only old_string is refused."""
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "x = 1\n", "new_string": ""}
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "")


class KnownLimitTests(ToolSafetyTestCase):
    """Written down rather than left to be discovered.

    A match cannot be interrupted once it starts, so the screen has to decide
    in advance, and it decides by timing a short probe. A pattern whose blow-up
    only shows on longer input is therefore not detected, and lengthening the
    probe is not available: the probe would be the thing that hangs.

    The complete fix is matching in a killable subprocess. It is not done
    because the pattern comes from the model rather than an attacker, and the
    shapes a model actually writes are covered above.
    """

    def test_the_probe_is_short_enough_that_it_cannot_itself_hang(self) -> None:
        r"""The invariant that creates the limit, asserted directly.

        Asserting instead that some specific pattern slips through would be a
        test of this machine's clock speed: `(\d|\d\d)*$` sits close enough to
        the threshold to fall either side of it under load. The stable fact is
        that the probe is bounded, and everything the bound implies follows.
        """
        from harness.repl.tools import _PROBE_BUDGET_SECONDS, _PROBE_LENGTHS

        self.assertLessEqual(max(_PROBE_LENGTHS), 24)
        self.assertLessEqual(_PROBE_BUDGET_SECONDS, 0.05)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
