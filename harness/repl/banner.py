"""The wordmark drawn at the top of a session.

A block font rather than an image, because the one thing a terminal banner has
to survive is being a terminal: no truecolor, no Unicode, eighty columns, a
pipe instead of a tty. Each of those degrades here rather than breaking.

The block character is U+2588, which is not in cp1252, so a legacy Windows
console cannot encode it and `console.write` would swallow the whole line. The
font is therefore rendered with whatever fill the stream can actually take, and
the layout is identical either way.
"""

from __future__ import annotations

from .render import Console, _encodable
from .theme import GUTTER

#: Five rows per glyph, drawn on a three-wide stroke. `#` marks ink and a space
#: marks paper, so the fill character can be swapped without touching the
#: shapes. Every glyph is padded to a fixed width so columns line up.
FONT: dict[str, tuple[str, ...]] = {
    "A": (
        "#######",
        "### ###",
        "#######",
        "### ###",
        "### ###",
    ),
    "I": (
        "###",
        "###",
        "###",
        "###",
        "###",
    ),
    "Y": (
        "### ###",
        "### ###",
        "#######",
        "  ###  ",
        "  ###  ",
    ),
    "T": (
        "#######",
        "  ###  ",
        "  ###  ",
        "  ###  ",
        "  ###  ",
    ),
    "R": (
        "#######",
        "### ###",
        "###### ",
        "### ###",
        "### ###",
    ),
    "-": (
        "     ",
        "     ",
        "#####",
        "     ",
        "     ",
    ),
    " ": ("   ",) * 5,
}

WORDMARK = "AI-YATRA"

HEIGHT = 5

#: Top to bottom, near-white into blue. A vertical ramp reads as lighting
#: rather than as decoration, and keeps the top row the most legible line.
RAMP = ("38;5;189", "38;5;153", "38;5;117", "38;5;75", "38;5;33")

#: The block a terminal that can draw it gets, and the one it gets otherwise.
FULL_BLOCK = "█"
ASCII_BLOCK = "#"


def render(word: str = WORDMARK, *, fill: str = FULL_BLOCK) -> list[str]:
    """The word as five equal-length rows, drawn with *fill*."""
    glyphs = [FONT[character] for character in word.upper() if character in FONT]
    if not glyphs:
        return []
    rows = []
    for index in range(HEIGHT):
        row = " ".join(glyph[index] for glyph in glyphs)
        rows.append(row.replace("#", fill).rstrip())
    return rows


def width(word: str = WORDMARK) -> int:
    rows = render(word, fill="#")
    return max((len(row) for row in rows), default=0)


def draw(console: Console, word: str = WORDMARK, *, indent: str = " " * GUTTER) -> list[str]:
    """The painted rows, or an empty list when there is no room for them.

    Returning the lines rather than printing them keeps this testable without
    a terminal, and lets the caller decide what to do when it does not fit.
    """
    fill = FULL_BLOCK if _encodable(FULL_BLOCK, console.stream) else ASCII_BLOCK
    rows = render(word, fill=fill)
    if not rows or len(indent) + width(word) > console.width:
        return []
    return [indent + console.paint(row, RAMP[index]) for index, row in enumerate(rows)]
