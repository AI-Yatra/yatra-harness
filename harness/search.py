"""Web search: what each backend needs asked, and how its answer is read.

Search is the one capability that reaches post-training information, and it
is also the one most likely to change under you: endpoints move, response
shapes get new fields, and a scraped HTML page is rewritten without notice.
So the backend is configuration rather than code, and everything that can be
decided without a network -- building the request, reading the response --
is a pure function over text. Those are the parts that break, and they are
tested against recorded payloads rather than against a live search engine.

Three backends ship. `brave` and `tavily` are JSON APIs and need a key.
`duckduckgo` parses the HTML endpoint and needs none, so a workshop laptop
has a working search tool without anyone signing up for anything; it is also
the one most likely to break, and it says so.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigurationError, ToolError

KINDS = ("brave", "tavily", "duckduckgo")
NEEDS_KEY = {"brave", "tavily"}
DEFAULT_ENDPOINTS = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "tavily": "https://api.tavily.com/search",
    "duckduckgo": "https://html.duckduckgo.com/html/",
}
SNIPPET_LIMIT = 300
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class SearchConfig:
    kind: str = "duckduckgo"
    endpoint: str = ""
    api_key_env: str = ""
    max_results: int = 5

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigurationError(
                f"search.kind must be one of {', '.join(KINDS)}; got {self.kind!r}"
            )
        if not self.endpoint:
            object.__setattr__(self, "endpoint", DEFAULT_ENDPOINTS[self.kind])

    @property
    def host(self) -> str:
        return (urllib.parse.urlparse(self.endpoint).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class SearchRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def build_request(config: SearchConfig, query: str, *, key: str) -> SearchRequest:
    """The HTTP request this backend needs for `query`.

    A key is always carried in a header or a body, never in the URL. A query
    string reaches proxy logs and any redirect target, and neither is
    somewhere the event ledger's redaction can follow it.
    """
    text = " ".join(query.split())
    if not text:
        raise ToolError("web_search needs a non-empty query")
    if config.kind in NEEDS_KEY and not key:
        raise ToolError(
            f"web_search backend {config.kind!r} needs a credential; set "
            f"{config.api_key_env or 'its api_key_env'} or run `harness auth add`"
        )
    if config.kind == "brave":
        parameters = urllib.parse.urlencode({"q": text, "count": config.max_results})
        return SearchRequest(
            f"{config.endpoint}?{parameters}",
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
    if config.kind == "tavily":
        payload = json.dumps(
            {"query": text, "max_results": config.max_results, "api_key": key}
        )
        return SearchRequest(
            config.endpoint,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=payload,
        )
    parameters = urllib.parse.urlencode({"q": text})
    return SearchRequest(f"{config.endpoint}?{parameters}")


def parse_results(config: SearchConfig, payload: str) -> list[SearchResult]:
    """Read a backend's answer into results, or say the answer was unreadable."""
    if config.kind == "duckduckgo":
        results = _parse_duckduckgo(payload)
    else:
        results = _parse_json(config.kind, payload)
    return results[: config.max_results]


def render(results: list[SearchResult]) -> str:
    """What the model sees.

    Numbered, with the URL on its own line, and a closing note that a result
    is a pointer rather than an answer. Without that note a model treats the
    snippet as the finding and never opens the page it came from.
    """
    if not results:
        return "no results"
    lines = []
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title}")
        lines.append(f"   {result.url}")
        if result.snippet:
            lines.append(f"   {result.snippet}")
    lines.append("")
    lines.append(
        "These are search results, not sources. Read a page with browser_fetch "
        "before relying on anything above."
    )
    return "\n".join(lines)


def _parse_json(kind: str, payload: str) -> list[SearchResult]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ToolError(f"the {kind} search response could not be read as JSON") from exc
    if not isinstance(value, dict):
        raise ToolError(f"the {kind} search response could not be read")
    if kind == "brave":
        raw = ((value.get("web") or {}).get("results")) or []
        snippet_key = "description"
    else:
        raw = value.get("results") or []
        snippet_key = "content"
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        results.append(
            SearchResult(
                title=_clean(str(item.get("title") or url)),
                url=url,
                snippet=_clean(str(item.get(snippet_key) or "")),
            )
        )
    return results


_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r"(?P<rest>.*?)(?=<a[^>]+class=\"result__a\"|\Z)",
    re.S,
)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S)


def _parse_duckduckgo(payload: str) -> list[SearchResult]:
    results = []
    for match in _DDG_RESULT.finditer(payload):
        url = _unwrap_duckduckgo(html.unescape(match.group("url")))
        if not url.startswith(("http://", "https://")):
            continue
        snippet = _DDG_SNIPPET.search(match.group("rest") or "")
        results.append(
            SearchResult(
                title=_clean(match.group("title")),
                url=url,
                snippet=_clean(snippet.group("snippet")) if snippet else "",
            )
        )
    return results


def _unwrap_duckduckgo(url: str) -> str:
    """Recover the real target from a `/l/?uddg=...` redirect wrapper."""
    if "uddg=" not in url:
        return url
    query = urllib.parse.urlparse(url).query
    target = urllib.parse.parse_qs(query).get("uddg")
    return target[0] if target else url


def _clean(text: str) -> str:
    """Plain text, bounded.

    Markup in a snippet costs context budget and invites a model to read tags
    as structure it should obey.
    """
    stripped = html.unescape(_TAG.sub("", text))
    collapsed = " ".join(stripped.split())
    return collapsed[:SNIPPET_LIMIT]


def search_config_from_dict(raw: dict[str, Any] | None, path: str = "search") -> SearchConfig:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(value, {"kind", "endpoint", "api_key_env", "max_results"}, path)
    return SearchConfig(
        kind=schema.string(value.get("kind", "duckduckgo"), f"{path}.kind"),
        endpoint=(
            schema.string(value["endpoint"], f"{path}.endpoint")
            if value.get("endpoint")
            else ""
        ),
        api_key_env=(
            schema.string(value["api_key_env"], f"{path}.api_key_env")
            if value.get("api_key_env")
            else ""
        ),
        max_results=schema.integer(
            value.get("max_results", 5), f"{path}.max_results", minimum=1
        ),
    )
