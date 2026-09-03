"""Telling a paste from typing, without help from the terminal.

An operator pasted an eight-line prompt. `input()` returned the first line and
left seven in the buffer, so the first became the message and the rest were
answered into the permission question that opened next. A pasted line
beginning "1" would have approved an action nobody saw.

Bracketed paste is the right mechanism and is not enough: Git Bash on Windows
does not implement it, which is the terminal this was reported from, and the
same gap breaks multi-line paste in every comparable CLI. So underneath it
there is a rule that needs no terminal support: lines that arrive together are
one message. A person cannot type a second line fifty milliseconds after
pressing Enter on the first.
"""

from __future__ import annotations

import time
import unittest

from harness.repl.reading import (
    PASTE_END,
    PASTE_START,
    LineReader,
    read_message,
)

LINES = ["first line", "second line", "third line"]


def scripted(lines: list[str], delays: list[float]):
    """A stdin that delivers lines on a schedule."""
    pairs = iter(zip(lines, delays, strict=True))

    def read() -> str:
        try:
            text, wait = next(pairs)
        except StopIteration:
            raise EOFError from None
        time.sleep(wait)
        return text

    return read


def reader(lines: list[str], delays: list[float]) -> LineReader:
    found = LineReader(scripted(lines, delays))
    found.start()
    return found


class GroupingTests(unittest.TestCase):
    def test_lines_that_arrive_together_are_one_message(self) -> None:
        """The bug: a pasted prompt became one message per line."""
        message = read_message(reader(LINES, [0, 0, 0]))
        self.assertEqual(message, "first line\nsecond line\nthird line")

    def test_lines_typed_with_a_pause_stay_separate(self) -> None:
        """Otherwise every conversation becomes one enormous turn."""
        found = reader(LINES, [0, 0.4, 0.4])
        self.assertEqual(read_message(found), "first line")
        self.assertEqual(read_message(found), "second line")

    def test_a_pipe_is_never_grouped(self) -> None:
        """A pipe delivers everything at once, so timing says nothing there.

        A file of commands is a list of messages; a paste is one.
        """
        self.assertEqual(read_message(reader(LINES, [0, 0, 0]), group=False), "first line")

    def test_a_trailing_backslash_still_continues(self) -> None:
        """The explicit form keeps working, and waits however long it takes."""
        found = reader(["one \\", "two"], [0, 0.3])
        self.assertEqual(read_message(found), "one \ntwo")

    def test_end_of_input_returns_nothing(self) -> None:
        self.assertIsNone(read_message(reader([], [])))

    def test_end_of_input_stays_at_the_end(self) -> None:
        """A second read must not block forever on an exhausted stream."""
        found = reader(["only"], [0])
        self.assertEqual(read_message(found), "only")
        self.assertIsNone(read_message(found))

    def test_a_bracketed_paste_is_taken_whole(self) -> None:
        """Used where the terminal provides it, because it is exact."""
        lines = [PASTE_START + "alpha", "beta", "gamma" + PASTE_END]
        self.assertEqual(read_message(reader(lines, [0, 0, 0])), "alpha\nbeta\ngamma")

    def test_a_single_line_bracketed_paste_works(self) -> None:
        found = reader([PASTE_START + "alone" + PASTE_END], [0])
        self.assertEqual(read_message(found), "alone")


class DrainTests(unittest.TestCase):
    """The safety half. Buffered text is not consent."""

    def test_pending_lines_are_discarded(self) -> None:
        found = reader(LINES, [0, 0, 0])
        time.sleep(0.1)
        self.assertGreater(found.drain(), 0)

    def test_draining_leaves_nothing_to_answer_with(self) -> None:
        found = reader(LINES, [0, 0, 0])
        time.sleep(0.1)
        found.drain()
        self.assertIsNone(found.next_line(timeout=0.05))

    def test_draining_an_empty_reader_is_harmless(self) -> None:
        self.assertEqual(reader([], []).drain(), 0)

    def test_the_approval_prompt_drains_before_it_reads(self) -> None:
        """So a question cannot be answered by text typed before it existed."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "harness" / "repl" / "render.py")
        ask = source.read_text(encoding="utf-8")
        ask = ask[ask.index("def ask("):]
        self.assertLess(
            ask.index("drain()"),
            ask.index("next_line()"),
            "the prompt reads before it drains, so buffered text can answer it",
        )


class BoundsTests(unittest.TestCase):
    def test_one_message_is_bounded(self) -> None:
        """A runaway producer on stdin must not grow a message without limit."""
        from harness.repl.reading import MAX_PASTE_LINES

        many = [f"line {n}" for n in range(MAX_PASTE_LINES + 50)]
        message = read_message(reader(many, [0] * len(many)))
        self.assertLessEqual(len(message.splitlines()), MAX_PASTE_LINES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
