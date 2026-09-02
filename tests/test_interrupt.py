"""Stopping a turn without corrupting what it was doing.

The agent already had a cooperative cancel and nothing called it, so every
interrupt arrived as a KeyboardInterrupt raised wherever the interpreter
happened to be. Usually harmless, occasionally inside `edit_file` between
reading a file and writing it back.

These tests cover the flag reaching the loop, the handler being installed and
removed around a turn, and the second press still being able to interrupt
immediately.
"""

from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.execution.workspace import Workspace
from harness.repl.agent import Agent, Events, Interrupted
from harness.repl.approvals import Gate, Mode
from harness.repl.conversation import Conversation
from harness.repl.model import ChatModel
from harness.repl.shell import Options, Shell
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class CancelFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.config = load_config(ROOT / "configs" / "ay.yaml")
        self.agent = Agent(
            model=ChatModel(self.config.router.routes["inception"]),
            conversation=Conversation("test"),
            toolset=ReplToolset(Workspace(self.root, ()), self.config),
            gate=Gate(self.config.policy, mode=Mode.FULL_AUTO),
            config=self.config,
            events=Events(),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_loop_runs_until_cancel_is_called(self) -> None:
        self.agent._check_cancelled()  # noqa: SLF001 - the check under test

    def test_cancel_makes_the_next_checkpoint_stop(self) -> None:
        self.agent.cancel()
        with self.assertRaises(Interrupted):
            self.agent._check_cancelled()  # noqa: SLF001

    def test_cancel_is_idempotent(self) -> None:
        self.agent.cancel()
        self.agent.cancel()
        with self.assertRaises(Interrupted):
            self.agent._check_cancelled()  # noqa: SLF001

    def test_a_new_turn_starts_uncancelled(self) -> None:
        """Otherwise one interrupt would end every later turn instantly."""
        self.agent.cancel()
        self.agent._cancel.clear()  # noqa: SLF001 - what send() does first
        self.agent._check_cancelled()  # noqa: SLF001

    def test_the_loop_checks_between_steps_and_between_tool_calls(self) -> None:
        """Both call sites matter: a turn can be long in either place."""
        source = (ROOT / "harness" / "repl" / "agent.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("self._check_cancelled()"), 2)


class InterruptHandlerTests(unittest.TestCase):
    """The shell's side: install a handler, and put back what was there."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.shell = Shell(
            Options(
                config_path=ROOT / "configs" / "ay.yaml",
                root=Path(self._tmp.name),
                mode=Mode.SUGGEST,
                model_override="inception",
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_handler_is_installed_for_the_turn(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        with self.shell._interruptible():  # noqa: SLF001
            during = signal.getsignal(signal.SIGINT)
        self.assertIsNot(during, before)

    def test_the_previous_handler_is_restored_afterwards(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        with self.shell._interruptible():  # noqa: SLF001
            pass
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_it_is_restored_even_when_the_turn_raises(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        with self.assertRaises(RuntimeError), self.shell._interruptible():  # noqa: SLF001
            raise RuntimeError("turn failed")
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_the_first_press_asks_the_agent_to_stop(self) -> None:
        with self.shell._interruptible():  # noqa: SLF001
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertTrue(self.shell.agent._cancel.is_set())  # noqa: SLF001

    def test_the_second_press_interrupts_immediately(self) -> None:
        """An operator pressing twice has stopped asking politely."""
        with self.shell._interruptible():  # noqa: SLF001
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGINT, None)

    def test_both_interrupt_shapes_end_the_turn_the_same_way(self) -> None:
        """`_run_turn` has to treat the cooperative stop like a Ctrl-C."""
        source = (ROOT / "harness" / "repl" / "shell.py").read_text(encoding="utf-8")
        self.assertIn("except (KeyboardInterrupt, Interrupted):", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
