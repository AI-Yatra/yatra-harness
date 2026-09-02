"""The demo repository is broken in exactly the two ways it claims to be.

A demo that quietly stops being broken -- or that is broken differently from
what its README says -- fails in front of an audience, which is the worst
place to find out. These tests pin the starting state.

The last class is the one that earns its keep. It checks the `best_move`
specification against a reference implementation of the rule the README
states, because the first version of that spec contained a case where the
"correct" answer contradicted the rule: the board offered X a winning move
and the test demanded a block.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from harness.autonomy.backlog import load_backlog
from harness.config import load_task

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "tictactoe"


def run_tests(target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=DEMO, capture_output=True, text=True, timeout=120,
    )


class StartingStateTests(unittest.TestCase):
    def test_the_rules_suite_has_exactly_one_failure(self) -> None:
        # One failure, not two: the flaw has to be a single identifiable
        # thing an agent can find and fix.
        result = run_tests("tests.test_winner")
        self.assertIn("FAILED (failures=1)", result.stderr, result.stderr)

    def test_the_failure_is_the_anti_diagonal(self) -> None:
        self.assertIn("test_the_other_diagonal_wins", run_tests("tests.test_winner").stderr)

    def test_the_feature_is_absent(self) -> None:
        self.assertNotIn("def best_move", (DEMO / "game.py").read_text(encoding="utf-8"))

    def test_the_feature_suite_fails(self) -> None:
        self.assertIn("FAILED", run_tests("tests.test_best_move").stderr)

    def test_the_two_suites_fail_independently(self) -> None:
        # Either task can be run first. A demo where one silently depends on
        # the other cannot be shown in the order the audience asks for.
        rules = run_tests("tests.test_winner").stderr
        feature = run_tests("tests.test_best_move").stderr
        self.assertNotIn("best_move", rules)
        self.assertNotIn("test_the_other_diagonal_wins", feature)

    def test_the_repository_describes_itself(self) -> None:
        self.assertTrue((DEMO / "AGENTS.md").is_file())
        self.assertIn("Do not edit", (DEMO / "AGENTS.md").read_text(encoding="utf-8"))


class DemoContractTests(unittest.TestCase):
    def test_every_demo_task_loads(self) -> None:
        for path in sorted((ROOT / "demo" / "tasks").glob("*.yaml")):
            with self.subTest(task=path.name):
                self.assertEqual(load_task(path).workspace_seed, DEMO.resolve())

    def test_every_demo_task_protects_the_tests(self) -> None:
        # The whole point of the demo: the agent cannot make the tests pass
        # by editing the tests.
        for path in sorted((ROOT / "demo" / "tasks").glob("*.yaml")):
            with self.subTest(task=path.name):
                self.assertIn("tests/**", load_task(path).protected_paths)

    def test_every_demo_task_requires_a_real_diff(self) -> None:
        for path in sorted((ROOT / "demo" / "tasks").glob("*.yaml")):
            with self.subTest(task=path.name):
                self.assertTrue(load_task(path).acceptance.require_non_empty_diff)

    def test_the_demo_backlog_lists_both_items_as_pending(self) -> None:
        features = load_backlog(DEMO / "feature_list.json")
        self.assertEqual(len(features), 2)
        self.assertFalse(any(feature.passes for feature in features))

    def test_the_demo_config_has_no_fallback_route(self) -> None:
        # A fallback to a replay script written for another task would make a
        # failed demo look like a successful one doing unrelated work.
        from harness.config import load_config

        self.assertEqual(load_config(ROOT / "demo" / "config.yaml").router.fallbacks, ())


class SpecificationConsistencyTests(unittest.TestCase):
    """Every expectation in the feature spec matches the rule it states."""

    @staticmethod
    def reference(board: list[str], player: str) -> int:
        sys.path.insert(0, str(DEMO))
        import game  # noqa: PLC0415

        # The shipped winner() is missing the anti-diagonal. The reference has
        # to behave like a correct implementation, or it would agree with the
        # bug instead of with the rule.
        lines = [*game.winning_lines(), (2, 4, 6)]

        def won(cells: list[str], who: str) -> bool:
            return any(all(cells[index] == who for index in line) for line in lines)

        if player not in game.PLAYERS:
            raise ValueError("unknown player")
        if game.is_full(board):
            raise ValueError("board is full")
        other = "O" if player == "X" else "X"
        for who in (player, other):
            for cell in game.empty_cells(board):
                if won(game.place(board, cell, who), who):
                    return cell
        return game.empty_cells(board)[0]

    def test_no_expectation_contradicts_the_stated_rule(self) -> None:
        sys.path.insert(0, str(DEMO))
        import game  # noqa: PLC0415

        source = (DEMO / "tests" / "test_best_move.py").read_text(encoding="utf-8")
        cases = re.findall(r'best_move\(board_from\((.*?)\), "([XO])"\), (\d+)\)', source, re.S)
        self.assertGreaterEqual(len(cases), 6, "the spec should assert several concrete boards")
        for raw, player, expected in cases:
            text = "".join(re.findall(r'"([^"]*)"', raw))
            board = [game.EMPTY if cell == "." else cell for cell in text if cell in "XO."]
            with self.subTest(board=text, player=player):
                self.assertEqual(self.reference(board, player), int(expected))


class ExampleBacklogTests(unittest.TestCase):
    def test_the_shipped_example_backlog_is_pending(self) -> None:
        # It is edited by running it, so a committed copy that says "passes"
        # means somebody forgot to reset it.
        value = json.loads((ROOT / "examples" / "feature_list.json").read_text(encoding="utf-8"))
        self.assertTrue(value)
        self.assertFalse(any(feature.get("passes") for feature in value))


if __name__ == "__main__":
    unittest.main()
