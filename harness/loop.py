"""The self-feeding loop: work the backlog until it is done or stuck.

Goal mode still needs a person to say what the goal is. This is the rung
above it: read the backlog, take the next unfinished feature, pursue it,
record the outcome against evidence, go round again.

Everything interesting here is about stopping. A loop that cannot stop is not
autonomous, it is unattended, and the difference is whether it can tell the
operator why it stopped. So there are three endings and each one is named: the
backlog is finished, the feature budget is spent, or every remaining feature
has been tried and failed.

A failed feature is skipped rather than retried immediately. Goal mode has
already retried it as many times as it was allowed to; going straight round
again would let one hard feature consume the entire budget while the rest of
the backlog stayed untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backlog import Feature, load_backlog, mark_feature, next_unfinished
from .goal import GoalRequest
from .util import atomic_write_json, utc_now


@dataclass(frozen=True, slots=True)
class LoopRequest:
    backlog: Path
    config_path: Path
    skill_path: Path
    runs_dir: Path
    seed: Path | None = None
    repository: Path | None = None
    base_ref: str = ""
    max_features: int = 10
    max_attempts: int = 2
    max_seconds_per_feature: float = 1800.0


@dataclass(frozen=True, slots=True)
class FeatureOutcome:
    feature_id: str
    achieved: bool
    reason: str
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class LoopResult:
    completed: bool
    reason: str
    outcomes: tuple[FeatureOutcome, ...] = field(default_factory=tuple)
    record_path: Path | None = None


Pursuer = Callable[[Feature, LoopRequest], Any]


def goal_for(feature: Feature, request: LoopRequest) -> GoalRequest:
    """One backlog feature, expressed as a goal.

    The feature's own acceptance commands become the goal's stopping
    condition. That is the whole reason a feature is required to have them:
    the loop never decides for itself what finished means.
    """
    return GoalRequest(
        objective=feature.description,
        acceptance=feature.acceptance,
        config_path=request.config_path,
        skill_path=request.skill_path,
        runs_dir=request.runs_dir,
        seed=request.seed,
        repository=request.repository,
        base_ref=request.base_ref,
        protect=feature.protect,
        max_attempts=request.max_attempts,
        max_seconds=request.max_seconds_per_feature,
    )


def run_loop(request: LoopRequest, *, pursue: Pursuer) -> LoopResult:
    """Work the backlog, recording every outcome as it happens."""
    loop_id = f"loop-{uuid.uuid4().hex[:10]}"
    directory = Path(request.runs_dir) / loop_id
    directory.mkdir(parents=True, exist_ok=True)
    outcomes: list[FeatureOutcome] = []
    failed: set[str] = set()
    completed = False
    reason = ""

    while True:
        if len(outcomes) >= request.max_features:
            reason = f"feature budget of {request.max_features} reached"
            break
        features = load_backlog(request.backlog)
        feature = next_unfinished(features, skip=failed)
        if feature is None:
            remaining = [item for item in features if not item.passes]
            if remaining:
                # Everything left has already been tried and failed. Going
                # round again would produce the same failures more slowly.
                reason = (
                    f"stuck: {len(remaining)} feature(s) remain and each has already "
                    "failed this run"
                )
            else:
                completed, reason = True, "nothing left to do; every feature passes"
            break
        try:
            result = pursue(feature, request)
        except Exception as exc:  # noqa: BLE001
            # An exception is not a failed feature; it is the loop losing the
            # ability to work at all, and continuing would just repeat it.
            reason = f"stopped after an error on {feature.feature_id}: {type(exc).__name__}: {exc}"
            break
        evidence = f"{result.last_run_id}: {result.reason}".strip(": ")
        mark_feature(
            request.backlog, feature.feature_id, passes=result.achieved, evidence=evidence
        )
        outcomes.append(
            FeatureOutcome(
                feature_id=feature.feature_id,
                achieved=result.achieved,
                reason=result.reason,
                run_id=result.last_run_id,
            )
        )
        if not result.achieved:
            failed.add(feature.feature_id)

    record = directory / "loop.json"
    atomic_write_json(
        record,
        {
            "loop_id": loop_id,
            "created_at": utc_now(),
            "backlog": str(request.backlog),
            "completed": completed,
            "reason": reason,
            "outcomes": [
                {
                    "feature_id": outcome.feature_id,
                    "achieved": outcome.achieved,
                    "reason": outcome.reason,
                    "run_id": outcome.run_id,
                }
                for outcome in outcomes
            ],
        },
    )
    return LoopResult(completed, reason, tuple(outcomes), record)
