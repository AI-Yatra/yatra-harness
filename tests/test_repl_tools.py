"""Tests for the conversational tool set.

These are the tools that touch the operator's real files, so the cases that
matter most are the refusals: an edit that would be ambiguous, a path that
would escape, a command shaped wrongly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.core.errors import WorkspaceError
from harness.execution.workspace import Workspace
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class ToolsetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "teaching.yaml")
        self.tools = ReplToolset(Workspace(self.root, ()), self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class ReadTests(ToolsetTestCase):
    def test_read_returns_numbered_lines(self) -> None:
        self.write("a.txt", "one\ntwo\nthree\n")
        outcome = self.tools.dispatch("read_file", {"path": "a.txt"})
        self.assertTrue(outcome.ok)
        self.assertIn("1", outcome.content)
        self.assertIn("two", outcome.content)
        self.assertEqual(outcome.detail, "3 lines")

    def test_read_honours_offset_and_limit(self) -> None:
        self.write("a.txt", "\n".join(str(n) for n in range(1, 101)))
        outcome = self.tools.dispatch("read_file", {"path": "a.txt", "offset": 50, "limit": 5})
        self.assertIn("50", outcome.content)
        self.assertIn("54", outcome.content)
        self.assertNotIn("│60", outcome.content.replace(" ", ""))
        self.assertIn("more lines", outcome.content)

    def test_reading_a_directory_says_so(self) -> None:
        (self.root / "sub").mkdir()
        outcome = self.tools.dispatch("read_file", {"path": "sub"})
        self.assertFalse(outcome.ok)
        self.assertIn("directory", outcome.content)

    def test_a_missing_file_is_an_error_not_a_crash(self) -> None:
        outcome = self.tools.dispatch("read_file", {"path": "nope.txt"})
        self.assertFalse(outcome.ok)

    def test_a_path_outside_the_root_is_refused(self) -> None:
        outcome = self.tools.dispatch("read_file", {"path": "../escape.txt"})
        self.assertFalse(outcome.ok)
        self.assertIn("escape", outcome.content.lower())

    def test_an_absolute_path_inside_the_root_is_accepted(self) -> None:
        path = self.write("a.txt", "hello\n")
        outcome = self.tools.dispatch("read_file", {"path": str(path)})
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("hello", outcome.content)

    def test_an_absolute_path_outside_the_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "x.txt"
            outside.write_text("secret", encoding="utf-8")
            outcome = self.tools.dispatch("read_file", {"path": str(outside)})
        self.assertFalse(outcome.ok)


class EditTests(ToolsetTestCase):
    def test_an_exact_edit_applies(self) -> None:
        self.write("m.py", "def add(a, b):\n    return a - b\n")
        outcome = self.tools.dispatch(
            "edit_file",
            {"path": "m.py", "old_string": "return a - b", "new_string": "return a + b"},
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("return a + b", (self.root / "m.py").read_text(encoding="utf-8"))

    def test_a_missing_old_string_says_to_read_the_file(self) -> None:
        self.write("m.py", "value = 1\n")
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "value = 2", "new_string": "value = 3"}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("not found", outcome.content)
        self.assertIn("Read the file", outcome.content)
        # The file must be untouched by a failed edit.
        self.assertEqual((self.root / "m.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_an_ambiguous_edit_is_refused_with_the_count(self) -> None:
        self.write("m.py", "x = 1\nx = 1\nx = 1\n")
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "x = 1", "new_string": "x = 2"}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("3 times", outcome.content)
        self.assertEqual((self.root / "m.py").read_text(encoding="utf-8"), "x = 1\nx = 1\nx = 1\n")

    def test_replace_all_makes_an_ambiguous_edit_explicit(self) -> None:
        self.write("m.py", "x = 1\nx = 1\n")
        outcome = self.tools.dispatch(
            "edit_file",
            {"path": "m.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True},
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertEqual((self.root / "m.py").read_text(encoding="utf-8"), "x = 2\nx = 2\n")

    def test_an_identical_replacement_is_refused(self) -> None:
        self.write("m.py", "a\n")
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "a", "new_string": "a"}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("identical", outcome.content)

    def test_an_edit_reports_the_line_counts(self) -> None:
        self.write("m.py", "one\ntwo\n")
        outcome = self.tools.dispatch(
            "edit_file", {"path": "m.py", "old_string": "two", "new_string": "two\nthree"}
        )
        self.assertTrue(outcome.ok)
        self.assertIn("+", outcome.detail)


class WriteTests(ToolsetTestCase):
    def test_write_creates_a_file_and_its_parents(self) -> None:
        outcome = self.tools.dispatch(
            "write_file", {"path": "pkg/sub/new.txt", "content": "hello\n"}
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertEqual((self.root / "pkg" / "sub" / "new.txt").read_text(encoding="utf-8"), "hello\n")

    def test_write_over_an_existing_file_says_updated(self) -> None:
        self.write("a.txt", "old\n")
        outcome = self.tools.dispatch("write_file", {"path": "a.txt", "content": "new\n"})
        self.assertIn("updated", outcome.content)

    def test_write_outside_the_root_is_refused(self) -> None:
        outcome = self.tools.dispatch("write_file", {"path": "../evil.txt", "content": "x"})
        self.assertFalse(outcome.ok)
        self.assertFalse((self.root.parent / "evil.txt").exists())


class SearchTests(ToolsetTestCase):
    def test_glob_finds_by_pattern(self) -> None:
        self.write("src/a.py", "")
        self.write("src/b.txt", "")
        outcome = self.tools.dispatch("glob", {"pattern": "*.py"})
        self.assertIn("src/a.py", outcome.content)
        self.assertNotIn("b.txt", outcome.content)

    def test_glob_skips_vendor_directories(self) -> None:
        self.write("node_modules/pkg/index.js", "")
        self.write("app.js", "")
        outcome = self.tools.dispatch("glob", {"pattern": "*.js"})
        self.assertIn("app.js", outcome.content)
        self.assertNotIn("node_modules", outcome.content)

    def test_grep_reports_file_and_line(self) -> None:
        self.write("a.py", "import os\nvalue = 3\n")
        outcome = self.tools.dispatch("grep", {"pattern": r"value\s*="})
        self.assertIn("a.py:2", outcome.content)

    def test_grep_rejects_a_bad_expression_clearly(self) -> None:
        outcome = self.tools.dispatch("grep", {"pattern": "([unclosed"})
        self.assertFalse(outcome.ok)
        self.assertIn("regular expression", outcome.content)

    def test_grep_can_be_scoped_by_glob(self) -> None:
        self.write("a.py", "needle\n")
        self.write("b.md", "needle\n")
        outcome = self.tools.dispatch("grep", {"pattern": "needle", "glob": "*.md"})
        self.assertIn("b.md", outcome.content)
        self.assertNotIn("a.py", outcome.content)

    def test_list_dir_marks_directories(self) -> None:
        self.write("sub/x.txt", "")
        outcome = self.tools.dispatch("list_dir", {})
        self.assertIn("sub/", outcome.content)


class CommandTests(ToolsetTestCase):
    def test_a_command_returns_its_output(self) -> None:
        outcome = self.tools.dispatch(
            "run_command", {"command": ["python", "-c", "print('hi')"]}
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("hi", outcome.content)

    def test_a_failing_command_is_reported_not_raised(self) -> None:
        """A failing test command is information the model needs, not an error."""
        outcome = self.tools.dispatch(
            "run_command", {"command": ["python", "-c", "import sys; print('boom'); sys.exit(3)"]}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("exit code 3", outcome.content)
        self.assertIn("boom", outcome.content)

    def test_a_string_command_is_split_when_it_is_unambiguous(self) -> None:
        outcome = self.tools.dispatch("run_command", {"command": "python --version"})
        self.assertTrue(outcome.ok, outcome.content)

    def test_shell_syntax_in_a_string_command_is_refused(self) -> None:
        outcome = self.tools.dispatch("run_command", {"command": "cat a | grep b"})
        self.assertFalse(outcome.ok)
        self.assertIn("shell syntax", outcome.content)

    def test_an_empty_command_is_refused(self) -> None:
        outcome = self.tools.dispatch("run_command", {"command": []})
        self.assertFalse(outcome.ok)

    def test_a_command_runs_in_the_working_directory(self) -> None:
        self.write("marker.txt", "")
        outcome = self.tools.dispatch(
            "run_command",
            {"command": ["python", "-c", "import os; print(os.path.exists('marker.txt'))"]},
        )
        self.assertIn("True", outcome.content)


class DispatchTests(ToolsetTestCase):
    def test_an_unknown_tool_is_reported(self) -> None:
        outcome = self.tools.dispatch("teleport", {})
        self.assertFalse(outcome.ok)
        self.assertIn("No such tool", outcome.content)

    def test_unparseable_arguments_come_back_as_a_message(self) -> None:
        outcome = self.tools.dispatch("read_file", {"__parse_error__": "bad JSON"})
        self.assertFalse(outcome.ok)
        self.assertIn("bad JSON", outcome.content)

    def test_every_declared_tool_has_a_handler(self) -> None:
        for spec in self.tools.specs():
            self.assertIn(spec.name, self.tools._handlers, spec.name)

    def test_every_tool_declares_a_schema_the_model_can_use(self) -> None:
        for spec in self.tools.specs():
            self.assertEqual(spec.input_schema["type"], "object")
            self.assertIn("properties", spec.input_schema)
            for name in spec.input_schema.get("required", []):
                self.assertIn(name, spec.input_schema["properties"], f"{spec.name}.{name}")

    def test_the_workspace_still_refuses_absolute_paths_directly(self) -> None:
        with self.assertRaises(WorkspaceError):
            Workspace(self.root, ()).resolve("/etc/passwd")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
