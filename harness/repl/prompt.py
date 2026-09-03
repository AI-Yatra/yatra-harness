"""The system prompt, assembled once per session.

Kept stable and at the front of every request. Providers cache on prompt
prefixes, so the difference between building this once and rebuilding it per
turn is a cache hit on the entire conversation history.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import TYPE_CHECKING

from harness.models.prompting import PromptProfile
from harness.run.instructions import load_repository_instructions

from .approvals import Mode
from .blocks import compose

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import HarnessConfig

def build(
    config: HarnessConfig,
    root: Path,
    *,
    mode: Mode,
    profile: PromptProfile | None = None,
    extra: str = "",
) -> str:
    """The full system prompt for a session rooted at *root*."""
    active = profile or PromptProfile()
    parts = [compose(active), _environment(root, mode, active)]
    conventions = _conventions(config, root, active)
    if conventions:
        parts.append(conventions)
    if extra.strip():
        parts.append(active.block("Operator instructions", extra.strip()))
    return "\n\n".join(parts)


def _environment(root: Path, mode: Mode, profile: PromptProfile) -> str:
    lines = [
        profile.heading("Environment"),
        "",
        f"Working directory: {root}",
        f"Platform: {platform.system()} {platform.machine()}",
        f"Python: {platform.python_version()}",
    ]
    git = _git_summary(root)
    if git:
        lines.append(git)
    lines += [
        "",
        f"Approval mode: {mode.value} - the harness {mode.label}.",
        "Edits and commands may be refused by the operator. A refusal is final "
        "for that action; do not retry it, and do not try to reach the same "
        "effect another way without saying so.",
        *(
            [
                "In this mode nothing you do can change anything, so do not "
                "attempt an edit or a command to find out what would happen. "
                "Read what you need, then answer with the change you would "
                "make and why, concretely enough that the operator can judge "
                "it: the files, what changes in each, and how it would be "
                "checked. They will switch modes if they want it done.",
            ]
            if mode is Mode.PLAN
            else []
        ),
        "Paths you pass to tools are relative to the working directory and "
        "cannot escape it.",
        # One line, not a paragraph. A six-line version of this was measured
        # against two live runs and changed nothing: the model still opened
        # with list_dir and read_file. Tool choice is driven by the tool's own
        # description far more than by house rules in the system prompt, so
        # the persuasion lives there and this is only the tie-breaker.
        "When you do not already know where something lives, retrieve finds "
        "it; grep is for when you know the exact string.",
    ]
    return "\n".join(lines)


def _git_summary(root: Path) -> str:
    """Branch and dirtiness, if this is a git repository.

    Cheap and worth it: a model that knows the branch stops asking, and one
    that knows the tree is dirty stops assuming its own edits are the only
    uncommitted changes.
    """
    from harness.execution.process import run_process  # noqa: PLC0415
    from harness.execution.workspace import git_environment  # noqa: PLC0415

    if not (root / ".git").exists():
        return ""
    try:
        branch = run_process(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            timeout=5,
            max_output_chars=200,
            environment=git_environment(),
        )
        status = run_process(
            ["git", "status", "--porcelain"],
            cwd=root,
            timeout=5,
            max_output_chars=4_000,
            environment=git_environment(),
        )
    except OSError:
        return ""
    if branch.returncode != 0:
        return ""
    changed = len([line for line in status.output.splitlines() if line.strip()])
    state = f"{changed} uncommitted change{'s' if changed != 1 else ''}" if changed else "clean"
    return f"Git branch: {branch.output.strip()} ({state})"


def _conventions(config: HarnessConfig, root: Path, profile: PromptProfile) -> str:
    """The repository's own AGENTS.md or CLAUDE.md, if it has one."""
    if not config.context_instruction_files:
        return ""
    try:
        found = load_repository_instructions(
            root,
            config.context_instruction_files,
            config.context_max_instruction_chars,
        )
    except (OSError, ValueError):
        return ""
    if not found.text.strip():
        return ""
    named = ", ".join(found.sources)
    body = f"From {named} in this repository. Follow them.\n\n{found.text.strip()}"
    return profile.block("Repository conventions", body)
