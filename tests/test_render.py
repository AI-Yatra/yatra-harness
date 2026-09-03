"""The screen: one grid, one palette, and prose that does not depend on transport.

Three claims are worth holding down with tests, because each was untrue before
and each fails silently rather than loudly.

The palette is *measured*. A terminal's background is unknowable, so every
colour was chosen by maximising its worst-case WCAG contrast across white,
black and Solarized Dark. Those measurements are recomputed here from the SGR
codes themselves, so retuning a colour to something unreadable fails the suite
rather than shipping.

The grid has to survive losing colour. Piped output, `NO_COLOR` and a dumb
terminal all strip every code, and if hierarchy lived only in colour there
would be none left. So the columns are asserted with colour off.

Prose has to render identically however it arrives. It used to go through the
markdown renderer when a response arrived whole and straight to the terminal
unwrapped when it streamed, so the same words gave two different screens
depending on a route's `stream:` flag.
"""

from __future__ import annotations

import io
import random
import re
import unittest
from pathlib import Path
from unittest import mock

from harness.repl.render import (
    GUTTER,
    INDENT,
    MAX_MEASURE,
    Console,
    Glyphs,
    Prose,
    Renderer,
    _clip,
)
from harness.repl.theme import THEME, Theme

ROOT = Path(__file__).resolve().parents[1]

#: Backgrounds a session can land on and cannot detect.
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
SOLARIZED_DARK = (0x00, 0x2B, 0x36)

#: The arithmetic ceiling for one colour that must work on white *and* black
#: is sqrt(21). Nothing can beat it, so the bar is set a little under.
MIN_CONTRAST = 3.5

SAMPLE = (
    "I'll start by exploring the repository structure to understand what we're "
    "working with, because the layout decides where the tests live.\n"
    "\n"
    "## Two issues\n"
    "\n"
    "- `winning_lines()` is missing the anti-diagonal so the test fails, and this "
    "bullet is long enough that it has to wrap onto another line\n"
    "- `best_move` is not implemented\n"
    "\n"
    "```python\n"
    "lines.append((2, 4, 6))\n"
    "```\n"
    "\n"
    "Running the suite now."
)


