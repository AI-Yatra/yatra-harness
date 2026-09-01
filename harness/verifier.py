"""Independent completion gate: commands, diff, and protected-path integrity."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .config import HarnessConfig
from .contracts import TaskContract, VerificationResult
from .process import run_process
from .sandbox import build_sandbox
from .workspace import Workspace


class Verifier:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def verify(self, task: TaskContract, workspace: Workspace) -> VerificationResult:
        started = time.monotonic()
        changed = self._changed_paths(workspace.root)
        violations = tuple(path for path in changed if workspace.is_protected(path))
        command_results = []
        commands_passed = True
        # Acceptance runs in the same sandbox the tools did. A change proved
        # to work on the host and never tried in the environment it will
        # actually run in has not been proved to work.
        sandbox = build_sandbox(self.config.sandbox)
        local = self.config.sandbox.kind == "local"
        for command in task.acceptance.commands:
            if local and command and command[0] in {"python", "python3"}:
                normalized = [sys.executable, *command[1:]]
            else:
                normalized = list(command)
            result = sandbox.run(
                normalized,
                workspace=workspace.root,
                timeout=task.acceptance.timeout_seconds,
                max_output_chars=self.config.budgets.max_output_chars,
            )
            command_results.append(
                {
                    "command": list(command),
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                    "output": result.output,
                }
            )
            if result.returncode != 0 or result.timed_out:
                commands_passed = False
                break
        diff_passed = bool(changed) or not task.acceptance.require_non_empty_diff
        passed = commands_passed and diff_passed and not violations
        reasons = []
        if not commands_passed:
            reasons.append("an acceptance command failed")
        if not diff_passed:
            reasons.append("no implementation diff was produced")
        if violations:
            reasons.append("protected paths changed: " + ", ".join(violations))
        summary = "verification passed" if passed else "; ".join(reasons)
        return VerificationResult(
            passed=passed,
            commands=tuple(command_results),
            changed_paths=changed,
            protected_violations=violations,
            summary=summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _changed_paths(self, workspace: Path) -> tuple[str, ...]:
        # Tracked changes (modified/deleted) relative to HEAD.
        result = run_process(
            ["git", "diff", "--name-only", "HEAD", "--", "."],
            cwd=workspace,
            timeout=20,
            max_output_chars=self.config.budgets.max_output_chars,
        )
        tracked = tuple(line for line in result.output.splitlines() if line) if result.returncode == 0 else ()
        # Untracked files (new artifacts like contact.xlsx, scripts the agent
        # wrote) must count as implementation changes too. Without this, a
        # task that produces new files, the normal case for artifact-style
        # tasks, would always report "no implementation diff was produced".
        untracked = run_process(
            ["git", "ls-files", "--others", "--exclude-standard", "--"],
            cwd=workspace,
            timeout=20,
            max_output_chars=self.config.budgets.max_output_chars,
        )
        new_files = tuple(line for line in untracked.output.splitlines() if line) if untracked.returncode == 0 else ()
        all_paths = tracked + new_files
        # Defensive filter: ignore Python bytecode and editor temp files in
        # case the workspace's .gitignore is missing or stale. These should
        # never be considered as the "implementation diff".
        return tuple(
            path
            for path in all_paths
            if not path.endswith(".pyc")
            and "__pycache__" not in path.split("/")
        )

