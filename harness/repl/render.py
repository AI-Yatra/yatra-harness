"""What the operator sees.

Four rules shape everything here.

**The screen shows what happened, not what was sent to the model.** A 400-line
file read is one line on screen and 400 in the context.

**Nothing is printed that the terminal cannot encode.** A Windows console in
cp1252 raising mid-stream would end a session over decoration, so every glyph
has an ASCII fallback chosen once at startup.

**One grid.** Every line in the session shares a two-column gutter: marks hang
in it, content starts after it. Tool names, model prose and status lines all
begin at the same column, and nested detail sits exactly one gutter deeper.
Alignment is the only part of the hierarchy that survives a pipe, `NO_COLOR`,
or a monochrome terminal, so it carries the structure and colour only
reinforces it. The palette itself is in `theme.py`, with the measurements.

**Prose looks the same however it arrived.** Streamed text used to be written
straight to the terminal, unwrapped and unstyled, while a blocking response
went through the markdown renderer. The same model saying the same words
produced two different screens depending on a transport setting. `Prose` below
is the single renderer both paths feed, so the only difference streaming makes
is when the characters appear.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass

from .approvals import Request, Verdict
from .theme import GUTTER, INDENT, RESET, RIGHT_MARGIN, THEME, Theme

#: The widest comfortable measure. Beyond about this many columns the eye
#: loses the start of the next line on the way back, so a wide window gets
#: whitespace rather than longer lines.
MAX_MEASURE = 100

MIN_MEASURE = 40

#: `1.` or `3)` at the start of a line. Bounded to two digits: a longer run is
#: far more likely to be a version number or a date than a list.
ORDERED = re.compile(r"(\d{1,2}[.)])\s")


@dataclass(frozen=True, slots=True)
class Glyphs:
    bullet: str
    branch: str
    bar: str
    sep: str
    rule: str
    dot: str
    spinner: tuple[str, ...]

    @classmethod
    def for_stream(cls, stream) -> Glyphs:
        # One check for the whole set: a console that can encode any of these
        # can encode all of them, and mixing the two sets looks like a bug.
        if _encodable("⏺⎿│·─•⠋", stream):
            return cls("⏺", "⎿", "│", "·", "─", "•", tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
        return cls("*", "`-", "|", "-", "-", "-", tuple("|/-\\"))


def _encodable(text: str, stream) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Console:
    """Colour, width, and the one place anything reaches the terminal.

    The role methods are named for meaning rather than appearance, so what a
    line *is* is decided here and what it looks like is decided once, in
    `theme.py`.
    """

    def __init__(self, stream=None, *, colour: bool | None = None, theme: Theme = THEME) -> None:
        self.stream = stream or sys.stdout
        if colour is None:
            colour = (
                self.stream.isatty()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
            )
        self.colour = bool(colour)
        self.theme = theme
        self.glyphs = Glyphs.for_stream(self.stream)

    @property
    def width(self) -> int:
        try:
            columns = shutil.get_terminal_size((MAX_MEASURE, 24)).columns
        except OSError:
            columns = MAX_MEASURE
        return max(MIN_MEASURE, min(columns, MAX_MEASURE))

    @property
    def measure(self) -> int:
        """Columns available to content, once the grid has taken its margins."""
        return max(20, self.width - GUTTER - RIGHT_MARGIN)

    def paint(self, text: str, *codes: str) -> str:
        if not self.colour or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}{RESET}"

    # ------------------------------------------------------------------ roles

    def accent(self, text: str) -> str:
        """The agent's own marks, and anything the operator can act on."""
        return self.paint(text, self.theme.accent)

    def muted(self, text: str) -> str:
        """Secondary detail: arguments, counts, paths, hints."""
        return self.paint(text, self.theme.muted)

    def success(self, text: str) -> str:
        return self.paint(text, self.theme.success)

    def failure(self, text: str) -> str:
        return self.paint(text, self.theme.failure)

    def strong(self, text: str) -> str:
        """Emphasis that survives a terminal with no colour at all."""
        return self.paint(text, self.theme.strong)

    # ----------------------------------------------------------------- output

    def write(self, text: str = "") -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (UnicodeEncodeError, ValueError):
            # A stream that cannot encode a glyph must not end the session.
            self.stream.write(text.encode("ascii", "replace").decode("ascii"))
            self.stream.flush()

    def line(self, text: str = "") -> None:
        self.write(text + "\n")


