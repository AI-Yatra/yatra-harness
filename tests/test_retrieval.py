"""Retrieval: chunking, the persistent index, and fusing two rankings.

What a chunk *is* decides what retrieval can find, so most of this file is
about boundaries rather than about scoring. A forty-line window starting at
line 60 is a fragment of two functions and the whole of neither, and nothing
in it says which functions those were; a chunk that knows it is `_retry_after`
can be found by someone asking about retries.

The index is asserted to be incremental, because that is the difference
between a design that works on this repository and one that works on a real
one. An agent edits two files a turn; re-reading the tree each time is the
cost that made the old in-memory index unusable at scale.

Nothing here needs a network or an embedding model. The fusion tests use a
fake embedder so the ranking maths is exercised on every machine, and the one
test that wants the real model skips when the optional dependency is absent.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import ConfigurationError, ToolError
from harness.execution.chunking import (
    MAX_CHUNK_LINES,
    Chunk,
    chunk_text,
    python_chunks,
    window_chunks,
)
from harness.execution.indexing import (
    DATA_MAX_BYTES,
    RRF_K,
    SqliteIndex,
    discover,
    fts_query,
    fuse,
    index_path,
)
from harness.execution.retrieval import (
    KINDS,
    Hit,
    RetrievalConfig,
    Retriever,
    cosine,
    embedding_request,
    local_embedder,
    parse_embeddings,
    render_hits,
    retrieval_config_from_dict,
)

SAMPLE = '''\
"""Module docstring."""

import os

CONSTANT = 1


def alpha(value):
    """Add one."""
    return value + 1


class Machine:
    """A machine."""

    limit = 10

    def start(self):
        return "started"

    @property
    def running(self):
        return True
'''


class Workspace(unittest.TestCase):
    """A throwaway git repository, because discovery asks git."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        # The index goes beside the workspace, not inside it, which is where
        # `index_path` puts it in production. An index under the root would be
        # discovered by the next sync and index itself.
        self.root = base / "repo"
        self.index_dir = base / "index"
        self.root.mkdir()
        self._open: list[SqliteIndex] = []
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=False)

    def tearDown(self) -> None:
        # Closed before the directory goes. An open SQLite handle keeps its
        # WAL file locked, and on Windows a locked file makes the whole
        # directory undeletable -- which is why `_RETRIEVERS` closes on
        # eviction rather than dropping the reference.
        for index in self._open:
            index.close()
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def index(self, **kwargs) -> SqliteIndex:
        opened = SqliteIndex(self.root, path=self.index_dir / "i.db", **kwargs)
        self._open.append(opened)
        return opened


# ── chunking ───────────────────────────────────────────────────────────────


class PythonChunkingTests(unittest.TestCase):
    def chunks(self, text: str = SAMPLE) -> list[Chunk]:
        return python_chunks(text, "m.py")

    def symbols(self, text: str = SAMPLE) -> list[str]:
        return [chunk.symbol for chunk in self.chunks(text) if chunk.symbol]

    def test_a_function_becomes_its_own_chunk(self) -> None:
        self.assertIn("alpha", self.symbols())

    def test_a_method_is_named_for_its_class(self) -> None:
        self.assertIn("Machine.start", self.symbols())

    def test_a_class_keeps_a_chunk_for_its_own_body(self) -> None:
        """The docstring and the class attributes live there."""
        head = next(c for c in self.chunks() if c.symbol == "Machine")
        self.assertIn("limit = 10", head.text)

    def test_module_level_code_is_still_indexed(self) -> None:
        """Imports and constants belong to no definition and must not vanish."""
        text = "\n".join(chunk.text for chunk in self.chunks())
        self.assertIn("CONSTANT = 1", text)
        self.assertIn("import os", text)

    def test_a_decorator_stays_with_its_function(self) -> None:
        """`@property` is often the most informative line about a method."""
        chunk = next(c for c in self.chunks() if c.symbol == "Machine.running")
        self.assertIn("@property", chunk.text)

    def test_line_numbers_point_at_the_real_lines(self) -> None:
        lines = SAMPLE.splitlines()
        for chunk in self.chunks():
            self.assertEqual(chunk.text.splitlines(), lines[chunk.start_line - 1 : chunk.end_line])

    def test_an_unparseable_file_falls_back_rather_than_failing(self) -> None:
        """A file mid-edit is exactly when an agent most wants to search it."""
        self.assertEqual(python_chunks("def broken(:\n", "m.py"), [])
        self.assertTrue(chunk_text("def broken(:\n", "m.py", lines_per_chunk=40))

    def test_a_huge_function_is_split_but_keeps_its_name(self) -> None:
        body = "def enormous():\n" + "\n".join(f"    x = {n}" for n in range(MAX_CHUNK_LINES * 2))
        pieces = [c for c in python_chunks(body, "m.py") if c.symbol == "enormous"]
        self.assertGreater(len(pieces), 1)

    def test_a_non_python_file_uses_windows(self) -> None:
        chunks = chunk_text("a\nb\nc\n", "notes.md", lines_per_chunk=2)
        self.assertEqual([c.symbol for c in chunks], ["", ""])


class SearchTextTests(unittest.TestCase):
    """What is indexed, as opposed to what is shown."""

    def test_the_symbol_is_searchable(self) -> None:
        """The gap this closes: code says `backoff_seconds`, never "retry backoff"."""
        chunk = Chunk("a/b/providers.py", 1, 2, "pass", "_retry_after")
        self.assertIn("_retry_after", chunk.search_text)

    def test_the_path_is_searchable_in_pieces(self) -> None:
        chunk = Chunk("harness/models/providers.py", 1, 2, "pass")
        self.assertIn("providers", chunk.search_text)
        self.assertIn("models", chunk.search_text)

    def test_the_source_is_shown_without_the_indexing_preamble(self) -> None:
        chunk = Chunk("a.py", 1, 2, "the source", "sym")
        self.assertEqual(chunk.text, "the source")

    def test_the_label_names_the_symbol_when_there_is_one(self) -> None:
        self.assertEqual(Chunk("a.py", 1, 9, "x", "f").label, "a.py:1-9 (f)")
        self.assertEqual(Chunk("a.py", 1, 9, "x").label, "a.py:1-9")


class WindowTests(unittest.TestCase):
    def test_windows_cover_the_file(self) -> None:
        chunks = window_chunks("a\nb\nc\nd\ne\n", "f.txt", lines_per_chunk=2)
        self.assertEqual([(c.start_line, c.end_line) for c in chunks], [(1, 2), (3, 4), (5, 5)])

    def test_blank_windows_are_dropped(self) -> None:
        self.assertEqual(window_chunks("\n\n\n\n", "f.txt", lines_per_chunk=2), [])

    def test_an_offset_shifts_the_line_numbers(self) -> None:
        chunk = window_chunks("a\n", "f.txt", lines_per_chunk=2, offset=10)[0]
        self.assertEqual(chunk.start_line, 11)


# ── discovery ──────────────────────────────────────────────────────────────


class DiscoveryTests(Workspace):
    def test_git_ignore_rules_are_honoured(self) -> None:
        """The bug this fixes: 35% of this repository's index was build output."""
        self.write(".gitignore", "built/\n")
        self.write("kept.py", "x = 1\n")
        self.write("built/generated.py", "x = 1\n")
        found = discover(self.root, max_file_bytes=100_000)
        self.assertIn("kept.py", found)
        self.assertNotIn("built/generated.py", found)

    def test_a_brand_new_file_is_found_before_it_is_committed(self) -> None:
        """An agent searches for what it just wrote."""
        self.write("fresh.py", "x = 1\n")
        self.assertIn("fresh.py", discover(self.root, max_file_bytes=100_000))

    def test_a_tree_that_is_not_a_repository_still_works(self) -> None:
        plain = Path(tempfile.mkdtemp())
        (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.assertIn("a.py", discover(plain, max_file_bytes=100_000))

    def test_binaries_are_skipped(self) -> None:
        self.write("image.png", "not really")
        self.assertNotIn("image.png", discover(self.root, max_file_bytes=100_000))

    def test_a_large_data_file_is_skipped_where_code_that_size_is_not(self) -> None:
        """A big .json is a fixture or an artifact; a big .py is a module."""
        self.write("huge.json", json.dumps({"k": "v" * DATA_MAX_BYTES}))
        self.write("huge.py", "# pad\n" * DATA_MAX_BYTES)
        found = discover(self.root, max_file_bytes=DATA_MAX_BYTES * 10)
        self.assertNotIn("huge.json", found)
        self.assertIn("huge.py", found)

    def test_a_small_data_file_is_kept(self) -> None:
        self.write("package.json", '{"name": "x"}')
        self.assertIn("package.json", discover(self.root, max_file_bytes=100_000))

    def test_the_index_lives_outside_the_repository(self) -> None:
        """A question should never add an untracked directory to someone's repo."""
        self.assertNotIn(str(self.root), str(index_path(self.root)))


# ── the index ──────────────────────────────────────────────────────────────


class IndexTests(Workspace):
    def test_a_chunk_can_be_found_by_its_symbol(self) -> None:
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync()
        rows = index.search("Machine start", limit=5)
        self.assertTrue(any(row.symbol == "Machine.start" for row in rows))

    def test_only_changed_files_are_re_read(self) -> None:
        """The whole point of the rewrite: an agent edits two files a turn."""
        self.write("a.py", "def one(): pass\n")
        self.write("b.py", "def two(): pass\n")
        index = self.index()
        self.assertEqual(index.sync()[0], 2)
        self.assertEqual(index.sync()[0], 0, "an unchanged tree was re-read")
        self.write("a.py", "def one(): return 1\n")
        self.assertEqual(index.sync()[0], 1, "more than the edited file was re-read")

    def test_a_deleted_file_leaves_the_index(self) -> None:
        path = self.write("gone.py", "def vanished(): pass\n")
        index = self.index()
        index.sync()
        path.unlink()
        _, removed = index.sync()
        self.assertEqual(removed, 1)
        self.assertFalse(any(r.path == "gone.py" for r in index.search("vanished", limit=5)))

    def test_the_index_survives_the_process_that_built_it(self) -> None:
        self.write("m.py", SAMPLE)
        first = self.index()
        first.sync()
        first.close()
        second = self.index()
        self.assertEqual(second.sync()[0], 0, "a reopened index rebuilt itself")
        self.assertTrue(second.search("Machine", limit=3))

    def test_changing_the_chunk_size_rebuilds(self) -> None:
        """Old fragments ranked against new definitions is worse than rebuilding."""
        self.write("m.py", SAMPLE)
        self.index(lines_per_chunk=40).sync()
        rebuilt = self.index(lines_per_chunk=10)
        self.assertEqual(rebuilt.count(), 0)

    def test_an_undecodable_file_is_skipped_not_fatal(self) -> None:
        (self.root / "blob.txt").write_bytes(b"\xff\xfe\x00binary")
        index = self.index()
        index.sync()  # must not raise

    def test_an_empty_query_finds_nothing(self) -> None:
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync()
        self.assertEqual(index.search("", limit=5), [])

    def test_a_higher_score_means_a_better_match(self) -> None:
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync()
        scores = [row.score for row in index.search("machine start", limit=5)]
        self.assertEqual(scores, sorted(scores, reverse=True))


class QuerySafetyTests(unittest.TestCase):
    """An operator's question must never be read as FTS5 syntax."""

    def test_punctuation_cannot_become_an_operator(self) -> None:
        self.assertNotIn("(", fts_query("what happens (when it fails)?"))

    def test_a_bare_boolean_word_is_quoted(self) -> None:
        self.assertIn('"and"', fts_query("this AND that"))

    def test_terms_are_joined_with_or(self) -> None:
        """A question is a bag of hints; requiring all of them finds nothing."""
        self.assertIn(" OR ", fts_query("retry backoff"))

    def test_single_characters_are_dropped(self) -> None:
        self.assertEqual(fts_query("a b"), "")

    def test_a_query_of_pure_punctuation_is_empty(self) -> None:
        self.assertEqual(fts_query("?!..."), "")

    def test_a_hostile_query_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            index = SqliteIndex(root, path=root / "i.db")
            try:
                index.sync()
                self.assertEqual(index.search('" OR "" OR NEAR(', limit=3), [])
            finally:
                index.close()


# ── fusion ─────────────────────────────────────────────────────────────────


class FusionTests(unittest.TestCase):
    def test_agreement_beats_a_single_first_place(self) -> None:
        """The whole reason to fuse: two weak votes outrank one strong one."""
        scores = fuse([["a", "b"], ["c", "b"]])
        self.assertGreater(scores["b"], scores["a"])
        self.assertGreater(scores["b"], scores["c"])

    def test_position_is_all_that_counts(self) -> None:
        """BM25 and cosine are not on one scale; only rank is comparable."""
        self.assertEqual(fuse([["x"]])["x"], 1 / (RRF_K + 1))

    def test_an_empty_ranking_contributes_nothing(self) -> None:
        self.assertEqual(fuse([[], ["a"]]), {"a": 1 / (RRF_K + 1)})


class HybridTests(Workspace):
    """Fusion end to end, with a fake embedder so it runs anywhere."""

    def embed(self, texts):
        # One dimension per keyword. Crude, deterministic, and enough to put a
        # known chunk at the top of the semantic ranking.
        return [[float("alpha" in t), float("machine" in t)] for t in texts]

    def test_a_semantic_hit_reaches_the_results(self) -> None:
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync(embed=self.embed)
        self.assertGreater(index.vector_count(), 0)
        rows = index.hybrid_search("alpha", self.embed(["alpha"])[0], limit=5)
        self.assertTrue(rows)

    def test_no_vectors_degrades_to_lexical_rather_than_returning_nothing(self) -> None:
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync()  # no embedder, so no vectors
        self.assertEqual(index.vector_count(), 0)
        self.assertTrue(index.hybrid_search("machine", [1.0, 0.0], limit=5))

    def test_a_vector_from_another_model_is_ignored_not_fatal(self) -> None:
        """A stale index should stop matching, not raise a shape error."""
        self.write("m.py", SAMPLE)
        index = self.index()
        index.sync(embed=self.embed)
        self.assertEqual(index.vector_search([1.0] * 99, limit=5), [])

    def test_vectors_follow_their_file_out_of_the_index(self) -> None:
        path = self.write("m.py", SAMPLE)
        index = self.index()
        index.sync(embed=self.embed)
        path.unlink()
        index.sync(embed=self.embed)
        self.assertEqual(index.vector_count(), 0)


class RetrieverTests(Workspace):
    def test_lexical_needs_nothing(self) -> None:
        self.write("m.py", SAMPLE)
        retriever = Retriever(self.root, RetrievalConfig())
        retriever.index.path = self.index_dir / "i.db"
        self.assertTrue(retriever.search("machine", limit=3))
        retriever.close()

    def test_hybrid_without_the_dependency_reports_itself_degraded(self) -> None:
        """Silently answering lexically from a hybrid config is the bad outcome."""
        retriever = Retriever(self.root, RetrievalConfig(kind="hybrid"), embed=None)
        self.assertTrue(retriever.degraded)
        retriever.close()

    def test_hybrid_with_an_embedder_is_not_degraded(self) -> None:
        retriever = Retriever(
            self.root, RetrievalConfig(kind="hybrid"), embed=lambda texts: [[1.0] for _ in texts]
        )
        self.assertFalse(retriever.degraded)
        retriever.close()

    def test_an_edit_is_visible_to_the_next_query(self) -> None:
        """The agent changes the repository between questions."""
        retriever = Retriever(self.root, RetrievalConfig())
        self.write("later.py", "def appeared_later(): pass\n")
        self.assertTrue(retriever.search("appeared_later", limit=3))
        retriever.close()


class LocalModelTests(unittest.TestCase):
    def test_a_missing_dependency_gives_none_rather_than_raising(self) -> None:
        """Optional means optional: no model is not an error."""
        self.assertIsNone(local_embedder("definitely/not-a-real-model-xyz"))

    def test_the_real_model_encodes_when_it_is_installed(self) -> None:
        embed = local_embedder()
        if embed is None:
            self.skipTest("model2vec is not installed")
        vectors = embed(["retry backoff", "colour palette"])
        self.assertEqual(len(vectors), 2)
        self.assertGreater(len(vectors[0]), 0)


# ── the remote backend, unchanged ──────────────────────────────────────────


class CosineTests(unittest.TestCase):
    def test_identical_vectors_score_one(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 2.0], [1.0, 2.0]), 1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_a_zero_vector_scores_zero_rather_than_dividing_by_zero(self) -> None:
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_vectors_of_different_lengths_score_zero(self) -> None:
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)


class EmbeddingRequestTests(unittest.TestCase):
    def config(self) -> RetrievalConfig:
        return RetrievalConfig(kind="embedding", endpoint="https://api.example/v1/embeddings")

    def test_the_request_body_carries_the_texts_and_model(self) -> None:
        request = embedding_request(self.config(), ["a", "b"], key="secret")
        body = json.loads(request.body)
        self.assertEqual(body["input"], ["a", "b"])

    def test_the_key_travels_in_a_header(self) -> None:
        request = embedding_request(self.config(), ["a"], key="secret")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertNotIn("secret", request.body)

    def test_no_key_sends_no_authorization(self) -> None:
        self.assertNotIn("Authorization", embedding_request(self.config(), ["a"], key="").headers)

    def test_vectors_are_restored_to_the_order_they_were_asked_for(self) -> None:
        """Providers may answer out of order; a misaligned corpus is nonsense."""
        payload = json.dumps(
            {"data": [{"index": 1, "embedding": [9.0]}, {"index": 0, "embedding": [1.0]}]}
        )
        self.assertEqual(parse_embeddings(payload, 2), [[1.0], [9.0]])

    def test_an_unreadable_response_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            parse_embeddings("not json", 1)


# ── output and config ──────────────────────────────────────────────────────


class RenderTests(unittest.TestCase):
    def hits(self) -> list[Hit]:
        return [Hit(Chunk("a.py", 3, 9, "body", "alpha"), 1.5)]

    def test_hits_render_with_their_coordinates(self) -> None:
        self.assertIn("a.py:3-9", render_hits(self.hits()))

    def test_the_symbol_is_shown(self) -> None:
        self.assertIn("alpha", render_hits(self.hits()))

    def test_no_hits_says_so(self) -> None:
        self.assertEqual(render_hits([]), "no matches")

    def test_the_model_is_told_these_are_excerpts(self) -> None:
        """Otherwise it treats a ranked fragment as the whole file."""
        self.assertIn("read_file", render_hits(self.hits()))


class ConfigTests(unittest.TestCase):
    def test_the_default_backend_needs_nothing(self) -> None:
        self.assertEqual(RetrievalConfig().kind, "lexical")

    def test_the_three_backends(self) -> None:
        self.assertEqual(KINDS, ("lexical", "hybrid", "embedding"))

    def test_an_unknown_backend_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            RetrievalConfig(kind="magic")

    def test_embedding_requires_an_endpoint(self) -> None:
        with self.assertRaises(ConfigurationError):
            RetrievalConfig(kind="embedding")

    def test_hybrid_needs_no_endpoint(self) -> None:
        self.assertEqual(RetrievalConfig(kind="hybrid").kind, "hybrid")

    def test_the_local_model_is_separate_from_the_remote_one(self) -> None:
        """So switching backends cannot send a local name to a provider."""
        config = RetrievalConfig()
        self.assertNotEqual(config.local_model, config.model)

    def test_an_unknown_key_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            retrieval_config_from_dict({"kynd": "lexical"})

    def test_the_shipped_config_parses(self) -> None:
        from harness.config import load_config

        root = Path(__file__).resolve().parents[1]
        self.assertIn(load_config(root / "configs" / "ay.yaml").retrieval.kind, KINDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
