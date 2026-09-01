"""Per-run workspace lifecycle with canonical-path containment."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from .errors import WorkspaceError

IGNORED_NAMES = {".git", ".runs", ".venv", "__pycache__", ".DS_Store"}


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A git environment that ignores the operator's own git configuration.

    Every git call the harness makes runs with this. A developer's global
    config -- a commit template, a signing key, an alias -- must not change
    what a run does, or the same task stops behaving the same way on two
    machines. Identity is pinned for the same reason.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "AI Yatra Harness",
        "GIT_AUTHOR_EMAIL": "harness@local.invalid",
        "GIT_COMMITTER_NAME": "AI Yatra Harness",
        "GIT_COMMITTER_EMAIL": "harness@local.invalid",
    }
    # HOME and SSH_AUTH_SOCK are the credential path for a push. They are
    # absent from tool execution on purpose and present here on purpose:
    # cloning and pushing are harness actions, not model actions.
    for name in ("HOME", "SSH_AUTH_SOCK", "LANG", "SYSTEMROOT", "USERPROFILE"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return {**environment, **(extra or {})}


class Workspace:
    def __init__(self, root: Path, protected_paths: tuple[str, ...]) -> None:
        self.root = root.resolve()
        self.protected_paths = protected_paths

    def resolve(self, relative: str, *, must_exist: bool = False) -> Path:
        candidate_input = Path(relative)
        if candidate_input.is_absolute():
            raise WorkspaceError("absolute paths are not permitted")
        candidate = (self.root / candidate_input).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes workspace: {relative}") from exc
        if must_exist and not candidate.exists():
            raise WorkspaceError(f"workspace path does not exist: {relative}")
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise WorkspaceError(f"path is outside workspace: {path}") from exc

    def is_protected(self, relative: str) -> bool:
        normalized = Path(relative).as_posix().lstrip("./")
        for pattern in self.protected_paths:
            if fnmatch.fnmatch(normalized, pattern):
                return True
            # `tests/**` must also protect the `tests` directory itself, and a
            # caller passing `tests/` must be treated like `tests`.
            if pattern.endswith("/**") and fnmatch.fnmatch(
                normalized.rstrip("/"), pattern[:-3]
            ):
                return True
        return False

    def ensure_writable(self, relative: str) -> Path:
        path = self.resolve(relative)
        if self.is_protected(relative):
            raise WorkspaceError(f"protected path cannot be modified: {relative}")
        return path


class WorkspaceManager:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir.resolve()

    def create(
        self,
        run_id: str,
        seed: Path,
        protected_paths: tuple[str, ...],
        *,
        preserve_git: bool = False,
    ) -> Workspace:
        """Copy `seed` into a run workspace.

        By default the copy gets a fresh history with one baseline commit,
        which is what a fixture wants: the run's diff is then exactly what the
        agent did.

        `preserve_git` copies the existing repository instead. That is for a
        copy of a workspace someone has already been working in -- a review, a
        sub-agent -- where re-baselining would fold the uncommitted change into
        the baseline and leave `git diff` empty, so the reviewer would
        correctly conclude that nothing had been done.
        """
        workspace_dir = self._prepare_run_dir(run_id)
        source_git = Path(seed) / ".git"
        keep_git = preserve_git and source_git.exists()
        shutil.copytree(
            seed, workspace_dir, ignore=self._keep_git_ignore if keep_git else self._ignore
        )
        if keep_git:
            self._detach_remotes(workspace_dir)
        else:
            self._initialize_git(workspace_dir)
        return Workspace(workspace_dir, protected_paths)

    def _detach_remotes(self, workspace: Path) -> None:
        """Strip every remote from a copy made for reading.

        The copy inherits the original's remotes, and a workspace handed to a
        reviewer must not be able to publish anything.
        """
        for remote in (self._git(("remote",), cwd=workspace) or "").split():
            self._git(("remote", "remove", remote), cwd=workspace)

    def create_from_repository(
        self,
        run_id: str,
        repository: Path,
        protected_paths: tuple[str, ...],
        *,
        base_ref: str = "",
    ) -> Workspace:
        """Clone `repository` into the run workspace on a branch of its own.

        Seed mode starts a fresh history, which is right for a fixture and
        useless for a pull request: there is nothing to push. This clones
        instead, so the run carries the repository's real history and a real
        remote.

        Two details are deliberate. The source checkout is only ever read --
        the agent works in the clone, so a failed run cannot corrupt the
        developer's tree. And the clone's `origin`, which git points back at
        the local path it was cloned from, is repointed at the source's own
        upstream; left alone, a push would land in the developer's checkout
        rather than the remote the pull request needs to target.

        A clone starts from a commit, so uncommitted work in the source is
        not carried into the run.
        """
        source = Path(repository).expanduser().resolve()
        if not source.is_dir():
            raise WorkspaceError(f"repository is not a directory: {source}")
        if self._git(("rev-parse", "--git-dir"), cwd=source) is None:
            raise WorkspaceError(f"not a git repository: {source}")
        upstream = self._git(("remote", "get-url", "origin"), cwd=source) or str(source)
        workspace_dir = self._prepare_run_dir(run_id)
        self._clone_into(workspace_dir, source, base_ref, self.branch_name(run_id), upstream)
        return Workspace(workspace_dir, protected_paths)

    def _clone_into(
        self,
        workspace_dir: Path,
        source: Path,
        base_ref: str,
        branch: str,
        upstream: str = "",
    ) -> None:
        source = Path(source).expanduser().resolve()
        if self._git(("rev-parse", "--git-dir"), cwd=source) is None:
            raise WorkspaceError(f"not a git repository: {source}")
        target = upstream or self._git(("remote", "get-url", "origin"), cwd=source) or str(source)
        if self._git(("clone", "--quiet", str(source), str(workspace_dir)), cwd=self.runs_dir) is None:
            raise WorkspaceError(f"could not clone repository: {source}")
        start = self._resolve_base(workspace_dir, base_ref)
        if self._git(("checkout", "-q", "-B", branch, start), cwd=workspace_dir) is None:
            raise WorkspaceError(f"could not create branch {branch} at {start}")
        if self._git(("remote", "set-url", "origin", target), cwd=workspace_dir) is None:
            raise WorkspaceError(f"could not point origin at {target}")

    def create_for_session(
        self,
        session_id: str,
        *,
        protected_paths: tuple[str, ...],
        seed: Path | None = None,
        repository: Path | None = None,
        base_ref: str = "",
    ) -> Workspace:
        """The workspace belonging to a session, created once and reused after.

        Reuse alone would break the verifier: `git diff HEAD` for turn two
        would still contain turn one's changes, so every later turn would
        look productive whether or not it did anything. So outstanding work
        is committed before the next turn begins. That keeps each run's diff
        its own, and it makes the session's history a sequence of commits
        rather than one undifferentiated blob.
        """
        from .session import session_directory  # noqa: PLC0415 - avoids a cycle

        directory = session_directory(self.runs_dir, session_id)
        workspace_dir = directory / "workspace"
        if workspace_dir.is_dir():
            self._commit_outstanding(workspace_dir)
            return Workspace(workspace_dir, protected_paths)
        directory.mkdir(parents=True, exist_ok=True)
        if repository is not None:
            self._clone_into(workspace_dir, repository, base_ref, self.branch_name(session_id))
        elif seed is not None:
            shutil.copytree(seed, workspace_dir, ignore=self._ignore)
            self._initialize_git(workspace_dir)
        else:
            raise WorkspaceError("a session workspace needs either a seed or a repository")
        return Workspace(workspace_dir, protected_paths)

    def _commit_outstanding(self, workspace: Path) -> None:
        if self._git(("add", "-A"), cwd=workspace) is None:
            raise WorkspaceError(f"could not stage session work in {workspace}")
        if not self._git(("diff", "--cached", "--name-only"), cwd=workspace):
            return  # nothing outstanding; an empty commit would be noise
        if self._git(("commit", "-q", "-m", "harness session turn"), cwd=workspace) is None:
            raise WorkspaceError(f"could not commit session work in {workspace}")

    @staticmethod
    def branch_name(run_id: str) -> str:
        """The branch a run's work lands on. One name, derived, never guessed."""
        return f"harness/{run_id}"

    def _resolve_base(self, workspace: Path, base_ref: str) -> str:
        if not base_ref:
            return "HEAD"
        # A branch name has to be read through the remote-tracking ref because
        # a fresh clone only checks one branch out locally. A tag or a raw sha
        # resolves directly, so both spellings are tried before giving up.
        for candidate in (f"origin/{base_ref}", base_ref):
            if self._git(("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"), cwd=workspace):
                return candidate
        raise WorkspaceError(f"base ref does not exist in the repository: {base_ref}")

    def _prepare_run_dir(self, run_id: str) -> Path:
        run_dir = self.runs_dir / run_id
        if run_dir.exists():
            raise WorkspaceError(f"run directory already exists: {run_dir}")
        # 0o700 keeps run directories private on POSIX, where a bundle can
        # contain secrets in state.json. On Windows the same call produces an
        # ACL without an explicit entry for the owning user, which leaves the
        # directory writable but not renameable or removable by the person who
        # created it. There, inherit the parent's ACL instead.
        if os.name == "nt":
            run_dir.mkdir(parents=True)
        else:
            run_dir.mkdir(parents=True, mode=0o700)
        return run_dir / "workspace"

    @staticmethod
    def _git(arguments: tuple[str, ...], *, cwd: Path) -> str | None:
        """Run one git command, returning its stdout or None if it failed.

        Callers turn None into a WorkspaceError naming what they were trying
        to do, which reads better than a raw git exit code.
        """
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                env=git_environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def open(self, run_id: str, protected_paths: tuple[str, ...]) -> Workspace:
        workspace_dir = (self.runs_dir / run_id / "workspace").resolve()
        try:
            workspace_dir.relative_to(self.runs_dir)
        except ValueError as exc:
            raise WorkspaceError("run id resolved outside runs directory") from exc
        if not workspace_dir.is_dir():
            raise WorkspaceError(f"workspace does not exist: {workspace_dir}")
        return Workspace(workspace_dir, protected_paths)

    @staticmethod
    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in IGNORED_NAMES or name.endswith(".pyc")}

    @staticmethod
    def _keep_git_ignore(_directory: str, names: list[str]) -> set[str]:
        keep = IGNORED_NAMES - {".git"}
        return {name for name in names if name in keep or name.endswith(".pyc")}

    @staticmethod
    def _initialize_git(workspace: Path) -> None:
        environment = git_environment()
        # Write a workspace-local .gitignore so the agent's Python __pycache__
        # and editor temp files don't show up as untracked artifacts that
        # confuse the verifier's diff check.
        (workspace / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n.DS_Store\n",
            encoding="utf-8",
        )
        commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "AI Yatra Harness"],
            ["git", "config", "user.email", "harness@local.invalid"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "harness baseline"],
        )
        try:
            for command in commands:
                subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise WorkspaceError(f"could not initialize run workspace as a git repository: {exc}") from exc
