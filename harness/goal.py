"""Goal mode: attempt until the verifier passes, or stop and say why.

A run is one attempt. A goal is "keep attempting until this is true", and
turning the second into the first needs three things the runtime does not
supply on its own: a stopping condition that is not the model's opinion, a
budget that covers the whole pursuit rather than each try, and a way for the
next attempt to know why the last one failed.

The first of those is why an acceptance command is mandatory here even
though a plain run can go without one. A goal whose stopping condition is
"the model says it is finished" does not terminate on success, it terminates
on confidence, and it will happily burn its whole budget being sure.

The runner is injected so the loop can be tested without a model. What is
worth testing here is the decision structure -- when to retry, when to stop,
what to carry forward -- and none of that needs a provider.
"""

from __future__ import annotations

import json
import shlex
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .contracts import RunStatus
from .errors import HarnessError
from .tracing import root_context, trace_id_for
from .util import atomic_write_json, atomic_write_text, safe_slug, utc_now

# A failed verification is worth another attempt: the model gets the reason
# back and can repair. A blocked run is not -- the model asked a question,
# and asking it again unchanged cannot produce a different answer.
RETRYABLE = {RunStatus.FAILED, RunStatus.BUDGET_EXHAUSTED}


class GoalError(HarnessError):
    """A goal could not be pursued as stated."""


@dataclass(frozen=True, slots=True)
class GoalRequest:
    objective: str
    acceptance: tuple[str, ...]
    config_path: Path
    skill_path: Path
    runs_dir: Path
    seed: Path | None = None
    repository: Path | None = None
    base_ref: str = ""
    protect: tuple[str, ...] = ()
    max_attempts: int = 3
    max_seconds: float = 1800.0


@dataclass(frozen=True, slots=True)
class GoalAttempt:
    index: int
    run_id: str
    status: RunStatus
    reason: str


@dataclass(frozen=True, slots=True)
class GoalResult:
    achieved: bool
    reason: str
    attempts: tuple[GoalAttempt, ...] = field(default_factory=tuple)
    record_path: Path | None = None
    last_run_id: str = ""


# (task_path, attempt_index, trace_context) -> RunResult
Runner = Callable[[Path, int, str], Any]


def pursue(
    request: GoalRequest,
    *,
    runner: Runner,
    clock: Callable[[], float] = time.monotonic,
) -> GoalResult:
    """Attempt the goal until it is met, refused, or out of budget."""
    if not request.acceptance:
        raise GoalError(
            "a goal needs at least one acceptance command: without one its "
            "stopping condition is the model's own opinion of its work"
        )
    if (request.seed is None) == (request.repository is None):
        raise GoalError("a goal must name exactly one of a seed or a repository")

    goal_id = f"goal-{safe_slug(request.objective)[:32]}-{uuid.uuid4().hex[:6]}"
    directory = Path(request.runs_dir) / goal_id
    directory.mkdir(parents=True, exist_ok=True)
    started = clock()
    attempts: list[GoalAttempt] = []
    history: list[str] = []
    achieved = False
    reason = ""

    for index in range(1, request.max_attempts + 1):
        # Checked after the first attempt rather than before it: a goal given
        # a budget already spent should still be tried once, or the operator
        # learns nothing about why it was hopeless.
        if attempts and clock() - started >= request.max_seconds:
            reason = f"wall-clock budget of {request.max_seconds:g}s exhausted after {len(attempts)} attempt(s)"
            break
        task_path = directory / f"attempt-{index:02d}.yaml"
        atomic_write_text(task_path, _task_yaml(request, index, history), mode=0o600)
        # Every attempt joins one trace, so a pursuit reads as a single story
        # rather than as N unrelated runs that happen to share a directory.
        run = runner(task_path, index, root_context(trace_id_for(goal_id)))
        attempts.append(GoalAttempt(index, run.run_id, run.status, run.terminal_reason))
        if run.status is RunStatus.COMPLETED:
            achieved, reason = True, run.terminal_reason or "acceptance criteria passed"
            break
        if run.status not in RETRYABLE:
            reason = f"stopped after a {run.status.value} run: {run.terminal_reason}"
            break
        history.append(run.terminal_reason or run.status.value)
    else:
        reason = f"not achieved in {request.max_attempts} attempt(s); last failure: {history[-1] if history else 'unknown'}"

    record = directory / "goal.json"
    atomic_write_json(
        record,
        {
            "goal_id": goal_id,
            "objective": request.objective,
            "acceptance": list(request.acceptance),
            "achieved": achieved,
            "reason": reason,
            "created_at": utc_now(),
            "attempts": [
                {
                    "index": attempt.index,
                    "run_id": attempt.run_id,
                    "status": attempt.status.value,
                    "reason": attempt.reason,
                }
                for attempt in attempts
            ],
        },
    )
    return GoalResult(
        achieved=achieved,
        reason=reason,
        attempts=tuple(attempts),
        record_path=record,
        last_run_id=attempts[-1].run_id if attempts else "",
    )


def _task_yaml(request: GoalRequest, index: int, history: Sequence[str]) -> str:
    """The task contract for one attempt.

    Every attempt states the same objective and the same acceptance command.
    Only the constraints grow, carrying what previous attempts hit -- a fresh
    run that is not told why the last one failed will usually reproduce it.
    """
    constraints = [
        "Work in the workspace; the acceptance command decides whether you are done.",
        "Make the smallest change that satisfies the objective.",
    ]
    for offset, failure in enumerate(history, start=1):
        constraints.append(
            f"A previous attempt ({offset}) ended: {failure}. Do not repeat it."
        )
    task: dict[str, Any] = {
        "version": 1,
        "id": f"{safe_slug(request.objective)[:32]}-attempt-{index:02d}",
        "objective": request.objective,
        "constraints": constraints,
        "protected_paths": list(request.protect),
        "acceptance": {
            "commands": [shlex.split(command) for command in request.acceptance],
            "require_non_empty_diff": True,
            "timeout_seconds": 120,
        },
    }
    # Resolved rather than copied through: the attempt task is written into
    # the goal directory, and load_task resolves a relative path against the
    # task file, so a relative seed would be looked for inside .runs.
    if request.repository is not None:
        task["repository"] = str(Path(request.repository).resolve())
        if request.base_ref:
            task["base_ref"] = request.base_ref
    else:
        task["workspace_seed"] = str(Path(request.seed or ".").resolve())
    return yaml.safe_dump(task, sort_keys=False, allow_unicode=True)


def load_goal_record(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
