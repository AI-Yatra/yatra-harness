"""Spans: tying a run to the runs around it.

A run's ledger explains that run and stops there. A goal is several runs, a
session is many, and a delegation is a run inside a run -- and none of those
relationships were written down, so reconstructing what happened meant reading
directory names and guessing at the order.

Spans are recorded in the shape OpenTelemetry uses: a 32-hex trace id shared
by everything in one pursuit, a 16-hex span id per unit of work, and a parent
span id linking them. The shape is what makes the data portable -- these lines
can be shipped to a collector by anything that can read JSON -- while an SDK
in the hot path of a teaching harness would be a dependency, a version
constraint and a failure mode with no matching benefit.

Nothing here is allowed to end a run. A tracing failure that stops work trades
the record of the job for the job.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TRACE_NAMESPACE = uuid.UUID("6f3a1d64-6f6d-4b1e-9f3a-7a9d3c2b1e05")
TRACE_ID_CHARS = 32
SPAN_ID_CHARS = 16


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_for(name: str) -> str:
    """A stable trace id derived from a name.

    A session or a goal is one long-lived thing with many runs in it. Deriving
    the trace from its id means every run joins the same trace without anyone
    having to carry a value between processes -- including a session resumed
    days later from a different terminal.
    """
    return uuid.uuid5(TRACE_NAMESPACE, name).hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:SPAN_ID_CHARS]


def root_context(trace_id: str) -> str:
    """A context that joins `trace_id` with no parent span."""
    return f"{trace_id}:{'0' * SPAN_ID_CHARS}"


def format_trace_context(trace_id: str, span_id: str) -> str:
    """The one string a child process needs to join its parent's trace."""
    return f"{trace_id}:{span_id}"


def parse_trace_context(value: str) -> tuple[str | None, str | None]:
    """Read a trace context, treating anything malformed as absent.

    Tracing must never be the reason a run refuses to start, so a bad value
    here means "no parent" rather than an error.
    """
    if not value or ":" not in value:
        return None, None
    trace_id, _, span_id = value.partition(":")
    if len(trace_id) != TRACE_ID_CHARS or len(span_id) != SPAN_ID_CHARS:
        return None, None
    try:
        int(trace_id, 16)
        int(span_id, 16)
    except ValueError:
        return None, None
    return trace_id, span_id


class SpanRecorder:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.trace_id = trace_id or new_trace_id()
        self.enabled = enabled
        self._stack: list[str] = [parent_span_id] if parent_span_id else []

    def context(self) -> str:
        """The context a child run should join: this trace, the current span."""
        return format_trace_context(self.trace_id, self._stack[-1] if self._stack else "")

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        if not self.enabled:
            yield None
            return
        span_id = new_span_id()
        parent = self._stack[-1] if self._stack else None
        self._stack.append(span_id)
        started = time.time_ns()
        status = "OK"
        recorded = dict(attributes or {})
        try:
            yield span_id
        except BaseException as exc:
            status = "ERROR"
            recorded["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._stack.pop()
            recorded.setdefault("run_id", self.run_id)
            self._write(
                {
                    "trace_id": self.trace_id,
                    "span_id": span_id,
                    "parent_span_id": parent,
                    "name": name,
                    "start_unix_nano": started,
                    "end_unix_nano": time.time_ns(),
                    "status": status,
                    "attributes": recorded,
                }
            )

    def _write(self, span: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(span, sort_keys=True, ensure_ascii=False) + "\n"
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError:
            # Observability is not worth a run. A path that cannot be written
            # degrades to no spans, never to no work.
            self.enabled = False
