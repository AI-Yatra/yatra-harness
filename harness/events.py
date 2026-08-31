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

from .contracts import SCHEMA_VERSION, HarnessEvent
from .errors import StateError
from .redaction import Redactor
from .util import utc_now


class EventLog:
    def __init__(self, path: Path, run_id: str, redactor: Redactor | None = None) -> None:
        self.path = path
        self.run_id = run_id
        self.redactor = redactor or Redactor()
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = self._read_last_sequence()

    @property
    def sequence(self) -> int:
        return self._sequence

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
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    event = HarnessEvent(**raw)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise StateError(f"invalid event at {self.path}:{line_number}") from exc
                if event.schema_version != SCHEMA_VERSION:
                    raise StateError(
                        f"unsupported event schema {event.schema_version} at {self.path}:{line_number}"
                    )
                yield event

