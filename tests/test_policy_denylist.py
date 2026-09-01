"""The deny-list: an override the allowlist cannot argue with.

A prefix allowlist decides what shape a command has to start with. It cannot
express "never this, whatever else is true", because the dangerous forms are
usually reachable as arguments to a command that is legitimately allowed --
`python` is on the allowlist, and `python -c "..."` is arbitrary code.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import PolicyConfig, load_config
from harness.contracts import RiskLevel, ToolSpec
from harness.errors import ConfigurationError
from harness.policy import PolicyEngine

ROOT = Path(__file__).resolve().parents[1]
RUN_COMMAND = ToolSpec("run_command", "", {"type": "object"}, RiskLevel.EXECUTE)


def policy(**kwargs) -> PolicyConfig:
    defaults = {
        "approval_mode": "never",
        "allowed_commands": (("python",), ("git",)),
        "denied_commands": (),
        "network_enabled": False,
        "allowed_domains": (),
        "command_timeout_seconds": 30.0,
        "browser_timeout_seconds": 10.0,
    }
    return PolicyConfig(**{**defaults, **kwargs})


def engine(**kwargs) -> PolicyEngine:
    return PolicyEngine(policy(**kwargs), ("run_command",), lambda *_: True)


class DenyListTests(unittest.TestCase):
    def decide(self, command: list[str], **kwargs):
        return engine(**kwargs).evaluate(RUN_COMMAND, {"command": command})

    def test_an_allowed_command_still_passes(self) -> None:
        self.assertTrue(self.decide(["python", "-m", "unittest"]).allowed)

    def test_a_denied_pattern_is_refused_even_though_it_is_allowed(self) -> None:
        decision = self.decide(
            ["python", "-c", "import os"], denied_commands=(("python", "-c"),)
        )
        self.assertFalse(decision.allowed)
        self.assertIn("deny-list", decision.reason)

    def test_the_deny_list_beats_the_allowlist_regardless_of_order(self) -> None:
        decision = self.decide(
            ["git", "push", "origin", "main"],
            allowed_commands=(("git",),),
            denied_commands=(("git", "push"),),
        )
        self.assertFalse(decision.allowed)

    def test_a_denied_prefix_matches_anywhere_in_the_argument_list(self) -> None:
        # `python -m pip install` must be refused whether or not the model
        # puts flags in between; a prefix-only check is trivially dodged.
        decision = self.decide(
            ["python", "-X", "utf8", "-m", "pip", "install", "requests"],
            denied_commands=(("pip", "install"),),
        )
        self.assertFalse(decision.allowed)

    def test_an_unrelated_command_is_unaffected(self) -> None:
        self.assertTrue(
            self.decide(["python", "-m", "unittest"], denied_commands=(("pip", "install"),)).allowed
        )

    def test_python3_is_judged_like_python(self) -> None:
        decision = self.decide(["python3", "-c", "x"], denied_commands=(("python", "-c"),))
        self.assertFalse(decision.allowed)

    def test_the_reason_names_the_pattern_that_matched(self) -> None:
        decision = self.decide(["python", "-c", "x"], denied_commands=(("python", "-c"),))
        self.assertIn("python -c", decision.reason)

    def test_an_empty_deny_list_changes_nothing(self) -> None:
        self.assertTrue(self.decide(["python", "-m", "unittest"]).allowed)

    def test_a_denied_command_is_refused_before_approval_is_asked(self) -> None:
        asked: list[str] = []

        def approver(*arguments: object) -> bool:
            asked.append("asked")
            return True

        decision = PolicyEngine(
            policy(approval_mode="always", denied_commands=(("python", "-c"),)),
            ("run_command",),
            approver,
        ).evaluate(RUN_COMMAND, {"command": ["python", "-c", "x"]})
        self.assertFalse(decision.allowed)
        self.assertEqual(asked, [], "a denied command must never reach an approver")


class DenyListConfigTests(unittest.TestCase):
    def write(self, policy_body: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="harness-deny-"))
        self.addCleanup(lambda: None)
        path = directory / "config.yaml"
        path.write_text(
            "version: 1\n"
            "model_router:\n"
            "  primary: teaching\n"
            "  routes:\n"
            "    teaching:\n"
            "      kind: replay\n"
            f"      script: {ROOT / 'scenarios' / 'repair_demo.yaml'}\n"
            f"{policy_body}",
            encoding="utf-8",
        )
        return path

    def test_denied_commands_are_loaded(self) -> None:
        config = load_config(
            self.write("policy:\n  denied_commands:\n    - [python, -c]\n")
        )
        self.assertEqual(config.policy.denied_commands, (("python", "-c"),))

    def test_the_default_deny_list_is_empty(self) -> None:
        config = load_config(self.write("policy: {}\n"))
        self.assertEqual(config.policy.denied_commands, ())

    def test_a_malformed_deny_list_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_config(self.write("policy:\n  denied_commands: notalist\n"))


if __name__ == "__main__":
    unittest.main()
