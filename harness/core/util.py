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


def provider_error_message(body: str, limit: int = 300) -> str:
    """The provider's own message, whichever envelope it chose.

    Every gateway wraps errors differently and one of them wraps the wrapper:
    OpenAI uses ``{"error": {"message": ...}}``, OpenCode nests a second
    error object inside that, and Google returns the whole thing inside a
    one-element list. Showing the raw JSON instead makes a plain "invalid
    key" look like a parser failure, so this digs the sentence out.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body[:limit]
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return body[:limit]
    error = payload.get("error")
    if isinstance(error, dict):
        quota = _quota_summary(error)
        if quota:
            return quota
        for candidate in (error.get("message"), (error.get("error") or {}).get("message")):
            if isinstance(candidate, str) and candidate:
                return candidate[:limit]
    if isinstance(error, str) and error:
        return error[:limit]
    if isinstance(payload.get("message"), str):
        return str(payload["message"])[:limit]
    return body[:limit]


def _quota_summary(error: dict[str, Any]) -> str:
    """A one-line quota message built from the structured details.

    Google's quota errors put the two facts that matter -- the limit and how
    long to wait -- at the *end* of a long paragraph of boilerplate links, so
    truncating the message throws away exactly the useful part. The same
    numbers are in `details` as machine-readable fields, so they are read
    from there instead.
    """
    details = error.get("details")
    if not isinstance(details, list):
        return ""
    violation: dict[str, Any] = {}
    retry = ""
    for entry in details:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("@type", ""))
        if kind.endswith("QuotaFailure"):
            found = entry.get("violations")
            if isinstance(found, list) and found and isinstance(found[0], dict):
                violation = found[0]
        elif kind.endswith("RetryInfo") and entry.get("retryDelay"):
            retry = str(entry["retryDelay"])
    if not violation:
        return ""
    value = violation.get("quotaValue")
    model = (violation.get("quotaDimensions") or {}).get("model", "")
    identifier = str(violation.get("quotaId", ""))
    # The quota id is the only place the window is stated.
    window = "per day" if "PerDay" in identifier else "per minute" if "PerMinute" in identifier else ""
    tier = "free-tier " if "FreeTier" in identifier else ""
    parts = [f"quota exhausted: {tier}limit of {value} requests {window}".rstrip()]
    if model:
        parts.append(f"for {model}")
    summary = " ".join(parts)
    if retry:
        summary += f". Retry in {retry}"
    return summary


#: Substrings that mark a listed model as something other than a chat model.
#: Providers advertise embedding, speech and image endpoints in the same
#: catalogue, and suggesting one as a replacement wastes a turn.
NON_CHAT_MARKERS = (
    "embedding", "tts", "image", "audio", "live", "transcribe",
    "robotics", "computer-use", "omni", "vision", "guard", "rerank",
)


def is_chat_model(name: str) -> bool:
    lowered = name.lower()
    return not any(marker in lowered for marker in NON_CHAT_MARKERS)


def model_version(name: str) -> float:
    """The version number in a model id, for ranking newest first.

    `gemini-3.5-flash` is 3.5. A `latest` alias sorts above every numbered
    release because it is whatever the provider currently points at, which is
    the safest thing to suggest.
    """
    if "latest" in name:
        return 999.0
    found = re.search(r"-(\d+(?:\.\d+)?)", name)
    return float(found.group(1)) if found else 0.0
