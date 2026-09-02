"""Retries, circuit breaking, and ordered provider fallback.

This module owns *reliability*. The order in which routes are tried is decided
by :mod:`harness.llm_light`, which owns *priority*. The split matters: an
operator restating what they care about must not perturb retry behaviour, and a
change to retry behaviour must not silently reorder their preferences.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from harness.core.contracts import ModelRequest, ModelResponse, RunState
from harness.core.errors import (
    ConfigurationError,
    PermanentProviderError,
    ProviderExhausted,
    TransientProviderError,
)
from harness.models.llm_light import LLMLight, LLMLightConfig, RouteProfile, RoutingPlan
from harness.models.providers import Provider, provider_for

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import ModelRouterConfig, RouteConfig

RouterEvent = Callable[[str, dict], None]
BeforeCall = Callable[[str, int], None]


class ModelRouter:
    def __init__(
        self,
        config: ModelRouterConfig,
        *,
        providers: dict[str, Provider] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        llm_light: LLMLight | None = None,
        routes: dict[str, RouteConfig] | None = None,
        pinned: str = "",
    ) -> None:
        self.config = config
        # Providers are built lazily per route: a credentialed route that is
        # ranked but never reached must not fail the run just by existing.
        self.providers = providers if providers is not None else {}
        self.sleeper = sleeper
        self.llm_light = llm_light
        self.routes = routes or config.routes
        self.pinned = pinned if pinned in self.routes else ""
        # Resolved at first use and then frozen: every turn of a run must see
        # the same ordering, or a retry could silently use a different model.
        self._plan: RoutingPlan | None = None
        # Where streamed text goes. Set by the runtime; None means the turn
        # arrives all at once, exactly as it always did.
        self.on_delta: Callable[[str], None] | None = None

    def _provider(self, route_name: str) -> Provider:
        if route_name not in self.providers:
            self.providers[route_name] = provider_for(self.routes[route_name])
        provider = self.providers[route_name]
        # Attached here rather than at construction so a provider injected by
        # a test stays untouched unless it declares the attribute itself.
        if hasattr(provider, "on_delta"):
            provider.on_delta = self.on_delta
        return provider

    def resolve_routes(self, event: RouterEvent | None = None) -> tuple[str, ...]:
        """Return the ordered route names for this run, deciding once.

        With LLM Light enabled the primary/fallbacks declared in config are a
        *fallback ordering of last resort*, not the routing decision: LLM Light
        ranks every configured route. An explicit ``--model`` still pins the
        primary, because a direct operator instruction outranks a derived plan.
        """
        if self._plan is not None:
            return self._plan.routes
        declared = (self.config.primary, *self.config.fallbacks)
        pinned = self.pinned
        if self.llm_light is None or not self.llm_light.config.enabled:
            routes = (pinned, *(name for name in declared if name != pinned)) if pinned else declared
            self._plan = RoutingPlan(
                profile="(declared)",
                mode="declared",
                priorities=(),
                routes=tuple(dict.fromkeys(routes)),
                decisions=(),
            )
        else:
            plan = self.llm_light.plan(
                {name: profile_from_route(route) for name, route in self.routes.items()}
            )
            if pinned:
                plan = replace(
                    plan,
                    routes=(pinned, *(name for name in plan.routes if name != pinned)),
                )
            self._plan = plan
            if event is not None:
                event("LLM_LIGHT_PLAN", plan.as_dict())
        if self._plan.routes and event is not None and self.llm_light is not None:
            event(
                "MODEL_ROUTES_RESOLVED",
                {
                    "ordered_routes": list(self._plan.routes),
                    "profile": self._plan.profile,
                    "mode": self._plan.mode,
                },
            )
        return self._plan.routes

    def call(
        self,
        request: ModelRequest,
        state: RunState,
        *,
        event: RouterEvent,
        before_call: BeforeCall | None = None,
    ) -> ModelResponse:
        routes = self.resolve_routes(event)
        errors = []
        for route_index, route_name in enumerate(routes):
            if route_name in state.opened_routes:
                event("MODEL_ROUTE_SKIPPED", {"route": route_name, "reason": "circuit open"})
                continue
            try:
                provider = self._provider(route_name)
            except ConfigurationError as exc:
                # e.g. a credentialed route whose key is missing: not worth
                # retrying, and definitely not worth taking down the run when
                # the next route may still succeed.
                errors.append(f"{route_name}: {exc}")
                state.route_failures[route_name] = self.config.circuit_breaker_failures
                self._open_if_needed(route_name, state, event)
                event(
                    "MODEL_ROUTE_FAILED",
                    {"route": route_name, "transient": False, "error": str(exc)},
                )
                continue
            for attempt in range(self.config.retries_per_route + 1):
                event(
                    "MODEL_ROUTE_STARTED",
                    {"route": route_name, "attempt": attempt + 1, "fallback": route_index > 0},
                )
                try:
                    if before_call:
                        before_call(route_name, attempt + 1)
                    cursor = state.provider_cursors.get(route_name, 0)
                    response = provider.complete(request, cursor=cursor)
                    if response.next_cursor is not None:
                        state.provider_cursors[route_name] = response.next_cursor
                    state.route_failures[route_name] = 0
                    event(
                        "MODEL_ROUTE_SUCCEEDED",
                        {"route": route_name, "provider": response.provider, "attempt": attempt + 1},
                    )
                    return response
                except (PermanentProviderError, ConfigurationError) as exc:
                    errors.append(f"{route_name}: {exc}")
                    state.route_failures[route_name] = self.config.circuit_breaker_failures
                    self._open_if_needed(route_name, state, event)
                    event("MODEL_ROUTE_FAILED", {"route": route_name, "transient": False, "error": str(exc)})
                    break
                except TransientProviderError as exc:
                    errors.append(f"{route_name}: {exc}")
                    state.retries += 1
                    failures = state.route_failures.get(route_name, 0) + 1
                    state.route_failures[route_name] = failures
                    event(
                        "MODEL_ROUTE_FAILED",
                        {
                            "route": route_name,
                            "transient": True,
                            "attempt": attempt + 1,
                            "error": str(exc),
                        },
                    )
                    self._open_if_needed(route_name, state, event)
                    if route_name in state.opened_routes or attempt >= self.config.retries_per_route:
                        break
                    # A provider that says how long to wait knows better than
                    # our doubling does. Ignoring it means either hammering an
                    # endpoint that just asked us not to, or sleeping much
                    # longer than it needs.
                    asked = getattr(exc, "retry_after", 0.0) or 0.0
                    delay = asked or self.config.backoff_seconds * (2**attempt)
                    event(
                        "MODEL_RETRY_SCHEDULED",
                        {
                            "route": route_name,
                            "delay_seconds": delay,
                            "source": "provider" if asked else "backoff",
                        },
                    )
                    if delay:
                        self.sleeper(delay)
            if route_index + 1 < len(routes):
                event("MODEL_FALLBACK", {"from": route_name, "to": routes[route_index + 1]})
        raise ProviderExhausted("all model routes failed: " + " | ".join(errors))

    def _open_if_needed(self, route_name: str, state: RunState, event: RouterEvent) -> None:
        if (
            state.route_failures.get(route_name, 0) >= self.config.circuit_breaker_failures
            and route_name not in state.opened_routes
        ):
            state.opened_routes.append(route_name)
            event(
                "MODEL_CIRCUIT_OPENED",
                {"route": route_name, "failures": state.route_failures[route_name]},
            )


def profile_from_route(route: RouteConfig) -> RouteProfile:
    """Project a transport route onto its decision attributes.

    Only ranking inputs cross this boundary. Endpoints and credential variable
    names are dropped so a routing decision can be logged and replayed freely.
    """
    return RouteProfile(
        name=route.name,
        local=route.local,
        cost_per_1m_input=route.cost_per_1m_input,
        cost_per_1m_output=route.cost_per_1m_output,
        latency=route.latency,
        quality=route.quality,
        context_window=route.context_window,
        tool_support=route.tool_support,
    )


def build_llm_light(config: LLMLightConfig) -> LLMLight | None:
    """Build an LLM Light instance, or None when the feature is disabled."""
    return LLMLight(config) if config.enabled else None
