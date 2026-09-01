"""Defects found reviewing this branch before it was pushed.

Each of these is a case where two parts of the harness disagreed, or where a
failure path lost information it had already earned. They are grouped here
because that is what they have in common, not because they share a module.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.backlog import Feature, save_backlog
from harness.config import PolicyConfig
from harness.contracts import RiskLevel, ToolSpec
from harness.errors import WorkspaceError
from harness.loop import LoopRequest, run_loop
from harness.policy import PolicyEngine
from harness.runtime import subagent_approval

ROOT = Path(__file__).resolve().parents[1]


def policy(**kwargs) -> PolicyConfig:
    defaults = {
        "approval_mode": "mutations",
        "allowed_commands": (("python", "-m", "unittest"),),
        "denied_commands": (),
        "network_enabled": True,
        "allowed_domains": ("example.invalid",),
        "command_timeout_seconds": 30.0,
        "browser_timeout_seconds": 10.0,
    }
    return PolicyConfig(**{**defaults, **kwargs})


def spec(name: str, risk: RiskLevel) -> ToolSpec:
    return ToolSpec(name, "", {"type": "object"}, risk)


class SubagentApprovalTests(unittest.TestCase):
    """A delegated reviewer could not run the checks its own skill told it to.

    Sub-agents were given no approver at all, on the reasoning that a
    read-only agent needs no authorisation. That is true of reading and false
    of executing: under `approval_mode: mutations` -- what every remote config
    ships -- run_command is approval-gated, so the reviewer was silently
    refused the test run it had just been instructed to perform.
    """

    def engine(self, **kwargs) -> PolicyEngine:
        return PolicyEngine(
            policy(**kwargs),
            ("read_file", "run_command", "apply_patch", "browser_fetch"),
            subagent_approval(),
        )

    def test_a_sub_agent_may_read(self) -> None:
        self.assertTrue(self.engine().evaluate(spec("read_file", RiskLevel.READ), {}).allowed)

    def test_a_sub_agent_may_run_an_allowlisted_command(self) -> None:
        decision = self.engine().evaluate(
            spec("run_command", RiskLevel.EXECUTE), {"command": ["python", "-m", "unittest"]}
        )
        self.assertTrue(decision.allowed, decision.reason)

    def test_a_sub_agent_may_still_not_write(self) -> None:
        # The read-only guarantee is the invariant; the approver must not
        # become the hole in it.
        decision = self.engine().evaluate(spec("apply_patch", RiskLevel.WRITE), {})
        self.assertFalse(decision.allowed)

    def test_a_sub_agent_may_still_not_reach_the_network(self) -> None:
        decision = self.engine().evaluate(spec("browser_fetch", RiskLevel.NETWORK), {})
        self.assertFalse(decision.allowed)

    def test_the_command_allowlist_still_applies(self) -> None:
        decision = self.engine().evaluate(
            spec("run_command", RiskLevel.EXECUTE), {"command": ["rm", "-rf", "/"]}
        )
        self.assertFalse(decision.allowed)


class RunIdContainmentTests(unittest.TestCase):
    """A run id reaching the filesystem is resolved, and refused if it escapes.

    `resume` and the workspace manager both guard this. The commands added on
    this branch did not, and one of them pushes to a remote.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-containment-")
        self.addCleanup(self.temporary.cleanup)
        self.runs = Path(self.temporary.name) / "runs"
        (self.runs / "good").mkdir(parents=True)

    def resolve(self, run_id: str) -> Path:
        from harness.cli import run_directory

        return run_directory(self.runs, run_id)

    def test_an_ordinary_run_id_resolves_inside(self) -> None:
        self.assertEqual(self.resolve("good"), (self.runs / "good").resolve())

    def test_an_escaping_run_id_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.resolve("../../etc")

    def test_an_absolute_run_id_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.resolve("/etc")

    def test_an_empty_run_id_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.resolve("")


class LoopRecordTests(unittest.TestCase):
    """The loop's record survives the loop failing.

    load_backlog and mark_feature sat outside the try, so a backlog that
    became unreadable partway through raised out of run_loop -- discarding
    the record of every feature it had already completed.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-looprec-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.backlog = self.base / "feature_list.json"
        save_backlog(
            self.backlog,
            [
                Feature(feature_id="a", description="first", acceptance=("c",)),
                Feature(feature_id="b", description="second", acceptance=("c",)),
            ],
        )

    def request(self) -> LoopRequest:
        return LoopRequest(
            backlog=self.backlog,
            config_path=ROOT / "configs" / "teaching.yaml",
            skill_path=ROOT / "skills" / "repo-edit.yaml",
            runs_dir=self.base / "runs",
            seed=self.base,
            max_features=10,
        )

    def test_an_unreadable_backlog_stops_the_loop_without_raising(self) -> None:
        class Achieved:
            achieved, reason, last_run_id = True, "passed", "run-1"

        state = {"calls": 0}

        def pursue(feature, request):
            state["calls"] += 1
            if state["calls"] == 1:
                return Achieved()
            self.backlog.write_text("{ not json", encoding="utf-8")
            return Achieved()

        result = run_loop(self.request(), pursue=pursue)
        self.assertFalse(result.completed)
        self.assertIn("backlog", result.reason.lower())

    def test_work_already_done_is_still_recorded(self) -> None:
        class Achieved:
            achieved, reason, last_run_id = True, "passed", "run-1"

        state = {"calls": 0}

        def pursue(feature, request):
            state["calls"] += 1
            if state["calls"] == 2:
                self.backlog.write_text("{ not json", encoding="utf-8")
            return Achieved()

        result = run_loop(self.request(), pursue=pursue)
        record = json.loads(Path(result.record_path).read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(record["outcomes"]), 1)
        self.assertEqual(record["outcomes"][0]["feature_id"], "a")


class IndexCacheTests(unittest.TestCase):
    """The retrieval index cache is bounded.

    Keyed by workspace path, it gained an entry per run and dropped none. A
    `harness loop` over a long backlog would hold every workspace's chunks --
    and with the embedding backend, every workspace's vectors -- for the life
    of the process.
    """

    def test_the_cache_keeps_only_the_most_recent_workspaces(self) -> None:
        from harness.tools import _INDEX_CACHE, _remember_index

        _INDEX_CACHE.clear()
        for index in range(12):
            _remember_index((f"/ws/{index}", "lexical"), (0, 0), object())
        self.assertLessEqual(len(_INDEX_CACHE), 4)

    def test_the_newest_entry_survives(self) -> None:
        from harness.tools import _INDEX_CACHE, _remember_index

        _INDEX_CACHE.clear()
        for index in range(12):
            _remember_index((f"/ws/{index}", "lexical"), (0, 0), object())
        self.assertIn(("/ws/11", "lexical"), _INDEX_CACHE)


if __name__ == "__main__":
    unittest.main()
