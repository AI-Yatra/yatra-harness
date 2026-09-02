"""Atomic durable checkpoints and resume validation."""

from __future__ import annotations

import json
from pathlib import Path

from harness.core.contracts import RunState
from harness.core.errors import StateError
from harness.core.util import atomic_write_json, utc_now


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, state: RunState) -> None:
        state.updated_at = utc_now()
        atomic_write_json(self.path, state.to_dict())

    def load(self) -> RunState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("state root is not a mapping")
            return RunState.from_dict(raw)
        except FileNotFoundError as exc:
            raise StateError(f"checkpoint does not exist: {self.path}") from exc
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise StateError(f"checkpoint is corrupt or incompatible: {self.path}") from exc

