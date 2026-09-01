"""Retrieval: finding the relevant part of a repository too big to read.

`search_repo` matches a literal string. That is exact, and useless when you do
not already know the identifier: "where is the retry backoff decided" finds
nothing, because nobody wrote that sentence in the code. A model that cannot
find the right file reads the wrong ones, and a context budget is spent on
material that was never going to help.

Two backends, and the default is the one that always works. Lexical (BM25)
ranks by term statistics -- no key, no network, deterministic, and available
on a workshop laptop. Embedding ranks by cosine similarity against a
provider's vectors and finds things lexical search cannot, at the cost of a
dependency on that provider being reachable and paid for.

Everything that decides a ranking is a pure function over text, so the
scoring is tested without a network and the provider's answers are recorded
payloads. Retrieval that only works when someone has a key is retrieval that
is never exercised in CI.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, ToolError

KINDS = ("lexical", "embedding")
IGNORED_DIRECTORIES = {".git", ".runs", ".venv", "__pycache__", "node_modules", ".evals"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".png", ".jpg", ".jpeg",
                    ".gif", ".pdf", ".zip", ".gz", ".xlsx", ".ico", ".woff", ".woff2"}
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
# BM25 constants. k1 bounds how much repeating a term keeps helping; b decides
# how strongly a long chunk is penalised for its length. These are the
# standard values and there is no reason here to be original about them.
K1 = 1.5
B = 0.75


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    kind: str = "lexical"
    endpoint: str = ""
    api_key_env: str = ""
    model: str = "text-embedding-3-small"
    lines_per_chunk: int = 40
    max_file_bytes: int = 200_000
    max_chunks: int = 4_000
    limit: int = 5

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigurationError(
                f"retrieval.kind must be one of {', '.join(KINDS)}; got {self.kind!r}"
            )
        if self.kind == "embedding" and not self.endpoint:
            raise ConfigurationError("retrieval.endpoint is required when kind is embedding")


@dataclass(frozen=True, slots=True)
class Chunk:
    path: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


def tokenize(text: str) -> list[str]:
    """Identifier-aware lowercase tokens.

    `snake_case` and `CamelCase` are split as well as kept whole, so a query
    for "retry backoff" reaches `retry_backoff` and `retryBackoff` without the
    operator having to guess which convention the file used.
    """
    tokens: list[str] = []
    for match in TOKEN.finditer(text):
        word = match.group(0)
        tokens.append(word.lower())
        parts = [part for part in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", word) if part]
        if len(parts) > 1:
            tokens.extend(part.lower() for part in parts)
    return tokens


def chunk_file(path: Path, relative: str, *, lines_per_chunk: int) -> list[Chunk]:
    """Split one file into line-ranged chunks, skipping what cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Binary and undecodable files are not an error; they are just not
        # something a text index has anything to say about.
        return []
    lines = text.splitlines()
    chunks = []
    for start in range(0, len(lines), lines_per_chunk):
        window = lines[start : start + lines_per_chunk]
        body = "\n".join(window).strip()
        if not body:
            continue
        chunks.append(
            Chunk(
                path=relative,
                start_line=start + 1,
                end_line=start + len(window),
                text="\n".join(window),
            )
        )
    return chunks


def workspace_signature(root: Path, config: RetrievalConfig) -> tuple[int, int]:
    """A cheap fingerprint of the indexable tree: file count and newest mtime.

    An agent patches files as it works, so an index built at turn two is stale
    by turn four. Rebuilding on every query would re-read the repository -- and
    with the embedding backend, re-embed all of it -- for every question. This
    is the cheap thing to check in between.
    """
    count = 0
    newest = 0
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & IGNORED_DIRECTORIES or path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest = max(newest, stat.st_mtime_ns)
    return count, newest


def iter_chunks(root: Path, config: RetrievalConfig) -> list[Chunk]:
    """Every indexable chunk under `root`, bounded by the configured caps."""
    root = Path(root)
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & IGNORED_DIRECTORIES or path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        try:
            if path.stat().st_size > config.max_file_bytes:
                continue
        except OSError:
            continue
        chunks.extend(
            chunk_file(path, path.relative_to(root).as_posix(),
                       lines_per_chunk=config.lines_per_chunk)
        )
        if len(chunks) >= config.max_chunks:
            break
    return chunks[: config.max_chunks]


class BM25Index:
    """Ranking by term statistics. No key, no network, same answer every time."""

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self.chunks = list(chunks)
        self._tokens = [Counter(tokenize(chunk.text)) for chunk in self.chunks]
        self._lengths = [sum(counter.values()) for counter in self._tokens]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for counter in self._tokens:
            document_frequency.update(counter.keys())
        total = len(self.chunks)
        # Inverse document frequency. Without it a term that appears in every
        # chunk -- "the", "self", "import" -- would dominate every query it
        # happened to be in.
        self._idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def search(self, query: str, *, limit: int) -> list[Hit]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        hits = []
        for index, counter in enumerate(self._tokens):
            score = 0.0
            for term in terms:
                frequency = counter.get(term, 0)
                if not frequency:
                    continue
                idf = self._idf.get(term, 0.0)
                length_ratio = (self._lengths[index] / self._average) if self._average else 1.0
                score += idf * (frequency * (K1 + 1)) / (
                    frequency + K1 * (1 - B + B * length_ratio)
                )
            if score > 0:
                hits.append(Hit(self.chunks[index], score))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


