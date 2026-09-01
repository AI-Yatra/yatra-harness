"""Delegation: a bounded sub-agent that reports back.

The harness has always had an independent checker -- the verifier. What it
lacked was a way for the agent to ask a *second agent* something: explore
this area, review this change, find where this is handled.

A sub-agent here is deliberately read-only. Its deliverable is a report, not
an edit, and a report needs no verifier because it changes nothing. That
keeps the completion gate exactly where it was: one agent makes changes, one
verifier decides whether they worked, and delegation adds neither a second
writer nor a second opinion about whether the run is done.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.errors import ConfigurationError, ToolError
from harness.subagents import (
    Subagent,
    SubagentConfig,
    check_delegation_allowed,
    subagent_config_from_dict,
    subagent_task,
)

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_delegation_is_off_by_default(self) -> None:
        self.assertFalse(SubagentConfig().enabled)
        self.assertEqual(SubagentConfig().agents, {})

    def test_agents_are_loaded_by_name(self) -> None:
        config = subagent_config_from_dict(
            {"agents": {"explore": "skills/explore.yaml"}}, ROOT
        )
        self.assertIn("explore", config.agents)
        self.assertIsNone(config.agents["explore"].config)
        self.assertTrue(config.enabled)

    def test_a_relative_skill_path_is_resolved_against_the_config(self) -> None:
        config = subagent_config_from_dict({"agents": {"explore": "skills/explore.yaml"}}, ROOT)
        self.assertTrue(config.agents["explore"].skill.is_absolute())

    def test_a_missing_skill_file_is_refused_at_load(self) -> None:
        with self.assertRaises(ConfigurationError):
            subagent_config_from_dict({"agents": {"explore": "skills/nope.yaml"}}, ROOT)

    def test_a_sub_agent_may_declare_its_own_config(self) -> None:
        config = subagent_config_from_dict(
            {"agents": {"review": {"skill": "skills/review.yaml",
                                   "config": "configs/teaching.yaml"}}},
            ROOT,
        )
        self.assertEqual(config.agents["review"].config.name, "teaching.yaml")

    def test_a_missing_sub_agent_config_is_refused_at_load(self) -> None:
        with self.assertRaises(ConfigurationError):
            subagent_config_from_dict(
                {"agents": {"review": {"skill": "skills/review.yaml", "config": "configs/nope.yaml"}}},
                ROOT,
            )

    def test_no_agents_means_delegation_stays_off(self) -> None:
        self.assertFalse(subagent_config_from_dict({}, ROOT).enabled)


class GuardTests(unittest.TestCase):
    def config(self, **kwargs) -> SubagentConfig:
        defaults = {
            "agents": {"explore": Subagent(ROOT / "skills" / "explore.yaml")},
            "max_depth": 1,
            "max_calls": 2,
        }
        return SubagentConfig(**{**defaults, **kwargs})

    def test_a_known_agent_at_depth_zero_is_allowed(self) -> None:
        check_delegation_allowed(self.config(), "explore", depth=0, calls=0)

    def test_an_unknown_agent_is_refused_by_name(self) -> None:
        with self.assertRaises(ToolError) as caught:
            check_delegation_allowed(self.config(), "hacker", depth=0, calls=0)
        self.assertIn("hacker", str(caught.exception))
        self.assertIn("explore", str(caught.exception))

    def test_a_sub_agent_cannot_delegate_further_than_configured(self) -> None:
        # Without a depth cap a delegating agent can spawn a delegating agent,
        # and the budget that bounds a run stops bounding anything.
        with self.assertRaises(ToolError) as caught:
            check_delegation_allowed(self.config(), "explore", depth=1, calls=0)
        self.assertIn("depth", str(caught.exception))

    def test_the_number_of_delegations_is_capped(self) -> None:
        with self.assertRaises(ToolError) as caught:
            check_delegation_allowed(self.config(), "explore", depth=0, calls=2)
        self.assertIn("2", str(caught.exception))

    def test_delegation_disabled_refuses_everything(self) -> None:
        with self.assertRaises(ToolError):
            check_delegation_allowed(SubagentConfig(), "explore", depth=0, calls=0)


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-subagent-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()

    def task(self, objective: str = "find where clamping happens") -> dict:
        import yaml

        path = subagent_task(
            Path(self.temporary.name), "explore", objective, self.workspace, index=1
        )
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_the_sub_task_works_from_a_copy_of_the_parent_workspace(self) -> None:
        # A copy, not the workspace itself: a sub-agent that runs the test
        # suite must not be able to leave artifacts in the parent's diff.
        self.assertEqual(Path(self.task()["workspace_seed"]), self.workspace.resolve())

    def test_the_objective_is_carried_through(self) -> None:
        self.assertIn("clamping", self.task()["objective"])

    def test_a_report_is_not_required_to_change_anything(self) -> None:
        # The deliverable is findings. Requiring a diff would make an honest
        # "I looked and there is nothing here" report a failed run.
        self.assertFalse(self.task()["acceptance"]["require_non_empty_diff"])

    def test_the_sub_task_is_named_for_its_parent_and_index(self) -> None:
        self.assertIn("explore", self.task()["id"])

    def test_the_sub_agent_is_told_to_report_rather_than_edit(self) -> None:
        self.assertIn("report", " ".join(self.task()["constraints"]).lower())


class ShippedSkillTests(unittest.TestCase):
    def test_the_explore_skill_cannot_write(self) -> None:
        # A read-only sub-agent is only read-only if its skill says so.
        from harness.config import load_skill

        skill = load_skill(ROOT / "skills" / "explore.yaml")
        self.assertNotIn("apply_patch", skill.allowed_tools)
        self.assertNotIn("delegate", skill.allowed_tools)

    def test_the_review_skill_cannot_write_either(self) -> None:
        from harness.config import load_skill

        skill = load_skill(ROOT / "skills" / "review.yaml")
        self.assertNotIn("apply_patch", skill.allowed_tools)


class DelegationRunTests(unittest.TestCase):
    """A whole delegation, deterministically, with no model call.

    Both sides are replay scripts, so the delegation path is exercised on
    every machine and in CI rather than only when someone has a key.
    """

    def setUp(self) -> None:
        import os
        import shutil

        self.runs = Path(tempfile.mkdtemp(prefix="harness-delegation-"))
        self.addCleanup(shutil.rmtree, self.runs, True)
        self.environment = {**os.environ, "HARNESS_RUNS_DIR": str(self.runs)}

    def run_harness(self):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-m", "harness", "run", "tasks/repair_counter.yaml",
             "--config", "configs/delegation.yaml",
             "--skill", "skills/bugfix-delegating.yaml"],
            cwd=ROOT, capture_output=True, text=True, timeout=180, env=self.environment,
        )

    def bundles(self) -> list[Path]:
        return sorted(self.runs.iterdir())

    def test_a_delegating_run_completes(self) -> None:
        result = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status: COMPLETED", result.stdout)

    def test_the_sub_agent_gets_its_own_run_bundle(self) -> None:
        # Reusing the ordinary run machinery is the point: a sub-agent is as
        # inspectable and as replayable as its parent.
        self.run_harness()
        names = [path.name for path in self.bundles()]
        self.assertEqual(len(names), 2, names)
        self.assertTrue(any(name.startswith("subagent-explore") for name in names))

    def test_the_report_reaches_the_parent_as_an_observation(self) -> None:
        import json as json_module

        self.run_harness()
        parent = next(p for p in self.bundles() if p.name.startswith("repair-counter"))
        reports = [
            json_module.loads(line)["payload"]["content"]
            for line in (parent / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json_module.loads(line)["event_type"] == "TOOL_RESULT"
            and json_module.loads(line)["payload"].get("tool") == "delegate"
        ]
        self.assertEqual(len(reports), 1)
        self.assertIn("missing branch", reports[0])

    def test_the_delegation_is_visible_in_the_parent_ledger(self) -> None:
        import json as json_module

        self.run_harness()
        parent = next(p for p in self.bundles() if p.name.startswith("repair-counter"))
        kinds = [
            json_module.loads(line)["event_type"]
            for line in (parent / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("SUBAGENT_STARTED", kinds)
        self.assertIn("SUBAGENT_FINISHED", kinds)

    def test_the_sub_agent_leaves_the_parent_workspace_alone(self) -> None:
        # It works from a copy. A reviewer that runs the test suite must not
        # leave artifacts the parent is then judged on.
        self.run_harness()
        parent = next(p for p in self.bundles() if p.name.startswith("repair-counter"))
        child = next(p for p in self.bundles() if p.name.startswith("subagent-explore"))
        self.assertNotEqual(parent / "workspace", child / "workspace")
        self.assertTrue((child / "workspace" / "counter.py").is_file())

    def test_delegate_is_absent_when_no_agents_are_configured(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "harness", "tools", "--config", "configs/teaching.yaml",
             "--skill", "skills/bugfix.yaml"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertNotIn("delegate", result.stdout)


if __name__ == "__main__":
    unittest.main()
