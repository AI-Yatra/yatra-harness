"""Prompting practices as dials, resolved per route.

Lives beside `auth` and `providers` rather than with the REPL because it is a
fact about a model, not about a loop: which dials a route wants follows from
what that model was trained on, and the batch loop would want the same answer.
It also has to be readable from `config`, which sits above this layer.

The published prompt-engineering guidance is mostly written for one model
family, and the parts of it that are genuinely universal are a smaller set than
the parts that read as universal. `docs/research/prompting-practices.md` sorts
them; this module is what that sorting is for.

The design follows from one observation: the advice that matters most has to
move in opposite directions for different models. A strong model told to verify
its work verifies twice and wastes a turn. A weak one told nothing never checks
at all. A model trained on angle-bracket pseudo-tool-calls, shown a system
prompt full of angle brackets, starts emitting `<function=name>` as message
text instead of calling anything. So none of this belongs hardcoded in one
shared string. It belongs per route, next to the other facts about a model that
the config already records.

What is *not* here is as deliberate as what is. Assistant-turn prefill,
`budget_tokens` and `thinking` fields are provider API shape rather than
prompting, and telling an arbitrary model its context will be compacted for it
is a promise the harness, not the model, has to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import RouteConfig

#: How a block heading is drawn. The default is Markdown because it is the one
#: format every instruction-tuned model has seen in bulk, and because it fails
#: safe: a model that ignores a `##` reads slightly flatter prose, where a model
#: that imitates an XML tag emits a fake tool call and the turn does nothing.
DELIMITERS = ("markdown", "xml", "plain")
TOOL_EMPHASIS = ("proactive", "neutral", "cautious")


@dataclass(frozen=True, slots=True)
class PromptProfile:
    """Which optional blocks a route's system prompt is assembled from."""

    name: str = "standard"

    #: markdown | xml | plain. See DELIMITERS.
    delimiters: str = "markdown"

    #: proactive nudges a model that under-calls tools; cautious restrains one
    #: that edits when it was asked a question. neutral adds nothing, which is
    #: right for a model whose defaults are already good.
    tool_emphasis: str = "neutral"

    #: Ask for a self-check after editing. Worth tokens on a mid model and
    #: actively wasteful on a strong one, which re-reads its own diff anyway.
    verification: bool = True

    #: Ask for a short plan before acting. Only for models with no native
    #: reasoning mode; on a reasoning model it duplicates what already happens
    #: and leaks the scaffold into the answer.
    reasoning_scaffold: bool = False

    #: Only for endpoints that actually accept several tool calls in one turn.
    #: Inert at best on a model that cannot, and tokens either way.
    parallel_tools: bool = False

    #: Some models fall silent after a tool call and some narrate every one.
    #: The correction is per model, so it is a dial rather than fixed text.
    summarize_after_tools: bool = False

    #: For long tasks: keep durable state in a file rather than trusting the
    #: window to still hold it.
    state_file: bool = False

    #: A per-route sentence for a quirk no dial covers.
    extra: str = ""

    def validated(self) -> PromptProfile:
        if self.delimiters not in DELIMITERS:
            raise ValueError(
                f"delimiters must be one of {', '.join(DELIMITERS)}, got {self.delimiters!r}"
            )
        if self.tool_emphasis not in TOOL_EMPHASIS:
            raise ValueError(
                f"tool_emphasis must be one of {', '.join(TOOL_EMPHASIS)}, "
                f"got {self.tool_emphasis!r}"
            )
        return self

    def heading(self, text: str) -> str:
        """Draw a block heading in this profile's delimiter style."""
        if self.delimiters == "xml":
            return f"<{_slug(text)}>"
        if self.delimiters == "plain":
            return f"{text.upper()}:"
        return f"## {text}"

    def close(self, text: str) -> str:
        return f"</{_slug(text)}>" if self.delimiters == "xml" else ""

    def block(self, title: str, body: str) -> str:
        """A titled block, delimited the way this route wants it."""
        if not body.strip():
            return ""
        parts = [self.heading(title), body.strip()]
        tail = self.close(title)
        if tail:
            parts.append(tail)
        return "\n".join(parts)


def _slug(text: str) -> str:
    return text.strip().lower().replace(" ", "_")


