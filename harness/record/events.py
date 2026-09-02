"""Append-only, sequence-checked JSONL event ledger."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.core.contracts import SCHEMA_VERSION, HarnessEvent
from harness.core.errors import StateError
from harness.core.util import utc_now
from harness.record.redaction import Redactor


class EventLog:
    def __init__(self, path: Path, run_id: str, redactor: Redactor | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        #: Whether a half-written final line was ever seen in this file. Set
        #: on the first read and never cleared, because the useful question is
        #: whether the run ended mid-append, and repairing the file afterwards
        #: does not change the answer.
        self.truncated = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = self._read_last_sequence()
        if self.truncated:
            self._discard_torn_tail()

    @property
    def sequence(self) -> int:
        return self._sequence

    def _discard_torn_tail(self) -> None:
        """Cut the half-written last line off the file.

        Tolerating it on read is not enough on its own. The torn line has no
        newline, so the next append lands on the end of it and produces one
        corrupt line out of two good halves, which loses the appended event as
        well and turns a recoverable tail into damage in the middle. Removing
        it once, when the log is opened, is what makes the recovery hold.
        """
        with self.path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        keep = []
        for line in lines:
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                break
            keep.append(line if line.endswith("\n") else line + "\n")
        with self.path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(keep)

    def _read_last_sequence(self) -> int:
        if not self.path.exists():
            return 0
        sequence = 0
        for event in self.read():
            if event.sequence != sequence + 1:
                raise StateError(
                    f"event sequence gap in {self.path}: expected {sequence + 1}, got {event.sequence}"
                )
            sequence = event.sequence
        return sequence

    def append(self, event_type: str, payload: dict[str, Any] | None = None) -> HarnessEvent:
        with self._lock:
            self._sequence += 1
            event = HarnessEvent(
                schema_version=SCHEMA_VERSION,
                sequence=self._sequence,
                event_id=str(uuid.uuid4()),
                run_id=self.run_id,
                event_type=event_type,
                timestamp=utc_now(),
                payload=self.redactor.value(payload or {}),
            )
            line = json.dumps(asdict(event), sort_keys=True, ensure_ascii=False) + "\n"
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return event

    def read(self) -> Iterator[HarnessEvent]:
        """Every event in order, tolerating a crash during the final write.

        A ledger is append-only, so the one line that can be half-written is
        the last one: the process died between `os.write` and the newline. That
        is a recoverable shape, and refusing the whole file for it loses the
        entire history of the run for resume and replay, which is the opposite
        of what a durable log is for.

        A bad line anywhere else is different. Nothing rewrites earlier lines,
        so damage in the middle means the file was corrupted by something other
        than an interrupted append, and continuing past it would silently drop
        events. That still refuses.
        """
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
        last = max((index for index, line in enumerate(lines) if line.strip()), default=-1)
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            line_number = index + 1
            try:
                raw = json.loads(line)
                event = HarnessEvent(**raw)
            except (json.JSONDecodeError, TypeError) as exc:
                if index == last:
                    self.truncated = True
                    return
                raise StateError(f"invalid event at {self.path}:{line_number}") from exc
            if event.schema_version != SCHEMA_VERSION:
                raise StateError(
                    f"unsupported event schema {event.schema_version} at {self.path}:{line_number}"
                )
            yield event

