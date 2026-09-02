"""Deterministic, checkpoint-aware fault injection for reliability exercises."""

from __future__ import annotations

import re
from collections.abc import Callable

from harness.core.contracts import RunState
from harness.core.errors import InjectedCrash, TransientProviderError

PersistCallback = Callable[[], None]
EventCallback = Callable[[str, dict], None]


class FaultInjector:
    def __init__(
        self,
        specification: str,
        state: RunState,
        *,
        persist: PersistCallback,
        event: EventCallback,
    ) -> None:
        self.specification = specification.strip()
        self.state = state
        self.persist = persist
        self.event = event

    def before_model(self, route: str, attempt: int) -> None:
        name = "model-timeout-once"
        if self.specification != name or name in self.state.triggered_faults:
            return
        self._mark(name, {"route": route, "attempt": attempt})
        raise TransientProviderError("injected one-time model timeout")

    def after_checkpointed_tool(self) -> None:
        match = re.fullmatch(r"crash-after-tool=(\d+)", self.specification)
        if not match:
            return
        name = self.specification
        if name in self.state.triggered_faults or self.state.tool_calls < int(match.group(1)):
            return
        self._mark(name, {"tool_calls": self.state.tool_calls})
        raise InjectedCrash(
            f"injected crash after durable checkpoint for tool call {self.state.tool_calls}"
        )

    def _mark(self, name: str, details: dict) -> None:
        self.state.triggered_faults.append(name)
        self.event("FAULT_INJECTED", {"fault": name, **details})
        self.persist()

