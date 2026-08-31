"""The observe → propose → act → observe → verify/retry harness runtime."""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

from . import auth
from .artifacts import ArtifactStore
from .config import HarnessConfig, load_config, load_skill, load_task
from .context import ContextEngine
from .contracts import (
    SCHEMA_VERSION,
    ActionKind,
    RunResult,
    RunState,
    RunStatus,
    ToolResult,
)
from .errors import BudgetExceeded, InjectedCrash, ProviderExhausted
from .events import EventLog
from .faults import FaultInjector
from .llm_light import llm_light_from_dict
from .model_router import ModelRouter, build_llm_light
from .policy import ApprovalCallback, PolicyEngine
from .redaction import Redactor
from .state import StateStore
from .tools import ToolRegistry, build_registry
from .util import atomic_write_text, content_hash, safe_slug, truncate, utc_now
from .verifier import Verifier
from .workspace import Workspace, WorkspaceManager


def route_secrets(config: HarnessConfig) -> list[str]:
    """Every credential the configured routes could actually send.

    Resolution has to match `providers._secret` exactly. When the two
    disagree the run sends a key the redactor never learned about, and it
    reaches the ledger in the clear -- so this resolves by endpoint as well
    as by variable name, and both start and resume build the list here
    rather than each writing their own copy.
    """
    return [
        auth.resolve_route(route.api_key_env, route.base_url).key
        for route in config.router.routes.values()
        if route.api_key_env
    ]


