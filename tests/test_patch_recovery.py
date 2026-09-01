"""A patch that fails must leave the workspace exactly as it found it.

`git apply --3way` does not have that property. When the merge conflicts it
writes conflict markers into the file, leaves unmerged entries in the index,
and *then* exits non-zero. The harness reported the failure honestly and left
the corruption in place, so the next turn read a file full of `<<<<<<< ours`
and spent the rest of the run trying to understand it. Seen in a live demo:
the agent stopped and asked what state the file was in.

A failed tool call is a normal event. A failed tool call that damages the
workspace is not, because nothing downstream is written to expect it.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.artifacts import ArtifactStore
from harness.config import PolicyConfig
from harness.policy import PolicyEngine
from harness.tools import build_registry
from harness.workspace import Workspace, git_environment

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = "line1\nCHANGED\nline3\n"


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, env=git_environment(), check=True,
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()


class ConflictingPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        from harness.config import load_config, load_skill

        self.temporary = tempfile.TemporaryDirectory(prefix="harness-3way-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir(parents=True)
        self.file = self.root / "f.txt"

        git("init", "-q", "-b", "main", cwd=self.root)
        self.file.write_text("line1\nline2\nline3\n", encoding="utf-8")
        git("add", "-A", cwd=self.root)
        git("commit", "-q", "-m", "base", cwd=self.root)
        base_blob = git("rev-parse", "HEAD:f.txt", cwd=self.root)

        # A staged local edit, which is what lets --3way get far enough to
        # produce a conflict rather than refusing outright.
        self.file.write_text(ORIGINAL, encoding="utf-8")
        git("add", "f.txt", cwd=self.root)
        new_blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=self.root, env=git_environment(),
            input="line1\nOTHER\nline3\n", capture_output=True, text=True, check=True,
        ).stdout.strip()

        self.patch = (
            "diff --git a/f.txt b/f.txt\n"
            f"index {base_blob[:7]}..{new_blob[:7]} 100644\n"
            "--- a/f.txt\n+++ b/f.txt\n"
            "@@ -1,3 +1,3 @@\n line1\n-line2\n+OTHER\n line3\n"
        )

        config = load_config(ROOT / "configs" / "teaching.yaml")
        skill = load_skill(ROOT / "skills" / "bugfix.yaml")
        workspace = Workspace(self.root, ())
        policy = PolicyEngine(PolicyConfig(
            approval_mode="never", allowed_commands=(), denied_commands=(),
            network_enabled=False, allowed_domains=(),
            command_timeout_seconds=30.0, browser_timeout_seconds=10.0,
        ), skill.allowed_tools, lambda *_: True)
        self.registry = build_registry(
            config, skill, workspace,
            ArtifactStore(Path(self.temporary.name) / "run"), policy,
        )

    def apply(self):
        return self.registry.execute("c1", "apply_patch", {"patch": self.patch})

    def test_the_conflicting_patch_is_reported_as_a_failure(self) -> None:
        self.assertFalse(self.apply().ok)

    def test_the_file_is_left_exactly_as_it_was(self) -> None:
        self.apply()
        self.assertEqual(self.file.read_text(encoding="utf-8"), ORIGINAL)

    def test_no_conflict_markers_are_left_behind(self) -> None:
        self.apply()
        self.assertNotIn("<<<<<<<", self.file.read_text(encoding="utf-8"))

    def test_the_index_is_left_without_unmerged_entries(self) -> None:
        # An unmerged index makes every later `git diff` misleading, which is
        # what the verifier reads to decide whether the run did anything.
        self.apply()
        self.assertEqual(git("ls-files", "-u", cwd=self.root), "")

    def test_the_failure_says_the_patch_conflicted(self) -> None:
        result = self.apply()
        self.assertIn("conflict", (result.error or "").lower())

    def test_a_later_patch_can_still_be_applied(self) -> None:
        # The real cost of the corruption: the next turn could not work.
        self.apply()
        good = (
            "--- a/f.txt\n+++ b/f.txt\n"
            "@@ -1,3 +1,3 @@\n line1\n-CHANGED\n+FIXED\n line3\n"
        )
        result = self.registry.execute("c2", "apply_patch", {"patch": good})
        self.assertTrue(result.ok, result.error)
        self.assertIn("FIXED", self.file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
