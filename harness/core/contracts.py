"""Provider-neutral contracts shared across the entire harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from harness.core import schema

SCHEMA_VERSION = 1


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


class ActionKind(StrEnum):
    TOOL = "tool"
    FINISH = "finish"
    CLARIFY = "clarify"


class RiskLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    CONTROL = "control"


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    max_turns: int = 12
    max_tool_calls: int = 24
    max_seconds: float = 300.0
    max_context_chars: int = 24_000
    max_output_chars: int = 12_000
    max_verification_attempts: int = 3

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: str = "budgets") -> BudgetSpec:
        schema.reject_unknown(
            raw,
            {
                "max_turns",
                "max_tool_calls",
                "max_seconds",
                "max_context_chars",
                "max_output_chars",
                "max_verification_attempts",
            },
            path,
        )
        return cls(
            max_turns=schema.integer(raw.get("max_turns", 12), f"{path}.max_turns", minimum=1),
            max_tool_calls=schema.integer(
                raw.get("max_tool_calls", 24), f"{path}.max_tool_calls", minimum=1
            ),
            max_seconds=schema.number(
                raw.get("max_seconds", 300), f"{path}.max_seconds", minimum=0.1
            ),
            max_context_chars=schema.integer(
                raw.get("max_context_chars", 24_000),
                f"{path}.max_context_chars",
                minimum=1_000,
            ),
            max_output_chars=schema.integer(
                raw.get("max_output_chars", 12_000),
                f"{path}.max_output_chars",
                minimum=256,
            ),
            max_verification_attempts=schema.integer(
                raw.get("max_verification_attempts", 3),
                f"{path}.max_verification_attempts",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    commands: tuple[tuple[str, ...], ...]
    require_non_empty_diff: bool = True
    timeout_seconds: float = 60.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: str = "acceptance") -> VerificationSpec:
        schema.reject_unknown(
            raw, {"commands", "require_non_empty_diff", "timeout_seconds"}, path
        )
        return cls(
            commands=schema.command_list(schema.require(raw, "commands", path), f"{path}.commands"),
            require_non_empty_diff=schema.boolean(
                raw.get("require_non_empty_diff", True), f"{path}.require_non_empty_diff"
            ),
            timeout_seconds=schema.number(
                raw.get("timeout_seconds", 60), f"{path}.timeout_seconds", minimum=0.1
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskContract:
    task_id: str
    objective: str
    # Exactly one of these is set. A seed is copied into a fresh history and
    # suits a fixture; a repository is cloned with its history and remote,
    # which is what a run has to have to end in a pull request.
    workspace_seed: Path | None
    constraints: tuple[str, ...]
    protected_paths: tuple[str, ...]
    acceptance: VerificationSpec
    metadata: dict[str, Any] = field(default_factory=dict)
    repository: Path | None = None
    base_ref: str = ""
    # Copy the seed's git repository instead of starting a fresh history.
    # Set for a workspace copied from one already worked in, where a new
    # baseline would fold the change under review into it.
    preserve_git: bool = False

    @property
    def origin(self) -> Path:
        """Where the workspace comes from, whichever mode this task is in."""
        source = self.repository if self.repository is not None else self.workspace_seed
        if source is None:  # pragma: no cover - load_task rejects this first
            raise ValueError("task names neither a repository nor a workspace seed")
        return source


@dataclass(frozen=True, slots=True)
class SkillContract:
    skill_id: str
    instructions: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskLevel
    source: str = "native"

    def as_model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True, slots=True)
class ActionProposal:
    kind: ActionKind
    call_id: str
    name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: str
    turn: int
    messages: tuple[dict[str, Any], ...]
    tools: tuple[ToolSpec, ...]
    max_output_chars: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    route: str
    provider: str
    action: ActionProposal
    raw_summary: str = ""
    usage: dict[str, int | float] = field(default_factory=dict)
    next_cursor: int | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0

    def as_observation(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.name,
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    commands: tuple[dict[str, Any], ...]
    changed_paths: tuple[str, ...]
    protected_violations: tuple[str, ...]
    summary: str
    duration_ms: int


@dataclass(slots=True)
class RunState:
    schema_version: int
    run_id: str
    task_id: str
    status: RunStatus
    workspace: str
    started_at: str
    updated_at: str
    turn: int = 0
    tool_calls: int = 0
    verification_attempts: int = 0
    retries: int = 0
    event_sequence: int = 0
    elapsed_seconds: float = 0.0
    observations: list[dict[str, Any]] = field(default_factory=list)
    completed_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    provider_cursors: dict[str, int] = field(default_factory=dict)
    route_failures: dict[str, int] = field(default_factory=dict)
    opened_routes: list[str] = field(default_factory=list)
    triggered_faults: list[str] = field(default_factory=list)
    last_action: dict[str, Any] | None = None
    finish_summary: str = ""
    terminal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunState:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported state schema {raw.get('schema_version')!r}; expected {SCHEMA_VERSION}"
            )
        values = dict(raw)
        values["status"] = RunStatus(values["status"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    schema_version: int
    sequence: int
    event_id: str
    run_id: str
    event_type: str
    timestamp: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    terminal_reason: str
    run_dir: Path
    workspace: Path
    summary_path: Path

