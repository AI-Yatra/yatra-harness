"""GMI Cloud: the ordinary endpoint, and the router that chooses for you.

Two things are worth testing here and neither is the happy path.

The router takes a mode where every other endpoint takes a model id, and it
defaults `stream` to true where every other endpoint defaults it to false. A
request that forgot either would fail in a way that reads like something else:
an unknown-model error, or a "malformed response" that retries forever because
an event stream is not the JSON object the decoder expects.

The second is that a router which does not report what it chose is not
auditable. The metadata has to survive both transports, including the streamed
one where it arrives in a frame of its own after the content.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness.config import RouteConfig, load_config
from harness.core.contracts import ModelRequest, RiskLevel, ToolSpec
from harness.core.errors import ConfigurationError, TransientProviderError
from harness.models import auth
from harness.models.providers import (
    GMI_DEFAULT_MODE,
    GMI_MODES,
    GMI_ROUTER_URL,
    GmiRouterProvider,
    OpenAICompatibleProvider,
    describe_routing,
    provider_for,
)
from harness.models.streaming import StreamAccumulator, iter_sse_data
from harness.repl.model import ChatModel

ROOT = Path(__file__).resolve().parents[1]


def route(**kwargs: Any) -> RouteConfig:
    kwargs.setdefault("name", "gmi-router")
    kwargs.setdefault("kind", "gmi_router")
    kwargs.setdefault("model", "balanced")
    return RouteConfig(**kwargs)


def request() -> ModelRequest:
    return ModelRequest(
        run_id="test-run",
        turn=1,
        messages=({"role": "user", "content": "hello"},),
        tools=(ToolSpec("finish", "Finish.", {"type": "object"}, RiskLevel.CONTROL),),
        max_output_chars=1000,
    )


def completion(content: str = '{"type": "finish", "summary": "done"}') -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 7},
        "routing_metadata": {"selected_model": "zai-org/GLM-5-FP8", "task_type": "code"},
    }


class CatalogueTests(unittest.TestCase):
    """The plain endpoint needs no adapter, only a catalogue entry."""

    def test_gmi_is_a_known_provider(self) -> None:
        provider = auth.get_provider("gmi")
        self.assertEqual(provider.base_url, "https://api.gmi-serving.com/v1")
        self.assertEqual(provider.api, auth.API_OPENAI)

    def test_both_documented_variables_resolve(self) -> None:
        """The docs say GMI_API_KEY; the AI SDK says GMI_CLOUD_APIKEY."""
        for name in ("GMI_API_KEY", "GMI_CLOUD_APIKEY"):
            self.assertIs(auth.BY_ENV[name], auth.get_provider("gmi"))

    def test_the_common_aliases_reach_it(self) -> None:
        for alias in ("gmicloud", "gmi-cloud"):
            self.assertEqual(auth.get_provider(alias).name, "gmi")

    def test_it_claims_no_key_prefix(self) -> None:
        """GMI documents no prefix, and a guessed one misdetects other keys."""
        self.assertEqual(auth.get_provider("gmi").prefixes, ())

    def test_the_endpoint_resolves_a_stored_key(self) -> None:
        with mock.patch.dict("os.environ", {"GMI_API_KEY": "test-key"}, clear=False):
            credential = auth.resolve_route("GMI_API_KEY", auth.get_provider("gmi").base_url)
        self.assertTrue(credential.available)
        self.assertEqual(credential.provider, "gmi")


class ModeTests(unittest.TestCase):
    def test_the_route_model_is_the_mode(self) -> None:
        for mode in GMI_MODES:
            self.assertEqual(GmiRouterProvider(route(model=mode)).mode, mode)

    def test_an_unset_mode_is_balanced(self) -> None:
        self.assertEqual(GmiRouterProvider(route(model="")).mode, GMI_DEFAULT_MODE)

    def test_a_model_id_where_a_mode_belongs_is_refused(self) -> None:
        """Otherwise it is silently sent as a mode the router will not know."""
        with self.assertRaises(ConfigurationError) as caught:
            GmiRouterProvider(route(model="zai-org/GLM-5-FP8"))
        message = str(caught.exception)
        for mode in GMI_MODES:
            self.assertIn(mode, message)

    def test_the_mode_is_case_insensitive(self) -> None:
        self.assertEqual(GmiRouterProvider(route(model="QUALITY")).mode, "quality")


class BodyTests(unittest.TestCase):
    """The three ways this request differs from an ordinary one."""

    def body(self, **kwargs: Any) -> dict[str, Any]:
        provider = GmiRouterProvider(route(**kwargs))
        return provider._body(request())  # noqa: SLF001

    def test_no_model_is_sent(self) -> None:
        self.assertNotIn("model", self.body())

    def test_the_mode_is_sent_instead(self) -> None:
        self.assertEqual(self.body(model="cost")["mode"], "cost")

    def test_streaming_is_turned_off_explicitly(self) -> None:
        """This endpoint streams by default, alone among the ones we call.

        Left unsaid, the reply is an event stream to a caller decoding one
        JSON object, which reads as a malformed response: a transient error,
        so it retries, and fails identically every time.
        """
        self.assertIs(self.body()["stream"], False)

    def test_the_messages_and_tools_survive(self) -> None:
        body = self.body()
        self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual([tool["function"]["name"] for tool in body["tools"]], ["finish"])

    def test_an_explicit_stream_is_not_overwritten(self) -> None:
        provider = GmiRouterProvider(route())
        self.assertIs(provider.adapt_body({"model": "x", "stream": True})["stream"], True)

    def test_an_ordinary_route_is_left_alone(self) -> None:
        """The hook exists for one subclass and must cost the others nothing."""
        plain = OpenAICompatibleProvider(
            RouteConfig(name="p", kind="openai_compatible", model="m", base_url="https://x/v1")
        )
        body = {"model": "m", "messages": []}
        self.assertEqual(plain.adapt_body(dict(body)), body)


class EndpointTests(unittest.TestCase):
    def test_it_posts_to_the_router(self) -> None:
        self.assertEqual(GmiRouterProvider(route())._endpoint(), GMI_ROUTER_URL)  # noqa: SLF001

    def test_the_router_url_is_not_derived_from_base_url(self) -> None:
        """It is a different host from GMI's inference endpoint.

        Appending a path to `base_url` the way the OpenAI adapter does would
        produce a 404 against the wrong service.
        """
        self.assertNotIn("api.gmi-serving.com", GMI_ROUTER_URL)
        self.assertIn("console.gmicloud.ai", GMI_ROUTER_URL)

    def test_an_explicit_base_url_still_wins(self) -> None:
        """So a staging endpoint is reachable without a code change."""
        provider = GmiRouterProvider(route(base_url="https://staging.example/autoroute"))
        self.assertEqual(provider._endpoint(), "https://staging.example/autoroute")  # noqa: SLF001

    def test_it_authenticates_with_a_bearer_token(self) -> None:
        headers = GmiRouterProvider(route())._headers("secret")  # noqa: SLF001
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_it_defaults_to_the_gmi_variable(self) -> None:
        self.assertEqual(GmiRouterProvider(route()).default_api_key_env, "GMI_API_KEY")


class SelectionTests(unittest.TestCase):
    def test_the_kind_builds_the_router(self) -> None:
        self.assertIsInstance(provider_for(route()), GmiRouterProvider)

    def test_it_is_still_an_openai_provider(self) -> None:
        """Everything that reads a completion must keep working unchanged."""
        self.assertIsInstance(provider_for(route()), OpenAICompatibleProvider)

    def test_the_kind_is_accepted_by_the_config(self) -> None:
        from harness.config import OPENAI_SHAPED_KINDS, ROUTE_KINDS

        self.assertIn("gmi_router", ROUTE_KINDS)
        self.assertIn("gmi_router", OPENAI_SHAPED_KINDS)

    def test_the_shipped_config_carries_the_routes(self) -> None:
        config = load_config(ROOT / "configs" / "ay.yaml")
        self.assertEqual(config.router.routes["gmi"].kind, "openai_compatible")
        self.assertEqual(config.router.routes["gmi-m27"].kind, "openai_compatible")
        self.assertEqual(config.router.routes["gmi-router"].kind, "gmi_router")

    def test_the_shipped_routes_name_only_free_models(self) -> None:
        """Everything else on GMI bills, and a zero-balance account answers
        402 on the first request rather than at signup. A route pointed at a
        billed model looks configured and fails the moment it is used."""
        config = load_config(ROOT / "configs" / "ay.yaml")
        self.assertEqual(config.router.routes["gmi"].model, "MiniMaxAI/MiniMax-M3")
        self.assertEqual(config.router.routes["gmi-m27"].model, "MiniMaxAI/MiniMax-M2.7")

    def test_the_free_models_are_what_verification_probes(self) -> None:
        """Probing a billed model fails a key that is perfectly good."""
        probes = auth.get_provider("gmi").probe_models
        self.assertTrue(probes)
        for name in probes:
            self.assertIn("MiniMax", name)

    def test_the_shipped_router_route_claims_no_tool_support(self) -> None:
        """GMI does not document `tools` on the router.

        Claiming it and being wrong would offer a route that cannot call
        tools to a loop that is nothing but tool calls. Flip this once a live
        key proves otherwise.
        """
        config = load_config(ROOT / "configs" / "ay.yaml")
        self.assertFalse(config.router.routes["gmi-router"].tool_support)

    def test_every_route_uses_one_key(self) -> None:
        config = load_config(ROOT / "configs" / "ay.yaml")
        for name in ("gmi", "gmi-m27", "gmi-router"):
            self.assertEqual(config.router.routes[name].api_key_env, "GMI_API_KEY")

    def test_the_free_routes_stream_and_call_tools(self) -> None:
        """Both were confirmed doing each against a live key."""
        config = load_config(ROOT / "configs" / "ay.yaml")
        for name in ("gmi", "gmi-m27"):
            self.assertTrue(config.router.routes[name].stream)
            self.assertTrue(config.router.routes[name].tool_support)


class MetadataTests(unittest.TestCase):
    def test_a_response_records_what_was_chosen(self) -> None:
        provider = GmiRouterProvider(route())
        provider._normalize(completion(), request())  # noqa: SLF001
        self.assertEqual(provider.last_routing["selected_model"], "zai-org/GLM-5-FP8")

    def test_the_line_names_the_model(self) -> None:
        provider = GmiRouterProvider(route())
        provider.remember_routing(completion())
        self.assertIn("zai-org/GLM-5-FP8", provider.describe_routing())

    def test_a_response_without_metadata_says_nothing(self) -> None:
        provider = GmiRouterProvider(route())
        provider.remember_routing({"choices": []})
        self.assertEqual(provider.describe_routing(), "")

    def test_a_fallback_is_reported_with_its_reason(self) -> None:
        line = describe_routing(
            {
                "selected_model": "b",
                "attempted_primary_model": "a",
                "fallback_triggered": True,
                "fallback_reason": "rate limited",
            }
        )
        self.assertIn("a", line)
        self.assertIn("b", line)
        self.assertIn("rate limited", line)

    def test_a_renamed_field_costs_a_shorter_line_not_a_crash(self) -> None:
        """The metadata shape is the vendor's and may move under us."""
        self.assertEqual(describe_routing({"something_new": 1}), "routed")

    def test_stale_metadata_does_not_survive_a_plain_response(self) -> None:
        provider = GmiRouterProvider(route())
        provider.remember_routing(completion())
        provider.remember_routing({"choices": []})
        self.assertEqual(provider.last_routing, {})


