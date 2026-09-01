"""Repository-mode run workspaces.

Seed mode copies a directory and starts a fresh history. Repository mode
clones a real repository so the run has history and a remote, which is what
makes a pull request possible at the end of it.
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

from harness.config import load_task
from harness.errors import ConfigurationError, WorkspaceError
from harness.workspace import WorkspaceManager

ROOT = Path(__file__).resolve().parents[1]

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@local.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@local.invalid",
}


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


class RepositoryWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-repo-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.upstream = self.base / "upstream.git"
        self.source = self.base / "source"
        self.runs = self.base / "runs"
        git("init", "--bare", "-q", str(self.upstream), cwd=self.base)
        self.source.mkdir()
        git("init", "-q", "-b", "main", cwd=self.source)
        (self.source / "counter.py").write_text("value = 1\n", encoding="utf-8")
        git("add", "-A", cwd=self.source)
        git("commit", "-q", "-m", "initial", cwd=self.source)
        git("remote", "add", "origin", str(self.upstream), cwd=self.source)
        git("push", "-q", "origin", "main", cwd=self.source)
        self.manager = WorkspaceManager(self.runs)

    def create(self, run_id: str = "run-1", **kwargs: object):
        return self.manager.create_from_repository(run_id, self.source, (), **kwargs)

    def test_workspace_carries_the_repository_contents(self) -> None:
        workspace = self.create()
        self.assertEqual(
            (workspace.root / "counter.py").read_text(encoding="utf-8"), "value = 1\n"
        )

    def test_workspace_keeps_the_repository_history(self) -> None:
        workspace = self.create()
        self.assertIn("initial", git("log", "--oneline", cwd=workspace.root))

    def test_work_lands_on_a_branch_named_for_the_run(self) -> None:
        workspace = self.create("repair-counter-42")
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace.root)
        self.assertEqual(branch, "harness/repair-counter-42")

    def test_origin_points_at_the_upstream_not_the_local_checkout(self) -> None:
        # A clone's origin is the path it was cloned from. Left that way, a
        # push would land in the developer's own checkout instead of the
        # remote the pull request has to be opened against.
        workspace = self.create()
        self.assertEqual(
            git("remote", "get-url", "origin", cwd=workspace.root), str(self.upstream)
        )

    def test_the_source_checkout_is_never_touched(self) -> None:
        before = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.source)
        workspace = self.create()
        (workspace.root / "counter.py").write_text("value = 2\n", encoding="utf-8")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.source), before)
        self.assertEqual(
            (self.source / "counter.py").read_text(encoding="utf-8"), "value = 1\n"
        )
        self.assertEqual(git("status", "--porcelain", cwd=self.source), "")

    def test_a_base_ref_selects_what_the_branch_starts_from(self) -> None:
        first = git("rev-parse", "HEAD", cwd=self.source)
        (self.source / "counter.py").write_text("value = 99\n", encoding="utf-8")
        git("commit", "-qam", "second", cwd=self.source)
        workspace = self.create(base_ref=first)
        self.assertEqual(
            (workspace.root / "counter.py").read_text(encoding="utf-8"), "value = 1\n"
        )

    def test_an_unknown_base_ref_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            self.create(base_ref="no-such-branch")
        self.assertIn("no-such-branch", str(caught.exception))

    def test_a_directory_that_is_not_a_repository_is_refused(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(WorkspaceError) as caught:
            self.manager.create_from_repository("run-2", plain, ())
        self.assertIn("not a git repository", str(caught.exception))

    def test_a_repository_without_an_upstream_keeps_its_own_path_as_origin(self) -> None:
        solo = self.base / "solo"
        solo.mkdir()
        git("init", "-q", "-b", "main", cwd=solo)
        (solo / "a.txt").write_text("a\n", encoding="utf-8")
        git("add", "-A", cwd=solo)
        git("commit", "-q", "-m", "only", cwd=solo)
        workspace = self.manager.create_from_repository("run-3", solo, ())
        self.assertEqual(
            git("remote", "get-url", "origin", cwd=workspace.root), str(solo.resolve())
        )

    def test_uncommitted_source_changes_are_not_carried_into_the_run(self) -> None:
        # A clone starts from a commit. Saying so in a test keeps the
        # behaviour deliberate rather than surprising.
        (self.source / "counter.py").write_text("scratch\n", encoding="utf-8")
        workspace = self.create()
        self.assertEqual(
            (workspace.root / "counter.py").read_text(encoding="utf-8"), "value = 1\n"
        )

    def test_the_run_directory_is_reused_by_open(self) -> None:
        self.create("run-4")
        reopened = self.manager.open("run-4", ())
        self.assertTrue((reopened.root / "counter.py").is_file())


class PreservedGitTests(unittest.TestCase):
    """A workspace copied for review has to still show the change under review.

    Seed mode re-initialises git and commits a baseline, which is right for a
    fixture and wrong for a copy of a workspace someone has been working in:
    the uncommitted change becomes part of the baseline and `git diff` goes
    empty. A reviewer then correctly concludes that nothing was done.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-preserve-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.runs = self.base / "runs"
        self.upstream = self.base / "upstream.git"
        self.source = self.base / "source"
        git("init", "--bare", "-q", str(self.upstream), cwd=self.base)
        self.source.mkdir()
        git("init", "-q", "-b", "main", cwd=self.source)
        (self.source / "counter.py").write_text("value = 1\n", encoding="utf-8")
        git("add", "-A", cwd=self.source)
        git("commit", "-q", "-m", "initial", cwd=self.source)
        git("remote", "add", "origin", str(self.upstream), cwd=self.source)
        self.manager = WorkspaceManager(self.runs)
        # A worked-in workspace: one committed baseline, one uncommitted edit.
        self.worked = self.manager.create_from_repository("run-1", self.source, ())
        (self.worked.root / "counter.py").write_text("value = 2\n", encoding="utf-8")

    def test_a_plain_copy_loses_the_uncommitted_change(self) -> None:
        copied = self.manager.create("run-2", self.worked.root, ())
        self.assertEqual(git("status", "--porcelain", cwd=copied.root), "")

    def test_a_preserved_copy_still_shows_it(self) -> None:
        copied = self.manager.create("run-3", self.worked.root, (), preserve_git=True)
        self.assertIn("counter.py", git("diff", "--name-only", cwd=copied.root))

    def test_the_preserved_copy_has_the_original_history(self) -> None:
        copied = self.manager.create("run-4", self.worked.root, (), preserve_git=True)
        self.assertIn("initial", git("log", "--oneline", cwd=copied.root))

    def test_the_preserved_copy_cannot_push_anywhere(self) -> None:
        # It carries the original's remotes otherwise, and a copy made for
        # reading must not be able to publish.
        copied = self.manager.create("run-5", self.worked.root, (), preserve_git=True)
        self.assertEqual(git("remote", cwd=copied.root), "")

    def test_preserving_a_workspace_with_no_git_still_works(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("a\n", encoding="utf-8")
        copied = self.manager.create("run-6", plain, (), preserve_git=True)
        self.assertTrue((copied.root / "a.txt").is_file())

    def test_the_source_workspace_is_untouched(self) -> None:
        self.manager.create("run-7", self.worked.root, (), preserve_git=True)
        self.assertIn("counter.py", git("diff", "--name-only", cwd=self.worked.root))


class RepositoryTaskTests(unittest.TestCase):
    """A task names either a seed directory or a repository, never both."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-task-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repo"
        self.repository.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repository)
        (self.repository / "a.txt").write_text("a\n", encoding="utf-8")
        git("add", "-A", cwd=self.repository)
        git("commit", "-q", "-m", "initial", cwd=self.repository)

    def write(self, body: str) -> Path:
        path = self.base / "task.yaml"
        path.write_text(
            "version: 1\n"
            "id: demo\n"
            "objective: change something\n"
            f"{body}"
            "acceptance:\n"
            "  commands:\n"
            "    - [python, --version]\n",
            encoding="utf-8",
        )
        return path

    def test_a_repository_task_records_the_repository(self) -> None:
        task = load_task(self.write("repository: repo\n"))
        self.assertEqual(task.repository, self.repository.resolve())
        self.assertIsNone(task.workspace_seed)

    def test_a_seed_task_still_records_the_seed(self) -> None:
        seed = self.base / "seed"
        seed.mkdir()
        task = load_task(self.write("workspace_seed: seed\n"))
        self.assertEqual(task.workspace_seed, seed.resolve())
        self.assertIsNone(task.repository)

    def test_a_base_ref_is_carried_through(self) -> None:
        task = load_task(self.write("repository: repo\nbase_ref: main\n"))
        self.assertEqual(task.base_ref, "main")

    def test_naming_both_is_refused(self) -> None:
        seed = self.base / "seed"
        seed.mkdir()
        with self.assertRaises(ConfigurationError) as caught:
            load_task(self.write("repository: repo\nworkspace_seed: seed\n"))
        self.assertIn("exactly one", str(caught.exception))

    def test_naming_neither_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            load_task(self.write(""))
        self.assertIn("exactly one", str(caught.exception))

    def test_a_repository_that_is_not_a_repository_is_refused_at_load(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(ConfigurationError) as caught:
            load_task(self.write("repository: plain\n"))
        self.assertIn("not a git repository", str(caught.exception))

    def test_a_base_ref_without_a_repository_is_refused(self) -> None:
        seed = self.base / "seed"
        seed.mkdir()
        with self.assertRaises(ConfigurationError) as caught:
            load_task(self.write("workspace_seed: seed\nbase_ref: main\n"))
        self.assertIn("base_ref", str(caught.exception))


class RepositoryRunTests(unittest.TestCase):
    """A whole run against a real repository, end to end.

    The unit tests above prove the workspace is built correctly. This proves
    the rest of the runtime -- verifier, artifacts, checkpoints -- is
    indifferent to which mode built it.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-repo-run-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.runs_dir = self.base / "runs"
        self.repository = self.base / "counter-repo"
        shutil.copytree(ROOT / "fixtures" / "buggy_counter", self.repository)
        git("init", "-q", "-b", "main", cwd=self.repository)
        git("add", "-A", cwd=self.repository)
        git("commit", "-q", "-m", "initial", cwd=self.repository)
        self.task = self.base / "task.yaml"
        self.task.write_text(
            "version: 1\n"
            "id: repair-counter-boundary\n"
            "objective: Repair the clamp lower bound.\n"
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

    def run_harness(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, "-m", "harness", "run", str(self.task),
                "--config", "configs/teaching.yaml",
                "--skill", "skills/bugfix.yaml",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "HARNESS_RUNS_DIR": str(self.runs_dir)},
        )

    def test_a_repository_run_completes_and_leaves_the_work_on_its_branch(self) -> None:
        result = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status: COMPLETED", result.stdout)
        run_dir = sorted(self.runs_dir.iterdir())[0]
        workspace = run_dir / "workspace"
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace)
        self.assertTrue(branch.startswith("harness/"), branch)
        self.assertNotEqual(git("status", "--porcelain", cwd=workspace), "")
        self.assertTrue((run_dir / "patch.diff").read_text(encoding="utf-8").strip())

    def test_the_source_repository_is_left_untouched_by_a_run(self) -> None:
        before = git("rev-parse", "HEAD", cwd=self.repository)
        self.run_harness()
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repository), before)
        self.assertEqual(git("status", "--porcelain", cwd=self.repository), "")

    def test_the_frozen_task_reloads_in_repository_mode(self) -> None:
        # A resumed run reloads inputs/task.yaml. If the frozen copy did not
        # round-trip, resume would die on a task no loader accepts.
        self.run_harness()
        run_dir = sorted(self.runs_dir.iterdir())[0]
        frozen = load_task(run_dir / "inputs" / "task.yaml")
        self.assertEqual(frozen.repository, self.repository.resolve())
        self.assertIsNone(frozen.workspace_seed)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["inputs"]["task"], "inputs/task.yaml")


if __name__ == "__main__":
    unittest.main()
