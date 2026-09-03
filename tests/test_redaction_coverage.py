"""Redaction has to cover the provider catalogue, not a copy of it.

The bug this guards against was not a bad regex, it was two lists that had to
agree and did not. The catalogue grew to seventeen credential prefixes while
the redactor matched one, so a groq, cerebras, google, nvidia or inception key
travelled into `events.jsonl` in the clear whenever a provider echoed it back
in a 401 body, which several do.

The coupling test at the top is the one that matters: it fails if someone adds
a provider whose keys would not be redacted.
"""

from __future__ import annotations

import unittest

from harness.models import auth
from harness.record.redaction import Redactor


class CatalogueCouplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_every_prefix_in_the_catalogue_is_redacted(self) -> None:
        """Adding a provider must not be able to add a leak."""
        uncovered = []
        for provider in auth.PROVIDERS:
            for prefix in provider.prefixes:
                key = prefix + "A" * 32
                if key in self.redactor.text(f"error: invalid key {key}"):
                    uncovered.append(f"{provider.name}:{prefix}")
        self.assertEqual(uncovered, [], "these prefixes reach the ledger in the clear")

    def test_the_catalogue_actually_has_prefixes_to_cover(self) -> None:
        """Guards the test above from passing vacuously."""
        prefixes = {p for provider in auth.PROVIDERS for p in provider.prefixes}
        self.assertGreater(len(prefixes), 10)


class KnownKeyShapeTests(unittest.TestCase):
    """One real-looking key per shape the catalogue issues."""

    SAMPLES = {
        "openai": "sk-proj-" + "A" * 32,
        "anthropic": "sk-ant-api03-" + "A" * 32,
        "openrouter": "sk-or-v1-" + "A" * 32,
        "dashscope": "sk-ws-" + "A" * 32,
        "groq": "gsk_" + "A" * 36,
        "cerebras": "csk-" + "A" * 36,
        "inception": "sk_" + "A" * 32,
        "google": "AIza" + "A" * 35,
        "google-oauth": "AQ.Ab" + "A" * 35,
        "nvidia": "nvapi-" + "A" * 32,
        "fireworks": "fw_" + "A" * 28,
        "chutes": "cpk_" + "A" * 28,
    }

    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_none_of_them_survive(self) -> None:
        for name, key in self.SAMPLES.items():
            body = f'{{"error": {{"message": "invalid api key {key}"}}}}'
            self.assertNotIn(key, self.redactor.text(body), name)

    def test_they_are_replaced_rather_than_deleted(self) -> None:
        """A silently vanishing key makes an error message unreadable."""
        out = self.redactor.text(f"key {self.SAMPLES['groq']} rejected")
        self.assertIn("<redacted>", out)
        self.assertIn("rejected", out)

    def test_a_key_nested_in_a_structure_is_redacted(self) -> None:
        payload = {"error": {"message": f"bad key {self.SAMPLES['google']}"}}
        self.assertNotIn(self.SAMPLES["google"], str(self.redactor.value(payload)))


class NonProviderSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_bearer_and_basic_headers(self) -> None:
        for scheme in ("Bearer", "Basic"):
            text = f"Authorization: {scheme} abcdefghijklmnop123456"
            self.assertNotIn("abcdefghijklmnop123456", self.redactor.text(text))

    def test_github_tokens(self) -> None:
        token = "ghp_" + "A" * 36
        self.assertNotIn(token, self.redactor.text(f"remote rejected {token}"))

    def test_aws_access_key_ids(self) -> None:
        key = "AKIA" + "B" * 16
        self.assertNotIn(key, self.redactor.text(f"aws said {key}"))

    def test_a_sensitive_key_name_redacts_whatever_the_value_is(self) -> None:
        for name in ("api_key", "Authorization", "password", "access-token"):
            self.assertEqual(self.redactor.value("anything", key=name), "<redacted>")

    def test_an_explicit_value_is_redacted_whatever_its_shape(self) -> None:
        """The live key is passed in directly and need not look like a key."""
        redactor = Redactor(["hunter2-not-key-shaped"])
        self.assertNotIn(
            "hunter2-not-key-shaped", redactor.text("token hunter2-not-key-shaped here")
        )


class FalsePositiveTests(unittest.TestCase):
    """Redaction that eats ordinary prose makes logs useless."""

    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_prefixes_mentioned_in_prose_survive(self) -> None:
        for text in (
            "the sk- prefix is shared by four providers",
            "use AQ. or AIza keys from aistudio",
            "see the gsk_ documentation",
            "csk- is cerebras",
        ):
            self.assertEqual(self.redactor.text(text), text)

    def test_ordinary_identifiers_survive(self) -> None:
        for text in (
            "harness/models/auth.py:411",
            "commit 29472c2 fixed the deny-list",
            "AssertionError: expected 30 passed",
        ):
            self.assertEqual(self.redactor.text(text), text)

    def test_a_short_value_is_not_treated_as_a_secret(self) -> None:
        redactor = Redactor(["ab"])
        self.assertIn("ab", redactor.text("ab is too short to be a secret"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
