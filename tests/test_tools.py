"""Tests for the tool registry, policy gate, and workspace containment."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import PolicyConfig, load_config, load_skill
from harness.core.contracts import RiskLevel, ToolSpec
from harness.core.errors import ToolError, WorkspaceError
from harness.execution.policy import PolicyEngine
from harness.execution.tools import ToolRegistry, build_registry, validate_json_schema
from harness.execution.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]

ALL_TOOLS = (
    "repo_tree",
    "search_repo",
    "read_file",
    "apply_patch",
    "run_command",
    "python_run",
    "git_diff",
    "browser_fetch",
    "finish",
    "repo_stats",
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-ws-")
        self.root = Path(self.temporary.name) / "ws"
        self.root.mkdir()
        (self.root / "counter.py").write_text("value = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_resolve_rejects_absolute_and_escaping_paths(self) -> None:
        workspace = Workspace(self.root, ("tests/**",))
        with self.assertRaises(WorkspaceError):
            workspace.resolve("/etc/passwd")
        with self.assertRaises(WorkspaceError):
            workspace.resolve("../outside")
        with self.assertRaises(WorkspaceError):
            workspace.resolve("counter.py/../../etc/passwd")

    def test_protected_path_matching(self) -> None:
        workspace = Workspace(self.root, ("tests/**",))
        self.assertTrue(workspace.is_protected("tests/test_x.py"))
        self.assertTrue(workspace.is_protected("tests/"))
        self.assertTrue(workspace.is_protected("tests"))
        self.assertFalse(workspace.is_protected("counter.py"))

    def test_ensure_writable_rejects_protected(self) -> None:
        workspace = Workspace(self.root, ("tests/**",))
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            workspace.ensure_writable("tests/test_x.py")


class PolicyTests(unittest.TestCase):
    def _config(self, **overrides: object) -> PolicyConfig:
        base = {
            "approval_mode": "never",
            "allowed_commands": (("python", "-m", "unittest"), ("git", "diff")),
            "denied_commands": (),
            "network_enabled": False,
            "allowed_domains": (),
            "command_timeout_seconds": 30.0,
            "browser_timeout_seconds": 10.0,
        }
        base.update(overrides)
        return PolicyConfig(**base)

    def _tool(self, name: str = "read_file", risk: RiskLevel = RiskLevel.READ) -> ToolSpec:
        return ToolSpec(name, "test tool", {"type": "object"}, risk)

    def test_disabled_tool_is_denied(self) -> None:
        policy = PolicyEngine(self._config(), ("read_file",))
        decision = policy.evaluate(self._tool("apply_patch", RiskLevel.WRITE), {})
        self.assertFalse(decision.allowed)
        self.assertIn("not enabled", decision.reason)

    def test_network_denied_when_disabled(self) -> None:
        policy = PolicyEngine(self._config(), ("browser_fetch",))
        decision = policy.evaluate(self._tool("browser_fetch", RiskLevel.NETWORK), {"url": "https://x"})
        self.assertFalse(decision.allowed)

    def test_command_allowlist(self) -> None:
        policy = PolicyEngine(self._config(), ("run_command",))
        allowed = policy.evaluate(self._tool("run_command", RiskLevel.EXECUTE), {"command": ["python", "-m", "unittest"]})
        self.assertTrue(allowed.allowed)
        denied = policy.evaluate(self._tool("run_command", RiskLevel.EXECUTE), {"command": ["rm", "-rf", "/"]})
        self.assertFalse(denied.allowed)

    def test_command_allowlist_normalizes_python_spelling(self) -> None:
        # `python3` and `python` run the same interpreter after normalization,
        # so policy must judge them identically.
        policy = PolicyEngine(self._config(), ("run_command",))
        tool = self._tool("run_command", RiskLevel.EXECUTE)
        self.assertTrue(
            policy.evaluate(tool, {"command": ["python3", "-m", "unittest"]}).allowed
        )
        self.assertTrue(
            policy.evaluate(tool, {"command": ["python", "-m", "unittest"]}).allowed
        )
        self.assertFalse(
            policy.evaluate(tool, {"command": ["python", "-c", "import os"]}).allowed
        )

    def test_mutations_require_approval(self) -> None:
        config = self._config(approval_mode="mutations")
        policy = PolicyEngine(config, ("apply_patch",), approval_callback=lambda *_: True)
        decision = policy.evaluate(self._tool("apply_patch", RiskLevel.WRITE), {"patch": "x"})
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.requires_approval)

    def test_no_approver_denies_with_approval_required(self) -> None:
        config = self._config(approval_mode="mutations")
        policy = PolicyEngine(config, ("apply_patch",))
        decision = policy.evaluate(self._tool("apply_patch", RiskLevel.WRITE), {"patch": "x"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_approval)

    def test_operator_denial(self) -> None:
        config = self._config(approval_mode="mutations")
        policy = PolicyEngine(config, ("apply_patch",), approval_callback=lambda *_: False)
        decision = policy.evaluate(self._tool("apply_patch", RiskLevel.WRITE), {"patch": "x"})
        self.assertFalse(decision.allowed)
        self.assertIn("denied", decision.reason)


class SchemaValidationTests(unittest.TestCase):
    def test_rejects_unknown_fields(self) -> None:
        spec = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
        with self.assertRaises(ToolError):
            validate_json_schema({"path": "a", "extra": 1}, spec, "tool")

    def test_validates_nested_types(self) -> None:
        spec = {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "count": {"type": "integer"},
            },
            "required": ["command"],
        }
        validate_json_schema({"command": ["git", "diff"], "count": 2}, spec, "tool")
        with self.assertRaises(ToolError):
            validate_json_schema({"command": ["git", 42]}, spec, "tool")

    def test_enum_enforced(self) -> None:
        spec = {"type": "string", "enum": ["a", "b"]}
        validate_json_schema("a", spec, "tool")
        with self.assertRaises(ToolError):
            validate_json_schema("c", spec, "tool")


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-registry-")
        self.workspace_root = Path(self.temporary.name) / "workspace"
        self.workspace_root.mkdir()
        (self.workspace_root / "notes.txt").write_text("hello harness\n", encoding="utf-8")
        self.workspace = Workspace(self.workspace_root, ("tests/**",))
        self.artifacts_dir = Path(self.temporary.name) / "artifacts"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _registry(self) -> ToolRegistry:
        config = load_config(ROOT / "configs" / "teaching.yaml")
        skill = load_skill(ROOT / "skills" / "bugfix.yaml")
        policy = PolicyEngine(config.policy, skill.allowed_tools)
        return build_registry(
            config,
            skill,
            self.workspace,
            type("A", (), {"run_dir": self.artifacts_dir, "write_payload": lambda *a, **k: "x"})(),
            policy,
        )

    def test_registry_lists_all_expected_tools(self) -> None:
        registry = self._registry()
        names = {spec.name for spec in registry.specs()}
        self.assertTrue({"repo_tree", "search_repo", "read_file", "apply_patch", "finish"} <= names)
        self.assertIn("repo_stats", names)  # MCP tool normalized into the registry

    def test_read_file_returns_bounded_content(self) -> None:
        registry = self._registry()
        result = registry.execute("c1", "read_file", {"path": "notes.txt"})
        self.assertTrue(result.ok)
        self.assertIn("hello harness", result.content)

    def test_unknown_tool_returns_failure_result(self) -> None:
        registry = self._registry()
        result = registry.execute("c1", "no_such_tool", {})
        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.error or "")

    def test_workspace_escape_is_blocked(self) -> None:
        registry = self._registry()
        result = registry.execute("c1", "read_file", {"path": "../../../etc/passwd"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error or "")

    def test_schema_violation_returns_failure_result(self) -> None:
        registry = self._registry()
        result = registry.execute("c1", "read_file", {"path": 42})
        self.assertFalse(result.ok)


class PolicyIntegrationTests(unittest.TestCase):
    def test_skill_gate_blocks_extra_tools(self) -> None:
        config = load_config(ROOT / "configs" / "teaching.yaml")
        policy = PolicyEngine(config.policy, ("read_file", "finish"))
        with tempfile.TemporaryDirectory(prefix="harness-policy-"):
            registry = ToolRegistry(
                policy,
                max_output_chars=1000,
                artifacts=type("A", (), {"write_payload": lambda *a, **k: "x"})(),
            )
            from harness.core.contracts import ToolSpec

            registry.register(
                ToolSpec("read_file", "Read.", {"type": "object"}, RiskLevel.READ),
                lambda args: ("content", {}),
            )
            registry.register(
                ToolSpec("apply_patch", "Patch.", {"type": "object"}, RiskLevel.WRITE),
                lambda args: ("patched", {}),
            )
            result = registry.execute("c1", "apply_patch", {})
            self.assertFalse(result.ok)
            self.assertIn("not enabled", result.error or "")


if __name__ == "__main__":
    unittest.main()
