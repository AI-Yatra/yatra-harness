"""Goal mode: keep attempting until the verifier passes, or stop and say why.

A run is one attempt. A goal is "keep attempting until this is true", which
needs a stopping condition that is not the model's opinion, a budget that
covers the whole pursuit rather than each try, and a way for attempt N+1 to
know why attempt N failed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.contracts import RunStatus
from harness.goal import GoalError, GoalRequest, pursue

ROOT = Path(__file__).resolve().parents[1]


class FakeRun:
    def __init__(self, status: RunStatus, reason: str = "", run_id: str = "") -> None:
        self.status = status
        self.terminal_reason = reason
        self.run_id = run_id or f"run-{id(self)}"
        self.run_dir = Path("/nonexistent")
        self.workspace = Path("/nonexistent")
        self.summary_path = Path("/nonexistent")


class GoalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-goal-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.seed = self.base / "seed"
        self.seed.mkdir()
        self.runs: list[Path] = []
        self.traces: list[str] = []
        self.clock = [0.0]

    def request(self, **kwargs) -> GoalRequest:
        defaults = {
            "objective": "Make the tests pass.",
            "acceptance": ("python -m unittest",),
            "seed": self.seed,
            "repository": None,
            "config_path": ROOT / "configs" / "teaching.yaml",
            "skill_path": ROOT / "skills" / "repo-edit.yaml",
            "runs_dir": self.base / "runs",
            "max_attempts": 3,
            "max_seconds": 600.0,
        }
        return GoalRequest(**{**defaults, **kwargs})

    def runner(self, *results):
        remaining = list(results)

        def run(task_path: Path, attempt: int, trace_context: str = ""):
            self.runs.append(task_path)
            self.traces.append(trace_context)
            self.clock[0] += 10.0
            return remaining.pop(0) if remaining else FakeRun(RunStatus.FAILED, "no more")

        return run

    def clock_fn(self) -> float:
        return self.clock[0]

    def pursue(self, *results, **kwargs):
        return pursue(
            self.request(**kwargs), runner=self.runner(*results), clock=self.clock_fn
        )


class StoppingTests(GoalTestCase):
    def test_a_goal_met_on_the_first_attempt_stops_there(self) -> None:
        result = self.pursue(FakeRun(RunStatus.COMPLETED, "acceptance criteria passed"))
        self.assertTrue(result.achieved)
        self.assertEqual(len(result.attempts), 1)

    def test_a_failed_attempt_is_retried(self) -> None:
        result = self.pursue(
            FakeRun(RunStatus.FAILED, "verification attempts exhausted"),
            FakeRun(RunStatus.COMPLETED, "acceptance criteria passed"),
        )
        self.assertTrue(result.achieved)
        self.assertEqual(len(result.attempts), 2)

    def test_the_attempt_budget_is_honoured(self) -> None:
        result = self.pursue(
            FakeRun(RunStatus.FAILED, "a"),
            FakeRun(RunStatus.FAILED, "b"),
            FakeRun(RunStatus.FAILED, "c"),
            FakeRun(RunStatus.FAILED, "d"),
        )
        self.assertFalse(result.achieved)
        self.assertEqual(len(result.attempts), 3)
        self.assertIn("3 attempt", result.reason)

    def test_the_wall_clock_budget_stops_the_pursuit(self) -> None:
        # Each fake attempt advances the clock by ten seconds.
        result = self.pursue(
            FakeRun(RunStatus.FAILED, "a"),
            FakeRun(RunStatus.FAILED, "b"),
            FakeRun(RunStatus.FAILED, "c"),
            max_attempts=99,
            max_seconds=25.0,
        )
        self.assertFalse(result.achieved)
        self.assertIn("budget", result.reason)
        self.assertLess(len(result.attempts), 99)

    def test_a_blocked_run_stops_instead_of_retrying(self) -> None:
        # The model asked a question. Asking it again unchanged wastes a turn
        # budget and cannot produce a different answer.
        result = self.pursue(
            FakeRun(RunStatus.BLOCKED, "which file did you mean?"),
            FakeRun(RunStatus.COMPLETED, "done"),
        )
        self.assertFalse(result.achieved)
        self.assertEqual(len(result.attempts), 1)
        self.assertIn("which file", result.reason)

    def test_a_budget_exhausted_run_is_retried(self) -> None:
        result = self.pursue(
            FakeRun(RunStatus.BUDGET_EXHAUSTED, "turn budget exhausted"),
            FakeRun(RunStatus.COMPLETED, "done"),
        )
        self.assertTrue(result.achieved)

    def test_at_least_one_attempt_always_runs(self) -> None:
        result = self.pursue(FakeRun(RunStatus.COMPLETED, "done"), max_seconds=0.0)
        self.assertEqual(len(result.attempts), 1)


class TraceTests(GoalTestCase):
    def test_every_attempt_joins_one_trace(self) -> None:
        # A pursuit should read as a single story rather than as N unrelated
        # runs that happen to share a directory.
        self.pursue(
            FakeRun(RunStatus.FAILED, "a"), FakeRun(RunStatus.COMPLETED, "done")
        )
        self.assertEqual(len(set(self.traces)), 1)
        self.assertTrue(self.traces[0])

    def test_two_goals_do_not_share_a_trace(self) -> None:
        self.pursue(FakeRun(RunStatus.COMPLETED, "done"))
        first = self.traces[0]
        self.traces.clear()
        self.pursue(FakeRun(RunStatus.COMPLETED, "done"))
        self.assertNotEqual(first, self.traces[0])


class CarryForwardTests(GoalTestCase):
    def task_of(self, index: int) -> dict:
        import yaml

        return yaml.safe_load(self.runs[index].read_text(encoding="utf-8"))

    def test_the_first_attempt_carries_no_failure_history(self) -> None:
        self.pursue(FakeRun(RunStatus.COMPLETED, "done"))
        self.assertNotIn("previous attempt", json.dumps(self.task_of(0)))

    def test_the_next_attempt_is_told_why_the_last_one_failed(self) -> None:
        self.pursue(
            FakeRun(RunStatus.FAILED, "an acceptance command failed"),
            FakeRun(RunStatus.COMPLETED, "done"),
        )
        constraints = json.dumps(self.task_of(1)["constraints"])
        self.assertIn("previous attempt", constraints)
        self.assertIn("an acceptance command failed", constraints)

    def test_the_objective_is_identical_across_attempts(self) -> None:
        self.pursue(
            FakeRun(RunStatus.FAILED, "a"), FakeRun(RunStatus.COMPLETED, "done")
        )
        self.assertEqual(self.task_of(0)["objective"], self.task_of(1)["objective"])

    def test_the_acceptance_command_is_the_stopping_condition(self) -> None:
        self.pursue(FakeRun(RunStatus.COMPLETED, "done"))
        task = self.task_of(0)
        self.assertEqual(task["acceptance"]["commands"], [["python", "-m", "unittest"]])
        self.assertTrue(task["acceptance"]["require_non_empty_diff"])

    def test_the_workspace_path_is_absolute_in_the_generated_task(self) -> None:
        # The attempt task is written into the goal directory, and load_task
        # resolves a relative path against the task file. A relative seed
        # would therefore be looked for inside .runs.
        self.pursue(FakeRun(RunStatus.COMPLETED, "done"))
        self.assertTrue(Path(self.task_of(0)["workspace_seed"]).is_absolute())

    def test_a_relative_seed_is_resolved_before_it_is_written(self) -> None:
        from harness.config import load_task  # noqa: PLC0415

        relative = Path(self.seed.name)
        import os  # noqa: PLC0415

        previous = os.getcwd()
        os.chdir(self.base)
        try:
            self.pursue(FakeRun(RunStatus.COMPLETED, "done"), seed=relative)
            task = load_task(self.runs[-1])
        finally:
            os.chdir(previous)
        self.assertEqual(task.workspace_seed, self.seed.resolve())

    def test_a_goal_without_an_acceptance_command_is_refused(self) -> None:
        # "Loop until you feel done" is exactly the thing a goal must not be.
        with self.assertRaises(GoalError) as caught:
            self.pursue(FakeRun(RunStatus.COMPLETED, "done"), acceptance=())
        self.assertIn("acceptance", str(caught.exception))


class RecordTests(GoalTestCase):
    def test_the_pursuit_is_recorded_next_to_the_runs(self) -> None:
        result = self.pursue(
            FakeRun(RunStatus.FAILED, "a", run_id="r1"),
            FakeRun(RunStatus.COMPLETED, "done", run_id="r2"),
        )
        record = json.loads(Path(result.record_path).read_text(encoding="utf-8"))
        self.assertTrue(record["achieved"])
        self.assertEqual([a["run_id"] for a in record["attempts"]], ["r1", "r2"])
        self.assertEqual(record["objective"], "Make the tests pass.")

    def test_every_attempt_records_its_status(self) -> None:
        result = self.pursue(
            FakeRun(RunStatus.FAILED, "a"), FakeRun(RunStatus.COMPLETED, "done")
        )
        self.assertEqual(
            [attempt.status for attempt in result.attempts],
            [RunStatus.FAILED, RunStatus.COMPLETED],
        )


if __name__ == "__main__":
    unittest.main()
