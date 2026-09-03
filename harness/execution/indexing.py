"""A workspace index that survives the process that built it.

The old index was built in memory, from a full walk of the tree, in every
process that asked a question. On this repository that is three seconds before
the first answer and it is linear in the size of the repository, so the cost
lands hardest exactly where retrieval matters most. Worse, it was rebuilt from
scratch when an agent edited one file, because the freshness check was a single
fingerprint over the whole tree.

So the index lives in SQLite, under the user's home rather than in the
repository, and it updates per file. An agent that edits two files in a
five-thousand-file repository re-reads two files.

FTS5 does the ranking. It is a full-text index with a real `bm25()` built into
the SQLite that ships with CPython, which means persistence, incremental
update and BM25 arrive together with no dependency at all. Verified present
before this was written, rather than assumed.

Discovery asks git. `git ls-files --cached --others --exclude-standard` is the
list of files that are tracked or could be, which is precisely the set worth
searching: it honours `.gitignore` and every other ignore rule the repository
already declares, and it includes a file the agent created a moment ago. The
previous walk ignored all of that and indexed build output -- on this
repository 35% of the index was generated JSON, and two copies of a generated
artifact outranked real code. A tree that is not a git repository falls back to
walking it.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
from array import array
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.execution.chunking import Chunk, chunk_text

#: A function from texts to vectors. Deliberately not a class: the caller
#: decides whether that is a local static model, a remote provider, or a
#: recorded fixture, and this module never needs to know.
Embedder = Callable[[list[str]], Sequence[Sequence[float]]]

#: Vectors are stored as raw little-endian float32. `array` is stdlib and the
#: format is fixed, so an index written on one machine reads on another.
_VECTOR_TYPE = "f"

#: Bumped when the schema or the meaning of a stored column changes, so an
#: index written by an older harness is rebuilt rather than misread.
SCHEMA_VERSION = 1

#: Never worth indexing whatever the ignore rules say.
IGNORED_DIRECTORIES = {".git", ".runs", ".venv", "__pycache__", "node_modules", ".evals", ".ay"}
IGNORED_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".xlsx", ".ico", ".woff", ".woff2", ".ttf", ".wasm",
    ".lock", ".map",
    # An index must never index itself. It normally lives outside the
    # repository, but an operator can point it anywhere, and a self-indexing
    # index grows on every sync and reports its own contents as matches.
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
}

#: Terms in an FTS5 query. Everything else in the operator's question is
#: punctuation, and FTS5 would read some of it as syntax: an unbalanced quote
#: or a bare `AND` turns a question into a parse error rather than a search.
_TERM = re.compile(r"[A-Za-z0-9_]+")

#: A much tighter cap for data formats than for code. A large `.json` is a
#: fixture, a lockfile or a generated artifact, and indexing it buries real
#: code: two copies of this repository's generated `atlas.json` outranked the
#: implementation on a query about it. A small one is a real config file and
#: worth having. zvec-grep reaches the same conclusion by the same route.
DATA_SUFFIXES = {".json", ".csv", ".tsv", ".xml", ".ndjson", ".jsonl", ".sql"}
DATA_MAX_BYTES = 32_000

#: `git` output can be arbitrarily large on a monorepo; this bounds the read.
_GIT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class Row:
    """One indexed chunk, as it comes back from a query."""

    path: str
    start_line: int
    end_line: int
    symbol: str
    text: str
    score: float

    def as_chunk(self) -> Chunk:
        return Chunk(self.path, self.start_line, self.end_line, self.text, self.symbol)


def index_path(root: Path) -> Path:
    """Where a workspace's index lives.

    Outside the repository, deliberately, for the same reason the credential
    store is: an index is machine state, it is large, and a repository should
    never gain an untracked directory because someone asked a question.
    """
    override = os.environ.get("YATRA_HARNESS_INDEX_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".yatra-harness" / "index"
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return base / f"{digest}.db"


def discover(root: Path, *, max_file_bytes: int) -> list[str]:
    """Every file worth indexing, relative and posix, in a stable order."""
    root = Path(root)
    listed = _git_files(root)
    candidates = listed if listed is not None else _walk(root)
    kept = []
    for relative in candidates:
        path = root / relative
        if Path(relative).suffix.lower() in IGNORED_SUFFIXES:
            continue
        if set(Path(relative).parts) & IGNORED_DIRECTORIES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        cap = DATA_MAX_BYTES if Path(relative).suffix.lower() in DATA_SUFFIXES else max_file_bytes
        if not path.is_file() or stat.st_size > cap:
            continue
        kept.append(relative)
    return sorted(set(kept))


def _git_files(root: Path) -> list[str] | None:
    """Ask git, or None when this is not a repository or git is absent."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _walk(root: Path) -> list[str]:
    out = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        out.append(path.relative_to(root).as_posix())
    return out


