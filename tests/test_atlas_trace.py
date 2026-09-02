"""Tests for the session recorder behind the atlas trace region.

The recorder's job is to be believable: it claims a component was on the path
and claims how long it held the turn, and the diagram states those numbers as
fact. So the cases here are the ones where it could lie without anyone
noticing, above all the timing arithmetic. Summing per-component durations
without subtracting nested calls once produced a harness that spent 14.4s
inside a 7.2s session, which no reader would have caught from the picture.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "atlas" / "scripts" / "trace_session.py"


def load_tracer():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("trace_session", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["trace_session"] = module
    spec.loader.exec_module(module)
    return module


class NamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tracer = load_tracer()

    def test_a_harness_module_becomes_a_component(self) -> None:
        self.assertEqual(self.tracer.component_of("harness.repl.agent"), "repl.agent")

    def test_anything_outside_the_package_is_dropped(self) -> None:
        for name in ("json", "urllib.request", "harnessing.other", ""):
            self.assertEqual(self.tracer.component_of(name), "", name)

    def test_a_component_reports_the_package_it_lives_in(self) -> None:
        self.assertEqual(self.tracer.layer_of("repl.agent"), "repl")
        self.assertEqual(self.tracer.layer_of("core.util"), "core")

    def test_a_root_level_module_has_no_layer(self) -> None:
        """`harness/cli.py` is an entry point, not a member of a layer."""
        self.assertEqual(self.tracer.layer_of("cli"), "")


class RecorderTests(unittest.TestCase):
    """Drive the hook over real calls rather than synthesising frames."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tracer = load_tracer()

    def record(self, work) -> object:
        rec = self.tracer.Recorder()
        rec.start()
        try:
            work()
        finally:
            rec.stop()
        return rec

    def test_it_records_the_components_a_call_passes_through(self) -> None:
        from harness.models import auth

        rec = self.record(lambda: auth.get_provider("inception"))
        self.assertIn("models.auth", rec.calls)

    def test_it_ignores_everything_outside_the_package(self) -> None:
        import json as json_module

        rec = self.record(lambda: json_module.dumps({"a": [1, 2, 3]}))
        self.assertEqual(rec.calls, {})
        self.assertEqual(rec.spans, [])

    def test_a_call_within_one_component_is_counted_but_not_a_crossing(self) -> None:
        """Only boundary crossings become spans; the rest would be noise.

        `resolve` calls several of its own module's helpers. All of them are
        counted, but the component was entered once, so it is one span.
        """
        from harness.models import auth

        rec = self.record(lambda: auth.resolve("inception"))
        self.assertGreater(rec.calls["models.auth"], 1)
        crossings = [s for s in rec.spans if s.component == "models.auth"]
        self.assertEqual(len(crossings), 1)

    def test_self_time_never_exceeds_the_time_actually_spent(self) -> None:
        """The bug this test exists for: nested calls counted twice.

        Every span's duration contains its callees' durations, so summing them
        across components overstates the total. Self time subtracts the
        children, which makes the sum bounded by the wall clock.
        """
        from harness.models import auth

        def work() -> None:
            for name in ("inception", "google", "dashscope"):
                auth.get_provider(name)
                auth.identify("sk_" + name)

        rec = self.record(work)
        elapsed_ms = max(s.t1 for s in rec.spans) * 1000
        total_self = sum(rec.self_ms.values())
        self.assertGreater(total_self, 0)
        self.assertLessEqual(total_self, elapsed_ms + 1e-6)

    def test_held_time_is_at_least_self_time_for_every_component(self) -> None:
        from harness.models import auth

        rec = self.record(lambda: auth.identify("sk_abc"))
        for name, self_ms in rec.self_ms.items():
            self.assertLessEqual(self_ms, rec.held_ms[name] + 1e-9, name)

    def test_the_first_harness_call_is_recorded_as_the_entry_point(self) -> None:
        from harness.models import auth

        rec = self.record(lambda: auth.get_provider("inception"))
        self.assertTrue(rec.entry.startswith("models.auth."))

    def test_every_span_is_closed_when_recording_stops(self) -> None:
        from harness.models import auth

        rec = self.record(lambda: auth.get_provider("inception"))
        self.assertTrue(rec.spans)
        for span in rec.spans:
            self.assertGreater(span.t1, 0.0, span.func)
            self.assertGreaterEqual(span.t1, span.t0, span.func)

    def test_an_edge_is_recorded_for_each_crossing(self) -> None:
        from harness.models import auth

        rec = self.record(lambda: auth.get_provider("inception"))
        self.assertTrue(rec.edges)
        for (caller, callee), count in rec.edges.items():
            self.assertNotEqual(caller, callee, "a component cannot cross into itself")
            self.assertGreater(count, 0)

    def test_the_hook_is_uninstalled_afterwards(self) -> None:
        """A profile hook left installed would slow every later test down."""
        from harness.models import auth

        self.record(lambda: auth.get_provider("inception"))
        self.assertIsNone(sys.getprofile())


class ProviderRouteTests(unittest.TestCase):
    """The route the recorded session runs against has to exist and be sane."""

    def test_inception_is_in_the_catalogue_with_a_chat_model_to_probe(self) -> None:
        from harness.models import auth

        provider = auth.get_provider("inception")
        self.assertEqual(provider.base_url, "https://api.inceptionlabs.ai/v1")
        self.assertEqual(provider.env, ("INCEPTION_API_KEY",))
        # mercury-edit-2 serves /fim and /edit and cannot call tools, so probing
        # with it would report a working key as broken.
        self.assertEqual(provider.probe_models, ("mercury-2",))

    def test_its_prefix_identifies_it_without_a_probe(self) -> None:
        from harness.models import auth

        detection = auth.identify("sk_0123456789abcdef")
        self.assertTrue(detection.certain)
        self.assertEqual(detection.provider.name, "inception")

    def test_the_underscore_prefix_does_not_capture_the_sk_dash_providers(self) -> None:
        from harness.models import auth

        self.assertFalse(auth.identify("sk-0123456789").certain)
        self.assertEqual(auth.identify("gsk_0123456789").provider.name, "groq")

    def test_the_repl_config_offers_the_route(self) -> None:
        from harness.config import load_config

        config = load_config(ROOT / "configs" / "ay.yaml")
        route = config.router.routes["inception"]
        self.assertEqual(route.model, "mercury-2")
        self.assertEqual(route.api_key_env, "INCEPTION_API_KEY")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