class Buffer(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return False


class AsciiBuffer(Buffer):
    encoding = "cp1252"


def console(**kwargs) -> Console:
    kwargs.setdefault("colour", False)
    return Console(Buffer(), **kwargs)


def render(chunks: list[str], **kwargs) -> str:
    con = console(**kwargs)
    prose = Prose(con)
    for chunk in chunks:
        prose.feed(chunk)
    prose.close()
    return con.stream.getvalue()


def columns(text: str) -> list[int]:
    """The starting column of each non-blank line."""
    return [len(line) - len(line.lstrip(" ")) for line in text.splitlines() if line.strip()]


# ── the palette ────────────────────────────────────────────────────────────


def _channel(value: int) -> float:
    scaled = value / 255
    return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = (_channel(v) for v in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def xterm_rgb(index: int) -> tuple[int, int, int]:
    """The fixed RGB of a 256-colour index, per the xterm palette."""
    if index >= 232:
        value = 8 + (index - 232) * 10
        return (value, value, value)
    levels = (0, 95, 135, 175, 215, 255)
    offset = index - 16
    return (levels[offset // 36], levels[(offset // 6) % 6], levels[offset % 6])


def colour_roles(theme: Theme) -> dict[str, int]:
    """Every role that names a 256-colour index, and which index."""
    roles = {}
    for name in ("accent", "muted", "success", "failure", "strong"):
        code = getattr(theme, name)
        match = re.fullmatch(r"38;5;(\d+)", code)
        if match:
            roles[name] = int(match.group(1))
    return roles


class PaletteTests(unittest.TestCase):
    def test_every_colour_is_readable_on_any_background(self) -> None:
        """The one test that stops a pretty colour from shipping unreadable."""
        for name, index in colour_roles(THEME).items():
            rgb = xterm_rgb(index)
            worst = min(contrast(rgb, bg) for bg in (WHITE, BLACK, SOLARIZED_DARK))
            self.assertGreaterEqual(
                worst, MIN_CONTRAST, f"{name} (colour {index}) is unreadable somewhere: {worst:.2f}"
            )

    def test_faint_is_never_used(self) -> None:
        """SGR 2 has no defined appearance.

        Some terminals ignore it, leaving no hierarchy at all; others render it
        unreadable on a dark background. A measured grey replaces it.
        """
        for name in ("accent", "muted", "success", "failure", "strong"):
            codes = getattr(THEME, name).split(";")
            self.assertNotIn("2", codes, f"{name} uses SGR 2")

    def test_the_sixteen_ansi_colours_are_never_used(self) -> None:
        """They have no standard; each terminal theme picks its own."""
        for name in ("accent", "muted", "success", "failure"):
            code = getattr(THEME, name)
            self.assertTrue(code.startswith("38;5;"), f"{name} is not a 256-colour code: {code}")

    def test_emphasis_survives_a_terminal_with_no_colour(self) -> None:
        self.assertEqual(THEME.strong, "1")

    def test_the_accent_is_the_blue_the_wordmark_ends_on(self) -> None:
        """One accent, or the banner and the session look like two programs."""
        red, green, blue = xterm_rgb(colour_roles(THEME)["accent"])
        self.assertGreater(blue, red)
        self.assertGreater(blue, green)

    def test_success_and_failure_are_a_matched_pair(self) -> None:
        """Otherwise a diff reads as one loud colour against one quiet one."""
        roles = colour_roles(THEME)
        good = min(contrast(xterm_rgb(roles["success"]), bg) for bg in (WHITE, BLACK))
        bad = min(contrast(xterm_rgb(roles["failure"]), bg) for bg in (WHITE, BLACK))
        self.assertLess(abs(good - bad), 1.0)


# ── the grid ───────────────────────────────────────────────────────────────


class GridTests(unittest.TestCase):
    """Asserted with colour off, because that is when only the grid is left."""

    def setUp(self) -> None:
        self.console = console()
        self.render = Renderer(self.console)

    @property
    def out(self) -> str:
        return self.console.stream.getvalue()

    def test_prose_and_tool_names_start_in_the_same_column(self) -> None:
        self.render.assistant_text("A sentence.")
        self.render.tool_start("read_file", "game.py")
        lines = [line for line in self.out.splitlines() if line.strip()]
        prose, card = lines[0], lines[1]
        self.assertEqual(prose.index("A"), GUTTER)
        self.assertEqual(card.index("Read"), GUTTER)

    def test_the_tool_mark_hangs_in_the_gutter(self) -> None:
        self.render.tool_start("read_file", "game.py")
        self.assertEqual(self.out[0], self.console.glyphs.bullet)

    def test_detail_sits_one_gutter_under_its_card(self) -> None:
        self.render.tool_result("first\nsecond")
        first, second = self.out.splitlines()
        self.assertEqual(first.index(self.console.glyphs.branch), GUTTER)
        self.assertEqual(second.index("second"), INDENT)

    def test_a_diff_is_nested_like_any_other_detail(self) -> None:
        """It is output from a tool and should not invent its own column."""
        self.render.diff("@@ -1 +1 @@\n-old\n+new")
        lines = self.out.splitlines()
        self.assertEqual(lines[0].index(self.console.glyphs.branch), GUTTER)
        self.assertEqual(lines[1].index("-old"), INDENT)

    def test_notices_and_errors_share_the_prose_column(self) -> None:
        self.render.notice("routed to a model")
        self.render.error("no credential")
        for line in self.out.splitlines():
            self.assertEqual(len(line) - len(line.lstrip(" ")), GUTTER)

    def test_nothing_is_run_to_the_edge_of_the_window(self) -> None:
        self.render.assistant_text("word " * 400)
        for line in self.out.splitlines():
            self.assertLessEqual(len(line), self.console.width)

    def test_a_wide_window_gets_whitespace_not_longer_lines(self) -> None:
        """Past about a hundred columns the eye loses the next line's start."""
        wide = console()
        with mock.patch(
            "shutil.get_terminal_size", return_value=type("S", (), {"columns": 400, "lines": 24})()
        ):
            self.assertEqual(wide.width, MAX_MEASURE)

    def test_the_measure_leaves_both_margins(self) -> None:
        self.assertLess(self.console.measure, self.console.width - GUTTER)


# ── prose ──────────────────────────────────────────────────────────────────


class ProseTests(unittest.TestCase):
    def test_the_transport_cannot_change_the_output(self) -> None:
        """The bug this class exists to prevent."""
        whole = render([SAMPLE])
        self.assertEqual(render(list(SAMPLE)), whole, "character-at-a-time differs")

    def test_any_chunking_gives_the_same_screen(self) -> None:
        """A provider splits a stream wherever it likes, including mid-word."""
        whole = render([SAMPLE])
        rng = random.Random(20260903)
        for _ in range(25):
            chunks, index = [], 0
            while index < len(SAMPLE):
                step = rng.randint(1, 20)
                chunks.append(SAMPLE[index : index + step])
                index += step
            self.assertEqual(render(chunks), whole)

    def test_a_long_line_is_wrapped_with_a_hanging_indent(self) -> None:
        out = render(["This sentence is deliberately much longer than any terminal " * 4])
        starts = columns(out)
        self.assertEqual(starts[0], GUTTER)
        self.assertTrue(all(start == INDENT for start in starts[1:]))

    def test_code_is_never_reflowed(self) -> None:
        """A reflowed code line is a wrong code line."""
        code = "x = [" + ", ".join(str(n) for n in range(80)) + "]"
        out = render(["```\n" + code + "\n```\n"])
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_the_fence_itself_is_not_drawn(self) -> None:
        out = render(["```python\nx = 1\n```\n"])
        self.assertNotIn("python", out)
        self.assertIn("x = 1", out)

    def test_a_heading_loses_its_hashes(self) -> None:
        self.assertNotIn("#", render(["## Two issues\n"]))

    def test_a_bullet_gets_a_real_bullet(self) -> None:
        out = render(["- one\n"], colour=False)
        self.assertNotIn("- one", out)
        self.assertIn("one", out)

    def test_a_numbered_item_is_marked_like_a_bulleted_one(self) -> None:
        """Models mix `-` and `1.` inside one answer.

        Rendering only the first left two list styles on the same screen,
        which is the inconsistency this whole grid exists to remove.
        """
        out = render(["1. first\n"], colour=True)
        self.assertIn(THEME.accent, out)

    def test_a_numbered_item_keeps_its_number(self) -> None:
        """Unlike a dash, the number carries meaning."""
        self.assertIn("1.", render(["1. first\n"]))

    def test_a_numbered_item_hangs_under_its_own_text(self) -> None:
        out = render(["7. " + "word " * 60 + "\n"])
        starts = columns(out)
        self.assertEqual(starts[0], GUTTER)
        # "7. " is three wide, so the text starts one past a bullet's would.
        self.assertTrue(all(start == GUTTER + 3 for start in starts[1:]))

    def test_a_wider_number_hangs_wider(self) -> None:
        out = render(["12. " + "word " * 60 + "\n"])
        self.assertTrue(all(start == GUTTER + 4 for start in columns(out)[1:]))

    def test_a_sentence_that_merely_contains_a_number_is_not_a_list(self) -> None:
        """`In 2026. the year ...` is prose, and a year is not a marker."""
        out = render(["In 2026. the year was fine\n"], colour=True)
        self.assertNotIn(THEME.accent, out)

    def test_blank_lines_between_paragraphs_survive(self) -> None:
        out = render(["one\n\ntwo\n"])
        self.assertIn("\n\n", out)

    def test_an_empty_response_prints_nothing(self) -> None:
        self.assertEqual(render(["   \n  "]).strip(), "")

    def test_close_resets_for_the_next_turn(self) -> None:
        con = console()
        prose = Prose(con)
        prose.feed("```\ncode\n")
        prose.close()
        prose.feed("plain text\n")
        prose.close()
        self.assertNotIn(con.glyphs.bar + " plain", con.stream.getvalue())


# ── degradation ────────────────────────────────────────────────────────────


class DegradationTests(unittest.TestCase):
    def test_a_console_that_cannot_encode_the_glyphs_uses_ascii(self) -> None:
        glyphs = Glyphs.for_stream(AsciiBuffer())
        for value in (glyphs.bullet, glyphs.branch, glyphs.bar, glyphs.dot, glyphs.rule):
            value.encode("cp1252")

    def test_the_glyph_set_is_all_or_nothing(self) -> None:
        """Half Unicode and half ASCII looks like a bug rather than a fallback."""
        unicode_set = Glyphs.for_stream(Buffer())
        ascii_set = Glyphs.for_stream(AsciiBuffer())
        self.assertNotEqual(unicode_set.bullet, ascii_set.bullet)
        self.assertNotEqual(unicode_set.branch, ascii_set.branch)

    def test_no_colour_still_leaves_the_structure(self) -> None:
        con = console()
        Renderer(con).tool_start("run_command", "pytest")
        out = con.stream.getvalue()
        self.assertNotIn("\033", out)
        self.assertIn("Run", out)

    def test_colour_is_off_when_not_a_terminal(self) -> None:
        self.assertFalse(Console(Buffer()).colour)

    def test_a_clip_says_that_it_clipped(self) -> None:
        self.assertTrue(_clip("x" * 100, 20).endswith("…"))
        self.assertEqual(len(_clip("x" * 100, 20)), 20)

    def test_a_short_line_is_left_alone(self) -> None:
        self.assertEqual(_clip("short", 20), "short")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class PasteTests(unittest.TestCase):
    """A paste is one message, and buffered text is never consent.

    Both halves of a bug an operator hit in a real session. A multi-line
    prompt pasted at the `>` prompt arrived as one message per line, so the
    first line became the turn and the remaining seven sat in the buffer. The
    agent then asked permission to run a command, `input()` answered the
    question with the next line of the paste, and the operator watched their
    own prompt being typed into a permission dialog.

    A line beginning "1" would have granted the permission.
    """

    def test_a_paste_is_wrapped_in_markers(self) -> None:
        from harness.repl.render import PASTE_END, PASTE_ON, PASTE_START

        self.assertTrue(PASTE_START.startswith("\x1b["))
        self.assertTrue(PASTE_END.startswith("\x1b["))
        self.assertIn("2004", PASTE_ON)

    def test_every_line_of_a_paste_becomes_one_message(self) -> None:
        from harness.repl.render import PASTE_END, PASTE_START

        lines = [PASTE_START + "first", "second", "third" + PASTE_END]
        pending = list(lines)
        parts = [pending.pop(0).split(PASTE_START, 1)[1]]
        while PASTE_END not in parts[-1]:
            parts.append(pending.pop(0))
        parts[-1] = parts[-1].split(PASTE_END, 1)[0]
        self.assertEqual("\n".join(parts), "first\nsecond\nthird")
        self.assertEqual(pending, [], "lines were left in the buffer")

    def test_draining_a_non_terminal_does_nothing(self) -> None:
        """A pipe has no keyboard buffer, and reading it would eat real input."""
        from harness.repl.render import drain_input

        self.assertEqual(drain_input(Buffer()), 0)

    def test_the_approval_prompt_drains_before_reading(self) -> None:
        """The safety half: a question cannot be answered by text typed before it."""
        source = (ROOT / "harness" / "repl" / "render.py").read_text(encoding="utf-8")
        ask = source[source.index("def ask("):]
        self.assertLess(
            ask.index("drain_input"),
            ask.index("input()"),
            "the prompt reads before it drains, so buffered text can answer it",
        )
