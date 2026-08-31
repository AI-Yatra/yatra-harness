"""Shared deterministic and filesystem-safe helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def safe_slug(value: str, *, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.").lower()
    return (slug or "run")[:limit]


def truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = f"\n… <truncated {len(value) - limit} characters>"
    keep = max(0, limit - len(marker))
    return value[:keep] + marker, True


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync of a directory's entries.

    On POSIX this guarantees the directory entry is durable. On Windows
    ``os.fsync`` on a directory handle raises ``PermissionError`` because
    Windows does not support fsync on directories, so we skip it there.
    Skipping is safe for the harness's purpose (durability of the just-written
    file is handled by the preceding ``os.fsync(file)``).
    """
    if sys.platform == "win32":
        return
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # Some filesystems (e.g. tmpfs on Linux containers) don't support
        # directory fsync. The atomic rename already gave us the new file;
        # skipping the directory fsync is acceptable for this harness.
        pass
    finally:
        os.close(directory_fd)


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode=mode)

