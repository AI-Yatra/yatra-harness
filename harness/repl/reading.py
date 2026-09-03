"""Telling a paste from typing.

A terminal hands a pasted block to the program as a run of lines with no
marker around them, so `input()` returns the first and leaves the rest in the
buffer. The operator sees one line become the message and then watches the
remainder answer whatever prompt opens next -- including a permission
question, where a line beginning "1" is an approval nobody gave.

The obvious fix is bracketed paste, where the terminal wraps the block in
`ESC[200~` and `ESC[201~`. It is the right mechanism and it is not enough:
**Git Bash on Windows does not implement it**, which is the terminal this was
reported from, and the same gap breaks multi-line paste in Codex CLI, Gemini
CLI, Claude Code and OpenCode. Python's own readline has a history of
mishandling the sequence as well.

So the marker is used when it arrives, and underneath it there is a mechanism
that needs no terminal support at all: **lines that arrive together are one
message**. A person cannot type a second line within fifty milliseconds of
pressing Enter on the first; a paste delivers the whole block at once. Reading
on a worker thread and taking everything that lands inside that window is the
approach the CLIs above converged on after bracketed paste failed them.

The thread exists only because a blocking read cannot be cancelled. It calls
`input()`, so line editing and history keep working, and the main thread waits
on a queue -- which also means Ctrl-C reaches the main thread while a read is
outstanding, instead of being swallowed by it.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

#: Terminals that implement bracketed paste wrap the block in these.
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
PASTE_ON = "\x1b[?2004h"
PASTE_OFF = "\x1b[?2004l"

#: How long to wait for another line before deciding the operator has stopped.
#: Fifty milliseconds is far longer than a paste needs and far shorter than a
#: person takes to compose a line, which is the whole reason the trick works.
#: It is also the window the other CLIs settled on independently.
PASTE_WINDOW = 0.05

#: A pasted block can be long. This bounds one message so a runaway producer
#: on stdin cannot grow it without limit.
MAX_PASTE_LINES = 500

_EOF = object()


class LineReader:
    """Lines from stdin, grouped by whether they arrived together.

    Started once per session. The worker outlives any single read, because a
    blocking `input()` cannot be interrupted and abandoning one would lose the
    line the operator has already typed.
    """

    def __init__(self, read_line: Callable[[], str] = input) -> None:
        self._read_line = read_line
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while True:
            try:
                self._queue.put(self._read_line())
            except (EOFError, KeyboardInterrupt, OSError, ValueError):
                self._queue.put(_EOF)
                return

    def next_line(self, timeout: float | None = None) -> str | None:
        """The next line, or None at end of input or when the wait expires."""
        try:
            item = self._queue.get(timeout=timeout) if timeout else self._queue.get()
        except queue.Empty:
            return None
        if item is _EOF:
            self._queue.put(_EOF)  # stays at end of input for every later read
            return None
        return str(item)

    def pending(self) -> bool:
        return not self._queue.empty()

    def drain(self) -> int:
        """Discard everything already read but not yet taken.

        Called before a permission question. Whatever is waiting was typed
        before the question existed, so it cannot be an answer to it, and
        letting it answer turns the gate into a formality.
        """
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return dropped
            if item is _EOF:
                self._queue.put(_EOF)
                return dropped
            dropped += 1


def read_message(
    reader: LineReader, *, window: float = PASTE_WINDOW, group: bool = True
) -> str | None:
    """One message, however many lines the operator sent at once.

    Three ways a message can span lines, in the order they are checked. A
    bracketed-paste marker is exact, so it wins where the terminal provides
    one. A trailing backslash is the operator saying so explicitly. Otherwise
    anything that arrives inside the window came from the same paste.

    `group` is off for anything that is not a terminal. A pipe hands over its
    whole contents at once, so timing says nothing about intent there and
    grouping would swallow an entire script into one message. A file of
    commands is a list of messages; a paste is one.
    """
    first = reader.next_line()
    if first is None:
        return None

    if PASTE_START in first:
        return _bracketed(reader, first)

    parts = [first]
    while parts[-1].endswith("\\"):
        parts[-1] = parts[-1][:-1]
        continuation = reader.next_line()
        if continuation is None:
            break
        parts.append(continuation)

    while group and len(parts) < MAX_PASTE_LINES:
        extra = reader.next_line(timeout=window)
        if extra is None:
            break
        parts.append(extra)
    return "\n".join(parts).strip()


def _bracketed(reader: LineReader, first: str) -> str:
    """Collect a block the terminal was kind enough to mark for us."""
    parts = [first.split(PASTE_START, 1)[1]]
    while PASTE_END not in parts[-1] and len(parts) < MAX_PASTE_LINES:
        line = reader.next_line(timeout=1.0)
        if line is None:
            break
        parts.append(line)
    parts[-1] = parts[-1].split(PASTE_END, 1)[0]
    return "\n".join(parts).strip()
