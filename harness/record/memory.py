"""What one session leaves for the next.

A session persisted its transcript and nothing else, so every conversation
started from zero: the same questions answered, the same conventions
rediscovered, the same dead end walked into twice. `Session` holds an id, a
directory and a list of turns, and none of that is memory.

Memory here is a markdown file in the project, not a vector store. That is a
deliberate choice and not a shortcut. A file can be read, edited and deleted
by the person it describes; it diffs; a team can commit it or ignore it; and
when it is wrong the fix is obvious. An embedding index of remembered facts is
none of those things, and for the amount a repository actually needs
remembering -- tens of lines, not thousands -- it would be machinery standing
in for a paragraph.

Three failures decide the design, and each is a rule below.

**Staleness.** A remembered fact outlives the thing it describes. "The tests
live in `spec/`" survives the day someone renames the directory, and the agent
then acts on it confidently. Every entry carries the date it was written and
is shown with its age, so the model reads a claim about the repository next to
how old the claim is.

**Growth.** A memory that only grows becomes the context problem it was meant
to solve. The file is capped, and the oldest entries fall off the end.

**Junk.** The expensive failure is not forgetting, it is remembering the
wrong thing: a fact true only of one branch, or a preference the operator
stated once and changed their mind about. The `remember` tool says what is
worth keeping, and the file is small enough to read at a glance.

Memory is scoped to the project, not the session, because the project is the
stable identity. Keying it to a session id would mean it never outlives the
thing it exists to outlive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

#: Where a project keeps what has been learned about it. Beside the settings,
#: because both describe the project rather than the run.
MEMORY_FILE = Path(".yatra") / "memory.md"

#: Entries, not bytes. Past this the oldest fall off, because a memory that
#: only grows becomes the context problem it was meant to solve.
MAX_ENTRIES = 40

#: A single entry is a sentence or two. Anything longer is documentation and
#: belongs in the repository proper, where it can be reviewed.
MAX_ENTRY_CHARS = 500

#: Beyond this an entry is shown with a warning rather than trusted silently.
#: Repositories move; a claim from two months ago is a lead, not a fact.
STALE_AFTER_DAYS = 45

_ENTRY = re.compile(r"^- \((\d{4}-\d{2}-\d{2})\)\s+(.*)$")

HEADER = (
    "# Project memory\n"
    "\n"
    "Written by the agent, read at the start of every session. Edit or delete\n"
    "anything here freely; it is a note to itself, not a record of anything.\n"
)


@dataclass(frozen=True, slots=True)
class Entry:
    written: date
    text: str

    @property
    def age_days(self) -> int:
        return (datetime.now(UTC).date() - self.written).days

    @property
    def stale(self) -> bool:
        return self.age_days > STALE_AFTER_DAYS

    def render(self) -> str:
        return f"- ({self.written.isoformat()}) {self.text}"


def path_for(root: Path) -> Path:
    return Path(root) / MEMORY_FILE


def load(root: Path) -> list[Entry]:
    """Everything remembered about this project, oldest first."""
    path = path_for(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries = []
    for line in text.splitlines():
        found = _ENTRY.match(line.strip())
        if not found:
            continue
        try:
            written = date.fromisoformat(found.group(1))
        except ValueError:
            continue
        body = found.group(2).strip()
        if body:
            entries.append(Entry(written, body))
    return entries


def remember(root: Path, text: str) -> Entry:
    """Add one fact, and drop the oldest if the file is full."""
    body = " ".join(text.split())[:MAX_ENTRY_CHARS].strip()
    if not body:
        raise ValueError("there is nothing to remember")
    entries = [entry for entry in load(root) if entry.text.lower() != body.lower()]
    entries.append(Entry(datetime.now(UTC).date(), body))
    save(root, entries[-MAX_ENTRIES:])
    return entries[-1]


def forget(root: Path, needle: str) -> int:
    """Drop every entry containing *needle*. Returns how many went.

    Present because the operator has to be able to correct the agent's memory
    without opening a file, and because a wrong memory is worse than none.
    """
    if not needle.strip():
        return 0
    entries = load(root)
    kept = [entry for entry in entries if needle.lower() not in entry.text.lower()]
    if len(kept) != len(entries):
        save(root, kept)
    return len(entries) - len(kept)


def save(root: Path, entries: list[Entry]) -> None:
    path = path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(entry.render() for entry in entries)
    path.write_text(f"{HEADER}\n{body}\n" if body else HEADER, encoding="utf-8")


def as_prompt(root: Path, *, limit: int = MAX_ENTRIES) -> str:
    """The block that goes into the system prompt, newest first.

    Newest first because that is the order they should be trusted in, and each
    stale line says so rather than being dropped. Dropping it would hide the
    fact that the agent believes something out of date; saying so lets the
    model weigh it and lets the operator see what to correct.
    """
    entries = load(root)
    if not entries:
        return ""
    lines = []
    for entry in reversed(entries[-limit:]):
        age = f"{entry.age_days}d ago" if entry.age_days else "today"
        mark = f"{age}, may be out of date" if entry.stale else age
        lines.append(f"- {entry.text} ({mark})")
    return (
        "Things earlier sessions in this repository learned. Treat them as "
        "leads rather than as facts, and check anything marked out of date "
        "before relying on it.\n\n" + "\n".join(lines)
    )
