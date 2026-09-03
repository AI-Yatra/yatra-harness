"""The markup, read the way a browser and a screen reader read it.

A placeholder is not a label. It vanishes the moment someone types, several
screen readers do not announce it at all, and it is styled faint by default.
Missing form labels are the third most common accessibility failure on the
web, found on roughly half of the top million home pages, which is why this
file spends most of its time on them.

Parsed with `html.parser` from the standard library rather than matched with
regular expressions, so the tests are about the document's structure and not
about how the file happens to be typed.
"""

from __future__ import annotations

import unittest
from html.parser import HTMLParser

import page


class Element:
    def __init__(self, tag: str, attributes: dict[str, str]) -> None:
        self.tag = tag
        self.attributes = attributes
        self.text = ""

    def get(self, name: str, default: str = "") -> str:
        return self.attributes.get(name, default)

    @property
    def classes(self) -> set[str]:
        return set(self.get("class").split())


class Document(HTMLParser):
    """Every element, flat, with the text that belongs to it."""

    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs) -> None:
        element = Element(tag, {key: (value or "") for key, value in attrs})
        self.elements.append(element)
        if tag not in ("input", "meta", "link", "br", "img", "hr"):
            self._open.append(element)

    def handle_endtag(self, tag: str) -> None:
        while self._open and self._open[-1].tag != tag:
            self._open.pop()
        if self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        for element in self._open:
            element.text += data

    def all(self, tag: str) -> list[Element]:
        return [element for element in self.elements if element.tag == tag]

    def first(self, tag: str, **match: str) -> Element | None:
        for element in self.all(tag):
            if all(element.get(key) == value for key, value in match.items()):
                return element
        return None

    def by_class(self, name: str) -> list[Element]:
        return [element for element in self.elements if name in element.classes]


def document(**kwargs) -> Document:
    return Document(page.render(**kwargs))


class LabelTests(unittest.TestCase):
    """Every control needs a name a screen reader will actually read out."""

    def inputs(self) -> list[Element]:
        return [
            element
            for element in document().all("input")
            if element.get("type") not in ("hidden", "submit")
        ]

    def test_there_are_two_controls_to_label(self) -> None:
        self.assertEqual(len(self.inputs()), 2)

    def test_every_control_has_a_label_pointing_at_it(self) -> None:
        parsed = document()
        targets = {label.get("for") for label in parsed.all("label")}
        for element in self.inputs():
            self.assertIn(
                element.get("id"),
                targets,
                f"the {element.get('name')!r} input has no <label for=...>",
            )

    def test_every_label_has_words_in_it(self) -> None:
        for label in document().all("label"):
            self.assertTrue(label.text.strip(), f"<label for={label.get('for')!r}> is empty")

    def test_a_placeholder_is_not_used_as_the_only_name(self) -> None:
        """It disappears on typing and is not announced consistently."""
        parsed = document()
        targets = {label.get("for") for label in parsed.all("label")}
        for element in self.inputs():
            if element.get("placeholder"):
                self.assertIn(element.get("id"), targets)

    def test_the_controls_keep_their_identifiers(self) -> None:
        """A label needs something to point at."""
        for element in self.inputs():
            self.assertTrue(element.get("id"), "an input has no id")


class ErrorTests(unittest.TestCase):
    """A failure has to reach someone who cannot see the screen."""

    def error(self, **kwargs) -> Element:
        found = document(**kwargs).by_class("error")
        self.assertTrue(found, "no element with class 'error'")
        return found[0]

    def test_the_message_is_shown(self) -> None:
        self.assertIn("no good", self.error(error="no good").text)

    def test_the_error_region_is_announced(self) -> None:
        """Without role=alert a screen reader never mentions the failure."""
        element = self.error(error="no good")
        self.assertTrue(
            element.get("role") == "alert" or element.get("aria-live") in ("polite", "assertive"),
            "the error needs role='alert' or aria-live",
        )

    def test_the_error_region_exists_before_there_is_an_error(self) -> None:
        """A region inserted at failure time is announced unreliably."""
        self.assertTrue(document().by_class("error"))


class FormTests(unittest.TestCase):
    def test_the_form_posts_to_login(self) -> None:
        form = document().first("form")
        self.assertIsNotNone(form)
        self.assertEqual(form.get("method").lower(), "post")
        self.assertEqual(form.get("action"), "/login")

    def test_the_password_is_masked(self) -> None:
        parsed = document()
        password = parsed.first("input", name="password")
        self.assertIsNotNone(password)
        self.assertEqual(password.get("type"), "password")

    def test_a_typed_username_survives_a_failed_attempt(self) -> None:
        parsed = Document(page.render(error="no", username="ada"))
        self.assertEqual(parsed.first("input", name="username").get("value"), "ada")

    def test_a_username_with_quotes_cannot_break_out_of_the_attribute(self) -> None:
        markup = page.render(username='" onfocus="alert(1)')
        self.assertNotIn('onfocus="alert(1)"', markup)

    def test_an_error_message_is_escaped(self) -> None:
        self.assertNotIn("<script>", page.render(error="<script>alert(1)</script>"))

    def test_the_page_declares_its_language(self) -> None:
        """A screen reader picks its pronunciation from this."""
        self.assertEqual(document().first("html").get("lang"), "en")


if __name__ == "__main__":
    unittest.main()