class Spinner:
    """A live "working" line that erases itself.

    Runs on its own thread so it keeps moving while the main thread blocks on
    a model request. Everything it prints stays on one line and is erased
    before anything else is written, so it never interleaves with output.
    """

    def __init__(self, console: Console, label: str = "thinking") -> None:
        self.console = console
        self.label = label
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = console.colour and console.stream.isatty()

    def __enter__(self) -> Spinner:
        if self._active:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _spin(self) -> None:
        frames = self.console.glyphs.spinner
        index = 0
        while not self._stop.wait(0.09):
            elapsed = int(time.monotonic() - self.started)
            frame = frames[index % len(frames)]
            index += 1
            sep = self.console.glyphs.sep
            hint = self.console.muted(f"{self.label} {elapsed}s {sep} ctrl-c to interrupt")
            self.console.write(f"\r\033[2K{self.console.accent(frame)} {hint}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.4)
            self._thread = None
        if self._active:
            self.console.write("\r\033[2K")


# ── prose ──────────────────────────────────────────────────────────────────


class Prose:
    """Model prose, rendered a line at a time.

    Fed either one whole response or a token at a time; the output is
    identical, which is the point. A line is emitted as soon as it is known to
    be complete -- at a newline, or when it has grown past the measure and can
    be broken at a space -- so streaming still arrives live.

    Only enough markdown to read comfortably. Headings and bullets get weight,
    fenced code is dimmed and left exactly as written. A full markdown renderer
    would reflow code and mangle it, which is worse than not rendering markdown
    at all.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._pending = ""
        self._in_code = False
        self._wrote = False
        #: Set once the current line's shape is known, so a line that is
        #: wrapped in pieces keeps one hanging indent throughout.
        self._continuation = False
        #: The column a wrapped line resumes at. A list item hangs under its
        #: own text, so the width of its marker decides it.
        self._hang = INDENT

    @property
    def wrote_anything(self) -> bool:
        return self._wrote

    def feed(self, text: str) -> None:
        if not text:
            return
        self._pending += text
        while True:
            if "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self._flush(line)
                self._continuation = False
                continue
            # A paragraph with no newline in sight still has to appear as it
            # arrives, so it is broken at the last space that fits.
            head = self._breakable(self._pending)
            if head is None:
                return
            chunk, self._pending = head
            self._emit(chunk, final=False)
            self._continuation = True

    def close(self) -> None:
        """Flush whatever is left, and reset for the next turn."""
        if self._pending.strip():
            self._flush(self._pending)
        self._pending = ""
        self._continuation = False
        self._in_code = False

    def _flush(self, line: str) -> None:
        """Emit one complete logical line, wrapped.

        Both paths go through here, and that is what makes them agree.
        Wrapping used to happen only while a line was still accumulating, so a
        whole response -- which arrives already newline-terminated -- was
        emitted unwrapped, while the same text streamed a token at a time came
        out wrapped. Same words, two different screens.
        """
        while True:
            head = self._breakable(line)
            if head is None:
                break
            chunk, line = head
            self._emit(chunk, final=False)
            self._continuation = True
        self._emit(line, final=True)

    # ------------------------------------------------------------- internals

    def _breakable(self, text: str) -> tuple[str, str] | None:
        """Split *text* at the last space that fits the measure, if it must."""
        if self._in_code:
            return None
        limit = self.console.measure - (self._hang - GUTTER if self._continuation else 0)
        if len(text) <= limit:
            return None
        cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            return None
        return text[:cut], text[cut + 1 :]

    def _emit(self, raw: str, *, final: bool) -> None:
        console = self.console
        stripped = raw.strip()

        if stripped.startswith("```"):
            # The fence itself is not drawn. The bar down the left already
            # says where the block starts and stops, and a rendered fence was
            # a second mark competing with the bullet for the same meaning.
            self._in_code = not self._in_code
            return
        if self._in_code:
            # Verbatim, and clipped rather than wrapped: a reflowed code line
            # is a wrong code line.
            body = raw[: console.measure - 2]
            self._line(console.muted(f"{console.glyphs.bar} ") + body)
            return
        if not stripped:
            if final:
                self._line("")
            return
        if self._continuation:
            self._line(" " * (self._hang - GUTTER) + stripped)
            return
        self._hang = INDENT
        if stripped.startswith("#"):
            self._line(console.strong(stripped.lstrip("# ").strip()))
            return
        if stripped.startswith(("- ", "* ", "+ ")):
            self._line(console.accent(console.glyphs.dot) + " " + stripped[2:])
            return
        ordered = ORDERED.match(stripped)
        if ordered:
            # A numbered item gets the same treatment as a bulleted one: the
            # marker in the accent, the text at the terminal's own colour.
            # Models mix `-` and `1.` in one answer, and rendering only the
            # first left two list styles on the same screen. The number itself
            # is kept -- unlike a dash it carries meaning -- so the hanging
            # indent is the marker's own width rather than a fixed one.
            marker = ordered.group(1)
            self._hang = GUTTER + len(marker) + 1
            self._line(console.accent(marker) + " " + stripped[len(marker) :].lstrip())
            return
        self._line(stripped)

    def _line(self, body: str) -> None:
        self._wrote = True
        self.console.line(" " * GUTTER + body if body else "")


# ── the renderer ───────────────────────────────────────────────────────────


class Renderer:
    """Draws turns, tool cards, diffs and prompts, all on the one grid."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.prose = Prose(console)

    # -------------------------------------------------------------- assistant

    def assistant_text(self, text: str) -> None:
        """A whole response at once. Same renderer the streamed path uses."""
        if not text.strip():
            return
        self.console.line()
        self.prose.feed(text)
        self.prose.close()
        self.console.line()

    def assistant_delta(self, text: str) -> None:
        """One fragment of a streamed response."""
        self.prose.feed(text)

    def assistant_done(self) -> None:
        """End of a streamed response, whether or not anything arrived."""
        self.prose.close()

    # ------------------------------------------------------------- tool cards

    def tool_start(self, name: str, display: str) -> None:
        """The mark hangs in the gutter; the name starts where prose does."""
        console = self.console
        label = console.strong(_pretty(name))
        room = console.measure - len(_pretty(name)) - 2
        target = console.muted(f"({_clip(display, room)})") if display else ""
        console.line(f"{console.accent(console.glyphs.bullet)} {label}{target}")

    def tool_result(self, detail: str, *, ok: bool = True) -> None:
        text = detail.strip() or ("done" if ok else "failed")
        tint = self.console.muted if ok else self.console.failure
        self._detail(text, tint)

    def tool_denied(self, reason: str) -> None:
        self._detail(reason.splitlines()[0], self.console.failure)

    def _detail(self, text: str, tint) -> None:
        """Nested one gutter under the tool card it belongs to."""
        console = self.console
        room = console.measure - GUTTER
        for index, line in enumerate(text.splitlines()[:12]):
            console.line(self._nest(index) + tint(_clip(line, room)))

    def _nest(self, index: int) -> str:
        """The gutter for detail hanging under a tool card.

        The branch mark on the first line only; every line after it aligns
        under that mark's text rather than repeating it, so a block of output
        reads as one thing.
        """
        if index:
            return " " * INDENT
        return f"{' ' * GUTTER}{self.console.muted(self.console.glyphs.branch)} "

    def diff(self, text: str, *, limit: int = 24) -> None:
        """A unified diff, aligned with tool detail and truncated to stay read."""
        console = self.console
        lines = [line for line in text.splitlines() if not line.startswith(("---", "+++"))]
        room = console.measure - GUTTER
        shown = lines[:limit]
        if len(lines) > limit:
            shown = [*shown, f" {len(lines) - limit} more lines"]
        for index, line in enumerate(shown):
            if line.startswith("+"):
                painted = console.success(_clip(line, room))
            elif line.startswith("-"):
                painted = console.failure(_clip(line, room))
            else:
                painted = console.muted(_clip(line, room))
            console.line(self._nest(index) + painted)

    # ----------------------------------------------------------------- status

    def notice(self, text: str) -> None:
        """The program talking, not the model. Same column as prose."""
        self.console.line(" " * GUTTER + self.console.muted(text))

    def error(self, text: str) -> None:
        self.console.line(" " * GUTTER + self.console.failure(text))

    def rule(self) -> None:
        mark = self.console.glyphs.rule
        self.console.line(" " * GUTTER + self.console.muted(mark * (self.console.measure // 2)))

    # ------------------------------------------------------------- approvals

    def ask(self, request: Request) -> Verdict:
        """The permission prompt. Defaults to no on anything unexpected."""
        console = self.console
        pad = " " * GUTTER
        console.line()
        console.line(pad + console.strong(request.question))
        if request.preview:
            console.line(
                " " * INDENT + console.muted(_clip(f"$ {request.preview}", console.measure - GUTTER))
            )
        options = (
            ("1", "Yes"),
            ("2", f"Yes, and {request.always_means}"),
            ("3", "No, tell the model what to do instead"),
        )
        for key, text in options:
            console.line(" " * INDENT + f"{console.accent(key)}  {text}")
        while True:
            try:
                console.write(pad + console.muted("choose 1-3 ") + console.accent("> "))
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.line()
                return Verdict.DENY
            # The operator's Enter supplies the newline at a real terminal but
            # not when input is piped, and without it the next line lands on
            # top of the prompt.
            if not console.stream.isatty():
                console.line()
            if answer in {"1", "y", "yes"}:
                return Verdict.ALLOW
            if answer in {"2", "a", "always"}:
                return Verdict.ALLOW_ALWAYS
            if answer in {"3", "n", "no", ""}:
                return Verdict.DENY


def _clip(text: str, room: int) -> str:
    """Cut a line to the measure, marking that something was cut."""
    room = max(8, room)
    return text if len(text) <= room else text[: room - 1] + "…"


def _pretty(name: str) -> str:
    """`read_file` reads better as `Read` in a tool card."""
    return {
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Edit",
        "list_dir": "List",
        "run_command": "Run",
        "glob": "Glob",
        "grep": "Grep",
    }.get(name, name)