class StreamTests(unittest.TestCase):
    """The metadata arrives in a frame of its own, after the content."""

    def accumulate(self, *chunks: dict[str, Any]) -> StreamAccumulator:
        accumulator = StreamAccumulator()
        for chunk in chunks:
            accumulator.feed(chunk)
        return accumulator

    def test_routing_metadata_survives_reassembly(self) -> None:
        payload = self.accumulate(
            {"choices": [{"delta": {"content": "hi"}}]},
            {"routing_metadata": {"selected_model": "m"}},
        ).as_payload()
        self.assertEqual(payload["routing_metadata"], {"selected_model": "m"})
        self.assertEqual(payload["choices"][0]["message"]["content"], "hi")

    def test_a_stream_without_metadata_gains_no_key(self) -> None:
        """Every other provider's payload must keep the shape it had."""
        payload = self.accumulate({"choices": [{"delta": {"content": "hi"}}]}).as_payload()
        self.assertNotIn("routing_metadata", payload)

    def test_an_error_frame_is_not_mistaken_for_an_answer(self) -> None:
        """A stream that dies mid-output still ends 200 with partial text.

        Without noticing the error frame, a truncated turn is indistinguishable
        from a short one and gets accepted as finished.
        """
        accumulator = self.accumulate(
            {"choices": [{"delta": {"content": "half an ans"}}]},
            {"error": {"message": "upstream model timed out"}},
        )
        self.assertIn("timed out", accumulator.error)

    def test_a_clean_stream_reports_no_error(self) -> None:
        self.assertEqual(self.accumulate({"choices": [{"delta": {"content": "hi"}}]}).error, "")

    def test_a_bare_string_error_is_read_too(self) -> None:
        self.assertEqual(self.accumulate({"error": "boom"}).error, "boom")

    def test_the_reader_refuses_a_failed_stream(self) -> None:
        provider = GmiRouterProvider(route())
        lines = [
            b'data: {"choices": [{"delta": {"content": "half"}}]}\n',
            b'data: {"error": {"message": "upstream model timed out"}}\n',
            b"data: [DONE]\n",
        ]
        with self.assertRaises(TransientProviderError) as caught:
            provider._read_stream(iter(lines))  # noqa: SLF001
        self.assertIn("timed out", str(caught.exception))

    def test_the_event_field_is_skipped_and_its_data_is_read(self) -> None:
        """The router labels the metadata frame with an `event:` line."""
        raw = [
            "event: routing_metadata\n",
            'data: {"routing_metadata": {"selected_model": "m"}}\n',
            "data: [DONE]\n",
        ]
        chunks = [json.loads(item) for item in iter_sse_data(raw)]
        self.assertEqual(chunks, [{"routing_metadata": {"selected_model": "m"}}])


