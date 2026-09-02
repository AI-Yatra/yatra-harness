"""End-to-end runtime tests: happy path, repair, faults, budgets, resume."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.models.llm_light import RoutingError

ROOT = Path(__file__).resolve().parents[1]


def _harness() -> tuple[str, ...]:
    # Use sys.executable so the test subprocess finds the same venv (with
    # PyYAML installed) as the test runner. Plain "python" on Windows resolves
    # to a system interpreter that has no yaml module.
    return (sys.executable, "-m", "harness")


class HarnessRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runs_dir = Path(tempfile.mkdtemp(prefix="harness-tests-"))
        self._patch_runs_dir = os.environ.get("HARNESS_RUNS_DIR")
        os.environ["HARNESS_RUNS_DIR"] = str(self.runs_dir)

    def tearDown(self) -> None:
        if self._patch_runs_dir is None:
            os.environ.pop("HARNESS_RUNS_DIR", None)
        else:
            os.environ["HARNESS_RUNS_DIR"] = self._patch_runs_dir
        shutil.rmtree(self.runs_dir, ignore_errors=True)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*_harness(), "run", "tasks/repair_counter.yaml", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "HARNESS_RUNS_DIR": str(self.runs_dir)},
        )

    def _last_run_dir(self) -> Path:
        candidates = sorted(self.runs_dir.iterdir(), reverse=True)
        self.assertTrue(candidates, "no run directory was created")
        return candidates[0]

    def test_happy_path_completes_and_writes_evidence(self) -> None:
        result = self._run("--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status: COMPLETED", result.stdout)
        run_dir = self._last_run_dir()
        for name in (
            "manifest.json",
            "state.json",
            "events.jsonl",
            "summary.md",
            "patch.diff",
            "result.json",
        ):
            self.assertTrue((run_dir / name).is_file(), f"missing {name}")
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["status"], "COMPLETED")
        result_file = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(result_file["verification_attempts"], 2)  # fail once, then pass

    def test_verifier_driven_repair_is_visible_in_events(self) -> None:
        result = self._run("--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml")
        self.assertEqual(result.returncode, 0)
        run_dir = self._last_run_dir()
        events = [
            json.loads(line)["event_type"]
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        self.assertIn("VERIFICATION_FAILED", events)
        self.assertIn("RETRY_LOOP_ENTERED", events)
        self.assertIn("VERIFICATION_PASSED", events)
        failed = events.index("VERIFICATION_FAILED")
        passed = events.index("VERIFICATION_PASSED")
        self.assertLess(failed, passed)

    def test_source_fixture_stays_unchanged(self) -> None:
        before = (ROOT / "fixtures" / "buggy_counter" / "counter.py").read_bytes()
        self._run("--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml")
        after = (ROOT / "fixtures" / "buggy_counter" / "counter.py").read_bytes()
        self.assertEqual(before, after)

    def test_fault_model_timeout_recovers_and_completes(self) -> None:
        result = self._run(
            "--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml",
            "--fault", "model-timeout-once",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status: COMPLETED", result.stdout)
        run_dir = self._last_run_dir()
        events = [
            json.loads(line)["event_type"]
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        self.assertIn("MODEL_RETRY_SCHEDULED", events)

    def test_broken_primary_falls_back_to_teaching(self) -> None:
        result = self._run(
            "--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml",
            "--model", "broken", "--fallback", "teaching",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self._last_run_dir()
        events = [
            json.loads(line)["event_type"]
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        self.assertIn("MODEL_FALLBACK", events)

    def test_budget_exhaustion_is_explicit(self) -> None:
        result = self._run(
            "--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml",
            "--max-turns", "1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("BUDGET_EXHAUSTED", result.stdout)

    def test_crash_injects_and_resume_completes(self) -> None:
        crashed = self._run(
            "--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml",
            "--fault", "crash-after-tool=2",
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertIn("INTERRUPTED", crashed.stderr)
        self.assertIn("resume run_id=", crashed.stderr)
        run_id = crashed.stderr.split("resume run_id=")[1].split()[0]
        resumed = subprocess.run(
            [*_harness(), "resume", run_id, "--runs-dir", str(self.runs_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("status: COMPLETED", resumed.stdout)
        state = json.loads((self.runs_dir / run_id / "state.json").read_text())
        self.assertEqual(state["status"], "COMPLETED")

    def test_llm_light_profile_pins_teaching(self) -> None:
        result = self._run(
            "--config", "configs/llm_light.yaml", "--skill", "skills/bugfix.yaml",
            "--profile", "teaching",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self._last_run_dir()
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        plans = [event for event in events if event["event_type"] == "MODEL_ROUTES_RESOLVED"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["payload"]["ordered_routes"], ["teaching"])

    def test_explicit_model_pin_outranks_llm_light(self) -> None:
        result = self._run(
            "--config", "configs/llm_light.yaml", "--skill", "skills/bugfix.yaml",
            "--model", "teaching",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self._last_run_dir()
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        resolved = next(
            event for event in events if event["event_type"] == "MODEL_ROUTES_RESOLVED"
        )
        self.assertEqual(resolved["payload"]["ordered_routes"][0], "teaching")

    def test_replay_runs_are_deterministic(self) -> None:
        first = self._run("--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml")
        second = self._run("--config", "configs/teaching.yaml", "--skill", "skills/bugfix.yaml")
        self.assertEqual(first.returncode, second.returncode)
        events_first = self._event_types(self._run_dir_before_last())
        events_second = self._event_types(self._last_run_dir())
        self.assertEqual(events_first, events_second)

    def _event_types(self, run_dir: Path) -> list[tuple[str, str]]:
        events = []
        for line in (run_dir / "events.jsonl").read_text().splitlines():
            event = json.loads(line)
            if event["event_type"] == "MODEL_RESPONSE":
                action = event["payload"]["action"]
                events.append((event["event_type"], f"{action['kind']}/{action['name']}"))
            else:
                events.append((event["event_type"], ""))
        return events

    def _run_dir_before_last(self) -> Path:
        candidates = sorted(self.runs_dir.iterdir(), reverse=True)
        return candidates[1]


class RuntimeUnitTests(unittest.TestCase):
    """Tight unit coverage of routing behavior without spawning processes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-unit-")
        self.runs_dir = Path(self.temporary.name) / "runs"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_with_llm_light_route_attributes(self) -> None:
        config = load_config(ROOT / "configs" / "llm_light.yaml")
        frontier = config.router.routes["remote-frontier"]
        self.assertEqual(frontier.kind, "anthropic")
        self.assertEqual(frontier.quality, 5.0)
        self.assertEqual(frontier.cost_per_1m_input, 3.0)
        self.assertEqual(frontier.latency, "medium")
        self.assertEqual(frontier.context_window, 200_000)
        self.assertFalse(frontier.local)
        self.assertTrue(config.llm_light.enabled)

    def test_with_overrides_validate_profile_and_priorities(self) -> None:
        config = load_config(ROOT / "configs" / "llm_light.yaml")
        overridden = config.with_overrides(profile="offline")
        self.assertEqual(overridden.llm_light.default_profile, "offline")
        prioritized = config.with_overrides(priorities=("cost", "latency"))
        self.assertEqual(prioritized.llm_light.priorities, ("cost", "latency"))
        with self.assertRaises(RoutingError):
            config.with_overrides(profile="nope")

    def test_teaching_config_is_unchanged_semantics(self) -> None:
        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertFalse(config.llm_light.enabled)
        self.assertEqual(config.router.primary, "teaching")
        self.assertEqual(config.router.routes["remote-api"].kind, "openai_compatible")


