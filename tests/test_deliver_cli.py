"""`harness run --deliver` and `harness deliver`, end to end.

The unit tests in test_delivery.py cover the mechanics. These cover the
operator's path into them: that a run can be delivered in one command, that a
finished run can be delivered later from its bundle alone, and that neither
one pushes anything without being told to.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.execution.workspace import git_environment
from tests.support import install_gh_stub

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = git_environment()


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=GIT_ENV, check=True,
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


class DeliverCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-deliver-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.runs = self.base / "runs"
        self.upstream = self.base / "upstream.git"
        self.repository = self.base / "counter-repo"
        git("init", "--bare", "-q", str(self.upstream), cwd=self.base)
        shutil.copytree(ROOT / "fixtures" / "buggy_counter", self.repository)
        git("init", "-q", "-b", "main", cwd=self.repository)
        git("add", "-A", cwd=self.repository)
        git("commit", "-q", "-m", "initial", cwd=self.repository)
        git("remote", "add", "origin", str(self.upstream), cwd=self.repository)
        git("push", "-q", "origin", "main", cwd=self.repository)
        self.task = self.base / "task.yaml"
        self.task.write_text(
            "version: 1\n"
            "id: repair-counter-boundary\n"
            "objective: Repair the clamp lower bound so it clamps to the bound.\n"
            f"repository: {self.repository}\n"
            "protected_paths:\n"
            "  - tests/**\n"
            "acceptance:\n"
            "  commands:\n"
            "    - [python, -m, unittest, discover, -s, tests]\n"
            "  require_non_empty_diff: true\n"
            "  timeout_seconds: 30\n",
            encoding="utf-8",
        )
        self.gh_calls = self.base / "gh-calls.txt"

    def stub_gh(self) -> None:
        binaries = self.base / "bin"
        install_gh_stub(binaries, self.gh_calls, url="https://example.invalid/pr/7")
        previous = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{binaries}{os.pathsep}{previous}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", previous))

    def harness(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "harness", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "HARNESS_RUNS_DIR": str(self.runs)},
        )

    def run_task(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.harness(
            "run", str(self.task),
            "--config", "configs/teaching.yaml",
            "--skill", "skills/bugfix.yaml",
            *extra,
        )

    def run_dir(self) -> Path:
        return sorted(self.runs.iterdir())[0]

    def test_a_run_delivers_nothing_by_default(self) -> None:
        result = self.run_task()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.run_dir() / "delivery").exists())
        self.assertEqual(git("branch", "--list", "harness/*", cwd=self.upstream), "")

    def test_deliver_commit_commits_without_pushing(self) -> None:
        result = self.run_task("--deliver", "commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.run_dir() / "delivery" / "delivery.json").read_text())
        self.assertFalse(record["pushed"])
        self.assertEqual(git("branch", "--list", "harness/*", cwd=self.upstream), "")

    def test_policy_yes_does_not_authorise_publishing(self) -> None:
        # --yes approves what the model may do inside the workspace. It must
        # not also mean "push this to a shared remote".
        result = self.run_task("--deliver", "pr", "--yes")
        self.assertIn("declined", result.stdout + result.stderr)
        self.assertEqual(git("branch", "--list", "harness/*", cwd=self.upstream), "")

    def test_deliver_pr_needs_yes_to_push_unattended(self) -> None:
        # No terminal, no --yes: the approval gate denies and delivery stops
        # with the commit intact rather than pushing silently.
        result = self.run_task("--deliver", "pr")
        self.assertIn("declined", result.stdout + result.stderr)
        self.assertEqual(git("branch", "--list", "harness/*", cwd=self.upstream), "")

    def test_deliver_pr_with_yes_pushes_and_opens(self) -> None:
        self.stub_gh()
        result = self.run_task("--deliver", "pr", "--deliver-yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("https://example.invalid/pr/7", result.stdout)
        self.assertIn("harness/", git("branch", "--list", "harness/*", cwd=self.upstream))
        self.assertIn("--base main", self.gh_calls.read_text(encoding="utf-8"))

    def test_a_finished_run_can_be_delivered_afterwards(self) -> None:
        self.run_task()
        run_id = self.run_dir().name
        result = self.harness(
            "deliver", run_id, "--runs-dir", str(self.runs), "--mode", "branch", "--yes"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("harness/", git("branch", "--list", "harness/*", cwd=self.upstream))

    def test_delivering_a_failed_run_is_refused(self) -> None:
        failing = self.base / "failing-task.yaml"
        failing.write_text(
            self.task.read_text(encoding="utf-8").replace(
                "    - [python, -m, unittest, discover, -s, tests]",
                '    - [python, -c, "raise SystemExit(1)"]',
            ),
            encoding="utf-8",
        )
        self.harness(
            "run", str(failing),
            "--config", "configs/teaching.yaml",
            "--skill", "skills/bugfix.yaml",
        )
        run_id = self.run_dir().name
        result = self.harness(
            "deliver", run_id, "--runs-dir", str(self.runs), "--mode", "commit", "--yes"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("COMPLETED", result.stdout + result.stderr)

    def test_an_explicit_base_reaches_gh(self) -> None:
        self.stub_gh()
        self.run_task("--deliver", "pr", "--deliver-yes", "--base", "develop")
        self.assertIn("--base develop", self.gh_calls.read_text(encoding="utf-8"))

    def test_a_seed_mode_run_cannot_be_delivered_as_a_pull_request(self) -> None:
        # A seed workspace has a fresh history and no remote. Saying so is
        # more useful than a git error about a missing origin.
        result = self.harness(
            "run", "tasks/repair_counter.yaml",
            "--config", "configs/teaching.yaml",
            "--skill", "skills/bugfix.yaml",
            "--deliver", "pr", "--deliver-yes",
        )
        self.assertIn("remote", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
