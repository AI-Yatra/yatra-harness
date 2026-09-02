"""Unit tests for LLM Light priority-based routing."""

from __future__ import annotations

import unittest

from harness.core.errors import RoutingError
from harness.models.llm_light import (
    DEFAULT_PRIORITIES,
    LLMLight,
    LLMLightConfig,
    RouteProfile,
    RoutingConstraints,
    RoutingPolicy,
    llm_light_from_dict,
)

ROUTES = {
    "local-small": RouteProfile(
        name="local-small", local=True, latency="medium", quality=2.5, context_window=32_768
    ),
    "local-fast": RouteProfile(
        name="local-fast", local=True, latency="low", quality=3.0, context_window=16_384
    ),
    "remote-cheap": RouteProfile(
        name="remote-cheap",
        local=False,
        cost_per_1m_input=0.15,
        cost_per_1m_output=0.60,
        latency="low",
        quality=3.5,
        context_window=128_000,
    ),
    "remote-frontier": RouteProfile(
        name="remote-frontier",
        local=False,
        cost_per_1m_input=3.0,
        cost_per_1m_output=15.0,
        latency="medium",
        quality=5.0,
        context_window=200_000,
    ),
}


class LLMLightOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.light = LLMLight(LLMLightConfig())

    def test_default_order_prioritizes_privacy_then_quality(self) -> None:
        plan = self.light.plan(ROUTES)
        # Both local routes are private; the quality tie-break picks the
        # better local model first.
        self.assertEqual(plan.primary, "local-fast")
        self.assertEqual(
            plan.routes,
            ("local-fast", "local-small", "remote-frontier", "remote-cheap"),
        )

    def test_quality_first_puts_frontier_on_top(self) -> None:
        plan = self.light.plan(ROUTES, priorities=("quality", "cost"))
        self.assertEqual(plan.primary, "remote-frontier")

    def test_cost_first_puts_cheapest_first(self) -> None:
        plan = self.light.plan(ROUTES, priorities=("cost", "latency"))
        # Every local route costs nothing; latency then decides between them.
        self.assertEqual(plan.primary, "local-fast")
        self.assertEqual(plan.routes[-1], "remote-frontier")

    def test_latency_first(self) -> None:
        plan = self.light.plan(ROUTES, priorities=("latency", "cost"))
        # local-fast, remote-cheap both low; local-fast wins on privacy next.
        self.assertEqual(plan.primary, "local-fast")

    def test_context_first_requires_large_window(self) -> None:
        plan = self.light.plan(ROUTES, priorities=("context", "quality"))
        self.assertEqual(plan.primary, "remote-frontier")

    def test_privacy_constraint_excludes_remote(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                constraints=RoutingConstraints(require_local=True),
            )
        )
        plan = light.plan(ROUTES)
        self.assertNotIn("remote-cheap", plan.routes)
        self.assertNotIn("remote-frontier", plan.routes)
        excluded = [item.name for item in plan.decisions if not item.selected]
        self.assertEqual(sorted(excluded), ["remote-cheap", "remote-frontier"])

    def test_cost_ceiling_excludes_frontier(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                constraints=RoutingConstraints(max_cost_per_1m=1.0),
            )
        )
        plan = light.plan(ROUTES)
        self.assertNotIn("remote-frontier", plan.routes)
        self.assertIn("remote-cheap", plan.routes)

    def test_allowed_list_acts_as_whitelist(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                constraints=RoutingConstraints(allowed=("local-fast", "local-small")),
            )
        )
        plan = light.plan(ROUTES)
        self.assertEqual(set(plan.routes), {"local-fast", "local-small"})

    def test_denied_list_excludes_specific_route(self) -> None:
        light = LLMLight(
            LLMLightConfig(constraints=RoutingConstraints(denied=("remote-frontier",)))
        )
        plan = light.plan(ROUTES)
        self.assertNotIn("remote-frontier", plan.routes)

    def test_all_routes_excluded_raises(self) -> None:
        light = LLMLight(
            LLMLightConfig(constraints=RoutingConstraints(require_local=True))
        )
        remote_only = {name: route for name, route in ROUTES.items() if not route.local}
        with self.assertRaises(RoutingError):
            light.plan(remote_only)

    def test_weighted_mode_blends_scores(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                mode="weighted",
                weights={"quality": 0.6, "cost": 0.4},
            )
        )
        plan = light.plan(ROUTES)
        # remote-cheap combines good quality with low cost and wins the blend.
        self.assertEqual(plan.primary, "remote-cheap")
        self.assertEqual(plan.mode, "weighted")
        scored = [item for item in plan.decisions if item.selected]
        self.assertTrue(all(item.score is not None for item in scored))
        by_score = sorted(scored, key=lambda item: item.score or 0, reverse=True)
        self.assertEqual(by_score[0].name, plan.primary)

    def test_weighted_quality_dominated_picks_frontier(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                mode="weighted",
                weights={"quality": 0.95, "cost": 0.05},
            )
        )
        plan = light.plan(ROUTES)
        self.assertEqual(plan.primary, "remote-frontier")

    def test_weighted_cost_dominated_picks_cheap_local(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                mode="weighted",
                weights={"quality": 0.1, "cost": 0.9},
            )
        )
        plan = light.plan(ROUTES)
        # local-fast and local-small both cost nothing; local-fast wins the
        # quality tie-break through the residual quality weight.
        self.assertEqual(plan.primary, "local-fast")

    def test_profile_lookup(self) -> None:
        light = LLMLight(
            LLMLightConfig(
                profiles={
                    "offline": RoutingPolicy(
                        mode="lexicographic",
                        priorities=DEFAULT_PRIORITIES,
                        constraints=RoutingConstraints(require_local=True),
                    ),
                }
            )
        )
        plan = light.plan(ROUTES, profile="offline")
        self.assertEqual(set(plan.routes), {"local-fast", "local-small"})

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(RoutingError):
            self.light.plan(ROUTES, profile="nope")

    def test_unknown_priority_key_raises(self) -> None:
        with self.assertRaises(RoutingError):
            self.light.plan(ROUTES, priorities=("cost", "flavor"))

    def test_duplicate_priority_raises(self) -> None:
        with self.assertRaises(RoutingError):
            self.light.plan(ROUTES, priorities=("cost", "cost"))

    def test_plan_is_deterministic(self) -> None:
        first = self.light.plan(ROUTES)
        second = self.light.plan(ROUTES)
        self.assertEqual(first.routes, second.routes)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_plan_dict_has_no_credentials(self) -> None:
        plan = self.light.plan(ROUTES)
        serialized = str(plan.as_dict())
        self.assertNotIn("api", serialized)
        self.assertNotIn("base_url", serialized)
        self.assertNotIn("secret", serialized)


