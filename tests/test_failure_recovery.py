"""What happens when the world misbehaves rather than the code.

A dropped socket, a crash between two writes, a command that outlives its
timeout, a file that is not text. None of these are bugs in the harness and all
of them used to end a session or a run, which is what makes them worth tests:
the failure is not the interesting part, the recovery is.
"""

from __future__ import annotations

import http.client
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.config import load_config
from harness.core.errors import StateError, TransientProviderError
from harness.execution.process import run_process
from harness.execution.workspace import Workspace
from harness.record.events import EventLog
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class ProviderErrorMappingTests(unittest.TestCase):
    """Everything recoverable has to arrive as TransientProviderError.

    That is the only vocabulary the router understands, so an exception outside
    it is not retried, not failed over, and not caught: it kills the turn.
    """

    def provider(self):
        from harness.models.providers import OpenAICompatibleProvider

        config = load_config(ROOT / "configs" / "ay.yaml")
        route = config.router.routes["inception"]
        return OpenAICompatibleProvider(route)

    def send_raising(self, error: BaseException):
        provider = self.provider()
        with patch("urllib.request.urlopen", side_effect=error):
            with patch.object(provider, "_secret", return_value="sk_test"):
                return provider.send({"model": "m", "messages": []})

    def test_a_connection_dropped_mid_response(self) -> None:
        """IncompleteRead is an HTTPException, not an OSError."""
        with self.assertRaises(TransientProviderError) as caught:
            self.send_raising(http.client.IncompleteRead(b"partial"))
        self.assertIn("mid-response", str(caught.exception))

    def test_other_http_protocol_errors(self) -> None:
        for error in (
            http.client.BadStatusLine("garbage"),
            http.client.LineTooLong("header line"),
            http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaises(TransientProviderError, msg=type(error).__name__):
                self.send_raising(error)

    def test_a_reset_socket(self) -> None:
        with self.assertRaises(TransientProviderError):
            self.send_raising(ConnectionResetError(104, "reset by peer"))

    def test_a_timeout(self) -> None:
        with self.assertRaises(TransientProviderError):
            self.send_raising(TimeoutError("timed out"))

    def test_a_200_carrying_html_rather_than_json(self) -> None:
        """A proxy error page is the usual source of this."""
        provider = self.provider()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<html><body>502 Bad Gateway</body></html>"

        with patch("urllib.request.urlopen", return_value=Response()):
            with patch.object(provider, "_secret", return_value="sk_test"):
                with self.assertRaises(TransientProviderError) as caught:
                    provider.send({"model": "m", "messages": []})
        self.assertIn("malformed", str(caught.exception))

    def test_a_body_that_is_not_valid_utf8(self) -> None:
        provider = self.provider()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"\xff\xfe\x00binary"

        with patch("urllib.request.urlopen", return_value=Response()):
            with patch.object(provider, "_secret", return_value="sk_test"):
                with self.assertRaises(TransientProviderError):
                    provider.send({"model": "m", "messages": []})


class LedgerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        log = EventLog(self.path, "r1")
        log.append("RUN_STARTED", {"n": 0})
        for index in range(1, 4):
            log.append("TURN_STARTED", {"n": index})
        self.clean = self.path.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_clean_ledger_reads_completely(self) -> None:
        self.assertEqual(len(list(EventLog(self.path, "r1").read())), 4)
        self.assertFalse(EventLog(self.path, "r1").truncated)

    def test_a_half_written_final_line_keeps_the_rest(self) -> None:
        """The crash shape: died between the write and the newline."""
        self.path.write_text(self.clean[:-12], encoding="utf-8")
        log = EventLog(self.path, "r1")
        self.assertEqual(len(list(log.read())), 3)
        self.assertTrue(log.truncated)

    def test_the_sequence_still_loads_after_a_torn_line(self) -> None:
        """Which is what lets the run be resumed rather than abandoned."""
        self.path.write_text(self.clean[:-12], encoding="utf-8")
        self.assertEqual(EventLog(self.path, "r1").sequence, 3)

    def test_a_ledger_that_is_only_a_torn_line_reads_as_empty(self) -> None:
        self.path.write_text('{"partial', encoding="utf-8")
        log = EventLog(self.path, "r1")
        self.assertEqual(list(log.read()), [])
        self.assertTrue(log.truncated)

    def test_damage_in_the_middle_still_refuses(self) -> None:
        """Nothing rewrites earlier lines, so this is real corruption."""
        lines = self.clean.splitlines(keepends=True)
        lines[1] = "{not json\n"
        self.path.write_text("".join(lines), encoding="utf-8")
        with self.assertRaises(StateError):
            list(EventLog(self.path, "r1").read())

    def test_appending_after_recovery_continues_the_sequence(self) -> None:
        self.path.write_text(self.clean[:-12], encoding="utf-8")
        log = EventLog(self.path, "r1")
        event = log.append("TURN_STARTED", {"n": 99})
        self.assertEqual(event.sequence, 4)
        self.assertEqual(len(list(EventLog(self.path, "r1").read())), 4)


class ProcessTreeTests(unittest.TestCase):
    """A timeout has to release what the command was holding."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "gc.py").write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        (self.root / "child.py").write_text(
            "import subprocess, sys, os, time\n"
            'g = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "gc.py")])\n'
            'print("GPID=%d" % g.pid, flush=True)\n'
            "time.sleep(120)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_grandchild_does_not_outlive_the_timeout(self) -> None:
        result = run_process(
            [sys.executable, str(self.root / "child.py")],
            cwd=self.root,
            timeout=4,
            max_output_chars=4_000,
        )
        self.assertTrue(result.timed_out)
        match = re.search(r"GPID=(\d+)", result.output)
        self.assertIsNotNone(match, result.output)
        pid = int(match.group(1))
        time.sleep(2)
        self.addCleanup(self._reap, pid)
        self.assertFalse(self._alive(pid), f"grandchild {pid} outlived the timeout")

    @staticmethod
    def _alive(pid: int) -> bool:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
            ).stdout
            return str(pid) in out
        try:
            import os

            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _reap(pid: int) -> None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            import contextlib
            import os
            import signal

            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


class WriteOverAwkwardFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.tools = ReplToolset(
            Workspace(self.root, ()), load_config(ROOT / "configs" / "ay.yaml")
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_binary_file_can_be_replaced_with_text(self) -> None:
        """The old contents are read only to count the change."""
        target = self.root / "blob.dat"
        target.write_bytes(b"\x00\x01\x02\xff\xfe" * 50)
        outcome = self.tools.dispatch("write_file", {"path": "blob.dat", "content": "clean\n"})
        self.assertTrue(outcome.ok, outcome.content)
        self.assertEqual(target.read_text(encoding="utf-8"), "clean\n")

    def test_a_latin1_file_can_be_replaced(self) -> None:
        target = self.root / "old.txt"
        target.write_bytes("café".encode("latin-1"))
        outcome = self.tools.dispatch("write_file", {"path": "old.txt", "content": "new\n"})
        self.assertTrue(outcome.ok, outcome.content)

    def test_writing_over_a_directory_is_refused_clearly(self) -> None:
        (self.root / "sub").mkdir()
        outcome = self.tools.dispatch("write_file", {"path": "sub", "content": "x"})
        self.assertFalse(outcome.ok)
        self.assertIn("directory", outcome.content)

    def test_an_ordinary_write_still_reports_its_counts(self) -> None:
        self.root.joinpath("a.txt").write_text("one\ntwo\n", encoding="utf-8")
        outcome = self.tools.dispatch("write_file", {"path": "a.txt", "content": "one\n"})
        self.assertTrue(outcome.ok)
        self.assertIn("-", outcome.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RetryAfterTests(unittest.TestCase):
    """A provider that says how long to wait should be believed."""

    @staticmethod
    def error(headers: dict[str, str]):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            "http://example", 429, "Too Many Requests", headers, io.BytesIO(b"{}")
        )

    def parse(self, headers: dict[str, str]) -> float:
        from harness.models.providers import _retry_after

        return _retry_after(self.error(headers))

    def test_delta_seconds_is_read(self) -> None:
        self.assertEqual(self.parse({"Retry-After": "30"}), 30.0)

    def test_a_fractional_value_is_read(self) -> None:
        self.assertEqual(self.parse({"Retry-After": "0.5"}), 0.5)

    def test_a_missing_header_falls_back_to_our_own_backoff(self) -> None:
        self.assertEqual(self.parse({}), 0.0)

    def test_the_http_date_form_is_declined_rather_than_guessed(self) -> None:
        """Clock skew makes a wrong answer worse than no answer."""
        self.assertEqual(self.parse({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 0.0)

    def test_an_absurd_wait_is_capped(self) -> None:
        self.assertEqual(self.parse({"Retry-After": "99999"}), 120.0)

    def test_a_negative_value_is_ignored(self) -> None:
        self.assertEqual(self.parse({"Retry-After": "-5"}), 0.0)

    def test_the_error_carries_it_to_the_router(self) -> None:
        error = TransientProviderError("rate limited", 429, 30)
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, 30.0)

    def test_errors_without_one_default_to_zero(self) -> None:
        self.assertEqual(TransientProviderError("boom").retry_after, 0.0)
