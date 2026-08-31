"""Side-effect-free reconstruction and integrity checking of a completed event ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .events import EventLog
from .util import content_hash


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    run_id: str
    events: int
    model_calls: int
    tool_calls: int
    verification_attempts: int
    terminal_event: str
    ledger_hash: str


def replay_run(run_dir: Path) -> ReplaySummary:
    run_id = run_dir.name
    log = EventLog(run_dir / "events.jsonl", run_id)
    events = list(log.read())
    if not events:
        raise ValueError(f"run has no events: {run_dir}")
    terminal = ""
    terminal_types = {
        "RUN_COMPLETED",
        "RUN_FAILED",
        "RUN_BLOCKED",
        "RUN_BUDGET_EXHAUSTED",
        "RUN_CANCELLED",
    }
    for event in events:
        if event.event_type in terminal_types:
            terminal = event.event_type
    return ReplaySummary(
        run_id=run_id,
        events=len(events),
        model_calls=sum(event.event_type == "MODEL_RESPONSE" for event in events),
        tool_calls=sum(event.event_type == "TOOL_RESULT" for event in events),
        verification_attempts=sum(event.event_type == "VERIFICATION_STARTED" for event in events),
        terminal_event=terminal,
        ledger_hash=content_hash(
            [
                {
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "payload": event.payload,
                }
                for event in events
            ]
        ),
    )

