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


class RouteResolutionTests(AuthTestCase):
    """A route names a variable and an endpoint. Either must reach the store.

    Naming a non-standard variable in a config is legitimate, and the error
    the adapters raise on a miss tells the operator to run `harness auth
    add` -- so a stored key that `add` accepted has to be reachable, or that
    advice sends them in a circle.
    """

    def test_a_known_variable_still_resolves(self) -> None:
        auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        credential = auth.resolve_route("DASHSCOPE_API_KEY")
        self.assertEqual(credential.source, auth.SOURCE_STORED)

    def test_the_environment_still_wins_over_the_store(self) -> None:
        auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        os.environ["DASHSCOPE_API_KEY"] = "sk-ws-from-environment"
        credential = auth.resolve_route("DASHSCOPE_API_KEY")
        self.assertEqual(credential.source, auth.SOURCE_ENV)
        self.assertEqual(credential.key, "sk-ws-from-environment")

    def test_a_custom_variable_resolves_through_the_endpoint(self) -> None:
        """The gap: `api_key_env: MY_QWEN_KEY` could not see a stored key."""
        auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        credential = auth.resolve_route(
            "MY_QWEN_KEY",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(credential.source, auth.SOURCE_STORED)
        self.assertEqual(credential.provider, "dashscope")

    def test_endpoint_matching_ignores_a_trailing_slash_and_case(self) -> None:
        auth.add("nvapi-" + "n" * 30)
        credential = auth.resolve_route(
            "CUSTOM", "HTTPS://Integrate.API.NVIDIA.com/v1/"
        )
        self.assertEqual(credential.provider, "nvidia")

    def test_a_custom_variable_set_in_the_environment_is_used_verbatim(self) -> None:
        os.environ["MY_QWEN_KEY"] = "sk-ws-exported"
        try:
            credential = auth.resolve_route("MY_QWEN_KEY", "https://example.invalid/v1")
            self.assertEqual(credential.source, auth.SOURCE_ENV)
            self.assertEqual(credential.key, "sk-ws-exported")
        finally:
            os.environ.pop("MY_QWEN_KEY", None)

    def test_an_unmatched_endpoint_resolves_to_nothing(self) -> None:
        auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        credential = auth.resolve_route("CUSTOM", "https://unknown.example/v1")
        self.assertFalse(credential.available)

    def test_a_stored_base_url_override_is_matched_too(self) -> None:
        """`auth add --base-url` records an endpoint; honour it on lookup."""
        auth.add("nvapi-" + "n" * 30, base_url="https://gateway.internal/v1")
        credential = auth.resolve_route("CUSTOM", "https://gateway.internal/v1")
        self.assertEqual(credential.provider, "nvidia")

    def test_no_endpoint_and_no_known_variable_is_empty_not_an_error(self) -> None:
        credential = auth.resolve_route("CUSTOM", "")
        self.assertFalse(credential.available)
        self.assertEqual(credential.source, auth.SOURCE_NONE)


class EnvFileTests(unittest.TestCase):
    """`.env` must parse the shapes people actually write, and both entry
    points must read it. Previously only `ay` loaded it, so a key that made
    the REPL work left `harness run` reporting no credential."""

    NAMES = ("YATRA_HARNESS_ENV_FILE", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY",
             "OPENAI_API_KEY", "QUOTED_KEY", "EXPORTED_KEY", "SPACED_KEY")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._previous = {name: os.environ.get(name) for name in self.NAMES}
        for name in self.NAMES:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _write(self, body: str) -> Path:
        path = self.tmp / ".env"
        path.write_text(body, encoding="utf-8")
        os.environ["YATRA_HARNESS_ENV_FILE"] = str(path)
        return path

    def test_a_plain_assignment_is_loaded(self) -> None:
        self._write("DASHSCOPE_API_KEY=sk-ws-plain\n")
        auth.load_env_file()
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-ws-plain")

    def test_double_quotes_are_stripped(self) -> None:
        self._write('QUOTED_KEY="sk-ws-quoted"\n')
        auth.load_env_file()
        self.assertEqual(os.environ["QUOTED_KEY"], "sk-ws-quoted")

    def test_single_quotes_are_stripped(self) -> None:
        self._write("QUOTED_KEY='sk-ws-quoted'\n")
        auth.load_env_file()
        self.assertEqual(os.environ["QUOTED_KEY"], "sk-ws-quoted")

    def test_an_export_prefix_is_accepted(self) -> None:
        """People paste the line they use in a shell profile."""
        self._write("export EXPORTED_KEY=sk-ws-exported\n")
        auth.load_env_file()
        self.assertEqual(os.environ["EXPORTED_KEY"], "sk-ws-exported")

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self._write("  SPACED_KEY  =  sk-ws-spaced  \n")
        auth.load_env_file()
        self.assertEqual(os.environ["SPACED_KEY"], "sk-ws-spaced")

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        self._write("# a comment\n\n   \nDASHSCOPE_API_KEY=sk-ws-real\n")
        auth.load_env_file()
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-ws-real")

    def test_a_key_containing_an_equals_sign_survives(self) -> None:
        self._write("DASHSCOPE_API_KEY=sk-ws-a=b=c\n")
        auth.load_env_file()
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-ws-a=b=c")

    def test_an_exported_variable_is_never_overwritten(self) -> None:
        os.environ["DASHSCOPE_API_KEY"] = "sk-ws-already-exported"
        self._write("DASHSCOPE_API_KEY=sk-ws-from-file\n")
        auth.load_env_file()
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-ws-already-exported")

    def test_a_missing_file_is_not_an_error(self) -> None:
        os.environ["YATRA_HARNESS_ENV_FILE"] = str(self.tmp / "absent.env")
        self.assertIsNone(auth.load_env_file())

    def test_an_unreadable_file_is_not_fatal(self) -> None:
        """A malformed .env must not stop the CLI from starting."""
        path = self.tmp / ".env"
        path.write_bytes(b"\xff\xfe not utf-8 at all \x00")
        os.environ["YATRA_HARNESS_ENV_FILE"] = str(path)
        auth.load_env_file()  # must not raise

    def test_the_file_is_found_by_walking_up_from_a_subdirectory(self) -> None:
        """Running from a nested working directory must still find it."""
        os.environ.pop("YATRA_HARNESS_ENV_FILE", None)
        (self.tmp / ".env").write_text("DASHSCOPE_API_KEY=sk-ws-walked\n", encoding="utf-8")
        nested = self.tmp / "a" / "b"
        nested.mkdir(parents=True)
        auth.load_env_file(nested)
        self.assertEqual(os.environ["DASHSCOPE_API_KEY"], "sk-ws-walked")


if __name__ == "__main__":
    unittest.main()
