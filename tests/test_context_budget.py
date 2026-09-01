"""What survives when the context budget runs out.

The dynamic context was one JSON document truncated from the end, and its
keys were sorted, which put `task` last. A run that filled its budget
therefore lost its own objective first and the model began inventing one.
Whatever else is dropped, the task must not be.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import load_config
from harness.context import ContextEngine
from harness.contracts import (
    SCHEMA_VERSION,
    RunState,
    RunStatus,
    TaskContract,
    VerificationSpec,
)
from harness.errors import ConfigurationError
from harness.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE = "Add a troubleshooting entry about running the suite through uv run."


class ContextBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-budget-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for index in range(40):
            (self.root / f"file_{index:03d}.py").write_text("x = 1\n", encoding="utf-8")
        self.config = load_config(ROOT / "configs" / "teaching.yaml")
        self.workspace = Workspace(self.root, ())
        self.task = TaskContract(
            task_id="demo",
            objective=OBJECTIVE,
            workspace_seed=self.root,
            constraints=("Edit documentation only.",),
            protected_paths=("tests/**",),
            acceptance=VerificationSpec(commands=(("python", "--version"),)),
        )
        self.skill = type(
            "Skill", (), {"skill_id": "demo", "instructions": "Be careful.", "allowed_tools": ()}
        )()

    def state(self, observations: int = 0, size: int = 4_000) -> RunState:
        now = "2026-01-01T00:00:00+00:00"
        state = RunState(
            schema_version=SCHEMA_VERSION,
            run_id="r",
            task_id="demo",
            status=RunStatus.RUNNING,
            workspace=str(self.root),
            started_at=now,
            updated_at=now,
        )
        state.observations = [
            {
                "call_id": f"call-{index}",
                "tool": "read_file",
                "ok": True,
                "content": f"observation {index} " + ("y" * size),
                "error": None,
                "metadata": {},
            }
            for index in range(observations)
        ]
        return state

    def build(self, state: RunState, config=None):
        return ContextEngine(config or self.config).build(
            self.task, self.skill, state, self.workspace, ()
        )

    def user_payload(self, build) -> dict:
        return json.loads(build.request.messages[1]["content"])

    def test_the_objective_survives_a_full_context(self) -> None:
        build = self.build(self.state(observations=30))
        self.assertEqual(self.user_payload(build)["task"]["objective"], OBJECTIVE)

    def test_the_acceptance_criteria_survive_a_full_context(self) -> None:
        payload = self.user_payload(self.build(self.state(observations=30)))
        self.assertEqual(payload["task"]["acceptance_commands"], [["python", "--version"]])
        self.assertEqual(payload["task"]["protected_paths"], ["tests/**"])

    def test_the_remaining_budget_survives_a_full_context(self) -> None:
        # The model has to be able to see that it is running out of turns.
        payload = self.user_payload(self.build(self.state(observations=30)))
        self.assertIn("remaining_turns", payload["run"])

    def test_the_model_is_told_which_commands_it_may_run(self) -> None:
        # Guessing at the allowlist costs turns. A live reviewer spent its
        # whole budget trying pytest against a unittest repository.
        payload = self.user_payload(self.build(self.state()))
        self.assertIn("available_commands", payload)
        self.assertEqual(
            payload["available_commands"],
            [list(command) for command in self.config.policy.allowed_commands],
        )

    def test_the_command_list_survives_a_full_context(self) -> None:
        payload = self.user_payload(self.build(self.state(observations=30)))
        self.assertIn("available_commands", payload)

    def test_the_payload_is_still_valid_json_under_pressure(self) -> None:
        # String-truncating a JSON document produces something no model can
        # parse. Whatever is dropped, the result stays well-formed.
        build = self.build(self.state(observations=30))
        self.assertIsInstance(self.user_payload(build), dict)

    def test_the_newest_observation_is_kept_over_the_oldest(self) -> None:
        build = self.build(self.state(observations=30))
        text = build.request.messages[1]["content"]
        self.assertIn("observation 29", text)
        self.assertNotIn("observation 0 ", text)

    def test_dropping_context_is_reported_rather_than_silent(self) -> None:
        build = self.build(self.state(observations=30))
        self.assertGreater(build.compacted_observations, 0)
        self.assertIn("dropped", json.dumps(self.user_payload(build)))

    def test_an_unpressured_context_keeps_everything(self) -> None:
        build = self.build(self.state(observations=1, size=10))
        payload = self.user_payload(build)
        self.assertEqual(len(payload["recent_observations"]), 1)
        self.assertTrue(payload["repository_map"])

    def test_the_context_never_exceeds_its_budget(self) -> None:
        build = self.build(self.state(observations=40, size=20_000))
        self.assertLessEqual(
            build.character_count, self.config.budgets.max_context_chars
        )

    def test_a_budget_too_small_for_the_task_itself_is_a_configuration_error(self) -> None:
        tiny = replace(self.config, budgets=replace(self.config.budgets, max_context_chars=1_000))
        with self.assertRaises(ConfigurationError):
            self.build(self.state(), tiny)

    def test_a_huge_single_observation_is_shortened_not_dropped(self) -> None:
        # One enormous tool result must still tell the model something. An
        # empty observation list would hide that the call even happened.
        build = self.build(self.state(observations=1, size=200_000))
        payload = self.user_payload(build)
        self.assertEqual(len(payload["recent_observations"]), 1)
        self.assertIn("read_file", json.dumps(payload["recent_observations"]))


if __name__ == "__main__":
    unittest.main()
