"""The same claims, against the real endpoints.

Skipped unless `YATRA_HARNESS_LIVE=1`, so the suite stays offline and
deterministic. Run it when a provider changes something:

    YATRA_HARNESS_LIVE=1 uv run python -m unittest tests.test_auth_gateways_live

Nothing here needs a credential. Every assertion is about a URL existing, an
envelope's shape, or a deliberately invalid key being rejected -- the things
that break silently when a provider moves a path, and that a stubbed test
cannot notice.
"""

from __future__ import annotations

import os
import unittest

from harness.models import auth

LIVE = os.environ.get("YATRA_HARNESS_LIVE") == "1"
BOGUS = "definitely-not-a-real-key-000000"


@unittest.skipUnless(LIVE, "set YATRA_HARNESS_LIVE=1 to run live provider checks")
class LiveGatewayTests(unittest.TestCase):
    def test_google_rejects_an_invalid_key_at_the_openai_surface(self) -> None:
        """Proves the /openai suffix is right: without it this is a 404."""
        provider = auth.get_provider("google")
        with self.assertRaises(auth._HTTPFailure) as caught:
            auth.list_models(provider, BOGUS, timeout=30)
        message = str(caught.exception)
        self.assertIn("400", message)
        self.assertIn("API key", message)
        self.assertNotIn("404", message)

    def test_opencode_lists_models_without_a_key(self) -> None:
        models = auth.list_models(auth.get_provider("opencode"), "", timeout=30)
        self.assertGreater(len(models), 10)
        self.assertTrue(any(m.endswith("-free") for m in models))

    def test_commandcode_lists_models_without_a_key(self) -> None:
        models = auth.list_models(auth.get_provider("commandcode"), "", timeout=30)
        self.assertGreater(len(models), 10)

    def test_the_shipped_probe_models_still_exist(self) -> None:
        """A retired probe id would make `auth verify` fail for a valid key."""
        for name in ("opencode", "commandcode"):
            provider = auth.get_provider(name)
            available = auth.list_models(provider, "", timeout=30)
            chosen = auth._probe_model(provider, available)
            self.assertIn(chosen, available, name)
            self.assertTrue(chosen.endswith("-free"), f"{name} probe is not free: {chosen}")

    def test_an_invalid_key_is_rejected_by_both_gateways(self) -> None:
        """Listing succeeds for anyone, so this is the check that matters."""
        for name, variable in (
            ("opencode", "OPENCODE_API_KEY"),
            ("commandcode", "COMMAND_CODE_API_KEY"),
        ):
            saved = os.environ.get(variable)
            os.environ[variable] = BOGUS
            try:
                result = auth.verify(name, timeout=40)
            finally:
                if saved is None:
                    os.environ.pop(variable, None)
                else:
                    os.environ[variable] = saved
            self.assertFalse(result.ok, f"{name} accepted a bogus key")
            self.assertIn("401", result.detail, name)

    def test_the_user_agent_is_not_blocked(self) -> None:
        """Cloudflare answers urllib's default with 403 'error code: 1010'."""
        for name in ("opencode", "commandcode"):
            models = auth.list_models(auth.get_provider(name), "", timeout=30)
            self.assertTrue(models, f"{name} returned nothing; blocked?")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
