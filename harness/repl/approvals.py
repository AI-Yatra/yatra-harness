"""Deciding what the model is allowed to actually do.

The REPL edits the operator's real working directory, so this is the module
that stands between a proposed side effect and the filesystem. Three rules,
in order:

1. The deny-list is absolute. A command matching it is refused and is never
   offered for approval, because a human clicking yes on a prompt is exactly
   the mistake the deny-list exists to prevent.
2. Reads never ask.
3. Everything else asks, unless the mode says otherwise or the operator has
   already said "don't ask again" for this shape of action.

The modes mirror the three that coding agents have converged on: ask about
everything, ask only about running commands, ask about nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from harness.core.contracts import RiskLevel, ToolSpec
from harness.execution.policy import denied_pattern, normalize_command

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import PolicyConfig


class Mode(StrEnum):
    """How much the gate asks about."""

    #: Ask before every write and every command.
    SUGGEST = "suggest"
    #: Edit files freely; still ask before running anything.
    AUTO_EDIT = "auto-edit"
    #: Never ask. The deny-list still applies.
    FULL_AUTO = "full-auto"

    @property
    def label(self) -> str:
        return {
            Mode.SUGGEST: "asks before edits and commands",
            Mode.AUTO_EDIT: "edits freely, asks before commands",
            Mode.FULL_AUTO: "does not ask",
        }[self]


class Verdict(StrEnum):
    ALLOW = "allow"
    ALLOW_ALWAYS = "allow-always"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Request:
    """A side effect waiting on a decision, described for a human."""

    tool: ToolSpec
    arguments: dict[str, Any]
    #: What is being touched: a path for an edit, the command for a run.
    target: str
    #: The one-line question, already phrased.
    question: str
    #: A diff or command preview, or empty when there is nothing to show.
    preview: str = ""
    #: What "don't ask again" would cover, in words.
    always_means: str = ""

    @property
    def always_key(self) -> str:
        """The key "don't ask again" is remembered under.

        Commands are remembered per program and edits per tool, because that
        is the granularity operators actually want: approving `git status`
        should not re-prompt for `git diff`, but approving an edit to one file
        must not silently authorize `rm`.
        """
        if self.tool.name == "run_command":
            return f"{self.tool.name}:{self.target}"
        return self.tool.name


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str
    asked: bool = False


Prompt = Callable[[Request], Verdict]


@dataclass
class Gate:
    """The approval gate for one session."""

    policy: PolicyConfig
    mode: Mode = Mode.SUGGEST
    prompt: Prompt | None = None
    #: Tool names, and (tool, target) pairs, the operator has blanket-approved.
    _always: set[str] = field(default_factory=set)

    def remember(self, key: str) -> None:
        self._always.add(key)

    def forget_all(self) -> None:
        self._always.clear()

    @property
    def standing_approvals(self) -> tuple[str, ...]:
        return tuple(sorted(self._always))

    # ------------------------------------------------------------------ rules

    def check(self, tool: ToolSpec, arguments: dict[str, Any]) -> Decision:
        """Decide whether this call may proceed, asking the operator if needed."""
        refusal = self._hard_refusal(tool, arguments)
        if refusal is not None:
            return Decision(False, refusal)

        if tool.risk is RiskLevel.READ:
            return Decision(True, "reads do not need approval")

        if not self._must_ask(tool.risk):
            return Decision(True, f"{self.mode.value} mode")

        request = self._describe(tool, arguments)
        for key in (tool.name, f"{tool.name}:{request.target}"):
            if key in self._always:
                return Decision(True, f"already approved: {key}")

        if self.prompt is None:
            # Different wording from a refusal on purpose. A model told it was
            # "denied" asks again next turn; told that nobody is there, it
            # stops asking and reports what is blocked instead.
            return Decision(
                False,
                f"{tool.name} needs approval and this session has no way to ask. "
                "Asking again will not help; say what is blocked instead.",
            )

        verdict = self.prompt(request)
        if verdict is Verdict.ALLOW_ALWAYS:
            self.remember(request.always_key)
            return Decision(True, "approved for the rest of the session", asked=True)
        if verdict is Verdict.ALLOW:
            return Decision(True, "approved", asked=True)
        return Decision(
            False,
            f"The operator declined to let you {request.question.rstrip('?').lower()}. "
            "Do not retry it; ask them what to do differently, or continue with "
            "something else.",
            asked=True,
        )

    def _must_ask(self, risk: RiskLevel) -> bool:
        if self.mode is Mode.FULL_AUTO:
            return False
        if self.mode is Mode.AUTO_EDIT:
            return risk in {RiskLevel.EXECUTE, RiskLevel.NETWORK}
        return risk in {RiskLevel.WRITE, RiskLevel.EXECUTE, RiskLevel.NETWORK}

    def _hard_refusal(self, tool: ToolSpec, arguments: dict[str, Any]) -> str | None:
        """What no mode and no operator may authorize."""
        if tool.risk is RiskLevel.NETWORK and not self.policy.network_enabled:
            return "network tools are disabled by this configuration"
        if tool.name != "run_command":
            return None
        command = arguments.get("command")
        if isinstance(command, str):
            command = command.split()
        if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
            return None  # the tool itself reports the shape error more usefully
        pattern = denied_pattern(tuple(command), self.policy.denied_commands)
        if pattern is not None:
            return (
                f"This command matches the deny-list pattern {pattern!r} and cannot be run, "
                "with or without approval. Find another way."
            )
        return None

    # ----------------------------------------------------------- presentation

    def _describe(self, tool: ToolSpec, arguments: dict[str, Any]) -> Request:
        if tool.name == "run_command":
            command = arguments.get("command")
            printable = " ".join(command) if isinstance(command, list) else str(command)
            head = normalize_command(tuple(command))[0] if isinstance(command, list) and command else "?"
            return Request(
                tool=tool,
                arguments=arguments,
                target=head,
                question=f"Run {printable}?",
                preview=printable,
                always_means=f"run any {head} command for the rest of this session",
            )
        path = str(arguments.get("path") or "?")
        verb = "Create or overwrite" if tool.name == "write_file" else "Edit"
        return Request(
            tool=tool,
            arguments=arguments,
            target=path,
            question=f"{verb} {path}?",
            always_means=f"{tool.name} any file for the rest of this session",
        )
