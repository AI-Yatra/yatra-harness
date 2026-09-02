"""What the operator sees.

Two rules shape everything here. The screen shows what happened, not what
was sent to the model: a 400-line file read is one line on screen and 400 in
the context. And nothing is ever printed that the terminal cannot encode --
a Windows console in cp1252 raising mid-stream would kill a session over
decoration, so every glyph has an ASCII fallback chosen once at startup.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass

from .approvals import Request, Verdict

RESET = "\033[0m"


@dataclass(frozen=True, slots=True)
class Glyphs:
    bullet: str
    branch: str
    bar: str
    sep: str
    spinner: tuple[str, ...]

    @classmethod
    def for_stream(cls, stream) -> Glyphs:
        # One check for the whole set: a console that can encode any of these
        # can encode all of them, and mixing the two sets looks like a bug.
        if _encodable("⏺⎿│·⠋", stream):
            return cls("⏺", "⎿", "│", "·", tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
        return cls("*", "`-", "|", "-", tuple("|/-\\"))


def _encodable(text: str, stream) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Console:
    """Colour, width, and the one place anything reaches the terminal."""

    def __init__(self, stream=None, *, colour: bool | None = None) -> None:
        self.stream = stream or sys.stdout
        if colour is None:
            colour = (
                self.stream.isatty()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
            )
        self.colour = bool(colour)
        self.glyphs = Glyphs.for_stream(self.stream)

    @property
    def width(self) -> int:
        try:
            return max(40, min(shutil.get_terminal_size((100, 24)).columns, 120))
        except OSError:
            return 100

    def paint(self, text: str, *codes: str) -> str:
        if not self.colour or not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}{RESET}"

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def accent(self, text: str) -> str:
        return self.paint(text, "38;5;209")

    def good(self, text: str) -> str:
        return self.paint(text, "38;5;71")

    def bad(self, text: str) -> str:
        return self.paint(text, "38;5;167")

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
            hint = self.console.dim(f"{self.label} {elapsed}s {sep} ctrl-c to interrupt")
            self.console.write(f"\r\033[2K{self.console.accent(frame)} {hint}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.4)
            self._thread = None
        if self._active:
            self.console.write("\r\033[2K")


class Renderer:
    """Draws turns, tool cards, diffs and prompts."""

    def __init__(self, console: Console) -> None:
        self.console = console

    # -------------------------------------------------------------- assistant

    def assistant_text(self, text: str) -> None:
        """Model prose, wrapped and lightly styled."""
        body = text.strip()
        if not body:
            return
        self.console.line()
        for line in _wrap_markdown(body, self.console):
            self.console.line(line)
        self.console.line()

    # ------------------------------------------------------------- tool cards

    def tool_start(self, name: str, display: str) -> None:
        label = self.console.bold(_pretty(name))
        target = self.console.dim(f"({display})") if display else ""
        self.console.line(f"{self.console.accent(self.console.glyphs.bullet)} {label}{target}")

    def tool_result(self, detail: str, *, ok: bool = True) -> None:
        mark = self.console.glyphs.branch
        text = detail.strip() or ("done" if ok else "failed")
        tint = self.console.dim if ok else self.console.bad
        for index, line in enumerate(text.splitlines()[:12]):
            prefix = f"  {mark} " if index == 0 else "     "
            self.console.line(self.console.dim(prefix) + tint(line[: self.console.width - 8]))

    def tool_denied(self, reason: str) -> None:
        self.console.line(
            self.console.dim(f"  {self.console.glyphs.branch} ") + self.console.bad(reason.splitlines()[0])
        )

    def diff(self, text: str, *, limit: int = 24) -> None:
        """A unified diff with the usual colouring, truncated to stay readable."""
        lines = [line for line in text.splitlines() if not line.startswith(("---", "+++"))]
        for line in lines[:limit]:
            if line.startswith("+"):
                painted = self.console.good(line)
            elif line.startswith("-"):
                painted = self.console.bad(line)
            elif line.startswith("@@"):
                painted = self.console.dim(line)
            else:
                painted = self.console.dim(line)
            self.console.line("     " + painted[: self.console.width + 20])
        if len(lines) > limit:
            self.console.line(self.console.dim(f"     +{len(lines) - limit} more diff lines"))

    # ----------------------------------------------------------------- status

    def notice(self, text: str) -> None:
        self.console.line(self.console.dim(f"  {text}"))

    def error(self, text: str) -> None:
        self.console.line(self.console.bad(f"  {text}"))

    def rule(self) -> None:
        mark = "-" if self.console.glyphs.sep == "-" else "─"
        self.console.line(self.console.dim(mark * min(self.console.width, 60)))

    # ------------------------------------------------------------- approvals

    def ask(self, request: Request) -> Verdict:
        """The permission prompt. Defaults to no on anything unexpected."""
        console = self.console
        console.line()
        console.line("  " + console.bold(request.question))
        if request.preview:
            console.line("  " + console.dim(f"$ {request.preview}"[: console.width - 4]))
        options = [
            ("1", "Yes"),
            ("2", f"Yes, and {request.always_means}"),
            ("3", "No, tell the model what to do instead"),
        ]
        for key, text in options:
            console.line(f"    {console.accent(key)}. {text}")
        while True:
            try:
                console.write("  " + console.dim("choose 1-3 > "))
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


def _wrap_markdown(text: str, console: Console) -> list[str]:
    """Enough markdown to read comfortably, and no more.

    Headings and bullets get weight, fenced code gets dimmed and left alone.
    A full markdown renderer would reflow code and mangle it, which is worse
    than not rendering markdown at all.
    """
    width = console.width
    out: list[str] = []
    in_code = False
    for raw in text.splitlines():
        if raw.lstrip().startswith("```"):
            in_code = not in_code
            fence = raw.strip()[3:].strip()
            out.append(console.dim(f"  {console.glyphs.sep} {fence}" if fence else "  ---"))
            continue
        if in_code:
            out.append(console.dim(f"  {console.glyphs.bar} ") + raw[: width + 20])
            continue
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("#"):
            out.append(console.bold(stripped.lstrip("# ").strip()))
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            out.extend(_wrap(stripped[2:], width - 4, first="  • ", rest="    ", console=console))
            continue
        out.extend(_wrap(stripped, width, first="", rest="", console=console))
    return out


def _wrap(text: str, width: int, *, first: str, rest: str, console: Console) -> list[str]:
    """Greedy word wrap with a hanging indent."""
    del console
    words = text.split()
    if not words:
        return []
    limit = max(20, width)
    lines: list[str] = []
    current = first + words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) > limit:
            lines.append(current)
            current = rest + word
        else:
            current = f"{current} {word}"
    lines.append(current)
    return lines
