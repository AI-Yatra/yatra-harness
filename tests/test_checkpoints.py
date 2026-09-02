"""Undo for a conversation.

The cases that matter are the ones where a half-working undo is worse than
none: a file created after the checkpoint that survives the restore, a history
that cannot be moved forward again, and a restore that silently discards
something the operator typed themselves between two turns.

The shadow repository is deliberately not the operator's own. Nothing here may
write to the history they will publish, and a directory that is not a git
repository at all still has to get an undo, since that is where it is needed
most.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.core.contracts import RiskLevel
from harness.execution.workspace import Workspace, git_environment
from harness.repl.agent import _MUTATING
from harness.repl.checkpoints import EXCLUDED, Checkpoints
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class CheckpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_text("one\n", encoding="utf-8")
        self.points = Checkpoints(self.root, self.root / ".ay" / "checkpoints.git")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")


class RecordTests(CheckpointTestCase):
    def test_the_store_is_created_on_first_use(self) -> None:
        self.assertFalse(self.points.store.exists())
        self.assertIsNotNone(self.points.record("first"))
        self.assertTrue(self.points.store.exists())

    def test_a_session_that_changes_nothing_creates_no_store(self) -> None:
        self.assertEqual(self.points.list(), [])
        self.assertFalse(self.points.store.exists())

    def test_each_record_adds_one(self) -> None:
        for index in range(3):
            (self.root / "a.py").write_text(f"x = {index}\n", encoding="utf-8")
            self.points.record(f"edit {index}")
        self.assertEqual(len(self.points.list()), 3)

    def test_the_label_is_kept(self) -> None:
        self.points.record("edit_file a.py")
        self.assertEqual(self.points.list()[0].label, "edit_file a.py")

    def test_it_works_where_there_is_no_git_repository(self) -> None:
        """Which is exactly where an undo matters most."""
        self.assertFalse((self.root / ".git").exists())
        self.assertIsNotNone(self.points.record("first"))


class RestoreTests(CheckpointTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.points.record("start")
        (self.root / "a.py").write_text("x = 999\n", encoding="utf-8")
        (self.root / "new.py").write_text("added later\n", encoding="utf-8")
        (self.root / "sub" / "b.txt").unlink()
        self.second = self.points.record("after")

    def test_modified_content_goes_back(self) -> None:
        self.points.restore(self.first.ref)
        self.assertEqual(self.read("a.py"), "x = 1\n")

    def test_a_file_created_afterwards_is_removed(self) -> None:
        """`checkout -- .` alone leaves it, producing a state that never was."""
        self.points.restore(self.first.ref)
        self.assertFalse((self.root / "new.py").exists())

    def test_a_deleted_file_comes_back(self) -> None:
        self.points.restore(self.first.ref)
        self.assertTrue((self.root / "sub" / "b.txt").exists())
        self.assertEqual(self.read("sub/b.txt"), "one\n")

    def test_the_later_states_are_not_thrown_away(self) -> None:
        """An undo that cannot be undone is a trap."""
        self.points.restore(self.first.ref)
        refs = {point.ref for point in self.points.list()}
        self.assertIn(self.second.ref, refs)

    def test_the_undo_can_itself_be_undone(self) -> None:
        self.points.restore(self.first.ref)
        self.points.record("after undo")
        self.points.restore(self.second.ref)
        self.assertEqual(self.read("a.py"), "x = 999\n")
        self.assertTrue((self.root / "new.py").exists())

    def test_changed_since_lists_what_a_restore_would_touch(self) -> None:
        changed = self.points.changed_since(self.first.ref)
        self.assertEqual(set(changed), {"a.py", "new.py", "sub/b.txt"})

    def test_changed_since_is_empty_when_nothing_moved(self) -> None:
        self.assertEqual(self.points.changed_since(self.second.ref), [])

    def test_an_operator_edit_between_turns_is_reported_before_it_is_lost(self) -> None:
        """The list is what the shell shows before asking to confirm."""
        (self.root / "mine.txt").write_text("my own work\n", encoding="utf-8")
        self.assertIn("mine.txt", self.points.changed_since(self.first.ref))


class IsolationTests(CheckpointTestCase):
    def test_the_operators_own_repository_is_untouched(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet", str(self.root)],
            check=True,
            capture_output=True,
            env=git_environment(),
        )
        self.points.record("first")
        (self.root / "a.py").write_text("x = 2\n", encoding="utf-8")
        self.points.record("second")
        log = subprocess.run(
            ["git", "-C", str(self.root), "log", "--oneline"],
            capture_output=True,
            text=True,
            env=git_environment(),
        )
        self.assertEqual(log.stdout.strip(), "", "the agent committed to the real history")

    def test_the_store_lives_under_the_session_directory(self) -> None:
        self.assertIn(".ay", self.points.store.parts)

    def test_the_store_and_the_real_git_are_excluded_from_snapshots(self) -> None:
        self.assertIn(".git/", EXCLUDED)
        self.assertIn(".ay/", EXCLUDED)

    def test_build_output_is_excluded(self) -> None:
        for name in ("node_modules/", ".venv/", "__pycache__/", "dist/"):
            self.assertIn(name, EXCLUDED)

    def test_a_gitignored_file_is_not_captured(self) -> None:
        """A documented limit, asserted so it stays a decision.

        Bypassing .gitignore needs `add --force`, which also switches off the
        store's own excludes, so it is all or nothing rather than a dial.
        Honouring it keeps secrets and build output out of a second repository;
        the price is that a change to a gitignored file cannot be undone here.
        """
        (self.root / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        (self.root / "secret.txt").write_text("keep\n", encoding="utf-8")
        first = self.points.record("start")
        (self.root / "secret.txt").write_text("clobbered\n", encoding="utf-8")
        self.points.record("after")
        self.points.restore(first.ref)
        self.assertEqual(self.read("secret.txt"), "clobbered\n")

    def test_an_untracked_file_is_captured(self) -> None:
        """Untracked is not ignored: a new file the agent wrote is covered."""
        first = self.points.record("start")
        (self.root / "fresh.py").write_text("new\n", encoding="utf-8")
        self.points.record("after")
        self.points.restore(first.ref)
        self.assertFalse((self.root / "fresh.py").exists())


class FailureTests(CheckpointTestCase):
    def test_a_broken_store_disables_rather_than_raises(self) -> None:
        """A session must survive losing its undo."""
        self.points.store.parent.mkdir(parents=True, exist_ok=True)
        self.points.store.write_text("not a repository", encoding="utf-8")
        self.assertIsNone(self.points.record("first"))
        self.assertFalse(self.points.enabled)
        self.assertTrue(self.points.reason)

    def test_listing_a_disabled_store_is_empty_not_an_error(self) -> None:
        self.points.enabled = False
        self.assertEqual(self.points.list(), [])

    def test_restoring_an_unknown_ref_fails_quietly(self) -> None:
        self.points.record("first")
        self.assertFalse(self.points.restore("0" * 40))


class AgentWiringTests(unittest.TestCase):
    """What gets snapshotted, and what would only cost time."""

    def test_writes_and_commands_are_snapshotted(self) -> None:
        self.assertIn(RiskLevel.WRITE, _MUTATING)
        self.assertIn(RiskLevel.EXECUTE, _MUTATING)

    def test_reads_are_not(self) -> None:
        """They cannot change the tree, so every snapshot would be identical."""
        self.assertNotIn(RiskLevel.READ, _MUTATING)

    def test_a_command_that_changes_files_is_captured(self) -> None:
        """The case snapshotting inside the edit tools would miss."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            points = Checkpoints(root, root / ".ay" / "checkpoints.git")
            first = points.record("start")
            config = load_config(ROOT / "configs" / "ay.yaml")
            tools = ReplToolset(Workspace(root, ()), config)
            import sys

            tools.dispatch(
                "run_command",
                {
                    "command": [
                        sys.executable,
                        "-c",
                        "import pathlib; pathlib.Path('a.py').write_text('x = 42\\n')",
                    ]
                },
            )
            points.record("run_command")
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "x = 42\n")
            points.restore(first.ref)
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "x = 1\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
