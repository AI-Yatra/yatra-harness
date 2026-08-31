"""Unit tests for harness_chat.py.

Tests the task generator and slash-command parsing. Does not run the
harness itself (that would require an API key and a live model).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _import_chat():
    # Import lazily so the test works when harness_chat.py is at the project root.
    sys.path.insert(0, str(ROOT))
    import harness_chat  # noqa: PLC0415
    return harness_chat


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
        # Doesn't have to be a specific model — just a non-empty string.
        self.assertTrue(isinstance(self.app.model, str))
        self.assertGreater(len(self.app.model), 0)


class CommandParsingTests(unittest.TestCase):
    """Verifies the public commands in the REPL (smoke tests via subprocess)."""

    def test_help_command_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "harness_chat.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("harness_chat.py", result.stdout)
        self.assertIn("--config", result.stdout)
        self.assertIn("--skill", result.stdout)


if __name__ == "__main__":
    unittest.main()
