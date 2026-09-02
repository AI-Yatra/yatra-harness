"""Evidence-bundle creation independent of transient model context."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from harness.core.contracts import RunState, VerificationResult
from harness.core.util import atomic_write_json, atomic_write_text, content_hash, truncate
from harness.record.redaction import Redactor


class ArtifactStore:
    def __init__(self, run_dir: Path, redactor: Redactor | None = None) -> None:
        self.run_dir = run_dir
        self.redactor = redactor or Redactor()
        self.payload_dir = run_dir / "artifacts" / "payloads"
        self.verification_dir = run_dir / "artifacts" / "verification"
        self.input_dir = run_dir / "inputs"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.verification_dir.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_input(self, source: Path, name: str) -> Path:
        destination = self.input_dir / name
        shutil.copyfile(source, destination)
        return destination

    def write_manifest(self, value: dict[str, Any]) -> Path:
        path = self.run_dir / "manifest.json"
        atomic_write_json(path, self.redactor.value(value))
        return path

    def write_payload(self, label: str, content: str) -> str:
        redacted = self.redactor.text(content)
        digest = content_hash({"label": label, "content": redacted})[:16]
        path = self.payload_dir / f"{label}-{digest}.txt"
        atomic_write_text(path, redacted)
        return path.relative_to(self.run_dir).as_posix()

    def write_verification(self, attempt: int, result: VerificationResult) -> Path:
        path = self.verification_dir / f"attempt-{attempt:02d}.json"
        value = {
            "passed": result.passed,
            "commands": list(result.commands),
            "changed_paths": list(result.changed_paths),
            "protected_violations": list(result.protected_violations),
            "summary": result.summary,
            "duration_ms": result.duration_ms,
        }
        atomic_write_json(path, self.redactor.value(value))
        return path

    def finalize(self, state: RunState, workspace: Path) -> Path:
        patch = self._git_output(workspace, ["git", "diff", "--binary", "HEAD"])
        atomic_write_text(self.run_dir / "patch.diff", patch)
        result = {
            "schema_version": state.schema_version,
            "run_id": state.run_id,
            "task_id": state.task_id,
            "status": state.status.value,
            "terminal_reason": state.terminal_reason,
            "turns": state.turn,
            "tool_calls": state.tool_calls,
            "verification_attempts": state.verification_attempts,
            "retries": state.retries,
            "finish_summary": state.finish_summary,
        }
        # result.json and summary.md are the artifacts an operator shares in a
        # bug report, so they are redacted like the event log. patch.diff is
        # deliberately not: it must stay byte-exact to remain appliable, and
        # docs/SECURITY.md states that limit.
        atomic_write_json(self.run_dir / "result.json", self.redactor.value(result))
        summary = self.redactor.text(self._summary_markdown(state, patch))
        path = self.run_dir / "summary.md"
        atomic_write_text(path, summary, mode=0o644)
        return path

    @staticmethod
    def _git_output(workspace: Path, command: list[str]) -> str:
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        return completed.stdout

    @staticmethod
    def _summary_markdown(state: RunState, patch: str) -> str:
        patch_preview, truncated = truncate(patch, 8_000)
        suffix = "\n\nThe patch preview was truncated; see `patch.diff`." if truncated else ""
        return (
            f"# Harness run {state.run_id}\n\n"
            f"- Status: **{state.status.value}**\n"
            f"- Task: `{state.task_id}`\n"
            f"- Terminal reason: {state.terminal_reason}\n"
            f"- Turns: {state.turn}\n"
            f"- Tool calls: {state.tool_calls}\n"
            f"- Verification attempts: {state.verification_attempts}\n"
            f"- Model retries: {state.retries}\n\n"
            f"## Completion summary\n\n{state.finish_summary or 'No completion summary.'}\n\n"
            f"## Patch\n\n```diff\n{patch_preview}\n```{suffix}\n"
        )

