"""Web search: the request each backend needs, and how its answer is read.

Every part that can be tested without a network is a pure function over a
recorded payload, so the suite never depends on a search engine being up or
on a key existing.
"""

from __future__ import annotations

import json
import unittest

from harness.core.errors import ConfigurationError, ToolError
from harness.execution.search import (
    SearchConfig,
    SearchResult,
    build_request,
    parse_results,
    render,
)


def config(**kwargs) -> SearchConfig:
    defaults = {
        "kind": "brave",
        "endpoint": "https://api.search.brave.com/res/v1/web/search",
        "api_key_env": "BRAVE_API_KEY",
        "max_results": 5,
    }
    return SearchConfig(**{**defaults, **kwargs})


class RequestTests(unittest.TestCase):
    def test_brave_puts_the_query_in_the_url(self) -> None:
        request = build_request(config(), "harness engineering", key="k")
        self.assertIn("q=harness+engineering", request.url)

    def test_brave_sends_its_key_in_a_header_not_the_url(self) -> None:
        # A key in a query string ends up in proxy logs and in any redirect
        # target; the ledger redaction cannot reach either.
        request = build_request(config(), "anything", key="secret-key")
        self.assertEqual(request.headers.get("X-Subscription-Token"), "secret-key")
        self.assertNotIn("secret-key", request.url)

    def test_tavily_posts_a_json_body(self) -> None:
        request = build_request(config(kind="tavily"), "anything", key="secret-key")
        self.assertEqual(request.method, "POST")
        self.assertIn("application/json", request.headers.get("Content-Type", ""))
        self.assertEqual(json.loads(request.body)["query"], "anything")

    def test_tavily_keeps_its_key_out_of_the_url_too(self) -> None:
        request = build_request(config(kind="tavily"), "anything", key="secret-key")
        self.assertNotIn("secret-key", request.url)

    def test_duckduckgo_needs_no_key(self) -> None:
        request = build_request(config(kind="duckduckgo", api_key_env=""), "x", key="")
        self.assertTrue(request.url.startswith("https://"))

    def test_a_backend_needing_a_key_says_so_when_it_has_none(self) -> None:
        with self.assertRaises(ToolError) as caught:
            build_request(config(), "anything", key="")
        self.assertIn("BRAVE_API_KEY", str(caught.exception))

    def test_the_result_count_is_bounded_by_the_configuration(self) -> None:
        self.assertIn("count=3", build_request(config(max_results=3), "x", key="k").url)

    def test_an_empty_query_is_refused(self) -> None:
        with self.assertRaises(ToolError):
            build_request(config(), "   ", key="k")

    def test_an_unknown_backend_is_refused_at_config_time(self) -> None:
        with self.assertRaises(ConfigurationError):
            SearchConfig(kind="altavista", endpoint="https://x", api_key_env="", max_results=5)


BRAVE = json.dumps(
    {
        "web": {
            "results": [
                {"title": "First", "url": "https://a.example/1", "description": "one <b>hit</b>"},
                {"title": "Second", "url": "https://b.example/2", "description": "two"},
            ]
        }
    }
)

TAVILY = json.dumps(
    {
        "results": [
            {"title": "First", "url": "https://a.example/1", "content": "one"},
            {"title": "Second", "url": "https://b.example/2", "content": "two"},
        ]
    }
)

DUCKDUCKGO = """
<div class="result"><a class="result__a" href="https://a.example/1">First</a>
<a class="result__snippet">one <b>hit</b></a></div>
<div class="result"><a class="result__a" href="https://b.example/2">Second</a>
<a class="result__snippet">two</a></div>
"""


class ParseTests(unittest.TestCase):
    def test_brave_results_are_read(self) -> None:
        results = parse_results(config(), BRAVE)
        self.assertEqual([r.title for r in results], ["First", "Second"])
        self.assertEqual(results[0].url, "https://a.example/1")

    def test_tavily_results_are_read(self) -> None:
        results = parse_results(config(kind="tavily"), TAVILY)
        self.assertEqual([r.url for r in results], ["https://a.example/1", "https://b.example/2"])

    def test_duckduckgo_results_are_read(self) -> None:
        results = parse_results(config(kind="duckduckgo"), DUCKDUCKGO)
        self.assertEqual([r.title for r in results], ["First", "Second"])

    def test_markup_is_stripped_from_snippets(self) -> None:
        # The snippet goes into the model's context; leaving tags in it wastes
        # budget and invites the model to treat them as instructions.
        results = parse_results(config(), BRAVE)
        self.assertEqual(results[0].snippet, "one hit")

    def test_more_results_than_configured_are_dropped(self) -> None:
        self.assertEqual(len(parse_results(config(max_results=1), BRAVE)), 1)

    def test_an_empty_answer_is_not_an_error(self) -> None:
        self.assertEqual(parse_results(config(), json.dumps({"web": {"results": []}})), [])

    def test_a_malformed_answer_is_named_rather_than_crashing(self) -> None:
        with self.assertRaises(ToolError) as caught:
            parse_results(config(), "not json at all")
        self.assertIn("could not be read", str(caught.exception))

    def test_a_result_missing_its_url_is_skipped(self) -> None:
        payload = json.dumps({"web": {"results": [{"title": "no url"}]}})
        self.assertEqual(parse_results(config(), payload), [])


class RenderTests(unittest.TestCase):
    def test_results_render_as_numbered_lines_with_urls(self) -> None:
        text = render([SearchResult("First", "https://a.example/1", "one")])
        self.assertIn("1. First", text)
        self.assertIn("https://a.example/1", text)
        self.assertIn("one", text)

    def test_no_results_says_so_plainly(self) -> None:
        self.assertIn("no results", render([]).lower())

    def test_the_rendering_tells_the_model_how_to_read_a_page(self) -> None:
        # A search result is a pointer. Without this the model treats the
        # snippet as the answer and never fetches the page.
        self.assertIn("browser_fetch", render([SearchResult("t", "https://a.example", "s")]))


if __name__ == "__main__":
    unittest.main()