class HarnessRuntime:
    def __init__(
        self,
        *,
        config: HarnessConfig,
        task: Any,
        skill: Any,
        state: RunState,
        workspace: Workspace,
        artifacts: ArtifactStore,
        events: EventLog,
        state_store: StateStore,
        approval_callback: ApprovalCallback | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.config = config
        self.task = task
        self.skill = skill
        self.state = state
        self.workspace = workspace
        self.artifacts = artifacts
        self.events = events
        self.state_store = state_store
        self._elapsed_base = state.elapsed_seconds
        self._active_started = time.monotonic()
        self.context_engine = ContextEngine(config)
        self.router = ModelRouter(
            config.router,
            sleeper=sleeper,
            llm_light=build_llm_light(config.llm_light),
            routes=config.router.routes,
            pinned=config.selected_model,
        )
        self.verifier = Verifier(config)
        self.policy = PolicyEngine(config.policy, skill.allowed_tools, approval_callback)
        self.registry: ToolRegistry = build_registry(
            config,
            skill,
            workspace,
            artifacts,
            self.policy,
            event_callback=self._emit,
        )
        self.faults = FaultInjector(
            config.fault,
            state,
            persist=lambda: self._checkpoint("fault-injection"),
            event=self._emit,
        )

    @classmethod
    def start(
        cls,
        *,
        config_path: Path,
        task_path: Path,
        skill_path: Path,
        model: str | None = None,
        fallback: str | None = None,
        max_turns: int | None = None,
        max_seconds: float | None = None,
        fault: str | None = None,
        profile: str | None = None,
        priorities: tuple[str, ...] = (),
        require_local: bool = False,
        max_cost_per_1m: float | None = None,
        approval_callback: ApprovalCallback | None = None,
        sleeper: Any = time.sleep,
    ) -> RunResult:
        config = load_config(config_path).with_overrides(
            model=model,
            fallback=fallback,
            max_turns=max_turns,
            max_seconds=max_seconds,
            fault=fault,
            profile=profile,
            priorities=priorities,
            require_local=require_local,
            max_cost_per_1m=max_cost_per_1m,
        )
        task = load_task(task_path)
        skill = load_skill(skill_path)
        run_id = cls._new_run_id(task.task_id)
        manager = WorkspaceManager(config.runs_dir)
        workspace = manager.create(run_id, task.workspace_seed, task.protected_paths)
        run_dir = config.runs_dir / run_id
        redactor = Redactor(route_secrets(config))
        artifacts = ArtifactStore(run_dir, redactor)
        cls._write_frozen_inputs(artifacts, config, task, skill)
        artifacts.write_manifest(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "created_at": utc_now(),
                "inputs": {
                    "config": "inputs/config.yaml",
                    "task": "inputs/task.yaml",
                    "skill": "inputs/skill.yaml",
                },
                "source_paths": {
                    "config": str(Path(config_path).resolve()),
                    "task": str(Path(task_path).resolve()),
                    "skill": str(Path(skill_path).resolve()),
                },
                "fault": config.fault,
                "input_digest": cls._input_digest(config, task, skill),
            }
        )
        now = utc_now()
        state = RunState(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            task_id=task.task_id,
            status=RunStatus.CREATED,
            workspace=str(workspace.root),
            started_at=now,
            updated_at=now,
        )
        state_store = StateStore(run_dir / "state.json")
        events = EventLog(run_dir / "events.jsonl", run_id, redactor)
        created = events.append(
            "RUN_CREATED",
            {
                "task_id": task.task_id,
                "skill_id": skill.skill_id,
                "primary_route": config.router.primary,
                "fallbacks": list(config.router.fallbacks),
                "workspace": str(workspace.root),
            },
        )
        state.event_sequence = created.sequence
        state_store.save(state)
        try:
            runtime = cls(
                config=config,
                task=task,
                skill=skill,
                state=state,
                workspace=workspace,
                artifacts=artifacts,
                events=events,
                state_store=state_store,
                approval_callback=approval_callback,
                sleeper=sleeper,
            )
        except Exception as exc:
            # A run directory must never be left in limbo: if runtime
            # construction fails (e.g. an MCP server is unreachable), record
            # the failure durably and re-raise so the operator sees the cause.
            try:
                state.status = RunStatus.FAILED
                state.terminal_reason = f"run could not be constructed: {type(exc).__name__}: {exc}"
                state_store.save(state)
                artifacts.finalize(state, workspace.root)
            except Exception as cleanup_error:  # noqa: BLE001 - best-effort cleanup
                # The original construction error is more useful than a cleanup
                # failure; never mask it.
                print(
                    f"warning: could not record construction failure for {run_id}: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                    file=sys.stderr,
                )
            raise
        try:
            return runtime.execute()
        except InjectedCrash as exc:
            raise InjectedCrash(f"{exc}; resume run_id={run_id}") from exc

    @classmethod
    def resume(
        cls,
        run_id: str,
        *,
        runs_dir: Path,
        approval_callback: ApprovalCallback | None = None,
        sleeper: Any = time.sleep,
    ) -> RunResult:
        run_dir = (runs_dir.expanduser().resolve() / run_id).resolve()
        try:
            run_dir.relative_to(runs_dir.expanduser().resolve())
        except ValueError as exc:
            raise ValueError("run id resolves outside runs directory") from exc
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        inputs = manifest["inputs"]
        config = load_config(run_dir / inputs["config"]).with_overrides(
            fault=str(manifest.get("fault", ""))
        )
        # A resumed run must route exactly as the original did, so the routing
        # decision is restored from the frozen config rather than recomputed.
        routing = HarnessRuntime._frozen_config_routing(run_dir / inputs["config"])
        if routing is not None:
            config = replace(config, llm_light=routing[0], selected_model=routing[1])
        task = load_task(run_dir / inputs["task"])
        skill = load_skill(run_dir / inputs["skill"])
        state_store = StateStore(run_dir / "state.json")
        state = state_store.load()
        redactor = Redactor(route_secrets(config))
        events = EventLog(run_dir / "events.jsonl", run_id, redactor)
        state.event_sequence = events.sequence
        workspace = WorkspaceManager(config.runs_dir).open(run_id, task.protected_paths)
        artifacts = ArtifactStore(run_dir, redactor)
        if state.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXHAUSTED,
            RunStatus.CANCELLED,
        }:
            summary = run_dir / "summary.md"
            return RunResult(run_id, state.status, state.terminal_reason, run_dir, workspace.root, summary)
        runtime = cls(
            config=config,
            task=task,
            skill=skill,
            state=state,
            workspace=workspace,
            artifacts=artifacts,
            events=events,
            state_store=state_store,
            approval_callback=approval_callback,
            sleeper=sleeper,
        )
        runtime._emit("RUN_RESUMED", {"from_status": state.status.value, "turn": state.turn})
        runtime._checkpoint("resume")
        return runtime.execute()

    def execute(self) -> RunResult:
        self.state.status = RunStatus.RUNNING
        self._emit("RUN_STARTED", {"turn": self.state.turn})
        self._checkpoint("run-start")
        while True:
            try:
                self._check_budgets(before_turn=True)
            except BudgetExceeded as exc:
                return self._terminate(RunStatus.BUDGET_EXHAUSTED, str(exc))
            self.state.status = RunStatus.RUNNING
            self.state.turn += 1
            self._emit("TURN_STARTED", {"turn": self.state.turn})
            context = self.context_engine.build(
                self.task,
                self.skill,
                self.state,
                self.workspace,
                self.registry.specs(),
            )
            self._emit(
                "CONTEXT_BUILT",
                {
                    "turn": self.state.turn,
                    "character_count": context.character_count,
                    "repo_entries": context.repo_entries,
                    "compacted_observations": context.compacted_observations,
                },
            )
            if context.compacted_observations:
                self._emit(
                    "CONTEXT_COMPACTED",
                    {"observations": context.compacted_observations},
                )
            try:
                response = self.router.call(
                    context.request,
                    self.state,
                    event=self._emit,
                    before_call=self.faults.before_model,
                )
            except ProviderExhausted as exc:
                return self._terminate(RunStatus.FAILED, str(exc))
            action = response.action
            self.state.last_action = {
                "kind": action.kind.value,
                "call_id": action.call_id,
                "name": action.name,
                "arguments": action.arguments,
                "summary": action.summary,
                "route": response.route,
            }
            self._emit(
                "MODEL_RESPONSE",
                {
                    "route": response.route,
                    "provider": response.provider,
                    "action": self.state.last_action,
                    "usage": response.usage,
                },
            )
            self._checkpoint("model-response")
            if action.kind is ActionKind.CLARIFY:
                return self._terminate(
                    RunStatus.BLOCKED,
                    action.summary or "model requested clarification",
                )
            if action.kind is ActionKind.FINISH:
                result = self._verify(action.summary)
                if result is not None:
                    return result
                continue
            if action.kind is not ActionKind.TOOL or not action.name:
                return self._terminate(RunStatus.FAILED, "model returned an invalid action")
            try:
                self._check_budgets(before_turn=False)
            except BudgetExceeded as exc:
                return self._terminate(RunStatus.BUDGET_EXHAUSTED, str(exc))
            result = self._execute_tool(action.call_id, action.name, action.arguments)
            if action.name == "finish" and result.ok:
                verification = self._verify(result.content)
                if verification is not None:
                    return verification
            self.faults.after_checkpointed_tool()

    def _execute_tool(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult:
        existing = self.state.completed_tool_calls.get(call_id)
        if existing:
            result = ToolResult(**existing)
            self._emit("TOOL_RESULT_REUSED", {"call_id": call_id, "tool": name})
            return result
        self.state.tool_calls += 1
        self._emit("TOOL_REQUESTED", {"call_id": call_id, "tool": name, "arguments": arguments})
        result = self.registry.execute(call_id, name, arguments)
        self.state.completed_tool_calls[call_id] = asdict(result)
        observation = result.as_observation()
        self.state.observations.append(observation)
        self._emit(
            "TOOL_RESULT",
            {
                "call_id": call_id,
                "tool": name,
                "ok": result.ok,
                "content": result.content,
                "error": result.error,
                "metadata": result.metadata,
                "duration_ms": result.duration_ms,
            },
        )
        self._checkpoint("tool-result")
        return result

    def _verify(self, summary: str) -> RunResult | None:
        self.state.finish_summary = summary
        self.state.status = RunStatus.VERIFYING
        self.state.verification_attempts += 1
        attempt = self.state.verification_attempts
        self._emit("VERIFICATION_STARTED", {"attempt": attempt, "claim": summary})
        self._checkpoint("verification-start")
        result = self.verifier.verify(self.task, self.workspace)
        reference = self.artifacts.write_verification(attempt, result)
        payload = {
            "attempt": attempt,
            "passed": result.passed,
            "summary": result.summary,
            "changed_paths": list(result.changed_paths),
            "protected_violations": list(result.protected_violations),
            "artifact_ref": reference.relative_to(self.artifacts.run_dir).as_posix(),
            "duration_ms": result.duration_ms,
        }
        if result.passed:
            self._emit("VERIFICATION_PASSED", payload)
            return self._terminate(RunStatus.COMPLETED, "acceptance criteria passed")
        self._emit("VERIFICATION_FAILED", payload)
        command_summary = json.dumps(list(result.commands), ensure_ascii=False)
        bounded, was_truncated = truncate(command_summary, self.config.budgets.max_output_chars)
        metadata = {
            "attempt": attempt,
            "changed_paths": list(result.changed_paths),
            "artifact_ref": payload["artifact_ref"],
            "truncated": was_truncated,
        }
        self.state.observations.append(
            {
                "call_id": f"verification-{attempt}",
                "tool": "verifier",
                "ok": False,
                "content": f"{result.summary}\n{bounded}",
                "error": result.summary,
                "metadata": metadata,
            }
        )
        if attempt >= self.config.budgets.max_verification_attempts:
            return self._terminate(
                RunStatus.FAILED,
                f"verification attempts exhausted: {result.summary}",
            )
        self.state.status = RunStatus.RUNNING
        self._emit("RETRY_LOOP_ENTERED", {"attempt": attempt, "reason": result.summary})
        self._checkpoint("verification-failed")
        return None

    def _check_budgets(self, *, before_turn: bool) -> None:
        self._refresh_elapsed()
        if self.state.elapsed_seconds >= self.config.budgets.max_seconds:
            raise BudgetExceeded(
                f"active wall-time budget exhausted ({self.config.budgets.max_seconds}s)"
            )
        if before_turn and self.state.turn >= self.config.budgets.max_turns:
            raise BudgetExceeded(f"turn budget exhausted ({self.config.budgets.max_turns})")
        if not before_turn and self.state.tool_calls >= self.config.budgets.max_tool_calls:
            raise BudgetExceeded(
                f"tool-call budget exhausted ({self.config.budgets.max_tool_calls})"
            )

    def _refresh_elapsed(self) -> None:
        self.state.elapsed_seconds = self._elapsed_base + (time.monotonic() - self._active_started)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = self.events.append(event_type, payload)
        self.state.event_sequence = event.sequence

    def _checkpoint(self, reason: str) -> None:
        self._refresh_elapsed()
        event = self.events.append(
            "CHECKPOINT_SAVED",
            {
                "reason": reason,
                "status": self.state.status.value,
                "turn": self.state.turn,
                "tool_calls": self.state.tool_calls,
            },
        )
        self.state.event_sequence = event.sequence
        self.state_store.save(self.state)

    def _terminate(self, status: RunStatus, reason: str) -> RunResult:
        self._refresh_elapsed()
        self.state.status = status
        self.state.terminal_reason = reason
        event_type = {
            RunStatus.COMPLETED: "RUN_COMPLETED",
            RunStatus.FAILED: "RUN_FAILED",
            RunStatus.BLOCKED: "RUN_BLOCKED",
            RunStatus.BUDGET_EXHAUSTED: "RUN_BUDGET_EXHAUSTED",
            RunStatus.CANCELLED: "RUN_CANCELLED",
        }[status]
        self._emit(event_type, {"reason": reason, "status": status.value})
        self.state_store.save(self.state)
        summary = self.artifacts.finalize(self.state, self.workspace.root)
        return RunResult(
            run_id=self.state.run_id,
            status=status,
            terminal_reason=reason,
            run_dir=self.artifacts.run_dir,
            workspace=self.workspace.root,
            summary_path=summary,
        )

    @staticmethod
    def _new_run_id(task_id: str) -> str:
        timestamp = utc_now().replace(":", "").replace("-", "").replace("+00:00", "Z")
        timestamp = timestamp.split(".", 1)[0]
        return f"{safe_slug(task_id)}-{timestamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _input_digest(config: HarnessConfig, task: Any, skill: Any) -> str:
        return content_hash(
            {
                "config": HarnessRuntime._frozen_config(config),
                "task": HarnessRuntime._frozen_task(task),
                "skill": HarnessRuntime._frozen_skill(skill),
            }
        )

    @staticmethod
    def _frozen_config_routing(frozen_config_path: Path):
        """Rebuild the LLM Light config and pinned model from a frozen config.

        Returns None for runs recorded before LLM Light existed, which keeps old
        run directories resumable.
        """
        try:
            raw = yaml.safe_load(Path(frozen_config_path).read_text(encoding="utf-8"))
        except (FileNotFoundError, yaml.YAMLError):
            return None
        if not isinstance(raw, dict) or "llm_light" not in raw:
            return None
        return llm_light_from_dict(raw["llm_light"]), str(raw.get("selected_model", ""))

    @staticmethod
    def _write_frozen_inputs(
        artifacts: ArtifactStore,
        config: HarnessConfig,
        task: Any,
        skill: Any,
    ) -> None:
        values = {
            "config.yaml": HarnessRuntime._frozen_config(config),
            "task.yaml": HarnessRuntime._frozen_task(task),
            "skill.yaml": HarnessRuntime._frozen_skill(skill),
        }
        for name, value in values.items():
            atomic_write_text(
                artifacts.input_dir / name,
                yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
            )

    @staticmethod
    def _frozen_config(config: HarnessConfig) -> dict[str, Any]:
        return {
            "version": 1,
            "runs_dir": str(config.runs_dir),
            "budgets": asdict(config.budgets),
            "model_router": {
                "primary": config.router.primary,
                "fallbacks": list(config.router.fallbacks),
                "retries_per_route": config.router.retries_per_route,
                "backoff_seconds": config.router.backoff_seconds,
                "circuit_breaker_failures": config.router.circuit_breaker_failures,
                "routes": {
                    name: {
                        "kind": route.kind,
                        "model": route.model,
                        "base_url": route.base_url,
                        "api_key_env": route.api_key_env,
                        **({"script": str(route.script)} if route.script else {}),
                        "timeout_seconds": route.timeout_seconds,
                        "local": route.local,
                    }
                    for name, route in config.router.routes.items()
                },
            },
            "policy": {
                "approval_mode": config.policy.approval_mode,
                "allowed_commands": [list(command) for command in config.policy.allowed_commands],
                "network_enabled": config.policy.network_enabled,
                "allowed_domains": list(config.policy.allowed_domains),
                "command_timeout_seconds": config.policy.command_timeout_seconds,
                "browser_timeout_seconds": config.policy.browser_timeout_seconds,
            },
            "mcp": [
                {
                    "name": server.name,
                    "command": list(server.command),
                    "protocol_version": server.protocol_version,
                    "timeout_seconds": server.timeout_seconds,
                    "enabled": server.enabled,
                }
                for server in config.mcp_servers
            ],
            "context": {
                "recent_observations": config.context_recent_observations,
                "repo_entries": config.context_repo_entries,
            },
            # Persisted so a resumed run routes identically to the original.
            "llm_light": config.llm_light.as_dict(),
            "selected_model": config.selected_model,
        }

    @staticmethod
    def _frozen_task(task: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "id": task.task_id,
            "objective": task.objective,
            "workspace_seed": str(task.workspace_seed),
            "constraints": list(task.constraints),
            "protected_paths": list(task.protected_paths),
            "acceptance": {
                "commands": [list(command) for command in task.acceptance.commands],
                "require_non_empty_diff": task.acceptance.require_non_empty_diff,
                "timeout_seconds": task.acceptance.timeout_seconds,
            },
            "metadata": task.metadata,
        }

    @staticmethod
    def _frozen_skill(skill: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "id": skill.skill_id,
            "instructions": skill.instructions,
            "allowed_tools": list(skill.allowed_tools),
        }

