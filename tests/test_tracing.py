"""Traces: tying a run to the runs around it.

Each run's ledger explains that run and stops there. A goal is several runs, a
session is many, and a delegation is a run inside a run -- and none of those
relationships were written down anywhere, so "what happened" could only be
reconstructed by reading directory names and guessing.

Spans are recorded in the shape OpenTelemetry uses (trace id, span id, parent
span id, start and end, attributes) rather than through an SDK. The shape is
what makes the data portable; a vendor dependency in the hot path of a
teaching harness is a cost with no matching benefit.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.record.tracing import SpanRecorder, new_trace_id, parse_trace_context


class TraceIdTests(unittest.TestCase):
    def test_a_trace_id_is_a_32_character_hex_string(self) -> None:
        # The OpenTelemetry wire format. Matching it means these spans can be
        # shipped to a collector without rewriting the identifiers.
        value = new_trace_id()
        self.assertEqual(len(value), 32)
        int(value, 16)

    def test_two_trace_ids_differ(self) -> None:
        self.assertNotEqual(new_trace_id(), new_trace_id())


class TraceContextTests(unittest.TestCase):
    def test_an_empty_context_yields_nothing(self) -> None:
        self.assertEqual(parse_trace_context(""), (None, None))

    def test_a_context_carries_a_trace_and_a_parent_span(self) -> None:
        trace, span = parse_trace_context("a" * 32 + ":" + "b" * 16)
        self.assertEqual(trace, "a" * 32)
        self.assertEqual(span, "b" * 16)

    def test_a_malformed_context_is_ignored_rather_than_fatal(self) -> None:
        # Tracing must never be the reason a run does not start.
        self.assertEqual(parse_trace_context("not-a-context"), (None, None))
        self.assertEqual(parse_trace_context("zzz:zzz"), (None, None))


class SpanRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-tracing-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "spans.jsonl"

    def recorder(self, **kwargs) -> SpanRecorder:
        return SpanRecorder(self.path, run_id="run-1", **kwargs)

    def spans(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_a_span_is_written_when_it_closes(self) -> None:
        recorder = self.recorder()
        with recorder.span("turn", {"turn": 1}):
            pass
        self.assertEqual(len(self.spans()), 1)
        self.assertEqual(self.spans()[0]["name"], "turn")

    def test_a_span_records_its_duration(self) -> None:
        recorder = self.recorder()
        with recorder.span("turn"):
            pass
        span = self.spans()[0]
        self.assertGreaterEqual(span["end_unix_nano"], span["start_unix_nano"])

    def test_nested_spans_record_their_parent(self) -> None:
        recorder = self.recorder()
        with recorder.span("turn"):
            with recorder.span("tool"):
                pass
        by_name = {span["name"]: span for span in self.spans()}
        self.assertEqual(by_name["tool"]["parent_span_id"], by_name["turn"]["span_id"])

    def test_every_span_shares_the_run_trace_id(self) -> None:
        recorder = self.recorder()
        with recorder.span("a"):
            pass
        with recorder.span("b"):
            pass
        self.assertEqual(len({span["trace_id"] for span in self.spans()}), 1)

    def test_a_run_joins_the_trace_it_was_given(self) -> None:
        # This is what ties a sub-agent to its parent, and every run in a goal
        # or a session to the pursuit it belongs to.
        trace = "c" * 32
        recorder = self.recorder(trace_id=trace, parent_span_id="d" * 16)
        with recorder.span("root"):
            pass
        span = self.spans()[0]
        self.assertEqual(span["trace_id"], trace)
        self.assertEqual(span["parent_span_id"], "d" * 16)

    def test_the_context_it_hands_down_names_the_current_span(self) -> None:
        recorder = self.recorder()
        with recorder.span("root"):
            handed = recorder.context()
        trace, parent = parse_trace_context(handed)
        self.assertEqual(trace, recorder.trace_id)
        self.assertEqual(parent, self.spans()[0]["span_id"])

    def test_a_failing_span_records_the_error_and_re_raises(self) -> None:
        recorder = self.recorder()
        with self.assertRaises(ValueError):
            with recorder.span("boom"):
                raise ValueError("exploded")
        span = self.spans()[0]
        self.assertEqual(span["status"], "ERROR")
        self.assertIn("exploded", span["attributes"]["error"])

    def test_attributes_are_recorded(self) -> None:
        recorder = self.recorder()
        with recorder.span("tool", {"tool": "read_file", "ok": True}):
            pass
        self.assertEqual(self.spans()[0]["attributes"]["tool"], "read_file")

    def test_the_run_id_is_on_every_span(self) -> None:
        recorder = self.recorder()
        with recorder.span("a"):
            pass
        self.assertEqual(self.spans()[0]["attributes"]["run_id"], "run-1")

    def test_a_recorder_that_cannot_write_does_not_break_the_run(self) -> None:
        # Observability is not worth a run. A directory that cannot be created
        # must degrade to no spans, not to no work.
        blocked = Path(self.temporary.name) / "file"
        blocked.write_text("not a directory", encoding="utf-8")
        recorder = SpanRecorder(blocked / "spans.jsonl", run_id="run-1")
        with recorder.span("a"):
            pass  # must not raise

    def test_a_disabled_recorder_writes_nothing(self) -> None:
        recorder = self.recorder(enabled=False)
        with recorder.span("a"):
            pass
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