Embedder = Callable[[list[str]], list[list[float]]]


class EmbeddingIndex:
    """Ranking by cosine similarity against a provider's vectors."""

    def __init__(self, chunks: Sequence[Chunk], embed: Embedder) -> None:
        self.chunks = list(chunks)
        self.embed = embed
        self._vectors: list[list[float]] | None = None
        # Built eagerly so a provider that is already unreachable is
        # discovered once, here, rather than on every query.
        self._fallback: BM25Index | None = None

    def _corpus(self) -> list[list[float]] | None:
        if self._vectors is not None or self._fallback is not None:
            return self._vectors
        try:
            self._vectors = self.embed([chunk.text for chunk in self.chunks])
        except Exception:  # noqa: BLE001 - any provider failure degrades the same way
            # Retrieval going quiet is worse than retrieval being approximate:
            # the model does not know it asked a question that could not be
            # answered, it just gets nothing and reads the wrong files.
            self._fallback = BM25Index(self.chunks)
        return self._vectors

    def search(self, query: str, *, limit: int) -> list[Hit]:
        vectors = self._corpus()
        if vectors is None:
            return self._fallback.search(query, limit=limit) if self._fallback else []
        try:
            query_vector = self.embed([query])[0]
        except Exception:  # noqa: BLE001
            self._fallback = self._fallback or BM25Index(self.chunks)
            return self._fallback.search(query, limit=limit)
        hits = [
            Hit(chunk, cosine(query_vector, vector))
            for chunk, vector in zip(self.chunks, vectors, strict=False)
        ]
        hits = [hit for hit in hits if hit.score > 0]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Similarity, defined as zero wherever it would otherwise be undefined.

    Mismatched lengths mean the index was built with a different embedding
    model than the query used. A stale index should stop matching, not end a
    run with a shape error.
    """
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def embedding_request(config: RetrievalConfig, texts: list[str], *, key: str) -> EmbeddingRequest:
    """An OpenAI-compatible /embeddings call. The key travels in a header."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return EmbeddingRequest(
        url=config.endpoint,
        headers=headers,
        body=json.dumps({"model": config.model, "input": texts}),
    )


def parse_embeddings(payload: str, expected: int) -> list[list[float]]:
    """Read vectors back, restoring the order they were asked for.

    The `index` field is authoritative rather than the array order: providers
    are permitted to return them out of order, and a silently misaligned
    corpus produces rankings that look plausible and are nonsense.
    """
    try:
        value = json.loads(payload)
        data = value["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ToolError("the embeddings response could not be read") from exc
    vectors: list[list[float]] = [[] for _ in range(expected)]
    for item in data:
        try:
            position = int(item.get("index", 0))
            vector = [float(number) for number in item["embedding"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError("an embedding entry could not be read") from exc
        if 0 <= position < expected:
            vectors[position] = vector
    return vectors


def render_hits(hits: Iterable[Hit]) -> str:
    """What the model sees: excerpts with the coordinates to go read more."""
    listed = list(hits)
    if not listed:
        return "no matches"
    lines = []
    for hit in listed:
        lines.append(f"--- {hit.chunk.path}:{hit.chunk.start_line}-{hit.chunk.end_line} "
                     f"(score {hit.score:.2f})")
        lines.append(hit.chunk.text)
        lines.append("")
    lines.append(
        "These are ranked excerpts, not whole files. Open one with read_file "
        "before relying on the code around it."
    )
    return "\n".join(lines)


def retrieval_config_from_dict(raw: dict[str, Any] | None, path: str = "retrieval") -> RetrievalConfig:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(
        value,
        {"kind", "endpoint", "api_key_env", "model", "lines_per_chunk",
         "max_file_bytes", "max_chunks", "limit"},
        path,
    )

    def text(name: str, default: str) -> str:
        return schema.string(value[name], f"{path}.{name}") if value.get(name) else default

    return RetrievalConfig(
        kind=text("kind", "lexical"),
        endpoint=text("endpoint", ""),
        api_key_env=text("api_key_env", ""),
        model=text("model", "text-embedding-3-small"),
        lines_per_chunk=schema.integer(
            value.get("lines_per_chunk", 40), f"{path}.lines_per_chunk", minimum=5
        ),
        max_file_bytes=schema.integer(
            value.get("max_file_bytes", 200_000), f"{path}.max_file_bytes", minimum=1_000
        ),
        max_chunks=schema.integer(
            value.get("max_chunks", 4_000), f"{path}.max_chunks", minimum=10
        ),
        limit=schema.integer(value.get("limit", 5), f"{path}.limit", minimum=1),
    )
