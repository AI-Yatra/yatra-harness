"""Unit tests for ay.py.

Tests the task generator and slash-command parsing. Does not run the
harness itself (that would require an API key and a live model).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _import_chat():
    # Import lazily so the test works when ay.py is at the project root.
    sys.path.insert(0, str(ROOT))
    import ay  # noqa: PLC0415
    return ay


class ChatAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat = _import_chat()
        self.config = ROOT / "configs" / "palimpsest-config.yaml"
        self.skill = ROOT / "skills" / "palimpsest-skill.yaml"
        self.app = self.chat.ChatApp(self.config, self.skill, verbose=False)

    def test_slug_truncates_long_messages(self) -> None:
        slug = self.app._slug("a" * 200)
        self.assertLessEqual(len(slug), 40)
        self.assertTrue(slug.startswith("a"))

    def test_slug_normalizes_separators(self) -> None:
        slug = self.app._slug("Hello, World! Foo? Bar.")
        self.assertEqual(slug, "hello-world-foo-bar")

    def test_slug_empty_message_falls_back(self) -> None:
        self.assertEqual(self.app._slug(""), "task")
        self.assertEqual(self.app._slug("---"), "task")

    def test_write_task_produces_valid_yaml(self) -> None:
        path = self.app._write_task("Summarize the README")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertTrue(data["id"].startswith("chat-summarize-the-readme"))
            self.assertEqual(data["objective"], "Summarize the README")
            self.assertEqual(data["workspace_seed"], "../../fixtures/chat_seed")
            self.assertEqual(
                data["acceptance"]["commands"],
                [["python", "-c", "print('chat acceptance ok')"]],
            )
            self.assertFalse(data["acceptance"]["require_non_empty_diff"])
        finally:
            path.unlink(missing_ok=True)

    def test_write_task_handles_special_characters(self) -> None:
        # Special chars in the message should not break the YAML.
        path = self.app._write_task('Build "contact.xlsx" with columns: name, email, phone.')
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIn("contact.xlsx", data["objective"])
        finally:
            path.unlink(missing_ok=True)

    def test_detect_model_returns_string(self) -> None:
        # Doesn't have to be a specific model, just a non-empty string.
        self.assertTrue(isinstance(self.app.model, str))
        self.assertGreater(len(self.app.model), 0)


class CommandParsingTests(unittest.TestCase):
    """Verifies the public commands in the REPL (smoke tests via subprocess)."""

    def test_help_command_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ay.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        # argparse prog is pinned to "ay" so the usage line reads the same
        # whether the REPL is started as `ay`, `uv run ay`, or `python ay.py`.
        self.assertIn("usage: ay", result.stdout)
        self.assertIn("--config", result.stdout)
        self.assertIn("--skill", result.stdout)
        self.assertIn("--seed", result.stdout)
        self.assertIn("--accept", result.stdout)


class ChatTestCase(unittest.TestCase):
    """Shared isolation for tests that touch credentials or the config.

    Every test runs against a throwaway store and a non-existent .env, never
    the developer's real credentials.
    """

    ENV_VARS = ("YATRA_HARNESS_AUTH_FILE", "YATRA_HARNESS_ENV_FILE",
                "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "HARNESS_REMOTE_API_KEY")

    def setUp(self) -> None:
        self.chat = _import_chat()
        from harness import auth  # noqa: PLC0415
        self.auth = auth
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._previous_env = {name: os.environ.get(name) for name in self.ENV_VARS}
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(self.tmp / "auth.json")
        for name in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(name, None)
        # _load_env() would otherwise walk up to the developer's real .env
        # and setdefault a live key into the environment, hiding the bug.
        os.environ["YATRA_HARNESS_ENV_FILE"] = str(self.tmp / "absent.env")
        self.config = ROOT / "configs" / "palimpsest-config.yaml"
        self.skill = ROOT / "skills" / "palimpsest-skill.yaml"

    def tearDown(self) -> None:
        for name, value in self._previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _gate(self, config: Path | None = None) -> bool:
        """Run the startup gate, swallowing its operator-facing output."""
        app = self.chat.ChatApp(config or self.config, self.skill, verbose=False)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            return app._check_key()

    def _reprimaried(self, route: str) -> Path:
        """teaching.yaml with `route` promoted to primary.

        teaching.yaml lists five routes and `teaching` is first, so pointing
        primary at a later one separates "the primary route" from "the first
        route in the file" without inventing a config from scratch.
        """
        source = ROOT / "configs" / "teaching.yaml"
        text = source.read_text(encoding="utf-8")
        text = text.replace("primary: teaching", f"primary: {route}", 1)
        path = self.tmp / "reprimaried.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    DASHSCOPE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    def _config_needing(self, env_var: str, base_url: str) -> Path:
        """The default config, repointed at another provider.

        Both the variable and the endpoint move. Changing only one produces
        a config that contradicts itself -- a DashScope endpoint labelled
        with an Anthropic variable -- which is not a case worth pinning
        behaviour on.
        """
        text = self.config.read_text(encoding="utf-8")
        text = text.replace("api_key_env: DASHSCOPE_API_KEY", f"api_key_env: {env_var}")
        text = text.replace(f"base_url: {self.DASHSCOPE_URL}", f"base_url: {base_url}")
        path = self.tmp / "rerouted-config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

class CredentialGateTests(ChatTestCase):
    """The startup gate in `ay` must agree with `ay auth`.

    `ay auth add` and `harness auth add` are one code path by construction
    (ay delegates), so a key accepted by one must be visible to the other.
    """

    def test_stored_credential_satisfies_the_startup_gate(self) -> None:
        """The reported bug: `ay auth add` stores a key, then `ay` says none."""
        self.auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        self.assertEqual(self.auth.resolve("dashscope").source, self.auth.SOURCE_STORED)
        self.assertTrue(self._gate())

    def test_environment_still_satisfies_the_startup_gate(self) -> None:
        os.environ["DASHSCOPE_API_KEY"] = "sk-ws-from-environment"
        self.assertTrue(self._gate())

    def test_missing_credential_is_still_refused(self) -> None:
        self.assertFalse(self._gate())

    def test_gate_follows_the_config_not_a_hardcoded_provider(self) -> None:
        """The config states which variable it needs; the gate must read it."""
        config = self._config_needing("ANTHROPIC_API_KEY", "https://api.anthropic.com")
        self.auth.add("sk-ant-api03-" + "a" * 30)
        self.assertTrue(self._gate(config))

    def test_a_key_for_the_wrong_provider_does_not_open_the_gate(self) -> None:
        config = self._config_needing("ANTHROPIC_API_KEY", "https://api.anthropic.com")
        self.auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        self.assertFalse(self._gate(config))

    def test_a_stored_key_is_only_offered_to_its_own_endpoint(self) -> None:
        """The safety property behind resolving by endpoint.

        The endpoint fallback matches only a provider's own base URL, so it
        can hand a key to that provider and to nobody else. A config
        pointing a custom variable at somebody else's host gets nothing.
        """
        config = self._config_needing("CUSTOM_KEY", "https://api.anthropic.com")
        self.auth.add("sk-ws-" + "s" * 20, provider="dashscope")
        self.assertFalse(self._gate(config))

    def test_a_keyless_config_starts_without_any_credential(self) -> None:
        """teaching.yaml routes to a replay script: fully offline, no key."""
        self.assertTrue(self._gate(ROOT / "configs" / "teaching.yaml"))

    def test_an_unreadable_config_defers_to_the_harness(self) -> None:
        """The gate is a courtesy, not the config validator. A broken config
        must surface the harness's own error, not a bogus credential error."""
        path = self.tmp / "broken-config.yaml"
        path.write_text("this: [is not a valid harness config", encoding="utf-8")
        self.assertTrue(self._gate(path))


    def test_a_custom_variable_resolves_through_the_route_endpoint(self) -> None:
        """teaching.yaml's remote-api route names HARNESS_REMOTE_API_KEY and
        points at api.openai.com. A stored OpenAI key must satisfy it."""
        config = self._reprimaried("remote-api")
        self.auth.add("sk-proj-" + "o" * 30)
        self.assertTrue(self._gate(config))

    def test_a_custom_variable_with_no_stored_key_is_still_refused(self) -> None:
        config = self._reprimaried("remote-api")
        self.assertFalse(self._gate(config))


