"""A backlog the harness can work through on its own.

`harness goal` needs someone to say what the goal is. A loop that discovers
its own next piece of work needs that list to live somewhere durable, with
each item carrying how it will be checked -- otherwise "done" is decided by
whoever last read the diff.

feature_list.json is that list. The rule that makes it worth having is that a
feature is only marked complete against recorded evidence: a run id and the
commands that passed. A backlog where an agent can tick its own box is a
to-do list, not a gate.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from harness.autonomy.backlog import (
    Feature,
    load_backlog,
    mark_feature,
    next_unfinished,
    save_backlog,
)
from harness.core.errors import ConfigurationError


class LoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-backlog-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "feature_list.json"

    def write(self, value) -> Path:
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return self.path

    def feature(self, **kwargs) -> dict:
        base = {
            "id": "qna-001",
            "description": "Answers cite their source document.",
            "acceptance": ["python -m unittest discover -s tests"],
            "passes": False,
        }
        return {**base, **kwargs}

    def test_features_load_in_file_order(self) -> None:
        features = load_backlog(self.write([self.feature(id="a"), self.feature(id="b")]))
        self.assertEqual([feature.feature_id for feature in features], ["a", "b"])

    def test_acceptance_commands_are_carried(self) -> None:
        features = load_backlog(self.write([self.feature()]))
        self.assertEqual(features[0].acceptance, ("python -m unittest discover -s tests",))

    def test_a_feature_defaults_to_not_passing(self) -> None:
        value = self.feature()
        del value["passes"]
        self.assertFalse(load_backlog(self.write([value]))[0].passes)

    def test_a_feature_without_an_acceptance_command_is_refused(self) -> None:
        # A backlog item with no way to check it cannot be worked
        # autonomously; the loop would have nothing to stop on.
        value = self.feature()
        del value["acceptance"]
        with self.assertRaises(ConfigurationError) as caught:
            load_backlog(self.write([value]))
        self.assertIn("acceptance", str(caught.exception))

    def test_duplicate_ids_are_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_backlog(self.write([self.feature(id="a"), self.feature(id="a")]))

    def test_a_non_list_backlog_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_backlog(self.write({"features": []}))

    def test_unreadable_json_is_named(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_backlog(self.path)


class SelectionTests(unittest.TestCase):
    def features(self, *flags: bool) -> list[Feature]:
        return [
            Feature(feature_id=f"f{index}", description="d", acceptance=("c",), passes=flag)
            for index, flag in enumerate(flags)
        ]

    def test_the_first_unfinished_feature_is_chosen(self) -> None:
        chosen = next_unfinished(self.features(True, False, False))
        self.assertEqual(chosen.feature_id, "f1")

    def test_a_finished_backlog_yields_nothing(self) -> None:
        self.assertIsNone(next_unfinished(self.features(True, True)))

    def test_an_empty_backlog_yields_nothing(self) -> None:
        self.assertIsNone(next_unfinished([]))

    def test_named_features_are_skipped(self) -> None:
        # Used by the loop to avoid re-picking something it just failed.
        chosen = next_unfinished(self.features(False, False), skip={"f0"})
        self.assertEqual(chosen.feature_id, "f1")


class MarkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-mark-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "feature_list.json"
        save_backlog(
            self.path,
            [
                Feature(feature_id="a", description="first", acceptance=("c",)),
                Feature(feature_id="b", description="second", acceptance=("c",)),
            ],
        )

    def test_marking_records_the_pass_and_its_evidence(self) -> None:
        mark_feature(self.path, "a", passes=True, evidence="run-123")
        features = {f.feature_id: f for f in load_backlog(self.path)}
        self.assertTrue(features["a"].passes)
        self.assertIn("run-123", features["a"].evidence)

    def test_marking_one_feature_leaves_the_others_alone(self) -> None:
        mark_feature(self.path, "a", passes=True, evidence="run-123")
        features = {f.feature_id: f for f in load_backlog(self.path)}
        self.assertFalse(features["b"].passes)

    def test_a_failure_is_recorded_rather_than_erased(self) -> None:
        # A backlog that forgets its failures sends the loop round the same
        # wall until its budget runs out.
        mark_feature(self.path, "a", passes=False, evidence="run-9 failed: tests red")
        features = {f.feature_id: f for f in load_backlog(self.path)}
        self.assertFalse(features["a"].passes)
        self.assertIn("tests red", features["a"].evidence)

    def test_marking_an_unknown_feature_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            mark_feature(self.path, "nope", passes=True, evidence="x")

    def test_the_file_stays_valid_json_a_human_can_read(self) -> None:
        mark_feature(self.path, "a", passes=True, evidence="run-123")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsInstance(value, list)
        self.assertIn("\n", self.path.read_text(encoding="utf-8"))

    def test_concurrent_marks_do_not_lose_each_other(self) -> None:
        # The loop can run features in parallel, and two workers finishing at
        # once must not leave one of the results behind.
        def mark(name: str) -> None:
            mark_feature(self.path, name, passes=True, evidence=f"run-{name}")

        threads = [threading.Thread(target=mark, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        features = {f.feature_id: f for f in load_backlog(self.path)}
        self.assertTrue(features["a"].passes)
        self.assertTrue(features["b"].passes)


if __name__ == "__main__":
    unittest.main()
