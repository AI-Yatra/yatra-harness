"""Colour, measured rather than eyeballed.

A stylesheet can hide a message completely without anything looking broken:
the markup is right, the text is there, the server sent it, and the visitor
sees a blank space where the explanation should be. That is the failure this
file exists to catch, and it is why the numbers are computed instead of
inspected.

The arithmetic is the WCAG 2 contrast ratio: relative luminance of each
colour, lighter over darker, plus 0.05 top and bottom. 4.5:1 is the threshold
for body text at normal size, and an error message is exactly that.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

STYLE = Path(__file__).resolve().parents[1] / "static" / "style.css"

#: Body text at normal size. Large text is allowed 3:1; this is not large.
MINIMUM_CONTRAST = 4.5

_HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def stylesheet() -> str:
    return STYLE.read_text(encoding="utf-8")


def variables(css: str) -> dict[str, str]:
    """The `--name: value` declarations, so a rule using one can resolve it."""
    return {
        name: value.strip()
        for name, value in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", css)
    }


def rule(css: str, selector: str) -> str:
    """The body of the first rule for *selector*, or an empty string."""
    pattern = re.compile(
        r"(?:^|[,{}])\s*" + re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}", re.MULTILINE
    )
    found = pattern.search(css)
    return found.group(1) if found else ""


def declared(body: str, property_name: str) -> str:
    found = re.search(rf"(?:^|;)\s*{re.escape(property_name)}\s*:\s*([^;]+)", body)
    return found.group(1).strip() if found else ""


def resolve(value: str, css: str) -> str:
    """Follow `var(--name)` to the colour it stands for."""
    seen = 0
    while value.startswith("var(") and seen < 5:
        name = value[4:].split(")")[0].strip().lstrip("-")
        value = variables(css).get(name, "")
        seen += 1
    return value.strip()


def rgb(value: str) -> tuple[int, int, int] | None:
    found = _HEX.search(value)
    if not found:
        return None
    digits = found.group(1)
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _channel(value: int) -> float:
    scaled = value / 255
    return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4


def luminance(colour: tuple[int, int, int]) -> float:
    red, green, blue = (_channel(part) for part in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class StylesheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = stylesheet()

    def colour_of(self, selector: str, property_name: str = "color"):
        raw = declared(rule(self.css, selector), property_name)
        self.assertTrue(raw, f"{selector} declares no {property_name}")
        value = rgb(resolve(raw, self.css))
        self.assertIsNotNone(value, f"{selector}'s {property_name} is not a hex colour: {raw}")
        return value

    def card_background(self):
        return self.colour_of(".card", "background")

    def test_the_error_message_is_readable_on_its_own_background(self) -> None:
        """An empty red box is worse than no box: it says something is wrong
        and refuses to say what."""
        behind = self.colour_of(".error", "background")
        ratio = contrast(self.colour_of(".error"), behind)
        self.assertGreaterEqual(
            ratio,
            MINIMUM_CONTRAST,
            f"the error text has {ratio:.2f}:1 against the panel it sits on; "
            f"WCAG asks for {MINIMUM_CONTRAST}:1, so the panel appears empty",
        )

    def test_the_error_message_is_readable_on_the_card(self) -> None:
        """The whole point. An invisible error is the same as no error."""
        ratio = contrast(self.colour_of(".error"), self.card_background())
        self.assertGreaterEqual(
            ratio,
            MINIMUM_CONTRAST,
            f"the error message has {ratio:.2f}:1 contrast against the card; "
            f"WCAG asks for {MINIMUM_CONTRAST}:1 and a visitor cannot read it",
        )

    def test_the_error_message_is_not_the_same_colour_as_ordinary_text(self) -> None:
        """It has to be distinguishable, not merely visible."""
        self.assertNotEqual(self.colour_of(".error"), self.colour_of("body"))

    def test_body_text_is_readable(self) -> None:
        ratio = contrast(self.colour_of("body"), self.card_background())
        self.assertGreaterEqual(ratio, MINIMUM_CONTRAST, f"body text is {ratio:.2f}:1")

    def test_the_submit_button_label_is_readable(self) -> None:
        ratio = contrast(self.colour_of(".submit"), self.colour_of(".submit", "background"))
        self.assertGreaterEqual(ratio, MINIMUM_CONTRAST, f"the button is {ratio:.2f}:1")

    def test_the_hint_text_is_readable(self) -> None:
        ratio = contrast(self.colour_of(".hint"), self.card_background())
        self.assertGreaterEqual(ratio, MINIMUM_CONTRAST, f"the hint is {ratio:.2f}:1")

    def test_the_fields_fit_inside_the_card(self) -> None:
        """The one thing wrong with this page that you can see at a glance.

        `.control` is `width: 100%` with padding and a border. Without
        `box-sizing: border-box` the browser adds those on top of the 100%, so
        every input is wider than the card holding it and punches out past
        both edges. It is the most common layout bug there is, and unlike the
        other three faults here it needs no test to notice.
        """
        rule = re.search(r"\*\s*\{([^}]*)\}", self.css)
        universal = rule.group(1) if rule else ""
        control = self.css[self.css.index(".control"):] if ".control" in self.css else ""
        applied = "border-box" in universal or "border-box" in control[:400]
        self.assertTrue(
            applied,
            "nothing sets box-sizing: border-box, so a width:100% field with "
            "padding is wider than the card that holds it",
        )

    def test_a_focused_control_is_visibly_focused(self) -> None:
        """Keyboard users have nothing else to tell them where they are."""
        body = rule(self.css, ".control:focus")
        self.assertTrue(body, "no .control:focus rule")
        self.assertNotIn("outline: none", body.replace(" ", " "))
        self.assertTrue(
            declared(body, "outline") or declared(body, "box-shadow"),
            ".control:focus draws no focus indicator",
        )


if __name__ == "__main__":
    unittest.main()
