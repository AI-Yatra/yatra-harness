"""Capability authorization separate from technical tool availability."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import PolicyConfig
from .contracts import RiskLevel, ToolSpec


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


ApprovalCallback = Callable[[ToolSpec, dict[str, Any], str], bool]


class PolicyEngine:
    def __init__(
        self,
        config: PolicyConfig,
        allowed_tools: tuple[str, ...],
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.config = config
        self.allowed_tools = frozenset(allowed_tools)
        self.approval_callback = approval_callback

    def evaluate(self, tool: ToolSpec, arguments: dict[str, Any]) -> PolicyDecision:
        if tool.name not in self.allowed_tools:
            return PolicyDecision(False, False, f"tool {tool.name!r} is not enabled by the skill")
        if tool.risk is RiskLevel.NETWORK and not self.config.network_enabled:
            return PolicyDecision(False, False, "network tools are disabled by policy")
        if tool.name == "run_command":
            command = arguments.get("command")
            if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
                return PolicyDecision(False, False, "run_command requires a string array")
            if not self._command_allowed(tuple(command)):
                return PolicyDecision(False, False, "command is not on the configured allowlist")
        requires_approval = self._requires_approval(tool.risk)
        if not requires_approval:
            return PolicyDecision(True, False, "allowed by policy")
        if self.approval_callback is None:
            return PolicyDecision(False, True, "approval is required but no approver is available")
        if self.approval_callback(tool, arguments, f"authorize {tool.risk.value} capability"):
            return PolicyDecision(True, True, "approved by operator")
        return PolicyDecision(False, True, "operator denied approval")

    def _command_allowed(self, command: tuple[str, ...]) -> bool:
        # Mirror the execution normalization in tools._normalize_command so
        # policy and execution agree about what a command *is*. A `python3`
        # spelling must be judged exactly like `python`, because both run the
        # same interpreter.
        normalized = self._normalize_command(command)
        return any(
            normalized[: len(prefix)] == prefix
            for prefix in self.config.allowed_commands
        )

    @staticmethod
    def _normalize_command(command: tuple[str, ...]) -> tuple[str, ...]:
        if command and command[0] in {"python", "python3"}:
            return ("python", *command[1:])
        return command

    def _requires_approval(self, risk: RiskLevel) -> bool:
        if self.config.approval_mode == "never":
            return False
        if self.config.approval_mode == "always":
            return True
        return risk in {RiskLevel.WRITE, RiskLevel.EXECUTE, RiskLevel.NETWORK}

