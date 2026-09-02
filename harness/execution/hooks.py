"""Operator commands that run when the agent does something.

Formatting after every edit, a lint gate, a notification, an audit line. All of
these are things people already automate around a coding agent, and without a
place to put them they end up as instructions in the prompt, where they are
advice the model may skip rather than something that happens.

These observe, they do not decide. A hook that could veto a tool call would be
a second authority over the same question `Gate` already answers, and two
places deciding one thing is how a permission system stops being trustworthy:
the operator reads the gate, the deny-list disagrees, and neither is wrong on
its own. So a hook runs after the call, learns what happened, and cannot change
it. If that ever needs to change, the honest shape is asymmetric, able to add a
refusal on top of an approval and never to grant one.

A hook is an ordinary command from the operator's own config, which is the same
trust as the deny-list and the allowlist they already write there. It is not
model input and the model cannot add one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from harness.core.errors import ConfigurationError
from harness.execution.process import run_process

#: When a hook can run. Deliberately few: these are the moments with something
#: worth reacting to, and a longer list would be surface nobody asked for.
EVENTS = ("tool_start", "tool_end", "turn_end")

#: A hook that has not finished by now is holding up the session, which is not
#: a trade worth making for something that cannot change the outcome anyway.
DEFAULT_TIMEOUT = 30.0

MAX_OUTPUT_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class Hook:
    """One command, and when to run it."""

    event: str
    command: tuple[str, ...]
    #: Glob against the tool name. Empty means every tool.
    match: str = ""
    timeout: float = DEFAULT_TIMEOUT
    name: str = ""

    def applies(self, event: str, tool: str) -> bool:
        if event != self.event:
            return False
        return not self.match or fnmatch(tool, self.match)

    @property
    def label(self) -> str:
        return self.name or " ".join(self.command[:3])


@dataclass
class HookReport:
    """What running the hooks produced, for the operator to see."""

    hook: Hook
    ok: bool
    output: str


def parse_hooks(raw: object, path: str) -> tuple[Hook, ...]:
    """Parse the `hooks` list, refusing anything that is not one.

    Validated at load time like the rest of the config, so a hook that would
    never fire fails the file rather than silently not running.
    """
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ConfigurationError(f"{path} must be a list of hooks")
    hooks: list[Hook] = []
    for index, entry in enumerate(raw):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{where} must be a mapping")
        unknown = sorted(set(entry) - {"event", "run", "match", "timeout", "name"})
        if unknown:
            raise ConfigurationError(f"{where} has unknown keys: {', '.join(unknown)}")
        event = str(entry.get("event", ""))
        if event not in EVENTS:
            raise ConfigurationError(
                f"{where}.event must be one of {', '.join(EVENTS)}; got {event!r}"
            )
        command = entry.get("run")
        if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
            raise ConfigurationError(f"{where}.run must be a non-empty array of strings")
        timeout = float(entry.get("timeout", DEFAULT_TIMEOUT))
        if timeout <= 0:
            raise ConfigurationError(f"{where}.timeout must be greater than zero")
        hooks.append(
            Hook(
                event=event,
                command=tuple(command),
                match=str(entry.get("match", "")),
                timeout=timeout,
                name=str(entry.get("name", "")),
            )
        )
    return tuple(hooks)


@dataclass
class HookRunner:
    """Runs the configured hooks and keeps them out of the session's way."""

    hooks: tuple[Hook, ...] = ()
    root: Path = field(default_factory=Path.cwd)
    #: Hooks that failed are disabled for the session. A formatter that is not
    #: installed would otherwise fail on every tool call for the rest of the
    #: conversation, and the tenth identical error tells nobody anything.
    _broken: set[str] = field(default_factory=set)

    def fire(self, event: str, *, tool: str = "", payload: dict[str, Any] | None = None) -> list[HookReport]:
        """Run every hook for this event. Never raises."""
        reports: list[HookReport] = []
        for hook in self.hooks:
            if not hook.applies(event, tool) or hook.label in self._broken:
                continue
            reports.append(self._run(hook, event, tool, payload or {}))
        return reports

    def _run(self, hook: Hook, event: str, tool: str, payload: dict[str, Any]) -> HookReport:
        context = json.dumps({"event": event, "tool": tool, **payload}, sort_keys=True)
        try:
            result = run_process(
                list(hook.command),
                cwd=self.root,
                timeout=hook.timeout,
                max_output_chars=MAX_OUTPUT_CHARS,
                environment=_hook_environment(event, tool),
                input_text=context,
            )
        except OSError as exc:
            self._broken.add(hook.label)
            return HookReport(hook, False, f"{type(exc).__name__}: {exc}")
        if result.timed_out:
            self._broken.add(hook.label)
            return HookReport(hook, False, f"timed out after {hook.timeout:g}s")
        if result.returncode != 0:
            # Not disabled on a non-zero exit: a lint hook failing is the hook
            # working, and it should keep reporting on the next edit too.
            return HookReport(hook, False, result.output.strip())
        return HookReport(hook, True, result.output.strip())


def _hook_environment(event: str, tool: str) -> dict[str, str]:
    """The environment a hook gets.

    Inherited, because a hook is the operator's own command and a formatter
    without PATH or a virtualenv is not one. The two variables added are the
    context most hooks want without having to parse the JSON on stdin.
    """
    import os  # noqa: PLC0415 - local so the module imports cleanly anywhere

    return {**os.environ, "HARNESS_EVENT": event, "HARNESS_TOOL": tool}


def hook_events(hooks: Sequence[Hook]) -> set[str]:
    """Which events anything is listening for, so nothing is assembled in vain."""
    return {hook.event for hook in hooks}
