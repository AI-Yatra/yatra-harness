"""Scoring a review instead of reading one.

A reviewing sub-agent already exists, and its output is prose. Prose is fine
to read and impossible to gate on: two reviews of the same diff cannot be
compared, and nothing can say "this one is not good enough" without a person
in the loop deciding what good enough meant this time.

A rubric fixes the dimensions in advance, so the reviewer scores what it was
asked to score rather than whatever it happened to notice, and a threshold
turns the result into a verdict.
"""

from __future__ import annotations

import json
import unittest

from harness.rubric import (
    DEFAULT_DIMENSIONS,
    RubricConfig,
    parse_review,
    render_rubric_prompt,
    verdict_for,
)


class PromptTests(unittest.TestCase):
    def test_the_prompt_names_every_dimension(self) -> None:
        prompt = render_rubric_prompt(RubricConfig())
        for dimension in DEFAULT_DIMENSIONS:
            self.assertIn(dimension, prompt)

    def test_the_prompt_states_the_scale(self) -> None:
        # A reviewer told to "score out of 2" without being told what 0, 1 and
        # 2 mean produces numbers that are not comparable between runs.
        prompt = render_rubric_prompt(RubricConfig())
        self.assertIn("0", prompt)
        self.assertIn("2", prompt)

    def test_the_prompt_demands_evidence(self) -> None:
        self.assertIn("file", render_rubric_prompt(RubricConfig()).lower())

    def test_the_prompt_asks_for_the_answer_where_the_harness_reads_it(self) -> None:
        # A reviewer that replies with bare JSON has not produced an action,
        # so the route fails and the run falls through to a fallback model
        # that answers a different question entirely. Seen in a live run.
        prompt = render_rubric_prompt(RubricConfig())
        self.assertIn("finish", prompt)
        self.assertIn("summary", prompt)

    def test_custom_dimensions_replace_the_defaults(self) -> None:
        prompt = render_rubric_prompt(RubricConfig(dimensions=("thread_safety",)))
        self.assertIn("thread_safety", prompt)
        self.assertNotIn("correctness", prompt)


class ParsingTests(unittest.TestCase):
    def review(self, **scores) -> str:
        return json.dumps({"scores": scores, "notes": "looked at the diff"})

    def test_scores_are_read(self) -> None:
        result = parse_review(self.review(correctness=2, verification=1), RubricConfig())
        self.assertEqual(result.scores["correctness"], 2)
        self.assertEqual(result.scores["verification"], 1)

    def test_json_embedded_in_prose_is_still_found(self) -> None:
        # Models wrap JSON in explanation no matter how firmly you ask.
        text = "Here is my review:\n" + self.review(correctness=2) + "\nHope that helps."
        self.assertEqual(parse_review(text, RubricConfig()).scores["correctness"], 2)

    def test_a_missing_dimension_scores_zero_and_is_reported(self) -> None:
        # Silently defaulting an unscored dimension to full marks would let a
        # lazy reviewer pass anything.
        result = parse_review(self.review(correctness=2), RubricConfig())
        self.assertEqual(result.scores["verification"], 0)
        self.assertIn("verification", result.missing)

    def test_an_out_of_range_score_is_clamped(self) -> None:
        result = parse_review(self.review(correctness=99), RubricConfig())
        self.assertEqual(result.scores["correctness"], 2)

    def test_a_non_numeric_score_becomes_zero(self) -> None:
        result = parse_review(self.review(correctness="great"), RubricConfig())
        self.assertEqual(result.scores["correctness"], 0)

    def test_unparseable_output_scores_nothing_and_says_so(self) -> None:
        result = parse_review("the diff looks fine to me", RubricConfig())
        self.assertEqual(set(result.scores.values()), {0})
        self.assertTrue(result.unparsed)

    def test_the_notes_are_kept(self) -> None:
        self.assertIn("looked at the diff", parse_review(self.review(correctness=2), RubricConfig()).notes)

    def test_unknown_dimensions_are_ignored(self) -> None:
        result = parse_review(self.review(correctness=2, vibes=2), RubricConfig())
        self.assertNotIn("vibes", result.scores)


class VerdictTests(unittest.TestCase):
    def scores(self, value: int) -> dict[str, int]:
        return dict.fromkeys(DEFAULT_DIMENSIONS, value)

    def test_full_marks_are_accepted(self) -> None:
        self.assertEqual(verdict_for(self.scores(2), RubricConfig()), "accept")

    def test_a_zero_on_any_dimension_blocks(self) -> None:
        # A hard floor per dimension, not an average. Averaging lets a
        # perfect score elsewhere hide a total failure in one place.
        scores = self.scores(2)
        scores["correctness"] = 0
        self.assertEqual(verdict_for(scores, RubricConfig()), "block")

    def test_a_middling_score_asks_for_revision(self) -> None:
        self.assertEqual(verdict_for(self.scores(1), RubricConfig()), "revise")

    def test_the_accept_threshold_is_configurable(self) -> None:
        self.assertEqual(
            verdict_for(self.scores(1), RubricConfig(accept_at=1.0)), "accept"
        )

    def test_no_scores_at_all_blocks(self) -> None:
        self.assertEqual(verdict_for({}, RubricConfig()), "block")


if __name__ == "__main__":
    unittest.main()
