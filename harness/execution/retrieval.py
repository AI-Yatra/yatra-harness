"""Finding the relevant part of a repository too big to read.

`grep` matches a literal string. That is exact, and useless when you do not
already know the identifier: "where is the retry backoff decided" finds
nothing, because nobody wrote that sentence in the code. A model that cannot
find the right file reads the wrong ones, and a context budget is spent on
material that was never going to help.

Three backends, and the default is the one that always works.

`lexical` is BM25 over a persistent SQLite FTS5 index. No key, no network, no
dependency, deterministic, and available on a laptop with nothing installed.

`hybrid` adds a local static embedding model and fuses the two rankings by
reciprocal rank. Static embeddings are lookup tables rather than a forward
pass, so the model is 8 MB, needs no GPU, and encodes this repository in about
a second. Measured on ten questions about this codebase, fusing lifted the
top-three hit rate from 40% to 70%.

`embedding` is the original remote-provider backend, kept because an operator
with a provider they trust should be able to use it.

The measurement that decided the default model is worth recording, because it
was the opposite of what everyone assumes. A *code*-specialised static model
scored worse than the general one -- 30% at top-three against 70%. Our queries
are English questions about code, not code searching for code, and the chunks
lead with a path and a symbol name. The smallest, most general model won.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.core.errors import ConfigurationError, ToolError
from harness.execution.chunking import Chunk
from harness.execution.indexing import Embedder, Row, SqliteIndex

KINDS = ("lexical", "hybrid", "embedding")

#: The static model a `hybrid` route uses unless told otherwise. Chosen by
#: measurement, not by name: see the module docstring.
DEFAULT_LOCAL_MODEL = "minishlab/potion-base-8M"


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    kind: str = "lexical"
    endpoint: str = ""
    api_key_env: str = ""
    model: str = "text-embedding-3-small"
    #: Only consulted by `hybrid`. Separate from `model` so switching backends
    #: does not silently send a local model name to a remote provider.
    local_model: str = DEFAULT_LOCAL_MODEL
    lines_per_chunk: int = 40
    max_file_bytes: int = 200_000
    limit: int = 5

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigurationError(
                f"retrieval.kind must be one of {', '.join(KINDS)}; got {self.kind!r}"
            )
        if self.kind == "embedding" and not self.endpoint:
            raise ConfigurationError("retrieval.endpoint is required when kind is embedding")


@dataclass(frozen=True, slots=True)
class Hit:
    chunk: Chunk
    score: float

    @classmethod
    def of(cls, row: Row) -> Hit:
        return cls(row.as_chunk(), row.score)


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


# ── local static embeddings ────────────────────────────────────────────────


def local_embedder(name: str = DEFAULT_LOCAL_MODEL) -> Embedder | None:
    """A static embedding model, or None when the optional dependency is absent.

    None rather than an exception, because the honest degradation is to keep
    answering lexically. A harness that refuses to search because an optional
    model is missing has turned an improvement into a requirement, and the
    stated promise is that a laptop with nothing installed still works.

    The import is local: `model2vec` pulls in numpy and a tokenizer, and a
    session that never retrieves should not pay for either.
    """
    try:
        from model2vec import StaticModel  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    try:
        model = StaticModel.from_pretrained(name)
    except Exception:  # noqa: BLE001 - a download, a cache miss, a bad name
        # The model is fetched on first use, so this is also the offline path.
        return None
    return lambda texts: model.encode(list(texts)).tolist()


# ── the retriever ──────────────────────────────────────────────────────────


class Retriever:
    """One workspace, kept searchable.

    Holds the index open and syncs before each query. Syncing is per file, so
    the usual cost between one question and the next is a `git ls-files` and a
    handful of `stat` calls, not a rebuild.
    """

    def __init__(
        self, root: Path, config: RetrievalConfig, *, embed: Embedder | None = None
    ) -> None:
        self.root = Path(root)
        self.config = config
        self.index = SqliteIndex(self.root, lines_per_chunk=config.lines_per_chunk)
        self.embed = embed
        #: Recorded rather than raised, so the caller can tell the operator
        #: that they are getting lexical results from a hybrid configuration.
        self.degraded = config.kind == "hybrid" and embed is None

    def sync(self) -> tuple[int, int]:
        return self.index.sync(
            max_file_bytes=self.config.max_file_bytes,
            embed=self.embed if self.config.kind == "hybrid" else None,
        )

    def search(self, query: str, *, limit: int = 0) -> list[Hit]:
        limit = limit or self.config.limit
        self.sync()
        if self.config.kind == "hybrid" and self.embed is not None:
            try:
                vector = list(self.embed([query])[0])
            except Exception:  # noqa: BLE001 - degrade, never fail the turn
                vector = None
            rows = self.index.hybrid_search(query, vector, limit=limit)
        else:
            rows = self.index.search(query, limit=limit)
        return [Hit.of(row) for row in rows]

    def close(self) -> None:
        self.index.close()


# ── the remote provider backend ────────────────────────────────────────────


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Similarity, defined as zero wherever it would otherwise be undefined.

    Mismatched lengths mean the index was built with a different embedding
    model than the query used. A stale index should stop matching, not end a
    run with a shape error.
    """
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
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


# ── output ─────────────────────────────────────────────────────────────────


def render_hits(hits: Iterable[Hit]) -> str:
    """What the model sees: excerpts with the coordinates to go read more."""
    listed = list(hits)
    if not listed:
        return "no matches"
    lines = []
    for hit in listed:
        lines.append(f"--- {hit.chunk.label} (score {hit.score:.2f})")
        lines.append(hit.chunk.text)
        lines.append("")
    lines.append(
        "These are ranked excerpts, not whole files. Open one with read_file "
        "before relying on the code around it."
    )
    return "\n".join(lines)


def retrieval_config_from_dict(raw: dict[str, Any] | None, path: str = "retrieval") -> RetrievalConfig:
    from harness.core import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(
        value,
        {"kind", "endpoint", "api_key_env", "model", "local_model", "lines_per_chunk",
         "max_file_bytes", "limit"},
        path,
    )

    def text(name: str, default: str) -> str:
        return schema.string(value[name], f"{path}.{name}") if value.get(name) else default

    return RetrievalConfig(
        kind=text("kind", "lexical"),
        endpoint=text("endpoint", ""),
        api_key_env=text("api_key_env", ""),
        model=text("model", "text-embedding-3-small"),
        local_model=text("local_model", DEFAULT_LOCAL_MODEL),
        lines_per_chunk=schema.integer(
            value.get("lines_per_chunk", 40), f"{path}.lines_per_chunk", minimum=5
        ),
        max_file_bytes=schema.integer(
            value.get("max_file_bytes", 200_000), f"{path}.max_file_bytes", minimum=1_000
        ),
        limit=schema.integer(value.get("limit", 5), f"{path}.limit", minimum=1),
    )
