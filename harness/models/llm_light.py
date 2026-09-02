"""LLM Light: turn declared operator priorities into an ordered model-route plan.

LLM Light owns exactly one decision: *in which order should routes be tried.*
It never transports a request, never retries, and never sees a credential. The
existing :mod:`harness.models.model_router` continues to own retries, backoff, and
circuit breaking on whatever order it is handed.

That split is deliberate and is the teaching point. Priorities are policy and
change per operator. Reliability is mechanism and changes per incident. Keeping
them apart means a new priority (say, "prefer the cheapest route that supports
tools") never touches the retry code, and a new retry strategy never silently
reorders an operator's preferences.

A route is described to LLM Light only by decision attributes -- locality,
cost, latency, quality, context window, tool support. Credentials and endpoints
are stripped at the boundary, so a routing decision can be logged and replayed
without leaking anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from harness.core.errors import RoutingError

# A blended token price is a proxy, not a bill. Agent workloads skew heavily
# toward input because the context is re-sent every turn, so input dominates.
INPUT_WEIGHT = 0.75
OUTPUT_WEIGHT = 0.25

PRIORITY_KEYS = ("privacy", "quality", "cost", "latency", "context")
LATENCY_RANKS = {"low": 0.0, "medium": 1.0, "high": 2.0}
MODES = ("lexicographic", "weighted")

# Score given to every candidate when the field cannot discriminate between them.
NEUTRAL_SCORE = 1.0

DEFAULT_PRIORITIES = ("privacy", "quality", "cost", "latency")


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """Decision attributes for one route. Transport details are deliberately absent."""

    name: str
    local: bool = True
    cost_per_1m_input: float = 0.0
    cost_per_1m_output: float = 0.0
    latency: str = "medium"
    quality: float = 3.0
    context_window: int = 8192
    tool_support: bool = True

    @property
    def blended_cost(self) -> float:
        return self.cost_per_1m_input * INPUT_WEIGHT + self.cost_per_1m_output * OUTPUT_WEIGHT

    def metric(self, key: str) -> tuple[float, bool]:
        """Return ``(value, higher_is_better)`` for one priority key."""
        if key == "privacy":
            return (1.0 if self.local else 0.0), True
        if key == "quality":
            return self.quality, True
        if key == "cost":
            return self.blended_cost, False
        if key == "latency":
            return LATENCY_RANKS[self.latency], False
        if key == "context":
            return float(self.context_window), True
        raise RoutingError(
            f"unknown priority {key!r}; expected one of {', '.join(PRIORITY_KEYS)}"
        )

    def as_metrics(self) -> dict[str, Any]:
        return {
            "local": self.local,
            "blended_cost_usd_per_1m": round(self.blended_cost, 4),
            "latency": self.latency,
            "quality": self.quality,
            "context_window": self.context_window,
            "tool_support": self.tool_support,
        }


@dataclass(frozen=True, slots=True)
class RoutingConstraints:
    """Hard filters. A route failing any of these is never a candidate."""

    require_local: bool = False
    require_tools: bool = True
    min_context_window: int = 0
    max_cost_per_1m: float | None = None
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()

    def rejects(self, profile: RouteProfile) -> list[str]:
        reasons = []
        if self.require_local and not profile.local:
            reasons.append("route is not local")
        if self.require_tools and not profile.tool_support:
            reasons.append("route does not support tool calls")
        if profile.context_window < self.min_context_window:
            reasons.append(
                f"context window {profile.context_window} is below the required "
                f"{self.min_context_window}"
            )
        if self.max_cost_per_1m is not None and profile.blended_cost > self.max_cost_per_1m:
            reasons.append(
                f"blended cost {profile.blended_cost:.4f} exceeds the ceiling "
                f"{self.max_cost_per_1m:.4f}"
            )
        if self.allowed and profile.name not in self.allowed:
            reasons.append("route is not on the allowed list")
        if profile.name in self.denied:
            reasons.append("route is denied by policy")
        return reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "require_local": self.require_local,
            "require_tools": self.require_tools,
            "min_context_window": self.min_context_window,
            "max_cost_per_1m": self.max_cost_per_1m,
            "allowed": list(self.allowed),
            "denied": list(self.denied),
        }


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    mode: str = "lexicographic"
    priorities: tuple[str, ...] = DEFAULT_PRIORITIES
    weights: Mapping[str, float] = field(default_factory=dict)
    constraints: RoutingConstraints = field(default_factory=RoutingConstraints)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise RoutingError(f"routing mode must be one of {', '.join(MODES)}")
        if self.mode == "weighted":
            # A weighted policy blends every key at once, so an ordering of
            # priorities carries no meaning and is not required.
            if not self.weights:
                raise RoutingError("weighted routing mode requires non-empty weights")
            unknown = sorted(set(self.weights) - set(PRIORITY_KEYS))
            if unknown:
                raise RoutingError(f"unknown weight keys: {', '.join(unknown)}")
            if any(value < 0 for value in self.weights.values()):
                raise RoutingError("routing weights must not be negative")
            if sum(self.weights.values()) <= 0:
                raise RoutingError("at least one routing weight must be positive")
            if self.priorities:
                validate_priorities(self.priorities)
            object.__setattr__(self, "priorities", ())
            return
        # Lexicographic mode compares keys one at a time, so the empty ordering
        # is meaningless; fall back to a sane default rather than failing.
        if not self.priorities:
            object.__setattr__(self, "priorities", DEFAULT_PRIORITIES)
            return
        validate_priorities(self.priorities)

    def with_priorities(self, priorities: tuple[str, ...]) -> RoutingPolicy:
        """Apply an ad-hoc priority order. An explicit order implies lexicographic."""
        return replace(self, mode="lexicographic", priorities=tuple(priorities))

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "priorities": list(self.priorities),
            "weights": dict(self.weights),
            "constraints": self.constraints.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class LLMLightConfig:
    enabled: bool = True
    default_profile: str = ""
    mode: str = "lexicographic"
    priorities: tuple[str, ...] = DEFAULT_PRIORITIES
    weights: Mapping[str, float] = field(default_factory=dict)
    constraints: RoutingConstraints = field(default_factory=RoutingConstraints)
    profiles: Mapping[str, RoutingPolicy] = field(default_factory=dict)

    def policy(
        self,
        profile: str = "",
        priorities: tuple[str, ...] = (),
    ) -> RoutingPolicy:
        name = profile or self.default_profile
        if name:
            if name not in self.profiles:
                raise RoutingError(
                    f"unknown LLM Light profile {name!r}; known profiles: "
                    f"{', '.join(sorted(self.profiles)) or '(none)'}"
                )
            base = self.profiles[name]
        else:
            base = RoutingPolicy(
                mode=self.mode,
                priorities=self.priorities,
                weights=dict(self.weights),
                constraints=self.constraints,
            )
        return base.with_priorities(priorities) if priorities else base

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_profile": self.default_profile,
            "mode": self.mode,
            "priorities": list(self.priorities),
            "weights": dict(self.weights),
            "constraints": self.constraints.as_dict(),
            "profiles": {name: policy.as_dict() for name, policy in sorted(self.profiles.items())},
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    name: str
    selected: bool
    rank: int
    reason: str
    score: float | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "route": self.name,
            "selected": self.selected,
            "rank": self.rank,
            "reason": self.reason,
            "metrics": dict(self.metrics),
        }
        if self.score is not None:
            value["score"] = round(self.score, 4)
        return value


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    profile: str
    mode: str
    priorities: tuple[str, ...]
    routes: tuple[str, ...]
    decisions: tuple[RouteDecision, ...]

    @property
    def primary(self) -> str:
        return self.routes[0]

    @property
    def fallbacks(self) -> tuple[str, ...]:
        return self.routes[1:]

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "mode": self.mode,
            "priorities": list(self.priorities),
            "ordered_routes": list(self.routes),
            "selected": [item.as_dict() for item in self.decisions if item.selected],
            "excluded": [item.as_dict() for item in self.decisions if not item.selected],
        }


def _constraints_from_dict(raw: Mapping[str, Any]) -> RoutingConstraints:
    ceiling = raw.get("max_cost_per_1m")
    return RoutingConstraints(
        require_local=bool(raw.get("require_local", False)),
        require_tools=bool(raw.get("require_tools", True)),
        min_context_window=int(raw.get("min_context_window", 0)),
        max_cost_per_1m=float(ceiling) if ceiling is not None else None,
        allowed=tuple(raw.get("allowed", ())),
        denied=tuple(raw.get("denied", ())),
    )


def _policy_from_dict(raw: Mapping[str, Any]) -> RoutingPolicy:
    return RoutingPolicy(
        mode=str(raw.get("mode", "lexicographic")),
        priorities=tuple(raw.get("priorities", ())),
        weights={str(key): float(value) for key, value in dict(raw.get("weights", {})).items()},
        constraints=(
            _constraints_from_dict(raw["constraints"])
            if "constraints" in raw
            else RoutingConstraints()
        ),
    )


def llm_light_from_dict(raw: Mapping[str, Any]) -> LLMLightConfig:
    """Rebuild an LLM Light config from its serialized form.

    Used when resuming a run so the routing decision is reproduced exactly
    rather than recomputed from config that may since have changed.
    """
    return LLMLightConfig(
        enabled=bool(raw.get("enabled", True)),
        default_profile=str(raw.get("default_profile", "")),
        mode=str(raw.get("mode", "lexicographic")),
        priorities=tuple(raw.get("priorities", DEFAULT_PRIORITIES)),
        weights={str(key): float(value) for key, value in dict(raw.get("weights", {})).items()},
        constraints=_constraints_from_dict(raw.get("constraints", {})),
        profiles={
            str(name): _policy_from_dict(value)
            for name, value in dict(raw.get("profiles", {})).items()
        },
    )


def validate_priorities(priorities: tuple[str, ...], path: str = "priorities") -> None:
    if not priorities:
        raise RoutingError(f"{path} must not be empty")
    unknown = [key for key in priorities if key not in PRIORITY_KEYS]
    if unknown:
        raise RoutingError(
            f"{path} contains unknown keys: {', '.join(unknown)}; "
            f"expected one of {', '.join(PRIORITY_KEYS)}"
        )
    duplicates = sorted({key for key in priorities if priorities.count(key) > 1})
    if duplicates:
        raise RoutingError(f"{path} contains duplicates: {', '.join(duplicates)}")


class LLMLight:
    """Rank routes by declared priorities. Pure: same inputs, same order, always."""

    def __init__(self, config: LLMLightConfig) -> None:
        self.config = config

    def plan(
        self,
        routes: Mapping[str, RouteProfile],
        *,
        profile: str = "",
        priorities: tuple[str, ...] = (),
    ) -> RoutingPlan:
        if not routes:
            raise RoutingError("no model routes are configured")
        policy = self.config.policy(profile, priorities)
        name = profile or self.config.default_profile

        candidates: list[RouteProfile] = []
        excluded: list[RouteDecision] = []
        for route_name in sorted(routes):
            candidate = routes[route_name]
            reasons = policy.constraints.rejects(candidate)
            if reasons:
                excluded.append(
                    RouteDecision(
                        name=route_name,
                        selected=False,
                        rank=0,
                        reason="; ".join(reasons),
                        metrics=candidate.as_metrics(),
                    )
                )
            else:
                candidates.append(candidate)
        if not candidates:
            detail = "; ".join(f"{item.name}: {item.reason}" for item in excluded)
            raise RoutingError(
                f"no route satisfies the declared constraints for profile "
                f"{name or '(default)'}: {detail}"
            )

        if policy.mode == "weighted":
            ordered, scores = self._weighted(candidates, policy)
        else:
            ordered, scores = self._lexicographic(candidates, policy)

        decisions = [
            RouteDecision(
                name=item.name,
                selected=True,
                rank=index + 1,
                reason=self._reason(policy, index),
                score=scores.get(item.name),
                metrics=item.as_metrics(),
            )
            for index, item in enumerate(ordered)
        ]
        return RoutingPlan(
            profile=name or "(default)",
            mode=policy.mode,
            priorities=policy.priorities,
            routes=tuple(item.name for item in ordered),
            decisions=(*decisions, *excluded),
        )

    @staticmethod
    def _lexicographic(
        candidates: list[RouteProfile], policy: RoutingPolicy
    ) -> tuple[list[RouteProfile], dict[str, float]]:
        def sort_key(candidate: RouteProfile) -> tuple:
            values = []
            for key in policy.priorities:
                value, higher_is_better = candidate.metric(key)
                values.append(-value if higher_is_better else value)
            # Name is the final tie-break so the order is deterministic.
            return (*values, candidate.name)

        return sorted(candidates, key=sort_key), {}

    @staticmethod
    def _weighted(
        candidates: list[RouteProfile], policy: RoutingPolicy
    ) -> tuple[list[RouteProfile], dict[str, float]]:
        total_weight = sum(policy.weights.values())
        scores = {candidate.name: 0.0 for candidate in candidates}
        for key, weight in policy.weights.items():
            if weight <= 0:
                continue
            values = {}
            higher_is_better = True
            for candidate in candidates:
                value, higher_is_better = candidate.metric(key)
                values[candidate.name] = value
            lowest, highest = min(values.values()), max(values.values())
            for candidate in candidates:
                if highest == lowest:
                    normalized = NEUTRAL_SCORE
                else:
                    normalized = (values[candidate.name] - lowest) / (highest - lowest)
                if not higher_is_better:
                    normalized = 1.0 - normalized
                scores[candidate.name] += weight * normalized
        for name in scores:
            scores[name] /= total_weight
        ordered = sorted(candidates, key=lambda item: (-scores[item.name], item.name))
        return ordered, scores

    @staticmethod
    def _reason(policy: RoutingPolicy, index: int) -> str:
        if index == 0:
            return "selected as primary by " + (
                "weighted score" if policy.mode == "weighted" else "priority order"
            )
        return f"fallback position {index} by " + (
            "weighted score" if policy.mode == "weighted" else "priority order"
        )
