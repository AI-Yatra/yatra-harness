"""What a run says when approval cannot be granted.

A non-interactive run denied every apply_patch and reported "operator denied
approval". The model read that as a decision that might go the other way next
time, and spent its whole budget asking again -- twelve denied patches in one
run, then BUDGET_EXHAUSTED.

The distinction the harness was not drawing: an operator who said no, and an
operator who is not there to ask. Only the first might change its mind, and
the message has to say which one this is.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness.cli import _approval, _resolve_approval
from harness.config import PolicyConfig
from harness.core.contracts import RiskLevel, ToolSpec
from harness.execution.policy import PolicyEngine


def policy(mode: str = "mutations") -> PolicyConfig:
    return PolicyConfig(
        approval_mode=mode, allowed_commands=(), denied_commands=(),
        network_enabled=False, allowed_domains=(),
        command_timeout_seconds=30.0, browser_timeout_seconds=10.0,
    )


WRITE = ToolSpec("apply_patch", "", {"type": "object"}, RiskLevel.WRITE)


class Arguments:
    def __init__(self, **kwargs) -> None:
        self.yes = kwargs.pop("yes", False)
        self.approval = kwargs.pop("approval", None)


class MessageTests(unittest.TestCase):
    def decide(self, callback):
        with io.StringIO() as sink, redirect_stdout(sink):
            return PolicyEngine(policy(), ("apply_patch",), callback).evaluate(WRITE, {})

    def test_an_absent_approver_says_the_tool_cannot_be_used(self) -> None:
        decision = self.decide(None)
        self.assertFalse(decision.allowed)
        self.assertIn("no approver is available", decision.reason)
        self.assertIn("cannot be used", decision.reason)

    def test_an_absent_approver_tells_the_model_not_to_retry(self) -> None:
        # This is the whole fix: without it the model asks again every turn.
        self.assertIn("will not succeed", self.decide(None).reason)

    def test_a_refusing_approver_is_worded_as_a_decision(self) -> None:
        decision = self.decide(lambda *_: False)
        self.assertIn("denied", decision.reason)
        self.assertNotIn("no approver is available", decision.reason)

    def test_an_approving_approver_still_allows(self) -> None:
        self.assertTrue(self.decide(lambda *_: True).allowed)


class ResolutionTests(unittest.TestCase):
    def test_yes_approves(self) -> None:
        callback = _resolve_approval(Arguments(yes=True))
        self.assertTrue(callback(WRITE, {}, "because"))

    def test_auto_approves(self) -> None:
        callback = _resolve_approval(Arguments(approval="auto"))
        self.assertTrue(callback(WRITE, {}, "because"))

    def test_never_is_a_decision_and_keeps_an_approver(self) -> None:
        # "never" is the operator saying no, which is a real answer. It must
        # not be reported as nobody being there to ask.
        self.assertIsNotNone(_resolve_approval(Arguments(approval="never")))

    def test_prompt_without_a_terminal_has_no_approver_at_all(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertIsNone(_resolve_approval(Arguments()))

    def test_prompt_with_a_terminal_keeps_the_prompt(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=True):
            self.assertIsNotNone(_resolve_approval(Arguments()))

    def test_a_denying_approver_still_denies(self) -> None:
        with io.StringIO() as sink, redirect_stdout(sink):
            self.assertFalse(_approval(False)(WRITE, {}, "because"))


if __name__ == "__main__":
    unittest.main()
