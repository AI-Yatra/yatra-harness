"""Bounded context compiler with deterministic compaction and artifact references."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import HarnessConfig
from .contracts import ModelRequest, RunState, SkillContract, TaskContract, ToolSpec
from .errors import ConfigurationError
from .util import truncate
from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class ContextBuild:
    request: ModelRequest
    character_count: int
    compacted_observations: int
    repo_entries: int


class ContextEngine:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def build(
        self,
        task: TaskContract,
        skill: SkillContract,
        state: RunState,
        workspace: Workspace,
        tools: tuple[ToolSpec, ...],
    ) -> ContextBuild:
        system = self._system(skill)
        repo_map, repo_entries = self._repo_map(workspace)
        recent_count = self.config.context_recent_observations
        old = state.observations[:-recent_count] if len(state.observations) > recent_count else []
        recent = state.observations[-recent_count:]
        compacted = [self._compact_observation(item) for item in old]
        dynamic = {
            "task": {
                "id": task.task_id,
                "objective": task.objective,
                "constraints": list(task.constraints),
                "protected_paths": list(task.protected_paths),
                "acceptance_commands": [list(command) for command in task.acceptance.commands],
            },
            "run": {
                "turn": state.turn,
                "tool_calls": state.tool_calls,
                "verification_attempts": state.verification_attempts,
                "remaining_turns": self.config.budgets.max_turns - state.turn,
                "remaining_tool_calls": self.config.budgets.max_tool_calls - state.tool_calls,
            },
            "repository_map": repo_map,
            "compacted_history": compacted,
            "recent_observations": recent,
        }
        user = json.dumps(dynamic, indent=2, sort_keys=True, ensure_ascii=False)
        tools_size = len(json.dumps([tool.as_model_tool() for tool in tools], ensure_ascii=False))
        available = self.config.budgets.max_context_chars - len(system) - tools_size
        if available < 1_000:
            raise ConfigurationError("context budget is too small for frozen instructions and tool schemas")
        bounded_user, user_truncated = truncate(user, available)
        if user_truncated:
            compacted = [*compacted, {"notice": "dynamic context tail was bounded by max_context_chars"}]
        messages = (
            {"role": "system", "content": system},
            {"role": "user", "content": bounded_user},
        )
        return ContextBuild(
            request=ModelRequest(
                run_id=state.run_id,
                turn=state.turn,
                messages=messages,
                tools=tools,
                max_output_chars=self.config.budgets.max_output_chars,
            ),
            character_count=len(system) + len(bounded_user) + tools_size,
            compacted_observations=len(old) + (1 if user_truncated else 0),
            repo_entries=repo_entries,
        )

    @staticmethod
    def _system(skill: SkillContract) -> str:
        return (
            "You are the decision component inside a controlled coding-agent harness. "
            "You may propose only registered tool calls. Never claim that you directly read, wrote, "
            "executed, browsed, or verified anything. Use finish only when the acceptance criteria "
            "appear satisfied; the harness will independently verify the claim. If verification fails, "
            "use the returned observation to repair the work. When no tool call is needed, return JSON "
            'of the form {"type":"finish","summary":"..."} or '
            '{"type":"clarify","question":"..."}.\n\n'
            f"SKILL {skill.skill_id}:\n{skill.instructions.strip()}"
        )

    def _repo_map(self, workspace: Workspace) -> tuple[list[str], int]:
        entries = []
        for path in sorted(workspace.root.rglob("*")):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(workspace.root).as_posix()
            if path.is_symlink():
                entries.append(relative + " -> <symlink>")
            elif path.is_dir():
                entries.append(relative + "/")
            elif path.is_file():
                entries.append(relative)
            if len(entries) >= self.config.context_repo_entries:
                break
        return entries, len(entries)

    @staticmethod
    def _compact_observation(item: dict) -> dict:
        content = str(item.get("content", ""))
        compact, _ = truncate(content.replace("\n", " "), 240)
        return {
            "call_id": item.get("call_id"),
            "tool": item.get("tool"),
            "ok": item.get("ok"),
            "summary": compact,
            "artifact_ref": (item.get("metadata") or {}).get("artifact_ref"),
        }

