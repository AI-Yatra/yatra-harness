"""Sessions: one workspace and one written memory across many messages.

Every message produced a fresh workspace, so turn two could not build on
turn one and did not know it had happened. A conversation that cannot
accumulate anything is a sequence of unrelated runs wearing a chat prompt.

A session gives it two things. A workspace that persists, so the second
message edits what the first one wrote. And a record on disk of what has
already been tried and how it ended, because the model does not remember
between runs and the filesystem does. The record deliberately keeps failures
as well as successes: a memory that only holds what worked teaches the next
turn to repeat what did not.

The record lives beside the session rather than inside its workspace. Written
into the workspace it would show up as an untracked file, and the verifier
would count the harness's own bookkeeping as the run's implementation diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import RunStatus
from .errors import WorkspaceError
from .util import atomic_write_json, utc_now

SESSIONS_DIRNAME = "sessions"
DEFAULT_NOTE_LIMIT = 5
MESSAGE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    directory: Path
    workspace: Path
    turns: tuple[dict[str, Any], ...] = ()


def session_directory(runs_dir: Path, session_id: str) -> Path:
    """Where a session lives, refusing an id that would escape the runs tree."""
    root = (Path(runs_dir) / SESSIONS_DIRNAME).resolve()
    candidate = (root / session_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"session id resolves outside the runs directory: {session_id!r}") from exc
    if candidate == root:
        raise WorkspaceError("session id must not be empty")
    return candidate


class SessionStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)

    def open(self, session_id: str) -> Session:
        directory = session_directory(self.runs_dir, session_id)
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / "session.json"
        turns: tuple[dict[str, Any], ...] = ()
        if record.is_file():
            try:
                value = json.loads(record.read_text(encoding="utf-8"))
                turns = tuple(value.get("turns") or [])
            except (OSError, json.JSONDecodeError):
                # A corrupt session log must not end the conversation. The
                # workspace is the substantive state; the log is memory, and
                # losing memory is survivable in a way losing work is not.
                turns = ()
        return Session(session_id, directory, directory / "workspace", turns)

    def record(
        self,
        session: Session,
        *,
        run_id: str,
        message: str,
        status: RunStatus,
        reason: str,
        changed: tuple[str, ...] = (),
    ) -> Session:
        # Re-read rather than trusting the caller's snapshot. A REPL holds one
        # Session object for the life of a conversation, and numbering turns
        # from a stale copy would give every turn the index 1.
        current = self.open(session.session_id).turns
        turns = [
            *current,
            {
                "index": len(current) + 1,
                "at": utc_now(),
                "run_id": run_id,
                "message": message[:MESSAGE_LIMIT],
                "status": status.value,
                "reason": reason,
                "changed_paths": list(changed),
            },
        ]
        atomic_write_json(
            session.directory / "session.json",
            {"session_id": session.session_id, "updated_at": utc_now(), "turns": turns},
        )
        return Session(session.session_id, session.directory, session.workspace, tuple(turns))

    @staticmethod
    def notes(session: Session, limit: int = DEFAULT_NOTE_LIMIT) -> str:
        """What the next turn should know about the ones before it."""
        if not session.turns:
            return ""
        lines = []
        for turn in session.turns[-limit:]:
            changed = ", ".join(turn.get("changed_paths") or []) or "no files changed"
            lines.append(
                f"Turn {turn['index']}: \"{turn['message']}\" ended {turn['status']} "
                f"({turn.get('reason', '')}); {changed}."
            )
        return "\n".join(lines)