class ConversationTests(unittest.TestCase):
    """The REPL path builds its own body and must get the same treatment."""

    def model(self) -> ChatModel:
        return ChatModel(route())

    def test_the_conversational_request_carries_a_mode_not_a_model(self) -> None:
        model = self.model()
        seen: dict[str, Any] = {}

        def capture(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            seen.update(body)
            return completion("answer")

        with mock.patch.object(model.provider, "send", side_effect=capture):
            model.converse([{"role": "user", "content": "hi"}], ())
        self.assertNotIn("model", seen)
        self.assertEqual(seen["mode"], "balanced")
        self.assertIs(seen["stream"], False)

    def test_the_turn_carries_the_routing_note(self) -> None:
        """On the turn, not on the model.

        A model can be a `RouteChain`, which delegates and has already moved
        on by the time anyone could ask it what the last provider did. Asking
        the model was also a second thing every model implementation had to
        supply, and the one that mattered did not.
        """
        model = self.model()
        with mock.patch.object(model.provider, "send", return_value=completion("answer")):
            turn = model.converse([{"role": "user", "content": "hi"}], ())
        self.assertTrue(any("zai-org/GLM-5-FP8" in note for note in turn.notes))

    def test_a_chain_of_routes_passes_the_note_through(self) -> None:
        """The regression that a live run found and the unit tests had not."""
        from harness.repl.model import RouteChain

        model = self.model()
        chain = RouteChain([model])
        with mock.patch.object(model.provider, "send", return_value=completion("answer")):
            turn = chain.converse([{"role": "user", "content": "hi"}], ())
        self.assertTrue(any("zai-org/GLM-5-FP8" in note for note in turn.notes))

    def test_the_answer_itself_is_read_normally(self) -> None:
        model = self.model()
        with mock.patch.object(model.provider, "send", return_value=completion("answer")):
            turn = model.converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(turn.text, "answer")

    def test_an_ordinary_route_adds_no_note(self) -> None:
        plain = ChatModel(
            RouteConfig(name="p", kind="openai_compatible", model="m", base_url="https://x/v1")
        )
        with mock.patch.object(plain.provider, "send", return_value=completion("answer")):
            turn = plain.converse([{"role": "user", "content": "hi"}], ())
        self.assertEqual(turn.notes, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
