"""Scoring a review instead of reading one.

The reviewing sub-agent's output is prose. Prose is fine to read and
impossible to gate on: two reviews of the same diff cannot be compared, and
nothing can say "not good enough" without a person deciding, again, what good
enough meant this time.

A rubric fixes the dimensions in advance so the reviewer scores what it was
asked to score rather than whatever it happened to notice, and a threshold
turns the result into a verdict. Two decisions here are deliberate. An
unscored dimension counts as zero, because defaulting it to full marks would
let a reviewer pass anything by saying less. And the verdict uses a floor per
dimension rather than an average, because an average lets a perfect score
somewhere hide a total failure somewhere else -- which is exactly the shape of
the review a model writes about its own area of confidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_SCORE = 2
DEFAULT_DIMENSIONS = (
    "correctness",
    "verification",
    "scope",
    "maintainability",
)
DIMENSION_GUIDANCE = {
    "correctness": "Does the change do what was asked, including the cases it does not obviously handle?",
    "verification": "Did the acceptance commands actually exercise the change, with evidence?",
    "scope": "Did the change stay inside what was asked, touching nothing else?",
    "maintainability": "Would the next person reading this understand why it is written this way?",
}


@dataclass(frozen=True, slots=True)
class RubricConfig:
    dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS
    accept_at: float = 2.0
    block_below: int = 1


@dataclass(frozen=True, slots=True)
class ReviewResult:
    scores: dict[str, int]
    notes: str = ""
    missing: tuple[str, ...] = field(default_factory=tuple)
    unparsed: bool = False

    @property
    def average(self) -> float:
        return (sum(self.scores.values()) / len(self.scores)) if self.scores else 0.0


def render_rubric_prompt(config: RubricConfig) -> str:
    """What the reviewer is asked for, in the form it must answer in."""
    lines = [
        "Score this change on each dimension below. The scale is fixed:",
        "  0 = fails this dimension outright",
        "  1 = partially satisfies it, with a specific gap",
        f"  {MAX_SCORE} = satisfies it, with evidence you can point at",
        "",
        "Dimensions:",
    ]
    for dimension in config.dimensions:
        guidance = DIMENSION_GUIDANCE.get(dimension, "")
        lines.append(f"  - {dimension}: {guidance}".rstrip(": "))
    lines.extend(
        [
            "",
            "Cite the file and line for anything you score below "
            f"{MAX_SCORE}. A score without evidence is an opinion, and an "
            "opinion is what this rubric exists to replace.",
            "",
            "When you are done, call the finish tool. Put this JSON in its "
            "summary, as a single line, and nothing you cannot support:",
            json.dumps(
                {
                    "scores": dict.fromkeys(config.dimensions, MAX_SCORE),
                    "notes": "what you found, with paths",
                }
            ),
            "",
            "Do not reply with the JSON as your message. It has to arrive as "
            "the finish summary, because that is the only field the harness "
            "records as your answer.",
        ]
    )
    return "\n".join(lines)


def parse_review(text: str, config: RubricConfig) -> ReviewResult:
    """Read a reviewer's answer, scoring anything it did not say as zero."""
    payload = _extract_json(text)
    if payload is None:
        return ReviewResult(
            scores=dict.fromkeys(config.dimensions, 0),
            notes=" ".join(text.split())[:2_000],
            missing=tuple(config.dimensions),
            unparsed=True,
        )
    raw_scores = payload.get("scores")
    raw_scores = raw_scores if isinstance(raw_scores, dict) else {}
    scores: dict[str, int] = {}
    missing: list[str] = []
    for dimension in config.dimensions:
        if dimension not in raw_scores:
            # Not defaulted to full marks: a reviewer that says less would
            # otherwise pass everything.
            scores[dimension] = 0
            missing.append(dimension)
            continue
        scores[dimension] = _clamp(raw_scores[dimension])
    return ReviewResult(
        scores=scores,
        notes=str(payload.get("notes") or "")[:4_000],
        missing=tuple(missing),
    )


def verdict_for(scores: dict[str, int], config: RubricConfig) -> str:
    """accept, revise or block -- with a floor, never an average alone."""
    if not scores:
        return "block"
    if any(value < config.block_below for value in scores.values()):
        return "block"
    average = sum(scores.values()) / len(scores)
    return "accept" if average >= config.accept_at else "revise"


def _clamp(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        # A reviewer answering "great" instead of a number has not scored the
        # dimension, so it has not passed it either.
        return 0
    return max(0, min(MAX_SCORE, number))


def _extract_json(text: str) -> dict[str, Any] | None:
    """Find the JSON object in a reply, however much prose surrounds it."""
    stripped = (text or "").strip()
    for candidate in _candidates(stripped):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _candidates(text: str) -> list[str]:
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates.extend(block.strip() for block in fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    return candidates
