"""Per-run workspace lifecycle with canonical-path containment."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from .errors import WorkspaceError

IGNORED_NAMES = {".git", ".runs", ".venv", "__pycache__", ".DS_Store"}


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

    def create(self, run_id: str, seed: Path, protected_paths: tuple[str, ...]) -> Workspace:
        run_dir = self.runs_dir / run_id
        workspace_dir = run_dir / "workspace"
        if run_dir.exists():
            raise WorkspaceError(f"run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True, mode=0o700)
        shutil.copytree(seed, workspace_dir, ignore=self._ignore)
        self._initialize_git(workspace_dir)
        return Workspace(workspace_dir, protected_paths)

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
    def _initialize_git(workspace: Path) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "AI Yatra Harness",
            "GIT_AUTHOR_EMAIL": "harness@local.invalid",
            "GIT_COMMITTER_NAME": "AI Yatra Harness",
            "GIT_COMMITTER_EMAIL": "harness@local.invalid",
        }
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
