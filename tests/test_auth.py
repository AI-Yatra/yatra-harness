"""Credential detection, storage, redaction, and resolution precedence."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness import auth


class AuthTestCase(unittest.TestCase):
    """Every test runs against a throwaway store, never the user's real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = {
            name: os.environ.get(name)
            for name in ("YATRA_HARNESS_AUTH_FILE", "DASHSCOPE_API_KEY",
                         "ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY")
        }
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(Path(self._tmp.name) / "auth.json")
        for name in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY",
                     "NVIDIA_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class DetectionTests(AuthTestCase):
    def test_longest_prefix_wins(self) -> None:
        self.assertEqual(auth.detect("sk-ant-api03-abc").name, "anthropic")
        self.assertEqual(auth.detect("sk-ant-abc").name, "anthropic")
        self.assertEqual(auth.detect("sk-proj-abc").name, "openai")
        self.assertEqual(auth.detect("sk-ws-abc").name, "dashscope")
        self.assertEqual(auth.detect("nvapi-abc").name, "nvidia")
        self.assertEqual(auth.detect("sk-or-v1-abc").name, "openrouter")

    def test_bare_sk_falls_back_to_openai(self) -> None:
        self.assertEqual(auth.detect("sk-legacykeyvalue").name, "openai")

    def test_unknown_shape_is_not_guessed(self) -> None:
        self.assertIsNone(auth.detect("wholly-unknown-key"))
        self.assertIsNone(auth.detect(""))

    def test_aliases_resolve(self) -> None:
        self.assertEqual(auth.get_provider("qwen").name, "dashscope")
        self.assertEqual(auth.get_provider("claude").name, "anthropic")
        self.assertEqual(auth.get_provider("GPT").name, "openai")

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(auth.AuthError):
            auth.get_provider("not-a-provider")


class RedactionTests(AuthTestCase):
    def test_redaction_never_reveals_the_middle(self) -> None:
        secret = "sk-ant-api03-" + "S" * 40 + "TAIL"
        shown = auth.redact(secret)
        self.assertNotIn("S" * 40, shown)
        self.assertNotIn(secret, shown)
        self.assertTrue(shown.startswith("sk-ant-"))
        self.assertIn("TAIL", shown)

    def test_short_secrets_are_fully_masked(self) -> None:
        self.assertEqual(auth.redact("abc"), "***")

    def test_missing_secret_reads_as_unset(self) -> None:
        self.assertEqual(auth.redact(""), "<unset>")
        self.assertEqual(auth.redact(None), "<unset>")


class StorageTests(AuthTestCase):
    def test_add_detects_provider_and_round_trips(self) -> None:
        record = auth.add("nvapi-" + "x" * 30)
        self.assertEqual(record["provider"], "nvidia")
        self.assertEqual(auth.resolve("nvidia").source, auth.SOURCE_STORED)
        self.assertEqual(auth.resolve("nvidia").key, "nvapi-" + "x" * 30)

    def test_stored_file_never_contains_a_plaintext_key_in_the_record(self) -> None:
        auth.add("nvapi-" + "y" * 30)
        record = auth.add("sk-ant-api03-" + "z" * 30)
        # The returned record is safe to print; the file itself holds the key.
        self.assertNotIn("z" * 30, json.dumps(record))

    def test_explicit_provider_overrides_detection(self) -> None:
        record = auth.add("sk-ant-api03-abc", provider="openai")
        self.assertEqual(record["provider"], "openai")

    def test_empty_key_is_refused(self) -> None:
        with self.assertRaises(auth.AuthError):
            auth.add("   ")

    def test_undetectable_key_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(auth.AuthError):
            auth.add("mystery-key-value")

    def test_remove_reports_whether_anything_was_removed(self) -> None:
        auth.add("nvapi-" + "x" * 30)
        self.assertTrue(auth.remove_key("nvidia"))
        self.assertFalse(auth.remove_key("nvidia"))

    def test_corrupt_store_raises_rather_than_silently_losing_keys(self) -> None:
        Path(os.environ["YATRA_HARNESS_AUTH_FILE"]).write_text("{not json", encoding="utf-8")
        with self.assertRaises(auth.AuthError):
            auth.load_store()


class ResolutionTests(AuthTestCase):
    def test_environment_beats_the_store(self) -> None:
        auth.add("sk-ws-" + "stored" * 3, provider="dashscope")
        os.environ["DASHSCOPE_API_KEY"] = "sk-ws-from-environment"
        credential = auth.resolve("dashscope")
        self.assertEqual(credential.source, auth.SOURCE_ENV)
        self.assertEqual(credential.key, "sk-ws-from-environment")

    def test_store_is_used_when_the_variable_is_absent(self) -> None:
        auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        credential = auth.resolve("dashscope")
        self.assertEqual(credential.source, auth.SOURCE_STORED)

    def test_missing_credential_reports_none(self) -> None:
        credential = auth.resolve("dashscope")
        self.assertEqual(credential.source, auth.SOURCE_NONE)
        self.assertFalse(credential.available)

    def test_resolve_env_maps_a_route_variable_to_a_stored_key(self) -> None:
        """Routes name a variable, not a provider. This is the path the
        provider adapters and the doctor both take."""
        auth.add("nvapi-" + "r" * 30)
        credential = auth.resolve_env("NVIDIA_API_KEY")
        self.assertEqual(credential.source, auth.SOURCE_STORED)
        self.assertEqual(credential.provider, "nvidia")

    def test_resolve_env_of_an_unknown_variable_is_empty_not_an_error(self) -> None:
        credential = auth.resolve_env("SOME_PRIVATE_VAR")
        self.assertFalse(credential.available)
        self.assertEqual(credential.source, auth.SOURCE_NONE)

    def test_local_providers_are_ready_without_a_key(self) -> None:
        rows = {row["provider"]: row for row in auth.status()}
        self.assertTrue(rows["ollama"]["ready"])
        self.assertFalse(rows["openai"]["ready"])

    def test_status_redacts_every_key(self) -> None:
        secret = "nvapi-" + "q" * 40
        auth.add(secret)
        for row in auth.status():
            self.assertNotIn(secret, json.dumps(row))


if __name__ == "__main__":
    unittest.main()
