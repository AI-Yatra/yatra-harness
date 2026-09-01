"""How old observations are folded down when they leave the recent window.

Truncating an observation to its first few hundred characters keeps the shape
of what happened -- which tool ran, whether it worked -- and discards the
content. That is the right trade for a short run and the wrong one for a long
one, where what the model needs from turn three is the fact it established,
not the first line of the file it read.

So the strategy is chosen rather than assumed. Truncation is deterministic,
free, and always available. Summarization spends a model call to keep meaning
instead of shape, and it degrades to truncation whenever it cannot run:
compaction is a context optimisation, and taking a run down because the
summarizer is unwell trades a smaller context for no run at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import ConfigurationError, HarnessError
from .util import truncate

KINDS = ("truncate", "summarize")
DEFAULT_SUMMARY_CHARS = 240
SUMMARY_INSTRUCTION = (
    "You are compacting the earlier part of a coding agent's run so it fits in "
    "a smaller context. Write a short factual digest of what the agent learned "
    "and did below: the files it inspected, what it found, what it changed, and "
    "anything that failed. Keep specifics -- names, paths, error text. Do not "
    "speculate, do not give advice, and do not describe the format of the input. "
    'Reply with JSON of the form {"type":"finish","summary":"..."}.'
)


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    kind: str = "truncate"
    route: str = ""
    max_chars: int = DEFAULT_SUMMARY_CHARS
    prompt_chars: int = 8_000

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigurationError(
                f"context.compaction.kind must be one of {', '.join(KINDS)}; got {self.kind!r}"
            )


class Compactor(Protocol):
    def compact(self, observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fold observations leaving the recent window into context entries."""


class TruncatingCompactor:
    """One bounded entry per observation. Deterministic and free."""

    def __init__(self, max_chars: int = DEFAULT_SUMMARY_CHARS) -> None:
        self.max_chars = max_chars

    def compact(self, observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.entry(item, self.max_chars) for item in observations]

    @staticmethod
    def entry(item: dict[str, Any], max_chars: int) -> dict[str, Any]:
        content = str(item.get("content", ""))
        compact, _ = truncate(content.replace("\n", " "), max_chars)
        return {
            "call_id": item.get("call_id"),
            "tool": item.get("tool"),
            "ok": item.get("ok"),
            "summary": compact,
            # The full content is still on disk. This is how the model gets
            # back to it after the text has been folded away.
            "artifact_ref": (item.get("metadata") or {}).get("artifact_ref"),
        }


class SummarizingCompactor:
    """One digest for the whole batch, written by a model."""

    def __init__(
        self,
        summarize: Callable[[str], str],
        prompt_chars: int = 8_000,
        max_chars: int = DEFAULT_SUMMARY_CHARS,
    ) -> None:
        self.summarize = summarize
        self.prompt_chars = prompt_chars
        self.max_chars = max_chars

    def compact(self, observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        if not observations:
            return []
        try:
            summary = self.summarize(self._prompt(observations))
        except (HarnessError, OSError, ValueError):
            return self._fallback(observations)
        text = " ".join(str(summary).split())
        if not text:
            return self._fallback(observations)
        bounded, _ = truncate(text, max(self.max_chars * 4, self.max_chars))
        return [
            {
                "call_id": "compaction",
                # Named so a model reading its own context can tell a recorded
                # observation from a paraphrase of several.
                "tool": "compaction",
                "ok": True,
                "summary": bounded,
                "covers_observations": len(observations),
                "artifact_refs": [
                    reference
                    for item in observations
                    if (reference := (item.get("metadata") or {}).get("artifact_ref"))
                ],
            }
        ]

    def _fallback(self, observations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return TruncatingCompactor(self.max_chars).compact(observations)

    def _prompt(self, observations: Sequence[dict[str, Any]]) -> str:
        lines = [SUMMARY_INSTRUCTION, ""]
        for item in observations:
            outcome = "ok" if item.get("ok") else f"failed: {item.get('error')}"
            content = " ".join(str(item.get("content", "")).split())
            lines.append(f"- {item.get('tool')} ({outcome}): {content}")
        bounded, _ = truncate("\n".join(lines), self.prompt_chars)
        return bounded


def build_compactor(
    config: CompactionConfig, *, summarize: Callable[[str], str] | None
) -> Compactor:
    """Pick the strategy, falling back when the configured one cannot run.

    A config asking to summarize with no route able to do it resolves to
    truncation rather than raising. The alternative is a run that dies at the
    moment its context first fills, which is the worst possible time.
    """
    if config.kind == "summarize" and summarize is not None:
        return SummarizingCompactor(summarize, config.prompt_chars, config.max_chars)
    return TruncatingCompactor(config.max_chars)


def compaction_config_from_dict(
    raw: dict[str, Any] | None, path: str = "context.compaction"
) -> CompactionConfig:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(value, {"kind", "route", "max_chars", "prompt_chars"}, path)
    return CompactionConfig(
        kind=schema.string(value.get("kind", "truncate"), f"{path}.kind"),
        route=schema.string(value["route"], f"{path}.route") if value.get("route") else "",
        max_chars=schema.integer(
            value.get("max_chars", DEFAULT_SUMMARY_CHARS), f"{path}.max_chars", minimum=80
        ),
        prompt_chars=schema.integer(
            value.get("prompt_chars", 8_000), f"{path}.prompt_chars", minimum=500
        ),
    )
