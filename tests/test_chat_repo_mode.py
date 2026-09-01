"""`ay --repo`: driving a real repository from the REPL, and `/pr`.

The REPL is the entry point most people will use, so the repository mode and
the delivery step have to be reachable from it without hand-writing a task
file.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from harness.workspace import git_environment

ROOT = Path(__file__).resolve().parents[1]


def _import_chat():
    sys.path.insert(0, str(ROOT))
    import ay  # noqa: PLC0415
    return ay


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=git_environment(), check=True,
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


class RepoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat = _import_chat()
        self.config = ROOT / "configs" / "palimpsest-config.yaml"
        self.skill = ROOT / "skills" / "palimpsest-skill.yaml"
        self.temporary = tempfile.TemporaryDirectory(prefix="ay-repo-")
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "repo"
        self.repository.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repository)
        (self.repository / "README.md").write_text("hello\n", encoding="utf-8")
        git("add", "-A", cwd=self.repository)
        git("commit", "-q", "-m", "initial", cwd=self.repository)

    def app(self, **kwargs):
        return self.chat.ChatApp(self.config, self.skill, verbose=False, **kwargs)

    def task_for(self, message: str, **kwargs) -> dict:
        app = self.app(**kwargs)
        path = app._write_task(message)
        self.addCleanup(path.unlink, True)
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_repo_mode_writes_a_repository_task(self) -> None:
        data = self.task_for("Fix the README typo", repository=self.repository)
        self.assertNotIn("workspace_seed", data)
        self.assertEqual(Path(data["repository"]), self.repository.resolve())

    def test_seed_mode_is_unchanged(self) -> None:
        data = self.task_for("Say hello")
        self.assertIn("workspace_seed", data)
        self.assertNotIn("repository", data)

    def test_a_repo_task_requires_a_real_diff(self) -> None:
        # Against a real repository, "the agent did nothing" must not read as
        # success the way it does for an open-ended chat message.
        data = self.task_for("Fix the README typo", repository=self.repository)
        self.assertTrue(data["acceptance"]["require_non_empty_diff"])

    def test_an_explicit_acceptance_command_still_wins(self) -> None:
        data = self.task_for(
            "Fix it", repository=self.repository, accept=["python -m pytest"]
        )
        self.assertEqual(data["acceptance"]["commands"], [["python", "-m", "pytest"]])

    def test_a_base_ref_is_carried_into_the_task(self) -> None:
        data = self.task_for("Fix it", repository=self.repository, base_ref="main")
        self.assertEqual(data["base_ref"], "main")

    def test_the_generated_repo_task_loads(self) -> None:
        from harness.config import load_task

        app = self.app(repository=self.repository)
        path = app._write_task("Fix the README typo")
        self.addCleanup(path.unlink, True)
        task = load_task(path)
        self.assertEqual(task.repository, self.repository.resolve())

    def test_the_banner_names_the_repository(self) -> None:
        app = self.app(repository=self.repository)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            app._print_banner()
            output = sink.getvalue()
        self.assertIn("repo:", output)
        self.assertIn(self.repository.name, output)

    def test_delivery_arguments_reach_the_harness_command(self) -> None:
        app = self.app(repository=self.repository, deliver="pr", base="develop")
        command = app._harness_command(Path("task.yaml"))
        self.assertIn("--deliver", command)
        self.assertIn("pr", command)
        self.assertIn("--base", command)
        self.assertIn("develop", command)

    def test_no_delivery_flag_is_passed_when_delivery_is_off(self) -> None:
        command = self.app(repository=self.repository)._harness_command(Path("task.yaml"))
        self.assertNotIn("--deliver", command)

    def test_pr_without_a_run_says_so(self) -> None:
        app = self.app(repository=self.repository)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            app._handle_command("/pr")
            output = sink.getvalue()
        self.assertIn("no run", output.lower())

    def test_pr_is_listed_in_the_help(self) -> None:
        self.assertIn("/pr", self.chat.HELP_TEXT)

    def test_the_last_run_id_is_remembered_from_harness_output(self) -> None:
        app = self.app(repository=self.repository)
        app._note_run_id("run_id: chat-fix-the-readme-abc123\n")
        self.assertEqual(app.last_run_id, "chat-fix-the-readme-abc123")

    def test_unrelated_output_does_not_become_a_run_id(self) -> None:
        app = self.app(repository=self.repository)
        app._note_run_id("status: COMPLETED\n")
        self.assertIsNone(app.last_run_id)


class RepoArgumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat = _import_chat()

    def test_repo_and_seed_together_are_refused(self) -> None:
        with io.StringIO() as sink, contextlib.redirect_stderr(sink):
            with self.assertRaises(SystemExit):
                self.chat.main_with_argv(["--repo", ".", "--seed", "."])

    def test_a_repo_that_is_not_a_repository_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with io.StringIO() as sink, contextlib.redirect_stdout(sink):
                code = self.chat.main_with_argv(["--repo", temporary])
                output = sink.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("git repository", output)


if __name__ == "__main__":
    unittest.main()