class LLMLightRoundTripTests(unittest.TestCase):
    def test_config_round_trip_preserves_policy(self) -> None:
        config = LLMLightConfig(
            enabled=True,
            default_profile="balanced",
            mode="weighted",
            weights={"quality": 0.45, "cost": 0.3, "latency": 0.15, "privacy": 0.1},
            constraints=RoutingConstraints(require_local=True, min_context_window=4096),
            profiles={
                "offline": RoutingPolicy(
                    mode="lexicographic",
                    priorities=("privacy", "quality"),
                    constraints=RoutingConstraints(require_local=True),
                ),
                "balanced": RoutingPolicy(
                    mode="weighted",
                    weights={"quality": 0.45, "cost": 0.3, "latency": 0.15, "privacy": 0.1},
                ),
            },
        )
        rebuilt = llm_light_from_dict(config.as_dict())
        self.assertEqual(rebuilt, config)
        plan = LLMLight(rebuilt).plan(ROUTES, profile="offline")
        self.assertEqual(set(plan.routes), {"local-fast", "local-small"})


class RouteProfileMetricTests(unittest.TestCase):
    def test_blended_cost_weights_input(self) -> None:
        profile = RouteProfile(
            name="x",
            local=False,
            cost_per_1m_input=1.0,
            cost_per_1m_output=1.0,
        )
        self.assertEqual(profile.blended_cost, 1.0)

    def test_metric_direction(self) -> None:
        profile = RouteProfile(name="x", local=True, quality=4.0, latency="low")
        value, higher_is_better = profile.metric("privacy")
        self.assertEqual((value, higher_is_better), (1.0, True))
        value, higher_is_better = profile.metric("cost")
        self.assertEqual((value, higher_is_better), (0.0, False))
        value, higher_is_better = profile.metric("latency")
        self.assertEqual((value, higher_is_better), (0.0, False))
        value, higher_is_better = profile.metric("quality")
        self.assertEqual((value, higher_is_better), (4.0, True))

    def test_unknown_metric_raises(self) -> None:
        profile = RouteProfile(name="x")
        with self.assertRaises(RoutingError):
            profile.metric("flavor")


if __name__ == "__main__":
    unittest.main()