class RouteAwarenessTests(ChatTestCase):
    """`ay` must read and write the *primary* route, not the first one.

    Every config shipped here happens to list its primary route first, so a
    first-match regex is right by luck. Reorder the routes and /model would
    rewrite a different route's model line than the banner reports.
    """

    def test_detect_model_reads_the_primary_route_not_the_first(self) -> None:
        config = self._reprimaried("remote-api")
        app = self.chat.ChatApp(config, self.skill, verbose=False)
        # `teaching` is the first route in the file; `remote-api` is primary.
        self.assertEqual(app.model, "configure-me")

    def test_set_model_writes_the_primary_route_not_the_first(self) -> None:
        config = self._reprimaried("remote-api")
        app = self.chat.ChatApp(config, self.skill, verbose=False)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            app._set_model("gpt-4o-mini")
        self.assertEqual(app.model, "gpt-4o-mini")
        reloaded = self.chat.ChatApp(config, self.skill, verbose=False)
        self.assertEqual(reloaded.model, "gpt-4o-mini")
        # The first route must be untouched.
        body = config.read_text(encoding="utf-8")
        self.assertIn("model: deterministic-repair-demo", body)

    def test_set_model_preserves_comments_and_structure(self) -> None:
        # The default config carries comments inside the primary route's own
        # block, immediately above the model line -- exactly what a careless
        # rewrite would eat. teaching.yaml has none, so it cannot show this.
        source = (self.config).read_text(encoding="utf-8")
        config = self.tmp / "commented.yaml"
        config.write_text(source, encoding="utf-8")
        before = config.read_text(encoding="utf-8")
        self.assertTrue([line for line in before.splitlines()
                         if line.strip().startswith("#")],
                        "fixture must actually contain comments")
        app = self.chat.ChatApp(config, self.skill, verbose=False)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            app._set_model("gpt-4o-mini")
        after = config.read_text(encoding="utf-8")
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        for line in before.splitlines():
            if line.strip().startswith("#"):
                self.assertIn(line, after)

    def test_detect_model_on_an_unreadable_config_is_not_fatal(self) -> None:
        path = self.tmp / "broken.yaml"
        path.write_text("this: [is not valid", encoding="utf-8")
        app = self.chat.ChatApp(path, self.skill, verbose=False)
        self.assertEqual(app.model, "?")

    def test_set_model_on_an_unreadable_config_reports_and_does_not_write(self) -> None:
        path = self.tmp / "broken.yaml"
        original = "this: [is not valid"
        path.write_text(original, encoding="utf-8")
        app = self.chat.ChatApp(path, self.skill, verbose=False)
        with io.StringIO() as sink, contextlib.redirect_stdout(sink):
            app._set_model("gpt-4o-mini")
            output = sink.getvalue()
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertTrue(output.strip(), "a refusal must say something")


if __name__ == "__main__":
    unittest.main()
