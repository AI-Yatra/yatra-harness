"""IFM, serving MBZUAI's K2 models.

An ordinary OpenAI-shaped provider, with one trap worth a test of its own.

IFM answers `GET /v1/models` to anyone, with no Authorization header at all.
Verified against the live endpoint: 200 and a two-model list, unauthenticated.
Several providers here are verified by listing models, and for this one that
would report success for a key that is expired, mistyped, or never supplied.
The only question an operator is asking when they run `auth verify` is whether
the key can complete, so this provider has to be verified by completing.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from harness.config import load_config
from harness.models import auth

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "ay.yaml"

# Confirmed live against https://api.ifm.ai/v1/models
MODELS = ("IFM/K2-Horizon-375B-A23B", "IFM/K2-Think-v2")


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = auth.get_provider("ifm")

    def test_the_documented_variable_is_the_first_one(self) -> None:
        """`export IFM_API_KEY=...` is what the quickstart tells people."""
        self.assertEqual(self.provider.env[0], "IFM_API_KEY")

    def test_the_base_url_is_the_documented_one(self) -> None:
        self.assertEqual(self.provider.base_url, "https://api.ifm.ai/v1")

    def test_it_speaks_the_openai_shape(self) -> None:
        self.assertEqual(self.provider.api, auth.API_OPENAI)

    def test_a_key_is_required(self) -> None:
        self.assertTrue(self.provider.needs_key)

    def test_the_documented_key_prefix_is_recognised(self) -> None:
        """So `ay auth add IFM-xf...` needs no --provider flag."""
        found = auth.detect("IFM-xf0000000000")
        self.assertIsNotNone(found, "the IFM- prefix is not recognised")
        self.assertEqual(found.name, "ifm")

    def test_the_prefix_does_not_collide_with_another_provider(self) -> None:
        """Two providers claiming one prefix makes `auth add` ask every time."""
        claimants = [p.name for p in auth.PROVIDERS if any(
            "IFM-".startswith(prefix) or prefix.startswith("IFM-") for prefix in p.prefixes
        )]
        self.assertEqual(claimants, ["ifm"])


class VerificationTests(unittest.TestCase):
    """The trap. A models probe here proves nothing about the key."""

    def test_the_key_is_checked_by_completing_not_by_listing(self) -> None:
        self.assertEqual(auth.get_provider("ifm").verify_via, auth.VERIFY_COMPLETION)

    def test_the_probe_models_are_ones_the_endpoint_serves(self) -> None:
        for model in auth.get_provider("ifm").probe_models:
            with self.subTest(model=model):
                self.assertIn(model, MODELS)

    def test_the_cheaper_model_is_probed_first(self) -> None:
        """70B dense before the 375B mixture of experts."""
        self.assertEqual(auth.get_provider("ifm").probe_models[0], "IFM/K2-Think-v2")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = load_config(CONFIG).router.routes

    def test_both_routes_exist(self) -> None:
        self.assertIn("ifm", self.routes)
        self.assertIn("ifm-think", self.routes)

    def test_the_default_route_is_the_agentic_model(self) -> None:
        """Horizon has the tool-calling serving config; Think is a reasoner."""
        self.assertEqual(self.routes["ifm"].model, "IFM/K2-Horizon-375B-A23B")

    def test_both_routes_name_ids_the_endpoint_actually_serves(self) -> None:
        for name in ("ifm", "ifm-think"):
            with self.subTest(route=name):
                self.assertIn(self.routes[name].model, MODELS)

    def test_both_routes_share_the_one_key(self) -> None:
        for name in ("ifm", "ifm-think"):
            with self.subTest(route=name):
                self.assertEqual(self.routes[name].api_key_env, "IFM_API_KEY")

    def test_the_route_variable_matches_the_provider(self) -> None:
        """A route naming a variable no provider claims cannot be stored into."""
        self.assertIn(self.routes["ifm"].api_key_env, auth.get_provider("ifm").env)

    def test_both_routes_point_at_the_provider_base_url(self) -> None:
        for name in ("ifm", "ifm-think"):
            with self.subTest(route=name):
                self.assertEqual(self.routes[name].base_url, auth.get_provider("ifm").base_url)

    def test_the_context_windows_are_the_published_ones(self) -> None:
        self.assertEqual(self.routes["ifm"].context_window, 524288)
        self.assertEqual(self.routes["ifm-think"].context_window, 262144)

    def test_a_stored_key_unlocks_both_routes(self) -> None:
        """What the startup hint offers when the primary route has no key."""
        from unittest import mock

        from harness.repl.shell import usable_routes

        class Resolved:
            def __init__(self, ok: bool) -> None:
                self.available = ok

        with mock.patch(
            "harness.repl.shell.auth.resolve_route",
            side_effect=lambda variable, _base: Resolved(variable == "IFM_API_KEY"),
        ):
            self.assertEqual(usable_routes(load_config(CONFIG)), ["ifm", "ifm-think"])


class ListingTests(unittest.TestCase):
    """`/model` has to stay readable as model ids get longer.

    The columns were fixed at 14 and 24 characters. `IFM/K2-Horizon-375B-A23B`
    is exactly 24, so the row rendered as `IFM/K2-Horizon-375B-A23Bno key`,
    with the status welded to the model id. A column sized for today's longest
    name is one route away from being too narrow, so they are measured now.
    """

    def listing(self) -> str:
        import subprocess
        import sys

        done = subprocess.run(
            [sys.executable, "ay.py", "--model", "local", "-C", str(ROOT)],
            cwd=ROOT, input="/model\n/exit\n",
            capture_output=True, text=True, timeout=180,
        )
        return done.stdout

    def test_every_route_is_separated_from_its_status(self) -> None:
        for line in self.listing().splitlines():
            if "no key" in line or "ready" in line:
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r"\s{2,}(no key|ready)\s*$")

    def test_the_longest_model_id_is_listed_in_full(self) -> None:
        self.assertIn("IFM/K2-Horizon-375B-A23B", self.listing())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
