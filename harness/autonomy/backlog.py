"""A backlog the harness can work through on its own.

`harness goal` needs someone to say what the goal is. A loop that discovers
its own next piece of work needs that list to live somewhere durable, and each
item has to carry how it will be checked -- otherwise "done" is decided by
whoever last read the diff, which is the judgement this whole harness exists
to take away from the model.

So a feature without an acceptance command is refused at load. It cannot be
worked autonomously: the loop would have nothing to stop on.

Marking is the other half. A feature is only recorded complete against
evidence -- the run id that produced it and the commands that passed -- and a
failure is written down rather than erased, because a backlog that forgets
its failures sends the loop round the same wall until its budget runs out.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from harness.core.errors import ConfigurationError
from harness.core.util import atomic_write_text, utc_now

# Marks are read-modify-write on one file, and the loop can run features in
# parallel. Two workers finishing at the same moment must not lose one of the
# results.
_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class Feature:
    feature_id: str
    description: str
    acceptance: tuple[str, ...]
    passes: bool = False
    evidence: str = ""
    category: str = ""
    verification: tuple[str, ...] = ()
    protect: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.feature_id,
            "description": self.description,
            "acceptance": list(self.acceptance),
            "passes": self.passes,
        }
        if self.category:
            value["category"] = self.category
        if self.verification:
            value["verification"] = list(self.verification)
        if self.protect:
            value["protect"] = list(self.protect)
        if self.evidence:
            value["evidence"] = self.evidence
        return value


def load_backlog(path: str | Path) -> list[Feature]:
    location = Path(path).expanduser().resolve()
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"backlog could not be read: {location}: {exc}") from exc
    if not isinstance(raw, list):
        raise ConfigurationError(f"backlog must be a JSON list of features: {location}")
    features: list[Feature] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{location.name}[{index}]"
        if not isinstance(item, dict):
            raise ConfigurationError(f"{where} is not an object")
        feature_id = str(item.get("id") or "").strip()
        if not feature_id:
            raise ConfigurationError(f"{where}.id is required")
        if feature_id in seen:
            # Two features with one id make "mark this one done" ambiguous.
            raise ConfigurationError(f"{where}.id {feature_id!r} is duplicated")
        seen.add(feature_id)
        acceptance = tuple(str(command) for command in (item.get("acceptance") or []) if str(command).strip())
        if not acceptance:
            raise ConfigurationError(
                f"{where}.acceptance is required: a feature with no way to check it "
                "cannot be worked autonomously"
            )
        features.append(
            Feature(
                feature_id=feature_id,
                description=str(item.get("description") or feature_id),
                acceptance=acceptance,
                passes=bool(item.get("passes", False)),
                evidence=str(item.get("evidence") or ""),
                category=str(item.get("category") or ""),
                verification=tuple(str(step) for step in (item.get("verification") or [])),
                protect=tuple(str(glob) for glob in (item.get("protect") or [])),
            )
        )
    return features


def save_backlog(path: str | Path, features: Sequence[Feature]) -> None:
    """Write the backlog back, indented, because a person also reads this."""
    atomic_write_text(
        Path(path),
        json.dumps([feature.as_dict() for feature in features], indent=2) + "\n",
        mode=0o644,
    )


def next_unfinished(features: Iterable[Feature], skip: set[str] | None = None) -> Feature | None:
    """The first feature still to do, in file order.

    File order is the priority order. Making it explicit means the person who
    owns the backlog decides what comes next, not the loop.
    """
    skipped = skip or set()
    for feature in features:
        if not feature.passes and feature.feature_id not in skipped:
            return feature
    return None


def mark_feature(path: str | Path, feature_id: str, *, passes: bool, evidence: str) -> Feature:
    """Record an outcome against a feature, under a lock, with its evidence."""
    with _LOCK:
        features = load_backlog(path)
        index = next(
            (position for position, item in enumerate(features) if item.feature_id == feature_id),
            None,
        )
        if index is None:
            raise ConfigurationError(f"no feature with id {feature_id!r} in {path}")
        updated = replace(
            features[index],
            passes=passes,
            evidence=f"{utc_now()} {evidence}".strip(),
        )
        features[index] = updated
        save_backlog(path, features)
        return updated
