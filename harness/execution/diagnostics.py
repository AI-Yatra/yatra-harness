"""What the project's own checker says about a file the agent just changed.

Without this the agent is editing blind. It writes a change, the change is
applied, and nothing tells it that the file no longer imports, no longer type
checks, or breaks a lint rule the repository enforces. It finds out later, if
it happens to run the right command, or it never finds out and hands back
something that does not compile.

**This is not a hook, and the difference is the whole point.** A hook observes
and its output deliberately never reaches the model, because a formatter that
is not installed is the operator's problem and a model told about it tries to
fix it. Diagnostics are the opposite: the report is *about the model's own
edit*, it is exactly what the model needs, and it is useless to anyone else.
One mechanism could not have both rules.

A CLI checker rather than a language server, deliberately. A language server
means a client, a lifecycle, a protocol and a per-language install, and the
projects that most want this already have `ruff`, `tsc` or `mypy` configured
and know how to run them. The tradeoff is real -- no go-to-definition, no call
graph -- and it is documented rather than hidden.

The one rule everything here is shaped by: **a diagnostic is not a failed
edit.** Another agent shipped this exact bug -- its model read the diagnostics
attached to a successful edit and concluded the edit had not been applied, so
it wrote the same change again. So the edit's own result comes first and
stands on its own, the report is separated from it and labelled, and the
tool's success flag never changes because a checker had something to say.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.errors import ConfigurationError
from harness.execution.process import run_process

#: Long enough for a type checker on one file, short enough that a hung
#: checker does not become a hung turn.
DEFAULT_TIMEOUT = 20.0

#: A checker that reports fifty problems is reporting a broken setup, not a
#: broken edit, and pasting all of it buries the edit that caused it.
DEFAULT_MAX_LINES = 20

#: Where the changed file's path is substituted. Without it the command is
#: run as written, which is what a whole-project checker wants.
FILE_TOKEN = "{file}"  # noqa: S105 - a substitution marker, not a secret


@dataclass(frozen=True, slots=True)
class DiagnosticsConfig:
    """The project's checker, and when to run it."""

    command: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    max_lines: int = DEFAULT_MAX_LINES
    #: Only files matching one of these are checked, so a Python type checker
    #: is not run over a Markdown edit.
    suffixes: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def applies_to(self, path: str) -> bool:
        if not self.enabled:
            return False
        return not self.suffixes or Path(path).suffix.lower() in self.suffixes


@dataclass(frozen=True, slots=True)
class Report:
    """What the checker said. Empty output means it was happy."""

    output: str
    #: True when the checker itself could not run: not installed, or it
    #: crashed. Told to the operator and not to the model, because a missing
    #: linter is the operator's problem and a model told about it tries to
    #: install one.
    broken: bool = False

    @property
    def clean(self) -> bool:
        return not self.output.strip()


def build_command(config: DiagnosticsConfig, path: str) -> list[str]:
    """The command for one file, with `{file}` filled in."""
    if FILE_TOKEN not in " ".join(config.command):
        return [*config.command, path]
    return [part.replace(FILE_TOKEN, path) for part in config.command]


def check(config: DiagnosticsConfig, root: Path, path: str) -> Report:
    """Run the checker over one changed file. Never raises."""
    if not config.applies_to(path):
        return Report("")
    try:
        result = run_process(
            build_command(config, path),
            cwd=root,
            timeout=config.timeout,
            max_output_chars=8_000,
        )
    except OSError as exc:
        return Report(f"{type(exc).__name__}: {exc}", broken=True)
    if result.timed_out:
        return Report(f"the checker did not finish in {config.timeout:g}s", broken=True)
    # The exit code is the signal, not the output. Checkers announce success
    # in prose -- ruff prints "All checks passed!", mypy "Success: no issues
    # found" -- and reading that as a finding would attach a report to every
    # clean edit and teach the model to ignore the section entirely.
    if result.returncode == 0:
        return Report("")
    text = result.output.strip()
    lines = text.splitlines()
    trimmed = lines[: config.max_lines]
    if len(lines) > config.max_lines:
        trimmed.append(f"... and {len(lines) - config.max_lines} more")
    return Report("\n".join(trimmed))


def attach(content: str, report: Report) -> str:
    """Put the report after the edit's own result, clearly separated.

    The wording is doing real work. It says the edit was applied, names what
    follows as a separate report, and tells the model not to repeat the edit.
    Another agent's model read diagnostics attached to a successful edit,
    concluded the edit had failed, and applied it a second time.
    """
    if report.broken or report.clean:
        return content
    return (
        f"{content}\n\n"
        "--- The edit above was applied. This is a separate report from the "
        "project's checker about the file as it now stands. Fix what it names; "
        "do not repeat the edit.\n"
        f"{report.output}"
    )


def diagnostics_config_from_dict(
    raw: dict[str, Any] | None, path: str = "diagnostics"
) -> DiagnosticsConfig:
    from harness.core import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(value, {"command", "timeout", "max_lines", "suffixes"}, path)
    raw_command = value.get("command", [])
    if isinstance(raw_command, str):
        # A string is what an operator writes first. Accepting it costs a line
        # and saves a confusing failure on the most likely spelling.
        raw_command = shlex.split(raw_command)
    if not isinstance(raw_command, list) or not all(isinstance(p, str) for p in raw_command):
        raise ConfigurationError(f"{path}.command must be an array of strings")
    timeout = schema.number(value.get("timeout", DEFAULT_TIMEOUT), f"{path}.timeout", minimum=0.1)
    return DiagnosticsConfig(
        command=tuple(raw_command),
        timeout=timeout,
        max_lines=schema.integer(
            value.get("max_lines", DEFAULT_MAX_LINES), f"{path}.max_lines", minimum=1
        ),
        suffixes=tuple(
            str(item).lower()
            for item in schema.string_list(value.get("suffixes", []), f"{path}.suffixes")
        ),
    )
