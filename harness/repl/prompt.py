"""The system prompt, assembled once per session.

Kept stable and at the front of every request. Providers cache on prompt
prefixes, so the difference between building this once and rebuilding it per
turn is a cache hit on the entire conversation history.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import TYPE_CHECKING

from harness.run.instructions import load_repository_instructions

from .approvals import Mode

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import HarnessConfig

BASE = """\
You are a coding agent running in a terminal, in the operator's own working \
directory. You have tools to read, search, edit and run things there.

How to work:
- Read a file before you edit it. edit_file matches text exactly, so guessing \
at the current contents will fail.
- Prefer edit_file over write_file for existing files. write_file replaces the \
whole file and silently discards anything you did not include.
- When you edit code, run the project's tests or linter afterwards if you can, \
and say what the result was.
- Use run_command for anything the shell would do. It takes an argument array \
and there is no shell, so pipes, redirection and globbing do not work; run the \
pieces separately.
- Search before you guess. grep and glob are cheap; a wrong assumption about \
where something lives is not.

How to answer:
- Be concise and concrete. This is a terminal, not a document.
- Answer the question that was asked. Do not add summaries of what you just \
did unless it is not obvious, and do not restate the file you just edited.
- If you are asked a question, answer it. Do not start editing files to \
answer a question about how something works.
- When you cannot do something, say so plainly and say what you would need.
- Never claim a command passed unless you ran it and saw it pass.
"""


def build(
    config: HarnessConfig,
    root: Path,
    *,
    mode: Mode,
    extra: str = "",
) -> str:
    """The full system prompt for a session rooted at *root*."""
    parts = [BASE, _environment(root, mode)]
    conventions = _conventions(config, root)
    if conventions:
        parts.append(conventions)
    if extra.strip():
        parts.append(f"# Operator instructions\n\n{extra.strip()}")
    return "\n\n".join(parts)


def _environment(root: Path, mode: Mode) -> str:
    lines = [
        "# Environment",
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
        "Paths you pass to tools are relative to the working directory and "
        "cannot escape it.",
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


def _conventions(config: HarnessConfig, root: Path) -> str:
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
    return (
        f"# Repository conventions\n\nFrom {named} in this repository. Follow them.\n\n"
        f"{found.text.strip()}"
    )
