"""Delegation: a bounded sub-agent that reports back.

The harness always had an independent checker -- the verifier -- and no way
for the agent to ask a *second agent* anything. Explore this area, review
this change, find where this is handled: all of it had to happen in the main
agent's own context, which is the context most worth keeping small.

A sub-agent here is read-only on purpose. Its deliverable is a report, not an
edit, and a report needs no verifier because it changes nothing. That keeps
the completion gate exactly where it was: one agent makes changes, one
verifier decides whether they worked, and delegation adds neither a second
writer nor a second opinion about whether the run is done.

It works from a copy of the parent's workspace rather than the workspace
itself. A reviewing sub-agent that runs the test suite would otherwise leave
build artifacts in the parent's diff, and the parent would be judged on them.

Every delegation is a full harness run with its own bundle, ledger and
checkpoints. A sub-agent that misbehaves is therefore as inspectable and as
replayable as the parent, which is the whole reason for running it through
the same machinery instead of a second, quieter code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigurationError, ToolError
from .util import atomic_write_text, safe_slug

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CALLS = 3


@dataclass(frozen=True, slots=True)
class Subagent:
    """One configured sub-agent: its skill, and optionally its own model.

    A separate config is the point rather than a convenience. The course
    argument the verifier already embodies -- the author of a piece of work
    is the worst judge of it -- applies again one level down: a reviewing
    sub-agent running the same model with the same settings as the agent that
    wrote the change shares its blind spots.
    """

    skill: Path
    config: Path | None = None


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    agents: dict[str, Subagent] = field(default_factory=dict)
    max_depth: int = DEFAULT_MAX_DEPTH
    max_calls: int = DEFAULT_MAX_CALLS
    max_turns: int = 6
    max_seconds: float = 300.0

    @property
    def enabled(self) -> bool:
        return bool(self.agents)


def check_delegation_allowed(
    config: SubagentConfig, agent: str, *, depth: int, calls: int
) -> Subagent:
    """Refuse a delegation that is unknown, too deep, or too frequent.

    The depth cap is the one that matters. Without it a delegating agent can
    spawn a delegating agent, and the per-run budget that bounds a run stops
    bounding anything at all.
    """
    if not config.enabled:
        raise ToolError("delegation is not configured for this harness")
    if agent not in config.agents:
        known = ", ".join(sorted(config.agents)) or "none"
        raise ToolError(f"unknown sub-agent {agent!r}; configured agents: {known}")
    if depth >= config.max_depth:
        raise ToolError(
            f"delegation depth limit reached ({config.max_depth}); a sub-agent may not delegate further"
        )
    if calls >= config.max_calls:
        raise ToolError(f"delegation limit reached ({config.max_calls} per run)")
    return config.agents[agent]


def subagent_task(
    directory: Path, agent: str, objective: str, workspace: Path, *, index: int
) -> Path:
    """Write the task contract for one delegation.

    Acceptance is a command that cannot fail and no diff is required, because
    the deliverable is findings. Requiring a diff would turn an honest "I
    looked and there is nothing there" into a failed run, which is the most
    useful answer a reviewer can give.
    """
    task = {
        "version": 1,
        "id": f"subagent-{safe_slug(agent)}-{index:02d}-{safe_slug(objective)[:24]}",
        "objective": objective,
        "workspace_seed": str(Path(workspace).resolve()),
        "constraints": [
            "You are a sub-agent. Your deliverable is a report, not a change.",
            "Answer the objective from evidence you read in the workspace.",
            "Finish with a summary that states what you found, with file paths.",
            "Say plainly when you found nothing; that is a useful answer.",
        ],
        "protected_paths": [],
        "acceptance": {
            "commands": [["python", "-c", "print('subagent report recorded')"]],
            "require_non_empty_diff": False,
            "timeout_seconds": 30,
        },
        "metadata": {"subagent": agent},
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"subagent-{index:02d}-{safe_slug(agent)}.yaml"
    atomic_write_text(path, yaml.safe_dump(task, sort_keys=False, allow_unicode=True), mode=0o600)
    return path


def _resolved_file(value: str, base: Path, where: str) -> Path:
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        resolved = (base / resolved).resolve()
    if not resolved.is_file():
        # Checked here so a typo fails at `harness doctor` rather than halfway
        # through a run, when a workspace and a run id already exist.
        raise ConfigurationError(f"{where} is not a file: {resolved}")
    return resolved


def subagent_config_from_dict(
    raw: dict[str, Any] | None, base: Path, path: str = "subagents"
) -> SubagentConfig:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(
        value, {"agents", "max_depth", "max_calls", "max_turns", "max_seconds"}, path
    )
    agents_raw = schema.mapping(value.get("agents", {}), f"{path}.agents")
    agents: dict[str, Subagent] = {}
    for name, entry in agents_raw.items():
        where = f"{path}.agents.{name}"
        # A bare string is the common case -- just a skill. A mapping is for
        # when the sub-agent should also run on a different model.
        if isinstance(entry, str):
            skill_value, config_value = entry, ""
        else:
            mapping = schema.mapping(entry, where)
            schema.reject_unknown(mapping, {"skill", "config"}, where)
            skill_value = schema.string(schema.require(mapping, "skill", where), f"{where}.skill")
            config_value = (
                schema.string(mapping["config"], f"{where}.config") if mapping.get("config") else ""
            )
        agents[str(name)] = Subagent(
            skill=_resolved_file(skill_value, base, f"{where}.skill"),
            config=_resolved_file(config_value, base, f"{where}.config") if config_value else None,
        )
    return SubagentConfig(
        agents=agents,
        max_depth=schema.integer(value.get("max_depth", DEFAULT_MAX_DEPTH), f"{path}.max_depth", minimum=0),
        max_calls=schema.integer(value.get("max_calls", DEFAULT_MAX_CALLS), f"{path}.max_calls", minimum=1),
        max_turns=schema.integer(value.get("max_turns", 6), f"{path}.max_turns", minimum=1),
        max_seconds=schema.number(value.get("max_seconds", 300), f"{path}.max_seconds", minimum=1),
    )
