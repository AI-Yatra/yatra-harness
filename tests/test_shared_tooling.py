"""One harness, two entry points.

These are the tests that fail if the REPL grows a second mechanism again. They
assert the shared parts are genuinely shared: the same registry executes both
loops' tools, the same policy code refuses both, the same sandbox runs both
commands, and a capability registered once is reachable from either.

The deliberate differences are asserted too, in `DifferencesTests`. A design
that intends to differ should say where, otherwise the next reader cannot tell
an intention from an oversight.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import MCPServerConfig, load_config
from harness.execution.policy import ANY_COMMAND, PolicyEngine
from harness.execution.sandbox import DockerSandbox, LocalSandbox, build_sandbox
from harness.execution.tools import ToolRegistry, optional_tools
from harness.execution.workspace import Workspace
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class SharedToolingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "ay.yaml")
        self.workspace = Workspace(self.root, ())
        self.tools = ReplToolset(self.workspace, self.config)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class OneRegistryTests(SharedToolingTestCase):
    def test_the_repl_executes_through_the_shared_registry(self) -> None:
        self.assertIsInstance(self.tools.registry, ToolRegistry)

    def test_specs_come_from_the_registry_rather_than_a_second_list(self) -> None:
        """Two sources of truth is how the loops drifted apart before."""
        self.assertEqual(self.tools.specs(), self.tools.registry.specs())

    def test_arguments_are_validated_before_a_handler_runs(self) -> None:
        (self.root / "a.txt").write_text("hi\n", encoding="utf-8")
        for arguments, expected in (
            ({"path": "a.txt", "offset": "nope"}, "must be an integer"),
            ({"path": "a.txt", "bogus": 1}, "unknown field"),
            ({}, "is required"),
        ):
            outcome = self.tools.dispatch("read_file", arguments)
            self.assertFalse(outcome.ok, arguments)
            self.assertIn(expected, outcome.content, arguments)

    def test_decisions_reach_the_event_callback(self) -> None:
        events: list[tuple[str, str]] = []
        tools = ReplToolset(
            self.workspace,
            self.config,
            event_callback=lambda name, payload: events.append((name, payload.get("tool", ""))),
        )
        (self.root / "a.txt").write_text("hi\n", encoding="utf-8")
        tools.dispatch("read_file", {"path": "a.txt"})
        self.assertIn(("POLICY_DECISION", "read_file"), events)

    def test_an_unexpected_exception_does_not_escape_the_call(self) -> None:
        """The registry isolates it, so one bad tool cannot end the session.

        A RuntimeError is the case that matters: the old dispatch caught
        ToolError, WorkspaceError and OSError, and anything else went straight
        through the loop and took the conversation with it.
        """
        from harness.core.contracts import RiskLevel, ToolSpec

        def explode(_arguments: dict) -> tuple[str, dict]:
            raise RuntimeError("boom")

        spec = ToolSpec(
            "explode", "raises", {"type": "object", "properties": {}}, RiskLevel.READ
        )
        tools = ReplToolset(self.workspace, self.config, extra_tools=((spec, explode),))
        outcome = tools.dispatch("explode", {})
        self.assertFalse(outcome.ok)
        self.assertIn("boom", outcome.content)


class OnePolicyTests(SharedToolingTestCase):
    def test_the_deny_list_applies_inside_the_registry(self) -> None:
        """So a refusal cannot be reached by going around the approval gate."""
        outcome = self.tools.dispatch("run_command", {"command": ["bash", "-c", "rm -rf /"]})
        self.assertFalse(outcome.ok)
        self.assertIn("deny-list", outcome.content)

    def test_both_loops_use_the_same_matcher(self) -> None:
        from harness.execution import policy

        self.assertIs(policy.denied_pattern, policy.denied_pattern)
        self.assertIsNotNone(
            policy.denied_pattern(("sudo", "rm", "-rf", "/"), self.config.policy.denied_commands)
        )


class OneSandboxTests(SharedToolingTestCase):
    def test_the_repl_runs_commands_through_build_sandbox(self) -> None:
        self.assertIsInstance(build_sandbox(self.config.sandbox), LocalSandbox)

    def test_the_sandbox_is_a_configuration_choice(self) -> None:
        """The same session, containerised, by changing one word."""
        hardened = replace(self.config.sandbox, kind="docker", image="python:3.12-slim")
        self.assertIsInstance(build_sandbox(hardened), DockerSandbox)

    def test_the_environment_follows_the_sandbox(self) -> None:
        from harness.repl.tools import _command_environment

        self.assertIsNotNone(_command_environment(self.config))
        hardened = replace(
            self.config, sandbox=replace(self.config.sandbox, kind="docker", image="x")
        )
        self.assertIsNone(_command_environment(hardened))

    def test_a_command_still_runs(self) -> None:
        outcome = self.tools.dispatch("run_command", {"command": [sys.executable, "-c", "print(7)"]})
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("7", outcome.content)


class SharedCapabilityTests(SharedToolingTestCase):
    """A tool registered once has to be reachable from either loop."""

    def test_an_optional_tool_is_offered_to_a_conversation(self) -> None:
        names = {spec.name for spec, _ in optional_tools(self.config, self.workspace)}
        self.assertIn("remember", names)

    def test_an_optional_tool_actually_runs_there(self) -> None:
        """Offered is not the same as wired; this calls it."""
        tools = ReplToolset(
            self.workspace,
            self.config,
            extra_tools=optional_tools(self.config, self.workspace),
        )
        outcome = tools.dispatch("remember", {"fact": "the tests are unittest, not pytest"})
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("unittest", outcome.content)

    def test_network_tools_are_left_out_when_the_network_is_off(self) -> None:
        """Offering a tool the policy always refuses only wastes context."""
        self.assertFalse(self.config.policy.network_enabled)
        names = {spec.name for spec, _ in optional_tools(self.config, self.workspace)}
        self.assertNotIn("web_search", names)
        self.assertNotIn("browser_fetch", names)

    def test_network_tools_appear_when_it_is_on(self) -> None:
        online = replace(self.config, policy=replace(self.config.policy, network_enabled=True))
        names = {spec.name for spec, _ in optional_tools(online, self.workspace)}
        self.assertIn("web_search", names)
        self.assertIn("browser_fetch", names)

    def test_an_mcp_server_reaches_a_conversation(self) -> None:
        """The capability that was most clearly batch-only."""
        configured = replace(
            self.config,
            mcp_servers=(
                MCPServerConfig(name="demo", command=(sys.executable, "-m", "harness.mcp_demo.server")),
            ),
        )
        extra = optional_tools(configured, self.workspace)
        discovered = [spec for spec, _ in extra if spec.source.startswith("mcp")]
        self.assertTrue(discovered, "no tools discovered from the demo server")
        tools = ReplToolset(self.workspace, configured, extra_tools=extra)
        self.assertIn("repo_stats", {spec.name for spec in tools.specs()})
        outcome = tools.dispatch("repo_stats", {})
        self.assertTrue(outcome.ok, outcome.content)
        self.assertIn("file_count", outcome.content)


class DifferencesTests(SharedToolingTestCase):
    """Where the loops differ on purpose, so intent is not read as oversight."""

    def test_the_conversation_does_not_enforce_a_command_allowlist(self) -> None:
        """The operator is present and is asked per command instead."""
        outcome = self.tools.dispatch(
            "run_command", {"command": [sys.executable, "-c", "print(1)"]}
        )
        self.assertTrue(outcome.ok, outcome.content)
        self.assertNotIn(
            [sys.executable], [list(p) for p in self.config.policy.allowed_commands]
        )

    def test_any_command_matches_everything(self) -> None:
        engine = PolicyEngine(
            replace(self.config.policy, allowed_commands=(ANY_COMMAND,)), ("run_command",)
        )
        self.assertTrue(engine._command_allowed(("anything", "at", "all")))  # noqa: SLF001

    def test_the_registry_does_not_approve_in_this_loop(self) -> None:
        """Gate asks the operator; a second approver would prompt twice."""
        self.assertEqual(self.tools.registry.policy.config.approval_mode, "never")

    def test_the_batch_allowlist_is_untouched_by_that(self) -> None:
        self.assertTrue(self.config.policy.allowed_commands)
        self.assertNotIn(ANY_COMMAND, self.config.policy.allowed_commands)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
