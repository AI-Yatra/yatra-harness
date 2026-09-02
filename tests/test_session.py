"""Sessions: one workspace and one memory across many messages.

Every message used to produce a fresh workspace, so turn two could not build
on turn one and did not know it had happened. A session gives a conversation
a workspace that persists and a written record of what has already been
done, because the model does not remember and the disk does.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.core.contracts import RunStatus
from harness.core.errors import WorkspaceError
from harness.run.session import SessionStore
from harness.run.workspace import WorkspaceManager, git_environment

ROOT = Path(__file__).resolve().parents[1]


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=git_environment(), check=True,
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-session-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.seed = self.base / "seed"
        self.seed.mkdir()
        (self.seed / "notes.txt").write_text("start\n", encoding="utf-8")
        self.runs = self.base / "runs"
        self.store = SessionStore(self.runs)

    def test_opening_a_session_twice_returns_the_same_directory(self) -> None:
        first = self.store.open("abc")
        second = self.store.open("abc")
        self.assertEqual(first.directory, second.directory)

    def test_a_new_session_has_no_turns(self) -> None:
        self.assertEqual(self.store.open("abc").turns, ())

    def test_a_recorded_turn_survives_reopening(self) -> None:
        session = self.store.open("abc")
        self.store.record(session, run_id="r1", message="do a thing",
                          status=RunStatus.COMPLETED, reason="passed", changed=("a.py",))
        reopened = self.store.open("abc")
        self.assertEqual(len(reopened.turns), 1)
        self.assertEqual(reopened.turns[0]["message"], "do a thing")

    def test_turns_are_numbered_in_order(self) -> None:
        session = self.store.open("abc")
        for index in range(3):
            self.store.record(session, run_id=f"r{index}", message=f"m{index}",
                              status=RunStatus.COMPLETED, reason="ok", changed=())
        self.assertEqual([turn["index"] for turn in self.store.open("abc").turns], [1, 2, 3])

    def test_a_session_id_that_escapes_the_runs_directory_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.store.open("../../etc")

    def test_notes_are_empty_for_a_new_session(self) -> None:
        self.assertEqual(self.store.notes(self.store.open("abc")), "")

    def test_notes_describe_what_already_happened(self) -> None:
        session = self.store.open("abc")
        self.store.record(session, run_id="r1", message="add a helper",
                          status=RunStatus.COMPLETED, reason="passed", changed=("util.py",))
        notes = self.store.notes(self.store.open("abc"))
        self.assertIn("add a helper", notes)
        self.assertIn("COMPLETED", notes)
        self.assertIn("util.py", notes)

    def test_notes_keep_the_most_recent_turns(self) -> None:
        session = self.store.open("abc")
        for index in range(10):
            self.store.record(session, run_id=f"r{index}", message=f"message-{index}",
                              status=RunStatus.COMPLETED, reason="ok", changed=())
        notes = self.store.notes(self.store.open("abc"), limit=3)
        self.assertIn("message-9", notes)
        self.assertNotIn("message-0", notes)

    def test_a_failed_turn_is_recorded_as_failed(self) -> None:
        # A session that only remembers its successes teaches the next turn
        # to repeat the failures.
        session = self.store.open("abc")
        self.store.record(session, run_id="r1", message="try it",
                          status=RunStatus.FAILED, reason="acceptance failed", changed=())
        self.assertIn("FAILED", self.store.notes(self.store.open("abc")))

    def test_the_record_is_readable_json(self) -> None:
        session = self.store.open("abc")
        self.store.record(session, run_id="r1", message="m", status=RunStatus.COMPLETED,
                          reason="ok", changed=())
        value = json.loads((session.directory / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(value["session_id"], "abc")


class SessionWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-session-ws-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.seed = self.base / "seed"
        self.seed.mkdir()
        (self.seed / "notes.txt").write_text("start\n", encoding="utf-8")
        self.manager = WorkspaceManager(self.base / "runs")

    def open(self, session_id: str = "s1"):
        return self.manager.create_for_session(session_id, seed=self.seed, protected_paths=())

    def test_the_first_open_seeds_the_workspace(self) -> None:
        workspace = self.open()
        self.assertEqual((workspace.root / "notes.txt").read_text(encoding="utf-8"), "start\n")

    def test_the_second_open_returns_the_same_workspace(self) -> None:
        first = self.open()
        (first.root / "added.txt").write_text("new\n", encoding="utf-8")
        second = self.open()
        self.assertEqual(first.root, second.root)
        self.assertTrue((second.root / "added.txt").is_file())

    def test_work_from_the_previous_turn_is_committed_before_the_next(self) -> None:
        # Without this the verifier's diff for turn two would still contain
        # turn one's changes, and every later turn would look productive.
        first = self.open()
        (first.root / "added.txt").write_text("new\n", encoding="utf-8")
        second = self.open()
        self.assertEqual(git("status", "--porcelain", cwd=second.root), "")
        self.assertIn("added.txt", git("show", "--name-only", "--pretty=", cwd=second.root))

    def test_two_sessions_do_not_share_a_workspace(self) -> None:
        first = self.open("s1")
        (first.root / "only-in-first.txt").write_text("x\n", encoding="utf-8")
        second = self.open("s2")
        self.assertNotEqual(first.root, second.root)
        self.assertFalse((second.root / "only-in-first.txt").exists())

    def test_a_clean_workspace_gains_no_empty_commit(self) -> None:
        self.open()
        before = git("rev-list", "--count", "HEAD", cwd=self.open().root)
        after = git("rev-list", "--count", "HEAD", cwd=self.open().root)
        self.assertEqual(before, after)

    def test_a_session_id_that_escapes_is_refused(self) -> None:
        with self.assertRaises(WorkspaceError):
            self.manager.create_for_session("../escape", seed=self.seed, protected_paths=())


if __name__ == "__main__":
    unittest.main()
