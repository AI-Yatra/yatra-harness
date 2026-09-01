"""Evals: does the harness still do the job, not does the code still compile.

The unit suite proves each part behaves. It cannot answer the question that
actually matters when a prompt, a model or a budget changes: does a run still
finish the task. That needs a set of cases, a recorded outcome for each, and
a threshold that fails CI when the number moves the wrong way.

A case that is expected to fail is as important as one expected to pass. A
suite where everything succeeds cannot tell a working harness from one whose
verifier has stopped verifying.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.contracts import RunStatus
from harness.errors import ConfigurationError
from harness.evals import EvalCase, EvalSuite, load_suite, run_suite

ROOT = Path(__file__).resolve().parents[1]


class FakeRun:
    def __init__(self, status: RunStatus, reason: str = "") -> None:
        self.status = status
        self.terminal_reason = reason
        self.run_id = f"run-{id(self)}"
        self.run_dir = Path("/nonexistent")
        self.workspace = Path("/nonexistent")
        self.summary_path = Path("/nonexistent")


class SuiteLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-evals-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def write(self, body: str) -> Path:
        path = self.base / "suite.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_suite_loads_its_cases(self) -> None:
        suite = load_suite(self.write(
            "version: 1\nid: demo\n"
            "defaults:\n"
            f"  config: {ROOT / 'configs' / 'teaching.yaml'}\n"
            f"  skill: {ROOT / 'skills' / 'bugfix.yaml'}\n"
            "cases:\n"
            f"  - id: repair\n    task: {ROOT / 'tasks' / 'repair_counter.yaml'}\n"
        ))
        self.assertEqual(suite.suite_id, "demo")
        self.assertEqual(len(suite.cases), 1)
        self.assertEqual(suite.cases[0].expect, RunStatus.COMPLETED)

    def test_defaults_are_applied_to_every_case(self) -> None:
        suite = load_suite(self.write(
            "version: 1\nid: demo\n"
            "defaults:\n"
            f"  config: {ROOT / 'configs' / 'teaching.yaml'}\n"
            f"  skill: {ROOT / 'skills' / 'bugfix.yaml'}\n"
            "cases:\n"
            f"  - id: a\n    task: {ROOT / 'tasks' / 'repair_counter.yaml'}\n"
            f"  - id: b\n    task: {ROOT / 'tasks' / 'repair_counter_fail.yaml'}\n"
            "    expect: FAILED\n"
        ))
        self.assertEqual(suite.cases[0].skill.name, "bugfix.yaml")
        self.assertEqual(suite.cases[1].expect, RunStatus.FAILED)

    def test_a_case_may_override_a_default(self) -> None:
        suite = load_suite(self.write(
            "version: 1\nid: demo\n"
            "defaults:\n"
            f"  config: {ROOT / 'configs' / 'teaching.yaml'}\n"
            f"  skill: {ROOT / 'skills' / 'bugfix.yaml'}\n"
            "cases:\n"
            f"  - id: a\n    task: {ROOT / 'tasks' / 'repair_counter.yaml'}\n"
            f"    skill: {ROOT / 'skills' / 'repo-edit.yaml'}\n"
        ))
        self.assertEqual(suite.cases[0].skill.name, "repo-edit.yaml")

    def test_a_missing_task_is_refused_at_load(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_suite(self.write(
                "version: 1\nid: demo\n"
                "defaults:\n"
                f"  config: {ROOT / 'configs' / 'teaching.yaml'}\n"
                f"  skill: {ROOT / 'skills' / 'bugfix.yaml'}\n"
                "cases:\n  - id: a\n    task: nope.yaml\n"
            ))

    def test_a_suite_with_no_cases_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_suite(self.write("version: 1\nid: demo\ncases: []\n"))

    def test_an_unknown_expected_status_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_suite(self.write(
                "version: 1\nid: demo\n"
                "defaults:\n"
                f"  config: {ROOT / 'configs' / 'teaching.yaml'}\n"
                f"  skill: {ROOT / 'skills' / 'bugfix.yaml'}\n"
                "cases:\n"
                f"  - id: a\n    task: {ROOT / 'tasks' / 'repair_counter.yaml'}\n"
                "    expect: MOSTLY_FINE\n"
            ))


class SuiteRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-evalrun-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def suite(self, *expectations: RunStatus, min_pass_rate: float = 1.0) -> EvalSuite:
        cases = tuple(
            EvalCase(
                case_id=f"case-{index}",
                task=ROOT / "tasks" / "repair_counter.yaml",
                config=ROOT / "configs" / "teaching.yaml",
                skill=ROOT / "skills" / "bugfix.yaml",
                expect=expectation,
            )
            for index, expectation in enumerate(expectations)
        )
        return EvalSuite("demo", cases, min_pass_rate=min_pass_rate)

    def runner(self, *results):
        remaining = list(results)

        def run(case: EvalCase):
            return remaining.pop(0)

        return run

    def test_a_case_matching_its_expectation_passes(self) -> None:
        report = run_suite(
            self.suite(RunStatus.COMPLETED),
            runner=self.runner(FakeRun(RunStatus.COMPLETED)),
            report_dir=self.base,
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.pass_rate, 1.0)

    def test_a_case_that_should_fail_and_does_counts_as_a_pass(self) -> None:
        # A suite where everything succeeds cannot tell a working harness
        # from one whose verifier has stopped verifying.
        report = run_suite(
            self.suite(RunStatus.FAILED),
            runner=self.runner(FakeRun(RunStatus.FAILED)),
            report_dir=self.base,
        )
        self.assertTrue(report.passed)

    def test_an_unexpected_success_is_a_failure(self) -> None:
        report = run_suite(
            self.suite(RunStatus.FAILED),
            runner=self.runner(FakeRun(RunStatus.COMPLETED)),
            report_dir=self.base,
        )
        self.assertFalse(report.passed)
        self.assertIn("COMPLETED", report.results[0].detail)

    def test_the_pass_rate_is_measured_across_cases(self) -> None:
        report = run_suite(
            self.suite(RunStatus.COMPLETED, RunStatus.COMPLETED),
            runner=self.runner(FakeRun(RunStatus.COMPLETED), FakeRun(RunStatus.FAILED)),
            report_dir=self.base,
        )
        self.assertEqual(report.pass_rate, 0.5)

    def test_a_threshold_below_the_rate_still_passes(self) -> None:
        report = run_suite(
            self.suite(RunStatus.COMPLETED, RunStatus.COMPLETED, min_pass_rate=0.5),
            runner=self.runner(FakeRun(RunStatus.COMPLETED), FakeRun(RunStatus.FAILED)),
            report_dir=self.base,
        )
        self.assertTrue(report.passed)

    def test_one_case_crashing_does_not_end_the_suite(self) -> None:
        # A suite that stops at the first exception reports one number when
        # the operator asked for all of them.
        def run(case: EvalCase):
            if case.case_id == "case-0":
                raise RuntimeError("provider exploded")
            return FakeRun(RunStatus.COMPLETED)

        report = run_suite(
            self.suite(RunStatus.COMPLETED, RunStatus.COMPLETED, min_pass_rate=0.5),
            runner=run,
            report_dir=self.base,
        )
        self.assertEqual(len(report.results), 2)
        self.assertIn("provider exploded", report.results[0].detail)

    def test_a_report_is_written(self) -> None:
        report = run_suite(
            self.suite(RunStatus.COMPLETED),
            runner=self.runner(FakeRun(RunStatus.COMPLETED)),
            report_dir=self.base,
        )
        value = json.loads(Path(report.report_path).read_text(encoding="utf-8"))
        self.assertEqual(value["suite_id"], "demo")
        self.assertEqual(value["pass_rate"], 1.0)
        self.assertEqual(len(value["results"]), 1)

    def test_each_result_records_how_long_it_took(self) -> None:
        report = run_suite(
            self.suite(RunStatus.COMPLETED),
            runner=self.runner(FakeRun(RunStatus.COMPLETED)),
            report_dir=self.base,
        )
        self.assertGreaterEqual(report.results[0].duration_ms, 0)


class ShippedSuiteTests(unittest.TestCase):
    def test_the_shipped_suite_loads(self) -> None:
        suite = load_suite(ROOT / "evals" / "teaching.yaml")
        self.assertGreaterEqual(len(suite.cases), 2)

    def test_the_shipped_suite_expects_a_failure_too(self) -> None:
        suite = load_suite(ROOT / "evals" / "teaching.yaml")
        self.assertIn(RunStatus.FAILED, [case.expect for case in suite.cases])


if __name__ == "__main__":
    unittest.main()