# ── presets ────────────────────────────────────────────────────────────────
# Named so a config can say what it wants rather than spell out seven fields,
# and so the reason for each combination is written down once.

PRESETS: dict[str, PromptProfile] = {
    # Small and fast. Nudged to use tools and to check itself, because that is
    # the failure mode at this tier: answering from the prompt without looking.
    # No planning scaffold: it tends to be narrated instead of acted on.
    "lean": PromptProfile(
        name="lean",
        tool_emphasis="proactive",
        verification=True,
        reasoning_scaffold=False,
        summarize_after_tools=True,
    ),
    # The default. Assumes a competent tool-caller that still benefits from
    # being told to verify.
    "standard": PromptProfile(name="standard"),
    # A strong model on a long task. Verification comes off because it already
    # re-reads its work, and asking twice costs a turn. State goes to a file
    # because the task will outlive the window.
    "deep": PromptProfile(
        name="deep",
        tool_emphasis="neutral",
        verification=False,
        parallel_tools=True,
        state_file=True,
    ),
    # For models with heavy XML in training, where tagged regions genuinely
    # parse better. Never the default: see the module docstring.
    "xml": PromptProfile(name="xml", delimiters="xml", parallel_tools=True),
    # Nothing optional at all. For measuring what the dials are worth, and for
    # a model that behaves worse the more it is told.
    "bare": PromptProfile(
        name="bare",
        tool_emphasis="neutral",
        verification=False,
        reasoning_scaffold=False,
        summarize_after_tools=False,
    ),
}

DEFAULT = "standard"


def get(name: str) -> PromptProfile:
    """Look up a preset by name."""
    key = (name or DEFAULT).strip().lower()
    if key not in PRESETS:
        raise KeyError(f"no prompt profile named {key!r}; have: {', '.join(sorted(PRESETS))}")
    return PRESETS[key]


def for_route(route: RouteConfig, override: str = "") -> PromptProfile:
    """The profile a route should use.

    An explicit name always wins. Otherwise it is inferred from the quality the
    config already records, which is a claim the operator made about the model
    and a better basis than guessing from its name. The bands are deliberately
    coarse: this picks a starting point, and a route that wants something else
    says so.
    """
    if override:
        return get(override)
    declared = getattr(route, "prompt_profile", "") or ""
    if declared:
        return get(declared)
    quality = float(getattr(route, "quality", 3.0) or 3.0)
    if quality >= 4.0:
        return PRESETS["deep"]
    if quality < 3.0:
        return PRESETS["lean"]
    return PRESETS[DEFAULT]


#: What `/profile <dial>` accepts, mapped to the field it sets. Both the field
#: name and the friendlier label work, because suggesting a name that is then
#: rejected is worse than having two spellings.
DIALS: dict[str, str] = {
    "delimiters": "delimiters",
    "tool_emphasis": "tool_emphasis",
    "verification": "verification",
    "reasoning_scaffold": "reasoning_scaffold",
    "plan_first": "reasoning_scaffold",
    "parallel_tools": "parallel_tools",
    "summarize_after_tools": "summarize_after_tools",
    "summarise_after_tools": "summarize_after_tools",
    "state_file": "state_file",
}


def dial(name: str) -> str:
    """The field a dial name sets, or "" if there is no such dial."""
    return DIALS.get(name.strip().lower().replace("-", "_"), "")


def describe(profile: PromptProfile) -> list[tuple[str, str]]:
    """Label and value for each dial, for `/profile` to print."""
    return [
        ("delimiters", profile.delimiters),
        ("tool emphasis", profile.tool_emphasis),
        ("verification", "on" if profile.verification else "off"),
        ("plan first", "on" if profile.reasoning_scaffold else "off"),
        ("parallel tools", "on" if profile.parallel_tools else "off"),
        ("summarise after tools", "on" if profile.summarize_after_tools else "off"),
        ("state file", "on" if profile.state_file else "off"),
    ]


def with_overrides(profile: PromptProfile, **dials: object) -> PromptProfile:
    """A copy with individual dials moved, for a `/profile <dial> <value>`."""
    return replace(profile, **dials).validated()  # type: ignore[arg-type]
