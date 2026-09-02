"""Tests for the session wordmark.

A banner is decoration, so the only interesting cases are the ones where the
terminal will not cooperate: no colour, no Unicode, not enough columns. Each of
those has to degrade rather than break, and the U+2588 case is not theoretical.
It is not in cp1252, so on a legacy Windows console an unguarded block
character makes `Console.write` drop the line and the banner silently vanishes.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from harness.repl import banner
from harness.repl.render import Console


class FakeStream:
    """A stream that reports an encoding, since StringIO's is read-only."""

    def __init__(self, encoding: str = "utf-8", tty: bool = True) -> None:
        self.encoding = encoding
        self.written: list[str] = []
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        text.encode(self.encoding)  # behave like the real thing
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class FontTests(unittest.TestCase):
    def test_every_glyph_is_five_rows(self) -> None:
        for character, rows in banner.FONT.items():
            self.assertEqual(len(rows), banner.HEIGHT, character)

    def test_every_glyph_is_rectangular(self) -> None:
        """A ragged glyph would knock every later column out of line."""
        for character, rows in banner.FONT.items():
            widths = {len(row) for row in rows}
            self.assertEqual(len(widths), 1, f"{character} has widths {widths}")

    def test_the_wordmark_only_uses_glyphs_that_exist(self) -> None:
        for character in banner.WORDMARK:
            self.assertIn(character, banner.FONT, character)

    def test_the_glyphs_are_drawn_with_ink_and_paper_only(self) -> None:
        for character, rows in banner.FONT.items():
            for row in rows:
                self.assertEqual(set(row) - {"#", " "}, set(), character)


class RenderTests(unittest.TestCase):
    def test_it_renders_five_rows(self) -> None:
        self.assertEqual(len(banner.render()), banner.HEIGHT)

    def test_the_fill_character_is_substituted_everywhere(self) -> None:
        rows = banner.render(fill="@")
        self.assertNotIn("#", "".join(rows))
        self.assertIn("@", "".join(rows))

    def test_the_default_fill_is_the_full_block(self) -> None:
        self.assertIn(banner.FULL_BLOCK, "".join(banner.render()))

    def test_an_unknown_character_is_skipped_rather_than_crashing(self) -> None:
        self.assertEqual(banner.render("A!A"), banner.render("AA"))

    def test_a_word_with_no_known_glyphs_renders_nothing(self) -> None:
        self.assertEqual(banner.render("!!!"), [])

    def test_it_is_case_insensitive(self) -> None:
        self.assertEqual(banner.render("ai-yatra"), banner.render("AI-YATRA"))

    def test_the_reported_width_matches_the_longest_row(self) -> None:
        rows = banner.render(fill="#")
        self.assertEqual(banner.width(), max(len(row) for row in rows))


class DrawTests(unittest.TestCase):
    def console(self, encoding: str = "utf-8", colour: bool = True) -> Console:
        return Console(FakeStream(encoding), colour=colour)

    def test_it_draws_blocks_when_the_stream_can_encode_them(self) -> None:
        rows = banner.draw(self.console("utf-8"))
        self.assertTrue(rows)
        self.assertIn(banner.FULL_BLOCK, "".join(rows))

    def test_it_falls_back_to_ascii_on_a_cp1252_console(self) -> None:
        """The case that would otherwise make the banner disappear."""
        rows = banner.draw(self.console("cp1252"))
        self.assertTrue(rows)
        joined = "".join(rows)
        self.assertNotIn(banner.FULL_BLOCK, joined)
        self.assertIn(banner.ASCII_BLOCK, joined)

    def test_the_layout_is_the_same_either_way(self) -> None:
        blocks = banner.render(fill=banner.FULL_BLOCK)
        ascii_art = banner.render(fill=banner.ASCII_BLOCK)
        self.assertEqual(
            [len(row) for row in blocks], [len(row) for row in ascii_art]
        )

    def test_every_row_carries_a_colour_when_colour_is_on(self) -> None:
        rows = banner.draw(self.console(colour=True))
        for row in rows:
            self.assertIn("\033[", row)

    def test_no_escape_codes_leak_when_colour_is_off(self) -> None:
        rows = banner.draw(self.console(colour=False))
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("\033", row)

    def test_the_ramp_covers_every_row(self) -> None:
        self.assertEqual(len(banner.RAMP), banner.HEIGHT)

    def test_it_draws_nothing_when_the_terminal_is_too_narrow(self) -> None:
        """Better no wordmark than one folded across two lines."""
        with patch.object(Console, "width", banner.width() - 4):
            self.assertEqual(banner.draw(self.console()), [])

    def test_it_draws_when_there_is_exactly_enough_room(self) -> None:
        with patch.object(Console, "width", banner.width() + 3):
            self.assertEqual(len(banner.draw(self.console())), banner.HEIGHT)

    def test_the_real_width_property_survives_those_two_tests(self) -> None:
        """patch.object restores it; an attribute delete would not have."""
        self.assertIsInstance(Console.width, property)

    def test_the_indent_is_applied_to_every_row(self) -> None:
        rows = banner.draw(self.console(colour=False), indent="    ")
        for row in rows:
            self.assertTrue(row.startswith("    "), repr(row[:8]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
