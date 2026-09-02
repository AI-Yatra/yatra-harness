"""Turning a verified run into a commit, a pushed branch, and a pull request.

Everything here is outward-facing, so the tests care as much about what the
harness refuses to do as about what it does.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.autonomy.delivery import DeliveryError, DeliveryRequest, deliver
from harness.core.contracts import RunStatus
from harness.execution.workspace import WorkspaceManager, git_environment

GIT_ENV = git_environment({"GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture"})


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=GIT_ENV, check=True,
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


class DeliveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-delivery-")
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
        self.run_id = "repair-counter-42"
        self.workspace = WorkspaceManager(self.runs).create_from_repository(
            self.run_id, self.source, ()
        )
        self.run_dir = self.runs / self.run_id
        self._write_verification()
        self.approvals: list[str] = []
        self.gh_calls = self.base / "gh-calls.txt"

    def _write_verification(self) -> None:
        directory = self.run_dir / "artifacts" / "verification"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "attempt-01.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "commands": [
                        {"command": ["python", "-m", "unittest"], "returncode": 0,
                         "timed_out": False, "truncated": False, "output": "OK"}
                    ],
                    "changed_paths": ["counter.py"],
                    "protected_violations": [],
                    "summary": "verification passed",
                    "duration_ms": 12,
                }
            ),
            encoding="utf-8",
        )

    def edit(self, text: str = "value = 2\n") -> None:
        (self.workspace.root / "counter.py").write_text(text, encoding="utf-8")

    def stub_gh(self, *, url: str = "https://example.invalid/pr/1", exit_code: int = 0) -> None:
        """Put a fake `gh` on PATH that records how it was called."""
        binaries = self.base / "bin"
        binaries.mkdir(exist_ok=True)
        script = binaries / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.gh_calls}"\n'
            f'echo "{url}"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        previous = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{binaries}{os.pathsep}{previous}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", previous))

    def approve(self, allowed: bool = True):
        def callback(description: str) -> bool:
            self.approvals.append(description)
            return allowed
        return callback

    def request(self, mode: str = "commit", **kwargs) -> DeliveryRequest:
        defaults = {
            "mode": mode,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "workspace": self.workspace.root,
            "objective": "Clamp the lower bound correctly.",
            "status": RunStatus.COMPLETED,
            "summary": "Fixed the clamp.",
        }
        return DeliveryRequest(**{**defaults, **kwargs})


class CommitTests(DeliveryTestCase):
    def test_a_verified_change_becomes_a_commit_on_the_run_branch(self) -> None:
        self.edit()
        result = deliver(self.request(), approve=self.approve())
        self.assertEqual(result.branch, "harness/repair-counter-42")
        self.assertTrue(result.commit)
        self.assertEqual(git("status", "--porcelain", cwd=self.workspace.root), "")
        self.assertIn("Clamp the lower bound", git("log", "-1", "--pretty=%B", cwd=self.workspace.root))

    def test_committing_needs_no_approval(self) -> None:
        # Nothing has left the machine yet, so nothing has to be authorised.
        self.edit()
        deliver(self.request(), approve=self.approve())
        self.assertEqual(self.approvals, [])

    def test_a_run_that_did_not_complete_is_refused(self) -> None:
        self.edit()
        with self.assertRaises(DeliveryError) as caught:
            deliver(self.request(status=RunStatus.FAILED), approve=self.approve())
        self.assertIn("FAILED", str(caught.exception))

    def test_a_run_with_nothing_to_deliver_is_refused(self) -> None:
        with self.assertRaises(DeliveryError) as caught:
            deliver(self.request(), approve=self.approve())
        self.assertIn("nothing to deliver", str(caught.exception))

    def test_untracked_files_are_delivered_too(self) -> None:
        (self.workspace.root / "report.md").write_text("hello\n", encoding="utf-8")
        deliver(self.request(), approve=self.approve())
        self.assertIn("report.md", git("show", "--name-only", "--pretty=", cwd=self.workspace.root))

    def test_the_commit_message_records_the_run(self) -> None:
        self.edit()
        deliver(self.request(), approve=self.approve())
        message = git("log", "-1", "--pretty=%B", cwd=self.workspace.root)
        self.assertIn(self.run_id, message)


class PushTests(DeliveryTestCase):
    def test_branch_mode_pushes_the_branch_to_the_upstream(self) -> None:
        self.edit()
        result = deliver(self.request("branch"), approve=self.approve())
        self.assertTrue(result.pushed)
        self.assertIn(
            "harness/repair-counter-42",
            git("branch", "--list", "harness/*", cwd=self.upstream),
        )

    def test_pushing_asks_for_approval_first(self) -> None:
        self.edit()
        deliver(self.request("branch"), approve=self.approve())
        self.assertEqual(len(self.approvals), 1)
        self.assertIn("push", self.approvals[0])

    def test_a_denied_push_leaves_the_commit_and_stops(self) -> None:
        self.edit()
        with self.assertRaises(DeliveryError) as caught:
            deliver(self.request("branch"), approve=self.approve(allowed=False))
        self.assertIn("declined", str(caught.exception))
        # The commit is local and harmless; refusing the push must not
        # unwind it, or a retry would have nothing to send.
        self.assertIn("harness/", git("branch", "--show-current", cwd=self.workspace.root))
        self.assertEqual(git("branch", "--list", "harness/*", cwd=self.upstream), "")

    def test_delivery_can_be_retried_after_a_denied_push(self) -> None:
        self.edit()
        with self.assertRaises(DeliveryError):
            deliver(self.request("branch"), approve=self.approve(allowed=False))
        result = deliver(self.request("branch"), approve=self.approve())
        self.assertTrue(result.pushed)


class PullRequestTests(DeliveryTestCase):
    def test_pr_mode_pushes_then_opens_a_pull_request(self) -> None:
        self.stub_gh()
        self.edit()
        result = deliver(self.request("pr"), approve=self.approve())
        self.assertEqual(result.pull_request_url, "https://example.invalid/pr/1")
        call = self.gh_calls.read_text(encoding="utf-8")
        self.assertIn("pr create", call)
        self.assertIn("--head harness/repair-counter-42", call)

    def test_the_pull_request_targets_the_repository_default_branch(self) -> None:
        self.stub_gh()
        self.edit()
        deliver(self.request("pr"), approve=self.approve())
        self.assertIn("--base main", self.gh_calls.read_text(encoding="utf-8"))

    def test_an_explicit_base_is_honoured(self) -> None:
        self.stub_gh()
        self.edit()
        deliver(self.request("pr", base="release"), approve=self.approve())
        self.assertIn("--base release", self.gh_calls.read_text(encoding="utf-8"))

    def test_opening_a_pull_request_is_approved_separately_from_pushing(self) -> None:
        self.stub_gh()
        self.edit()
        deliver(self.request("pr"), approve=self.approve())
        self.assertEqual(len(self.approvals), 2)
        self.assertIn("push", self.approvals[0])
        self.assertIn("pull request", self.approvals[1])

    def test_a_denied_pull_request_still_leaves_the_branch_pushed(self) -> None:
        self.stub_gh()
        self.edit()
        decisions = iter([True, False])

        with self.assertRaises(DeliveryError):
            deliver(self.request("pr"), approve=lambda _description: next(decisions))
        self.assertIn(
            "harness/repair-counter-42",
            git("branch", "--list", "harness/*", cwd=self.upstream),
        )
        self.assertFalse(self.gh_calls.exists())

    def test_a_failing_gh_reports_its_output(self) -> None:
        self.stub_gh(url="gh: could not create pull request", exit_code=1)
        self.edit()
        with self.assertRaises(DeliveryError) as caught:
            deliver(self.request("pr"), approve=self.approve())
        self.assertIn("could not create pull request", str(caught.exception))

    def test_a_missing_gh_is_named_rather_than_crashing(self) -> None:
        self.edit()
        previous = os.environ.get("PATH", "")
        empty = self.base / "empty-bin"
        empty.mkdir(exist_ok=True)
        # git still has to resolve, so keep its directory and drop everything
        # else; the point is that `gh` is not on the path.
        git_dir = subprocess.run(
            ["sh", "-c", "command -v git"], capture_output=True, text=True, check=True
        ).stdout.strip()
        os.environ["PATH"] = f"{empty}{os.pathsep}{Path(git_dir).parent}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", previous))
        with self.assertRaises(DeliveryError) as caught:
            deliver(self.request("pr"), approve=self.approve())
        self.assertIn("gh", str(caught.exception))


class PullRequestBodyTests(DeliveryTestCase):
    def body(self) -> str:
        self.stub_gh()
        self.edit()
        deliver(self.request("pr"), approve=self.approve())
        return (self.run_dir / "delivery" / "pull-request.md").read_text(encoding="utf-8")

    def test_the_body_states_the_objective(self) -> None:
        self.assertIn("Clamp the lower bound correctly.", self.body())

    def test_the_body_carries_the_verification_evidence(self) -> None:
        body = self.body()
        self.assertIn("python -m unittest", body)
        self.assertIn("verification passed", body)

    def test_the_body_names_the_run_it_came_from(self) -> None:
        self.assertIn(self.run_id, self.body())

    def test_the_body_lists_the_changed_paths(self) -> None:
        self.assertIn("counter.py", self.body())


class SubjectTests(unittest.TestCase):
    """The commit subject is a git subject line, so it has to read like one."""

    def subject(self, text: str) -> str:
        from harness.autonomy.delivery import subject

        return subject(text)

    def test_a_short_objective_is_used_as_is(self) -> None:
        self.assertEqual(self.subject("Fix the clamp lower bound"), "Fix the clamp lower bound")

    def test_a_trailing_period_is_dropped(self) -> None:
        self.assertEqual(self.subject("Fix the clamp."), "Fix the clamp")

    def test_only_the_first_sentence_is_used(self) -> None:
        self.assertEqual(
            self.subject("Fix the clamp. Then update the docs."), "Fix the clamp"
        )

    def test_folded_whitespace_is_collapsed(self) -> None:
        self.assertEqual(self.subject("Fix\n  the   clamp"), "Fix the clamp")

    def test_a_long_objective_is_cut_at_a_word_boundary(self) -> None:
        line = self.subject(
            "Add a short entry to the Troubleshooting section of docs/OPERATIONS.md "
            "explaining that the suite must run through uv run"
        )
        self.assertLessEqual(len(line), 72)
        self.assertFalse(line.endswith("…"))
        self.assertFalse(line.endswith("."))
        self.assertTrue(line.endswith(("d", "e", "f", "g", "n", "o", "s", "t", "y")), line)
        self.assertNotIn("  ", line)

    def test_a_single_enormous_word_is_still_bounded(self) -> None:
        self.assertLessEqual(len(self.subject("x" * 300)), 72)

    def test_an_empty_objective_still_produces_a_subject(self) -> None:
        self.assertTrue(self.subject("   "))


class DeliveryRecordTests(DeliveryTestCase):
    def test_the_run_bundle_records_what_was_delivered(self) -> None:
        self.stub_gh()
        self.edit()
        deliver(self.request("pr"), approve=self.approve())
        record = json.loads((self.run_dir / "delivery" / "delivery.json").read_text(encoding="utf-8"))
        self.assertEqual(record["mode"], "pr")
        self.assertEqual(record["branch"], "harness/repair-counter-42")
        self.assertTrue(record["pushed"])
        self.assertEqual(record["pull_request_url"], "https://example.invalid/pr/1")
        self.assertTrue(record["commit"])

    def test_a_commit_only_delivery_records_that_nothing_was_pushed(self) -> None:
        self.edit()
        deliver(self.request("commit"), approve=self.approve())
        record = json.loads((self.run_dir / "delivery" / "delivery.json").read_text(encoding="utf-8"))
        self.assertFalse(record["pushed"])
        self.assertEqual(record["pull_request_url"], "")


if __name__ == "__main__":
    unittest.main()
