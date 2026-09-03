"""What the approval question says about a command.

From a real session. A model sent `run_command` a string with newlines around
it instead of an array. The question that reached the operator was three lines
long, and the standing-permission option read "run any ? command for the rest
of this session" -- an "always" keyed on nothing, offered to someone who had
no way to tell what they were agreeing to.

The refusal check and the tool both split a string command before looking at
it. Only the part that talks to the operator did not.
"""

from __future__ import annotations

import unittest

from harness.config import PolicyConfig
from harness.core.contracts import RiskLevel, ToolSpec
from harness.repl.approvals import Gate, Mode

RUN = ToolSpec("run_command", "", {"type": "object"}, RiskLevel.EXECUTE)


def policy() -> PolicyConfig:
    return PolicyConfig(
        approval_mode="mutations",
        allowed_commands=(),
        denied_commands=(),
        network_enabled=False,
        allowed_domains=(),
        command_timeout_seconds=30.0,
        browser_timeout_seconds=10.0,
    )


def describe(command: object):
    gate = Gate(policy(), mode=Mode.SUGGEST, prompt=lambda _request: None)
    return gate._describe(RUN, {"command": command})


class StringCommandTests(unittest.TestCase):
    def test_a_string_command_names_the_program(self) -> None:
        self.assertEqual(describe("python -m unittest").target, "python")

    def test_the_standing_permission_names_the_program(self) -> None:
        """It used to offer "run any ? command for the rest of this session"."""
        self.assertIn("python", describe("python -m unittest").always_means)
        self.assertNotIn("?", describe("python -m unittest").always_means)

    def test_a_padded_command_asks_on_one_line(self) -> None:
        request = describe("\n\npython -m unittest discover -s tests\n\n")
        self.assertEqual(request.question, "Run python -m unittest discover -s tests?")
        self.assertNotIn("\n", request.question)

    def test_a_padded_command_still_names_the_program(self) -> None:
        self.assertEqual(describe("\npython -m unittest\n").target, "python")


class ListCommandTests(unittest.TestCase):
    def test_an_array_command_is_unchanged(self) -> None:
        request = describe(["python", "-m", "unittest"])
        self.assertEqual(request.question, "Run python -m unittest?")
        self.assertEqual(request.target, "python")

    def test_a_padded_array_head_still_names_the_program(self) -> None:
        """The same padding that used to walk through the deny list."""
        self.assertEqual(describe(["python ", "-m", "unittest"]).target, "python")


class ToolCardTests(unittest.TestCase):
    """The one-line rendering beside the tool name has to be one line."""

    def test_a_padded_command_renders_on_one_line(self) -> None:
        from harness.repl.agent import describe_arguments
        from harness.repl.conversation import ToolCall

        call = ToolCall(id="1", name="run_command", arguments={"command": "\npython -m x\n"})
        self.assertEqual(describe_arguments(call), "python -m x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