def fts_query(text: str) -> str:
    """An operator's question, as an FTS5 expression that cannot be a syntax error.

    Every term is quoted and joined with OR, so ranking decides which matches
    matter rather than a term being mandatory. A question is a bag of hints;
    requiring all of them finds nothing.
    """
    terms = {match.group(0).lower() for match in _TERM.finditer(text)}
    terms = {term for term in terms if len(term) > 1}
    return " OR ".join(f'"{term}"' for term in sorted(terms))


class SqliteIndex:
    """A workspace's chunks, ranked by FTS5, updated one file at a time."""

    def __init__(self, root: Path, *, lines_per_chunk: int = 40, path: Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.lines_per_chunk = lines_per_chunk
        self.path = path or index_path(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create()

    # ------------------------------------------------------------------ setup

    def _create(self) -> None:
        with self.connection as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS files "
                "(path TEXT PRIMARY KEY, mtime INTEGER NOT NULL, size INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
                "  path UNINDEXED, start_line UNINDEXED, end_line UNINDEXED,"
                "  symbol UNINDEXED, text UNINDEXED, body"
                ")"
            )
            # Vectors live beside the chunks rather than in their own store,
            # so one file changing invalidates both together. Two stores with
            # separate freshness rules is how a semantic hit ends up pointing
            # at a line that has since moved.
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors ("
                "  path TEXT NOT NULL, start_line INTEGER NOT NULL, vector BLOB NOT NULL,"
                "  PRIMARY KEY (path, start_line))"
            )
        if self._meta("fingerprint") != self._fingerprint():
            # The chunking changed, so every stored chunk was cut by different
            # rules. Mixing the two would rank old fragments against new
            # definitions, which is worse than paying to rebuild.
            self.clear()

    def _fingerprint(self) -> str:
        return f"{SCHEMA_VERSION}:{self.lines_per_chunk}"

    def _meta(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else ""

    def clear(self) -> None:
        with self.connection as db:
            db.execute("DELETE FROM chunks")
            db.execute("DELETE FROM files")
            db.execute("DELETE FROM vectors")
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('fingerprint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._fingerprint(),),
            )

    def close(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------------ sync

    def sync(
        self, *, max_file_bytes: int = 200_000, embed: Embedder | None = None
    ) -> tuple[int, int]:
        """Bring the index up to date. Returns (files changed, files removed).

        `embed` is called only for the chunks of files that actually changed,
        which is what makes a semantic index affordable to keep fresh: an
        agent editing two files re-embeds two files, not the repository.
        """
        present = discover(self.root, max_file_bytes=max_file_bytes)
        known = {
            path: (mtime, size)
            for path, mtime, size in self.connection.execute(
                "SELECT path, mtime, size FROM files"
            )
        }
        changed, gone = [], set(known) - set(present)
        for relative in present:
            try:
                stat = (self.root / relative).stat()
            except OSError:
                gone.add(relative)
                continue
            if known.get(relative) != (stat.st_mtime_ns, stat.st_size):
                changed.append((relative, stat.st_mtime_ns, stat.st_size))
        with self.connection as db:
            for relative in gone:
                db.execute("DELETE FROM chunks WHERE path = ?", (relative,))
                db.execute("DELETE FROM files WHERE path = ?", (relative,))
                db.execute("DELETE FROM vectors WHERE path = ?", (relative,))
            for relative, mtime, size in changed:
                db.execute("DELETE FROM chunks WHERE path = ?", (relative,))
                db.execute("DELETE FROM vectors WHERE path = ?", (relative,))
                fresh = self._chunks(relative)
                for chunk in fresh:
                    db.execute(
                        "INSERT INTO chunks (path, start_line, end_line, symbol, text, body) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (chunk.path, chunk.start_line, chunk.end_line, chunk.symbol,
                         chunk.text, chunk.search_text),
                    )
                if embed is not None and fresh:
                    for chunk, vector in zip(fresh, embed([c.search_text for c in fresh]), strict=False):
                        db.execute(
                            "INSERT OR REPLACE INTO vectors (path, start_line, vector) "
                            "VALUES (?, ?, ?)",
                            (chunk.path, chunk.start_line, _pack(vector)),
                        )
                db.execute(
                    "INSERT INTO files (path, mtime, size) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size",
                    (relative, mtime, size),
                )
        return len(changed), len(gone)

    def _chunks(self, relative: str) -> list[Chunk]:
        try:
            text = (self.root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary and undecodable files are not an error; a text index
            # simply has nothing to say about them.
            return []
        return chunk_text(text, relative, lines_per_chunk=self.lines_per_chunk)

    # ---------------------------------------------------------------- queries

    def search(self, query: str, *, limit: int = 5) -> list[Row]:
        """The best-matching chunks, most relevant first."""
        expression = fts_query(query)
        if not expression:
            return []
        try:
            rows = self.connection.execute(
                "SELECT path, start_line, end_line, symbol, text, bm25(chunks) AS rank "
                "FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                (expression, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # A term FTS5 still refuses is a bad query, not a broken session.
            return []
        # bm25() is negative and ascending-best. Flipped so every ranking in
        # the codebase means the same thing: larger is better.
        return [
            Row(path, start, end, symbol, text, -float(rank))
            for path, start, end, symbol, text, rank in rows
        ]

    def all_chunks(self) -> list[Chunk]:
        """Every chunk, for a backend that has to score them itself."""
        return [
            Chunk(path, start, end, text, symbol)
            for path, start, end, symbol, text in self.connection.execute(
                "SELECT path, start_line, end_line, symbol, text FROM chunks ORDER BY path, start_line"
            )
        ]

    def vector_search(self, query_vector: Sequence[float], *, limit: int = 5) -> list[Row]:
        """Chunks ranked by cosine similarity against a stored vector.

        A linear scan, on purpose. An approximate-nearest-neighbour structure
        earns its complexity somewhere past a million vectors; a repository is
        thousands, and numpy compares them all in about a millisecond. Adding
        an index here would be a dependency bought with nothing.
        """
        rows = self.connection.execute(
            "SELECT v.path, v.start_line, v.vector, c.end_line, c.symbol, c.text "
            "FROM vectors v JOIN chunks c ON c.path = v.path AND c.start_line = v.start_line"
        ).fetchall()
        if not rows:
            return []
        query = _unpack(_pack(query_vector))
        norm = sum(value * value for value in query) ** 0.5
        if not norm:
            return []
        scored: list[Row] = []
        for path, start, blob, end, symbol, text in rows:
            vector = _unpack(blob)
            if len(vector) != len(query):
                # The index was built with a different model. A stale vector
                # should stop matching, not produce a shape error mid-turn.
                continue
            other = sum(value * value for value in vector) ** 0.5
            if not other:
                continue
            dot = sum(a * b for a, b in zip(query, vector, strict=True))
            scored.append(Row(path, start, end, symbol, text, dot / (norm * other)))
        scored.sort(key=lambda row: row.score, reverse=True)
        return scored[:limit]

    def hybrid_search(
        self, query: str, query_vector: Sequence[float] | None, *, limit: int = 5
    ) -> list[Row]:
        """Lexical and semantic candidates, fused by rank.

        Lexical alone cannot answer a question whose words are not in the
        code; semantic alone drifts off an exact identifier. Fusing by rank
        keeps both honest, and falls back to lexical when there are no vectors
        so a missing model degrades the answer rather than removing it.
        """
        depth = max(limit * 4, 20)
        lexical = self.search(query, limit=depth)
        if query_vector is None:
            return lexical[:limit]
        semantic = self.vector_search(query_vector, limit=depth)
        if not semantic:
            return lexical[:limit]
        key = lambda row: f"{row.path}:{row.start_line}"  # noqa: E731
        fused = fuse([[key(row) for row in lexical], [key(row) for row in semantic]])
        best = {key(row): row for row in (*semantic, *lexical)}
        ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        return [
            Row(row.path, row.start_line, row.end_line, row.symbol, row.text, score)
            for identifier, score in ordered[:limit]
            if (row := best.get(identifier)) is not None
        ]

    def vector_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0])


def _pack(vector: Sequence[float]) -> bytes:
    return array(_VECTOR_TYPE, [float(value) for value in vector]).tobytes()


def _unpack(blob: bytes) -> array:
    values = array(_VECTOR_TYPE)
    values.frombytes(blob)
    return values


#: Reciprocal rank fusion's damping constant, from the original paper and
#: unchanged in every implementation worth copying, zvec-grep's included.
RRF_K = 60


def fuse(rankings: Sequence[Iterable[str]], *, k: int = RRF_K) -> dict[str, float]:
    """Combine rankings by position rather than by score.

    Scores from BM25 and from cosine similarity are not on the same scale and
    cannot be added or averaged; one of them would simply win. Rank is the
    only thing the two agree on, so fusion uses position and nothing else.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
    return scores
