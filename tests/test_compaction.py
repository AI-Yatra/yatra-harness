"""Compaction strategies: how old observations are folded down.

Truncating an observation to its first 240 characters keeps the shape of what
happened and throws away the content. That is fine for a short run and wrong
for a long one, where the thing the model needs from turn three is the fact
it established, not the first line of the file it read.

The strategy is therefore pluggable, and the summarizing one must degrade to
the truncating one rather than taking a run down when a provider is unwell.
"""

from __future__ import annotations

import unittest

from harness.compaction import (
    CompactionConfig,
    SummarizingCompactor,
    TruncatingCompactor,
    build_compactor,
)
from harness.errors import ConfigurationError, ProviderExhausted


def observation(index: int, content: str = "") -> dict:
    return {
        "call_id": f"call-{index}",
        "tool": "read_file",
        "ok": True,
        "content": content or f"contents of file {index} " + "x" * 500,
        "error": None,
        "metadata": {"artifact_ref": f"artifacts/payloads/p{index}.txt"},
    }


class TruncatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compactor = TruncatingCompactor(240)

    def test_each_observation_becomes_one_entry(self) -> None:
        self.assertEqual(len(self.compactor.compact([observation(1), observation(2)])), 2)

    def test_the_tool_and_outcome_are_kept(self) -> None:
        entry = self.compactor.compact([observation(1)])[0]
        self.assertEqual(entry["tool"], "read_file")
        self.assertTrue(entry["ok"])

    def test_the_artifact_reference_survives(self) -> None:
        # The full content is still on disk; the reference is how the model
        # gets back to it.
        entry = self.compactor.compact([observation(1)])[0]
        self.assertEqual(entry["artifact_ref"], "artifacts/payloads/p1.txt")

    def test_content_is_bounded(self) -> None:
        entry = self.compactor.compact([observation(1)])[0]
        self.assertLessEqual(len(entry["summary"]), 260)

    def test_nothing_in_produces_nothing_out(self) -> None:
        self.assertEqual(self.compactor.compact([]), [])


class SummarizingTests(unittest.TestCase):
    def summarizer(self, answer: str = "The counter clamps at the wrong bound."):
        seen: list[str] = []

        def summarize(prompt: str) -> str:
            seen.append(prompt)
            return answer

        return summarize, seen

    def test_many_observations_become_one_summary(self) -> None:
        summarize, _ = self.summarizer()
        result = SummarizingCompactor(summarize, 2_000).compact(
            [observation(index) for index in range(6)]
        )
        self.assertEqual(len(result), 1)
        self.assertIn("clamps at the wrong bound", result[0]["summary"])

    def test_the_summary_says_how_many_turns_it_covers(self) -> None:
        summarize, _ = self.summarizer()
        result = SummarizingCompactor(summarize, 2_000).compact(
            [observation(index) for index in range(6)]
        )
        self.assertIn("6", str(result[0]))

    def test_the_model_is_shown_the_observations_it_must_summarize(self) -> None:
        summarize, seen = self.summarizer()
        SummarizingCompactor(summarize, 2_000).compact([observation(1, "unique-marker")])
        self.assertIn("unique-marker", seen[0])

    def test_the_prompt_is_bounded(self) -> None:
        summarize, seen = self.summarizer()
        SummarizingCompactor(summarize, 500).compact(
            [observation(index) for index in range(50)]
        )
        self.assertLessEqual(len(seen[0]), 600)

    def test_a_provider_failure_falls_back_to_truncation(self) -> None:
        # Compaction is a context optimisation. Taking a run down because the
        # summarizer is unwell trades a smaller context for no run at all.
        def failing(_prompt: str) -> str:
            raise ProviderExhausted("all routes failed")

        result = SummarizingCompactor(failing, 2_000).compact(
            [observation(1), observation(2)]
        )
        self.assertEqual(len(result), 2)
        self.assertIn("contents of file 1", result[0]["summary"])

    def test_an_empty_summary_falls_back_too(self) -> None:
        result = SummarizingCompactor(lambda _p: "   ", 2_000).compact([observation(1)])
        self.assertEqual(len(result), 1)
        self.assertIn("contents of file 1", result[0]["summary"])

    def test_nothing_in_calls_no_model(self) -> None:
        summarize, seen = self.summarizer()
        self.assertEqual(SummarizingCompactor(summarize, 2_000).compact([]), [])
        self.assertEqual(seen, [])

    def test_the_summary_is_marked_as_a_summary(self) -> None:
        # A model reading its own context has to be able to tell a recorded
        # observation from a paraphrase of several.
        summarize, _ = self.summarizer()
        entry = SummarizingCompactor(summarize, 2_000).compact([observation(1)])[0]
        self.assertEqual(entry["tool"], "compaction")


class SelectionTests(unittest.TestCase):
    def test_the_default_is_truncation(self) -> None:
        compactor = build_compactor(CompactionConfig(), summarize=None)
        self.assertIsInstance(compactor, TruncatingCompactor)

    def test_summarize_needs_a_summarizer_to_be_available(self) -> None:
        # Configured to summarize with no route able to do it, the honest
        # outcome is the deterministic strategy, not a crash mid-run.
        compactor = build_compactor(CompactionConfig(kind="summarize"), summarize=None)
        self.assertIsInstance(compactor, TruncatingCompactor)

    def test_summarize_is_selected_when_it_can_run(self) -> None:
        compactor = build_compactor(CompactionConfig(kind="summarize"), summarize=lambda p: "s")
        self.assertIsInstance(compactor, SummarizingCompactor)

    def test_an_unknown_strategy_is_refused_at_config_time(self) -> None:
        with self.assertRaises(ConfigurationError):
            CompactionConfig(kind="telepathy")


class RuntimeWiringTests(unittest.TestCase):
    """The configured strategy has to actually reach the context engine."""

    def config(self, **kwargs):
        from dataclasses import replace
        from pathlib import Path as _Path

        from harness.config import load_config

        root = _Path(__file__).resolve().parents[1]
        return replace(load_config(root / "configs" / "teaching.yaml"), **kwargs)

    def test_the_default_config_truncates(self) -> None:
        self.assertEqual(self.config().compaction.kind, "truncate")

    def test_a_truncating_harness_builds_no_summarizer(self) -> None:
        # Never construct a path that could make an extra provider call when
        # the operator did not ask for one.
        from harness.runtime import HarnessRuntime

        runtime = HarnessRuntime.__new__(HarnessRuntime)
        runtime.config = self.config()
        self.assertIsNone(runtime._summarizer())

    def test_a_summarizing_harness_builds_one(self) -> None:
        from harness.runtime import HarnessRuntime

        runtime = HarnessRuntime.__new__(HarnessRuntime)
        runtime.config = self.config(compaction=CompactionConfig(kind="summarize"))
        self.assertIsNotNone(runtime._summarizer())

    def test_the_strategy_is_loaded_from_the_config_file(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        from harness.config import load_config

        root = _Path(__file__).resolve().parents[1]
        directory = _Path(tempfile.mkdtemp(prefix="harness-compaction-"))
        path = directory / "config.yaml"
        path.write_text(
            "version: 1\n"
            "model_router:\n"
            "  primary: teaching\n"
            "  routes:\n"
            "    teaching:\n"
            "      kind: replay\n"
            f"      script: {root / 'scenarios' / 'repair_demo.yaml'}\n"
            "context:\n"
            "  compaction:\n"
            "    kind: summarize\n"
            "    max_chars: 400\n",
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertEqual(config.compaction.kind, "summarize")
        self.assertEqual(config.compaction.max_chars, 400)


if __name__ == "__main__":
    unittest.main()
