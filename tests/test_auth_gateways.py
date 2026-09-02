"""Google AI Studio, OpenCode Zen and Command Code.

Two things here are worth more than the rest. The Google endpoint has a
suffix that is easy to leave off and produces a 404 rather than an obvious
failure. And both gateways serve `/models` without checking the key, so
verifying against it reports success for a key that is nonsense -- these
tests pin the completion probe that exists to stop that.

The network is stubbed. `test_auth_gateways_live.py` does the same against
the real endpoints and is skipped unless it is asked for.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from harness.config import load_config
from harness.models import auth

ROOT = Path(__file__).resolve().parents[1]
AY_CONFIG = ROOT / "configs" / "ay.yaml"

GATEWAYS = ("google", "opencode", "commandcode")


class ProviderTableTests(unittest.TestCase):
    def test_all_three_are_registered(self) -> None:
        for name in GATEWAYS:
            self.assertEqual(auth.get_provider(name).name, name)

    def test_aliases_people_actually_type_resolve(self) -> None:
        for alias, expected in (
            ("gemini", "google"),
            ("aistudio", "google"),
            ("google-ai-studio", "google"),
            ("zen", "opencode"),
            ("opencode-zen", "opencode"),
            ("cmd", "commandcode"),
            ("command-code", "commandcode"),
            ("command", "commandcode"),
        ):
            self.assertEqual(auth.get_provider(alias).name, expected, alias)

    def test_google_points_at_the_openai_compatible_surface(self) -> None:
        """The bare /v1beta is generateContent and 404s on chat/completions."""
        provider = auth.get_provider("google")
        self.assertEqual(
            provider.base_url,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )
        self.assertTrue(provider.base_url.endswith("/openai"))
        self.assertEqual(provider.api, auth.API_OPENAI)

    def test_the_documented_environment_variables_are_accepted(self) -> None:
        self.assertEqual(
            auth.get_provider("google").env, ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        )
        # OpenCode documents the second as an alias of the first.
        self.assertEqual(
            auth.get_provider("opencode").env,
            ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"),
        )
        # The CLI docs say COMMAND_CODE_API_KEY; the provider API docs say
        # CMD_API_KEY. Both are real, so both resolve.
        self.assertEqual(
            auth.get_provider("commandcode").env,
            ("COMMAND_CODE_API_KEY", "CMD_API_KEY"),
        )

    def test_every_declared_variable_maps_back_to_its_provider(self) -> None:
        for name in GATEWAYS:
            provider = auth.get_provider(name)
            for variable in provider.env:
                self.assertEqual(auth.BY_ENV[variable].name, name, variable)

    def test_a_google_key_is_detected_from_its_prefix(self) -> None:
        self.assertEqual(auth.detect("AIzaSyBnotarealkeyatall").name, "google")

    def test_the_gateways_do_not_claim_other_providers_keys(self) -> None:
        """Neither gateway documents a prefix, so neither may guess."""
        for key in ("sk-ant-api03-x", "sk-proj-x", "gsk_x", "nvapi-x"):
            detected = auth.detect(key)
            self.assertNotIn(
                detected.name if detected else "", {"opencode", "commandcode"}, key
            )

    def test_an_endpoint_maps_back_to_its_provider(self) -> None:
        for name in GATEWAYS:
            provider = auth.get_provider(name)
            self.assertEqual(auth.provider_for_base_url(provider.base_url).name, name)

    def test_the_gateways_verify_by_completion_not_by_listing(self) -> None:
        for name in ("opencode", "commandcode"):
            self.assertEqual(
                auth.get_provider(name).verify_via, auth.VERIFY_COMPLETION, name
            )

    def test_every_remote_provider_verifies_with_a_real_completion(self) -> None:
        """Listing is not evidence: some gateways serve it unauthenticated,
        and some providers gate the list on the key but completions on quota,
        so an empty account lists happily and then answers every request 402."""
        for provider in auth.PROVIDERS:
            if provider.api == auth.API_LOCAL or provider.api == auth.API_ANTHROPIC:
                continue
            self.assertEqual(
                provider.verify_via, auth.VERIFY_COMPLETION, provider.name
            )

    def test_anthropic_and_local_still_verify_by_listing(self) -> None:
        """Their completion shape is not the OpenAI one the probe builds."""
        for name in ("anthropic", "ollama", "vllm"):
            self.assertEqual(auth.get_provider(name).verify_via, auth.VERIFY_MODELS)

    def test_they_appear_in_status(self) -> None:
        listed = {row["provider"] for row in auth.status()}
        for name in GATEWAYS:
            self.assertIn(name, listed)


class ProbeModelTests(unittest.TestCase):
    def provider(self, *preferred: str) -> auth.Provider:
        return auth.Provider(
            "x", auth.API_OPENAI, ("X_KEY",), "https://x/v1", probe_models=preferred
        )

    def test_the_first_available_preference_wins(self) -> None:
        chosen = auth._probe_model(self.provider("b", "c"), ["a", "c", "b"])
        self.assertEqual(chosen, "b")

    def test_a_retired_preference_falls_through(self) -> None:
        """A hardcoded id that the gateway dropped must not become a 404."""
        chosen = auth._probe_model(self.provider("gone", "c"), ["a", "c"])
        self.assertEqual(chosen, "c")

    def test_a_free_model_is_preferred_over_a_paid_one(self) -> None:
        """Verifying a key should not bill for it."""
        chosen = auth._probe_model(self.provider(), ["expensive-pro", "cheap-free"])
        self.assertEqual(chosen, "cheap-free")

    def test_the_first_model_is_the_last_resort(self) -> None:
        self.assertEqual(auth._probe_model(self.provider(), ["only-one"]), "only-one")

    def test_no_models_yields_nothing_rather_than_an_index_error(self) -> None:
        self.assertEqual(auth._probe_model(self.provider("a"), []), "")

    def test_the_shipped_preferences_are_free_models(self) -> None:
        for name in ("opencode", "commandcode"):
            preferred = auth.get_provider(name).probe_models
            self.assertTrue(preferred, name)
            self.assertTrue(preferred[0].endswith("-free"), f"{name}: {preferred[0]}")


class ErrorMessageTests(unittest.TestCase):
    """Each of these envelopes is one a real provider actually returned."""

    def test_the_openai_shape(self) -> None:
        body = json.dumps({"error": {"message": "Invalid 'Authorization' header or token."}})
        self.assertEqual(auth._error_message(body), "Invalid 'Authorization' header or token.")

    def test_the_opencode_nested_shape(self) -> None:
        body = json.dumps({"type": "error", "error": {"type": "AuthError", "message": "Invalid API key."}})
        self.assertEqual(auth._error_message(body), "Invalid API key.")

    def test_the_google_shape(self) -> None:
        body = json.dumps({"error": {"code": 400, "message": "Please pass a valid API key"}})
        self.assertEqual(auth._error_message(body), "Please pass a valid API key")

    def test_a_bare_message_field(self) -> None:
        body = json.dumps({"success": False, "message": "404 Not found."})
        self.assertEqual(auth._error_message(body), "404 Not found.")

    def test_html_or_anything_unparseable_is_passed_through_truncated(self) -> None:
        self.assertIn("error code: 1010", auth._error_message("error code: 1010"))
        self.assertLessEqual(len(auth._error_message("x" * 500)), 160)


class StubGateway:
    """A gateway that serves /models to anyone and guards /chat/completions."""

    def __init__(self, *, models: list[str], valid_key: str) -> None:
        self.models = models
        self.valid_key = valid_key
        self.requests: list[tuple[str, str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body: dict) -> str:
                outer.requests.append(
                    (self.command, self.path, dict(self.headers))
                )
                del body
                return self.headers.get("Authorization", "")

            def do_GET(self) -> None:  # noqa: N802
                self._record({})
                if self.path.endswith("/models"):
                    # Deliberately unauthenticated, exactly like the real ones.
                    self._respond(
                        200,
                        {"object": "list", "data": [{"id": m} for m in outer.models]},
                    )
                else:
                    self._respond(404, {"error": {"message": "no such route"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                authorization = self._record(payload)
                if authorization != f"Bearer {outer.valid_key}":
                    self._respond(401, {"error": {"message": "Invalid API key."}})
                    return
                if payload.get("model") not in outer.models:
                    self._respond(404, {"error": {"message": "unknown model"}})
                    return
                self._respond(
                    200,
                    {"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
                )

            def log_message(self, *_args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> StubGateway:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = dict(os.environ)
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(Path(self._tmp.name) / "auth.json")
        for provider in auth.PROVIDERS:
            for variable in provider.env:
                os.environ.pop(variable, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def gateway_provider(self, server: StubGateway) -> auth.Provider:
        return auth.Provider(
            "opencode",
            auth.API_OPENAI,
            ("OPENCODE_API_KEY",),
            server.base_url,
            verify_via=auth.VERIFY_COMPLETION,
            probe_models=("free-one",),
        )

    def test_a_bad_key_is_rejected_even_though_listing_succeeds(self) -> None:
        """The whole reason the completion probe exists."""
        with StubGateway(models=["free-one", "paid"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            with mock.patch.object(auth, "BY_NAME", {**auth.BY_NAME, "opencode": provider}):
                os.environ["OPENCODE_API_KEY"] = "wrong"
                result = auth.verify("opencode", timeout=10)
        self.assertFalse(result.ok)
        self.assertIn("Invalid API key", result.detail)

    def test_a_good_key_is_accepted_and_names_the_model_it_proved_it_with(self) -> None:
        with StubGateway(models=["free-one", "paid"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            with mock.patch.object(auth, "BY_NAME", {**auth.BY_NAME, "opencode": provider}):
                os.environ["OPENCODE_API_KEY"] = "good"
                result = auth.verify("opencode", timeout=10)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("free-one", result.detail)
        self.assertIn("2 models", result.detail)

    def test_the_probe_is_one_token_so_verifying_costs_nothing(self) -> None:
        with StubGateway(models=["free-one"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            with mock.patch.object(auth, "BY_NAME", {**auth.BY_NAME, "opencode": provider}):
                os.environ["OPENCODE_API_KEY"] = "good"
                auth.verify("opencode", timeout=10)
        posts = [r for r in server.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_a_missing_key_is_reported_before_any_request(self) -> None:
        with StubGateway(models=["free-one"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            with mock.patch.object(auth, "BY_NAME", {**auth.BY_NAME, "opencode": provider}):
                result = auth.verify("opencode", timeout=10)
        self.assertFalse(result.ok)
        self.assertIn("no credential", result.detail)
        self.assertEqual(server.requests, [])

    def test_every_request_sends_a_user_agent(self) -> None:
        """urllib's default is blocked by the Cloudflare rules in front of
        both gateways, which returns 403 'error code: 1010' and reads exactly
        like a rejected key."""
        with StubGateway(models=["free-one"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            with mock.patch.object(auth, "BY_NAME", {**auth.BY_NAME, "opencode": provider}):
                os.environ["OPENCODE_API_KEY"] = "good"
                auth.verify("opencode", timeout=10)
        self.assertTrue(server.requests)
        for _method, _path, headers in server.requests:
            agent = headers.get("User-Agent", "")
            self.assertIn("yatra-harness", agent)
            self.assertNotIn("Python-urllib", agent)

    def test_listing_parses_the_openai_envelope(self) -> None:
        with StubGateway(models=["a", "b", "c"], valid_key="good") as server:
            provider = self.gateway_provider(server)
            self.assertEqual(auth.list_models(provider, "", timeout=10), ["a", "b", "c"])


class ConfigRouteTests(unittest.TestCase):
    """The shipped REPL config must actually reach these providers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(AY_CONFIG)

    def test_a_route_exists_for_each_gateway(self) -> None:
        for name in ("gemini", "opencode", "commandcode"):
            self.assertIn(name, self.config.router.routes)

    def test_each_route_names_the_providers_real_endpoint_and_variable(self) -> None:
        for route_name, provider_name in (
            ("gemini", "google"),
            ("opencode", "opencode"),
            ("commandcode", "commandcode"),
        ):
            route = self.config.router.routes[route_name]
            provider = auth.get_provider(provider_name)
            self.assertEqual(route.base_url, provider.base_url, route_name)
            self.assertIn(route.api_key_env, provider.env, route_name)

    def test_each_route_builds_the_documented_chat_endpoint(self) -> None:
        from harness.models.providers import provider_for

        expected = {
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "opencode": "https://opencode.ai/zen/v1/chat/completions",
            "commandcode": "https://api.commandcode.ai/provider/v1/chat/completions",
        }
        for name, url in expected.items():
            adapter = provider_for(self.config.router.routes[name])
            self.assertEqual(adapter._endpoint(), url, name)

    def test_a_stored_key_reaches_a_route_through_its_endpoint(self) -> None:
        """A route naming a variable this module does not know still resolves."""
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.environ.get("YATRA_HARNESS_AUTH_FILE")
            os.environ["YATRA_HARNESS_AUTH_FILE"] = str(Path(tmp) / "auth.json")
            try:
                for variable in ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"):
                    os.environ.pop(variable, None)
                auth.put_key("opencode", "stored-zen-key")
                route = self.config.router.routes["opencode"]
                credential = auth.resolve_route("SOMETHING_CUSTOM", route.base_url)
                self.assertEqual(credential.key, "stored-zen-key")
                self.assertEqual(credential.provider, "opencode")
            finally:
                if saved is None:
                    os.environ.pop("YATRA_HARNESS_AUTH_FILE", None)
                else:
                    os.environ["YATRA_HARNESS_AUTH_FILE"] = saved


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AmbiguousPrefixTests(unittest.TestCase):
    """A bare `sk-` is issued by four providers, so it identifies none."""

    def test_a_bare_sk_key_is_not_attributed_to_openai(self) -> None:
        found = auth.identify("sk-" + "x" * 64)
        self.assertFalse(found.certain)
        self.assertIsNone(found.provider)
        names = {p.name for p in found.candidates}
        self.assertIn("openai", names)
        self.assertIn("opencode", names)

    def test_a_distinctive_prefix_still_wins_outright(self) -> None:
        for key, expected in (
            ("sk-proj-abc", "openai"),
            ("sk-ant-api03-abc", "anthropic"),
            ("sk-or-v1-abc", "openrouter"),
            ("sk-ws-abc", "dashscope"),
            ("csk-abc", "cerebras"),
            ("gsk_abc", "groq"),
            ("AIzaSyAbc", "google"),
            ("AQ.Abc", "google"),
        ):
            found = auth.identify(key)
            self.assertTrue(found.certain, key)
            self.assertEqual(found.provider.name, expected, key)

    def test_the_longest_prefix_beats_the_shared_one(self) -> None:
        """`sk-ant-api03-` must not be swallowed by `sk-`."""
        self.assertEqual(auth.detect("sk-ant-api03-x").name, "anthropic")

    def test_an_unknown_shape_is_still_not_guessed(self) -> None:
        self.assertIsNone(auth.detect("totally-unknown-key"))


class AddResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = dict(os.environ)
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(Path(self._tmp.name) / "auth.json")
        for provider in auth.PROVIDERS:
            for variable in provider.env:
                os.environ.pop(variable, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def test_an_explicit_provider_skips_every_check(self) -> None:
        record = auth.add("sk-" + "x" * 40, provider="opencode")
        self.assertEqual(record["provider"], "opencode")
        self.assertIn("named by the operator", record["how"])

    def test_a_certain_prefix_stores_without_touching_the_network(self) -> None:
        record = auth.add("AQ.NotARealKey", probe=True)
        self.assertEqual(record["provider"], "google")
        self.assertIn("prefix", record["how"])

    def test_an_ambiguous_key_is_stored_where_it_authenticates(self) -> None:
        """The whole point: ask rather than guess."""
        with StubGateway(models=["free-one"], valid_key="the-key") as server:
            opencode = auth.Provider(
                "opencode", auth.API_OPENAI, ("OPENCODE_API_KEY",), server.base_url,
                prefixes=("sk-",), verify_via=auth.VERIFY_COMPLETION,
                probe_models=("free-one",),
            )
            openai = auth.Provider(
                "openai", auth.API_OPENAI, ("OPENAI_API_KEY",),
                "http://127.0.0.1:9/unreachable", prefixes=("sk-",),
            )
            with mock.patch.object(auth, "identify",
                                   lambda _k: auth.Detection(None, (openai, opencode))):
                record = auth.add("the-key", timeout=5)
        self.assertEqual(record["provider"], "opencode")
        self.assertIn("accepting the key", record["how"])

    def test_an_ambiguous_key_nobody_accepts_is_refused(self) -> None:
        """Better a clear error than a key filed under the wrong provider."""
        with StubGateway(models=["free-one"], valid_key="right") as server:
            candidate = auth.Provider(
                "opencode", auth.API_OPENAI, ("OPENCODE_API_KEY",), server.base_url,
                prefixes=("sk-",), verify_via=auth.VERIFY_COMPLETION,
                probe_models=("free-one",),
            )
            with mock.patch.object(auth, "identify",
                                   lambda _k: auth.Detection(None, (candidate,))):
                with self.assertRaises(auth.AuthError) as caught:
                    auth.add("wrong", timeout=5)
        self.assertIn("not accepted by any provider", str(caught.exception))
        self.assertEqual(auth.load_store().get("providers"), {})

    def test_probing_can_be_declined_and_then_it_refuses_to_guess(self) -> None:
        with self.assertRaises(auth.AuthError) as caught:
            auth.add("sk-" + "x" * 60, probe=False)
        message = str(caught.exception)
        self.assertIn("shared by", message)
        self.assertIn("--provider", message)
        self.assertEqual(auth.load_store().get("providers"), {})

    def test_check_credential_tests_a_key_that_is_not_stored(self) -> None:
        with StubGateway(models=["free-one"], valid_key="good") as server:
            provider = auth.Provider(
                "opencode", auth.API_OPENAI, ("OPENCODE_API_KEY",), server.base_url,
                verify_via=auth.VERIFY_COMPLETION, probe_models=("free-one",),
            )
            self.assertTrue(auth.check_credential(provider, "good", timeout=5)[0])
            self.assertFalse(auth.check_credential(provider, "bad", timeout=5)[0])


class ProbeFailureTests(unittest.TestCase):
    """Which probe failures are about the key, and which are about the model."""

    def failure(self, status: int) -> auth._HTTPFailure:
        return auth._HTTPFailure(f"HTTP {status}", status)

    def test_key_and_quota_failures_stop_the_search(self) -> None:
        for status in (401, 402, 403, 429):
            self.assertTrue(self.failure(status).about_the_credential, status)

    def test_a_bad_probe_model_does_not_condemn_the_key(self) -> None:
        """A 400 for the wrong input shape, a 404 for a retired id, a 500 from
        a speech endpoint -- none of these say anything about the credential."""
        for status in (400, 404, 500, 503):
            self.assertFalse(self.failure(status).about_the_credential, status)

    def test_the_probe_moves_past_a_model_that_is_not_a_chat_model(self) -> None:
        provider = auth.Provider(
            "x", auth.API_OPENAI, ("X_KEY",), "https://x/v1",
            verify_via=auth.VERIFY_COMPLETION,
        )
        # A catalogue whose best-ranked entries are speech and embedding.
        candidates = auth._probe_candidates(
            provider, ["x-asr-9.9", "x-embedding-9.9", "x-chat-3.0", "x-chat-1.0"]
        )
        self.assertIn("x-chat-3.0", candidates)
        self.assertNotIn("x-embedding-9.9", candidates)

    def test_several_models_are_offered_so_one_bad_guess_is_not_the_answer(self) -> None:
        provider = auth.Provider("x", auth.API_OPENAI, ("X_KEY",), "https://x/v1")
        many = [f"x-chat-{n}" for n in range(10)]
        self.assertEqual(len(auth._probe_candidates(provider, many)), auth.PROBE_ATTEMPTS)

    def test_the_models_prefix_is_stripped_before_probing(self) -> None:
        """Google lists `models/gemini-...` but takes the bare id."""
        provider = auth.Provider("x", auth.API_OPENAI, ("X_KEY",), "https://x/v1")
        chosen = auth._probe_candidates(provider, ["models/gemini-3.7-flash"])
        self.assertEqual(chosen, ["gemini-3.7-flash"])
