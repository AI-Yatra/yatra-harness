"""Repository instruction files, loaded into the model's system prompt.

An agent that has not read a repository's conventions invents them. `AGENTS.md`
is where a repository writes those conventions down, so the harness reads it
and puts it in front of the model rather than hoping the model goes looking.

These files are content inside the run workspace, which means a task can
influence what the model is told. That is the point -- it is how a repository
describes itself -- but it is also why the text is budgeted, labelled with the
file it came from, and placed after the harness's own instructions rather than
before them. The authority boundary in docs/ARCHITECTURE.md still holds: this
text can inform the model, and it cannot enable a tool, widen the command
allowlist, or satisfy the verifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.core.util import truncate


@dataclass(frozen=True, slots=True)
class RepositoryInstructions:
    text: str
    sources: tuple[str, ...]
    truncated: bool = False


def load_repository_instructions(
    root: Path, names: tuple[str, ...], max_chars: int
) -> RepositoryInstructions:
    """Read the configured instruction files at `root`, in the order given.

    Every failure mode here is non-fatal by design. A missing file is the
    normal case, and an unreadable one must not take a run down over
    documentation.
    """
    sections: list[str] = []
    sources: list[str] = []
    for name in names:
        path = _within(root, name)
        if path is None or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not body:
            continue
        sections.append(f"--- {name} ---\n{body}")
        sources.append(name)
    if not sections:
        return RepositoryInstructions("", (), False)
    text, was_truncated = truncate("\n\n".join(sections), max_chars)
    return RepositoryInstructions(text, tuple(sources), was_truncated)


def _within(root: Path, name: str) -> Path | None:
    """Resolve `name` under `root`, or None if it points anywhere else.

    The names are operator-configured rather than model-chosen, but they still
    reach the filesystem, so they get the same containment as every other path
    the harness resolves.
    """
    if not name or Path(name).is_absolute():
        return None
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate
