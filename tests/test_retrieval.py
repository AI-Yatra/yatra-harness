"""Retrieval: finding the relevant part of a repository too big to read.

`search_repo` matches a literal string. That is exact and useless when you do
not already know the identifier -- "where is the retry backoff decided" finds
nothing, because nobody wrote that sentence in the code.

Two backends. Lexical (BM25) ranks by term statistics, needs no key and no
network, and is the default so retrieval works on a machine with neither.
Embedding ranks by cosine similarity against a provider's vectors and finds
things lexical search cannot, at the cost of a dependency on that provider.
Both are exercised here without a network: the scoring, chunking and ranking
are pure functions, and the provider's answers are recorded payloads.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import ConfigurationError
from harness.execution.retrieval import (
    BM25Index,
    Chunk,
    EmbeddingIndex,
    RetrievalConfig,
    chunk_file,
    cosine,
    iter_chunks,
    render_hits,
)


class ChunkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-retrieval-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_small_file_is_one_chunk(self) -> None:
        path = self.write("a.py", "def f():\n    return 1\n")
        chunks = chunk_file(path, "a.py", lines_per_chunk=40)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start_line, 1)

    def test_a_large_file_is_split_with_line_numbers(self) -> None:
        path = self.write("b.py", "\n".join(f"line {index}" for index in range(100)))
        chunks = chunk_file(path, "b.py", lines_per_chunk=40)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].start_line, 1)
        self.assertEqual(chunks[1].start_line, 41)

    def test_chunks_carry_their_path(self) -> None:
        path = self.write("c/d.py", "x = 1\n")
        self.assertEqual(chunk_file(path, "c/d.py", lines_per_chunk=40)[0].path, "c/d.py")

    def test_binary_and_undecodable_files_are_skipped(self) -> None:
        path = self.root / "image.bin"
        path.write_bytes(b"\x00\x01\xff\xfe")
        self.assertEqual(chunk_file(path, "image.bin", lines_per_chunk=40), [])

    def test_an_empty_file_produces_no_chunks(self) -> None:
        self.assertEqual(chunk_file(self.write("e.py", "  \n"), "e.py", lines_per_chunk=40), [])

    def test_the_walk_skips_git_and_bytecode(self) -> None:
        self.write("keep.py", "x = 1\n")
        self.write(".git/config", "[core]\n")
        self.write("__pycache__/mod.pyc", "junk\n")
        paths = {chunk.path for chunk in iter_chunks(self.root, RetrievalConfig())}
        self.assertEqual(paths, {"keep.py"})

    def test_oversized_files_are_skipped(self) -> None:
        self.write("big.py", "x\n" * 200_000)
        self.write("small.py", "y = 1\n")
        config = RetrievalConfig(max_file_bytes=1_000)
        paths = {chunk.path for chunk in iter_chunks(self.root, config)}
        self.assertEqual(paths, {"small.py"})


def chunks(*pairs: tuple[str, str]) -> list[Chunk]:
    return [
        Chunk(path=path, start_line=1, end_line=1, text=text) for path, text in pairs
    ]


class SignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-signature-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def signature(self):
        from harness.execution.retrieval import workspace_signature

        return workspace_signature(self.root, RetrievalConfig())

    def test_an_unchanged_tree_has_a_stable_signature(self) -> None:
        self.assertEqual(self.signature(), self.signature())

    def test_a_new_file_changes_the_signature(self) -> None:
        before = self.signature()
        (self.root / "b.py").write_text("y = 2\n", encoding="utf-8")
        self.assertNotEqual(before, self.signature())

    def test_an_edited_file_changes_the_signature(self) -> None:
        # This is the case that matters: the agent patches a file it already
        # indexed, and an index that does not notice answers from the old one.
        import os

        before = self.signature()
        path = self.root / "a.py"
        path.write_text("x = 2\n", encoding="utf-8")
        os.utime(path, ns=(before[1] + 1_000_000, before[1] + 1_000_000))
        self.assertNotEqual(before, self.signature())

    def test_ignored_directories_do_not_affect_it(self) -> None:
        before = self.signature()
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / "a.pyc").write_text("junk", encoding="utf-8")
        self.assertEqual(before, self.signature())


class BM25Tests(unittest.TestCase):
    CORPUS = chunks(
        ("retry.py", "the router retries a failed route with exponential backoff and jitter"),
        ("verify.py", "the verifier runs acceptance commands and checks the diff"),
        ("auth.py", "credentials resolve from the environment then the stored file"),
    )

    def test_the_relevant_chunk_ranks_first(self) -> None:
        hits = BM25Index(self.CORPUS).search("retry backoff", limit=3)
        self.assertEqual(hits[0].chunk.path, "retry.py")

    def test_a_query_with_no_matching_terms_returns_nothing(self) -> None:
        self.assertEqual(BM25Index(self.CORPUS).search("kubernetes helm chart", limit=3), [])

    def test_results_are_limited(self) -> None:
        self.assertEqual(len(BM25Index(self.CORPUS).search("the", limit=1)), 1)

    def test_a_term_in_every_document_does_not_decide_the_ranking(self) -> None:
        # "the" appears everywhere and carries no information; without inverse
        # document frequency it would dominate every query.
        hits = BM25Index(self.CORPUS).search("the credentials", limit=1)
        self.assertEqual(hits[0].chunk.path, "auth.py")

    def test_an_empty_corpus_is_not_an_error(self) -> None:
        self.assertEqual(BM25Index([]).search("anything", limit=3), [])

    def test_an_empty_query_returns_nothing(self) -> None:
        self.assertEqual(BM25Index(self.CORPUS).search("   ", limit=3), [])

    def test_scores_are_ordered_highest_first(self) -> None:
        hits = BM25Index(self.CORPUS).search("router route verifier", limit=3)
        self.assertEqual([hit.score for hit in hits], sorted((h.score for h in hits), reverse=True))


class CosineTests(unittest.TestCase):
    def test_identical_vectors_score_one(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 2.0], [1.0, 2.0]), 1.0, places=6)

    def test_orthogonal_vectors_score_zero(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_a_zero_vector_scores_zero_rather_than_dividing_by_zero(self) -> None:
        self.assertEqual(cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_vectors_of_different_lengths_score_zero(self) -> None:
        # A provider changing embedding dimensions mid-index must not crash a
        # run; a stale index should simply stop matching.
        self.assertEqual(cosine([1.0], [1.0, 2.0]), 0.0)


class EmbeddingIndexTests(unittest.TestCase):
    CORPUS = chunks(("a.py", "alpha"), ("b.py", "beta"))

    def embedder(self, table: dict[str, list[float]]):
        calls: list[list[str]] = []

        def embed(texts: list[str]) -> list[list[float]]:
            calls.append(list(texts))
            return [table[text] for text in texts]

        return embed, calls

    def test_the_nearest_vector_ranks_first(self) -> None:
        embed, _ = self.embedder(
            {"alpha": [1.0, 0.0], "beta": [0.0, 1.0], "find alpha": [0.9, 0.1]}
        )
        index = EmbeddingIndex(self.CORPUS, embed)
        self.assertEqual(index.search("find alpha", limit=1)[0].chunk.path, "a.py")

    def test_the_corpus_is_embedded_once_and_reused(self) -> None:
        # An index that re-embeds on every query turns a cheap lookup into a
        # provider bill.
        embed, calls = self.embedder(
            {"alpha": [1.0, 0.0], "beta": [0.0, 1.0], "q": [1.0, 0.0]}
        )
        index = EmbeddingIndex(self.CORPUS, embed)
        index.search("q", limit=1)
        index.search("q", limit=1)
        self.assertEqual(len(calls), 3)  # one corpus batch, then one per query

    def test_an_embedding_failure_falls_back_to_lexical_search(self) -> None:
        # Retrieval going quiet is worse than retrieval being approximate.
        def failing(_texts: list[str]) -> list[list[float]]:
            raise OSError("embeddings endpoint unreachable")

        index = EmbeddingIndex(
            chunks(("retry.py", "exponential backoff"), ("auth.py", "credentials")), failing
        )
        hits = index.search("backoff", limit=1)
        self.assertEqual(hits[0].chunk.path, "retry.py")


class RenderTests(unittest.TestCase):
    def test_hits_render_with_path_and_line_range(self) -> None:
        from harness.execution.retrieval import Hit

        text = render_hits([Hit(Chunk("a.py", 10, 20, "body text"), 1.5)])
        self.assertIn("a.py:10-20", text)
        self.assertIn("body text", text)

    def test_no_hits_says_so(self) -> None:
        self.assertIn("no matches", render_hits([]).lower())

    def test_the_model_is_told_these_are_excerpts(self) -> None:
        from harness.execution.retrieval import Hit

        text = render_hits([Hit(Chunk("a.py", 1, 5, "x"), 1.0)])
        self.assertIn("read_file", text)


class ConfigTests(unittest.TestCase):
    def test_the_default_backend_is_lexical(self) -> None:
        self.assertEqual(RetrievalConfig().kind, "lexical")

    def test_an_unknown_backend_is_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            RetrievalConfig(kind="telepathy")

    def test_embedding_requires_an_endpoint(self) -> None:
        with self.assertRaises(ConfigurationError):
            RetrievalConfig(kind="embedding", endpoint="")


class EmbeddingRequestTests(unittest.TestCase):
    def test_the_request_body_carries_the_texts_and_model(self) -> None:
        from harness.execution.retrieval import embedding_request

        request = embedding_request(
            RetrievalConfig(kind="embedding", endpoint="https://x/v1/embeddings",
                            model="text-embedding-3-small"),
            ["a", "b"], key="k",
        )
        body = json.loads(request.body)
        self.assertEqual(body["input"], ["a", "b"])
        self.assertEqual(body["model"], "text-embedding-3-small")

    def test_the_key_travels_in_a_header(self) -> None:
        from harness.execution.retrieval import embedding_request

        request = embedding_request(
            RetrievalConfig(kind="embedding", endpoint="https://x/v1/embeddings"),
            ["a"], key="secret",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertNotIn("secret", request.url)

    def test_vectors_are_read_back_in_request_order(self) -> None:
        from harness.execution.retrieval import parse_embeddings

        payload = json.dumps(
            {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}
        )
        self.assertEqual(parse_embeddings(payload, 2), [[1.0, 0.0], [0.0, 1.0]])

    def test_a_malformed_embedding_response_is_named(self) -> None:
        from harness.core.errors import ToolError
        from harness.execution.retrieval import parse_embeddings

        with self.assertRaises(ToolError):
            parse_embeddings("not json", 1)


if __name__ == "__main__":
    unittest.main()
