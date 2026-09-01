"""The self-feeding loop: work the backlog until it is done or stuck.

Goal mode still needs a person to say what the goal is. This is the rung
above: the loop reads the backlog, picks the next unfinished feature, pursues
it, records the outcome against evidence, and goes round again.

The properties worth pinning are about stopping. A loop that never stops is
not autonomous, it is unattended -- and the difference is whether it can tell
the operator why it stopped.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.backlog import Feature, load_backlog, save_backlog
from harness.loop import LoopRequest, run_loop

ROOT = Path(__file__).resolve().parents[1]


class FakeGoal:
    def __init__(self, achieved: bool, reason: str = "", run_id: str = "r1") -> None:
        self.achieved = achieved
        self.reason = reason or ("acceptance criteria passed" if achieved else "not achieved")
        self.last_run_id = run_id
        self.attempts = ()
        self.record_path = Path("/nonexistent")


class LoopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-loop-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.seed = self.base / "seed"
        self.seed.mkdir()
        self.backlog = self.base / "feature_list.json"
        self.pursued: list[str] = []

    def features(self, count: int = 2) -> None:
        save_backlog(
            self.backlog,
            [
                Feature(feature_id=f"f{index}", description=f"feature {index}",
                        acceptance=("python -m unittest",))
                for index in range(count)
            ],
        )

    def request(self, **kwargs) -> LoopRequest:
        defaults = {
            "backlog": self.backlog,
            "config_path": ROOT / "configs" / "teaching.yaml",
            "skill_path": ROOT / "skills" / "repo-edit.yaml",
            "runs_dir": self.base / "runs",
            "seed": self.seed,
            "repository": None,
            "max_features": 10,
            "max_attempts": 2,
        }
        return LoopRequest(**{**defaults, **kwargs})

    def pursuer(self, *results):
        remaining = list(results)

        def pursue(feature, request):
            self.pursued.append(feature.feature_id)
            return remaining.pop(0) if remaining else FakeGoal(False)

        return pursue

    def loop(self, *results, **kwargs):
        # Not called `run`: that name is TestCase.run, and overriding it makes
        # unittest call this instead of running the test.
        return run_loop(self.request(**kwargs), pursue=self.pursuer(*results))

    def state(self) -> dict[str, Feature]:
        return {f.feature_id: f for f in load_backlog(self.backlog)}


class ProgressTests(LoopTestCase):
    def test_the_loop_works_features_in_order(self) -> None:
        self.features(2)
        self.loop(FakeGoal(True), FakeGoal(True))
        self.assertEqual(self.pursued, ["f0", "f1"])

    def test_a_completed_feature_is_marked_with_its_run(self) -> None:
        self.features(1)
        self.loop(FakeGoal(True, run_id="run-abc"))
        self.assertTrue(self.state()["f0"].passes)
        self.assertIn("run-abc", self.state()["f0"].evidence)

    def test_a_finished_backlog_stops_immediately(self) -> None:
        self.features(1)
        save_backlog(self.backlog, [f for f in load_backlog(self.backlog)][:0] or [
            Feature(feature_id="f0", description="d", acceptance=("c",), passes=True)
        ])
        result = self.loop(FakeGoal(True))
        self.assertEqual(self.pursued, [])
        self.assertTrue(result.completed)
        self.assertIn("nothing left", result.reason)

    def test_the_loop_reports_what_it_finished(self) -> None:
        self.features(2)
        result = self.loop(FakeGoal(True), FakeGoal(True))
        self.assertEqual([outcome.feature_id for outcome in result.outcomes], ["f0", "f1"])
        self.assertTrue(all(outcome.achieved for outcome in result.outcomes))


class StoppingTests(LoopTestCase):
    def test_a_failed_feature_is_recorded_and_skipped(self) -> None:
        # Not retried immediately: the loop moves on so one hard feature does
        # not consume the whole budget, and the failure is written down so a
        # later run knows what happened.
        self.features(2)
        self.loop(FakeGoal(False, "tests still red"), FakeGoal(True))
        self.assertEqual(self.pursued, ["f0", "f1"])
        self.assertFalse(self.state()["f0"].passes)
        self.assertIn("tests still red", self.state()["f0"].evidence)

    def test_the_loop_stops_when_every_remaining_feature_has_failed(self) -> None:
        self.features(2)
        result = self.loop(FakeGoal(False), FakeGoal(False))
        self.assertFalse(result.completed)
        self.assertIn("stuck", result.reason)

    def test_the_feature_budget_is_honoured(self) -> None:
        self.features(5)
        result = self.loop(*[FakeGoal(True)] * 5, max_features=2)
        self.assertEqual(len(self.pursued), 2)
        self.assertIn("2", result.reason)

    def test_an_empty_backlog_is_not_a_crash(self) -> None:
        save_backlog(self.backlog, [])
        result = self.loop(FakeGoal(True))
        self.assertTrue(result.completed)

    def test_a_pursuit_that_raises_stops_the_loop_with_the_reason(self) -> None:
        self.features(1)

        def exploding(feature, request):
            raise RuntimeError("provider gone")

        result = run_loop(self.request(), pursue=exploding)
        self.assertFalse(result.completed)
        self.assertIn("provider gone", result.reason)


class RecordTests(LoopTestCase):
    def test_the_loop_writes_a_record(self) -> None:
        self.features(2)
        result = self.loop(FakeGoal(True), FakeGoal(False, "nope"))
        value = json.loads(Path(result.record_path).read_text(encoding="utf-8"))
        self.assertEqual(len(value["outcomes"]), 2)
        self.assertEqual(value["outcomes"][0]["feature_id"], "f0")
        self.assertFalse(value["outcomes"][1]["achieved"])


class GoalShapeTests(LoopTestCase):
    def test_a_feature_becomes_a_goal_with_its_own_acceptance(self) -> None:
        from harness.loop import goal_for

        feature = Feature(
            feature_id="f0", description="Answers cite their source.",
            acceptance=("python -m unittest", "ruff check ."), protect=("tests/**",),
        )
        goal = goal_for(feature, self.request())
        self.assertIn("Answers cite their source.", goal.objective)
        self.assertEqual(goal.acceptance, ("python -m unittest", "ruff check ."))
        self.assertEqual(goal.protect, ("tests/**",))

    def test_the_goal_inherits_the_loop_workspace(self) -> None:
        from harness.loop import goal_for

        goal = goal_for(
            Feature(feature_id="f0", description="d", acceptance=("c",)), self.request()
        )
        self.assertEqual(goal.seed, self.seed)


if __name__ == "__main__":
    unittest.main()