class RedactionCoverageTests(unittest.TestCase):
    """Every credential a run can actually send must be scrubbable.

    The redactor is built from the configured routes before the run starts.
    If a route resolves its key by one path and the redactor collects keys
    by another, the key reaches the provider but never reaches the scrubber,
    and lands in the ledger in the clear.
    """

    NAMES = ("YATRA_HARNESS_AUTH_FILE", "YATRA_HARNESS_ENV_FILE",
             "DASHSCOPE_API_KEY", "HARNESS_REMOTE_API_KEY", "OPENAI_API_KEY",
             "BRAVE_API_KEY")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._previous = {name: os.environ.get(name) for name in self.NAMES}
        for name in self.NAMES:
            os.environ.pop(name, None)
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(self.tmp / "auth.json")
        os.environ["YATRA_HARNESS_ENV_FILE"] = str(self.tmp / "absent.env")

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def test_a_key_resolved_by_endpoint_is_still_redacted(self) -> None:
        """teaching.yaml's remote-api route names a custom variable and
        points at api.openai.com, so its key resolves by endpoint."""
        from harness.models import auth  # noqa: PLC0415
        from harness.runtime import route_secrets  # noqa: PLC0415

        secret = "sk-proj-" + "z" * 30
        auth.add(secret)
        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertIn(secret, route_secrets(config))

    def test_the_search_backend_key_is_collected_too(self) -> None:
        # web_search sends a credential the model router knows nothing about.
        # Collected anywhere but here, it would reach the ledger in the clear.
        from dataclasses import replace  # noqa: PLC0415

        from harness.execution.search import SearchConfig  # noqa: PLC0415
        from harness.runtime import route_secrets  # noqa: PLC0415

        secret = "brave-" + "q" * 24
        os.environ["BRAVE_API_KEY"] = secret
        config = replace(
            load_config(ROOT / "configs" / "teaching.yaml"),
            search=SearchConfig(kind="brave", api_key_env="BRAVE_API_KEY"),
        )
        self.assertIn(secret, route_secrets(config))

    def test_a_search_backend_without_a_key_adds_nothing(self) -> None:
        from harness.runtime import route_secrets  # noqa: PLC0415

        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertNotIn("", route_secrets(config))

    def test_an_exported_key_is_still_collected(self) -> None:
        from harness.runtime import route_secrets  # noqa: PLC0415

        os.environ["HARNESS_REMOTE_API_KEY"] = "sk-proj-exported-value"
        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertIn("sk-proj-exported-value", route_secrets(config))

    def test_routes_without_a_credential_contribute_nothing(self) -> None:
        from harness.runtime import route_secrets  # noqa: PLC0415

        config = load_config(ROOT / "configs" / "teaching.yaml")
        self.assertEqual([s for s in route_secrets(config) if s], [])


if __name__ == "__main__":
    unittest.main()
