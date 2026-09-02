# Prompting practices: a model-agnostic distillation

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices

This page has consolidated what used to be separate child pages (be-clear-and-direct,
multishot, chain-of-thought, xml-tags, system-prompts, prefilling, chain-prompts,
long-context-tips) into one document with sections: general principles, output and
formatting, tool use, thinking and reasoning, agentic systems, capability-specific tips,
migration. All content below was read in full from that page, not summarized from a
summary. Where the page names a specific Claude model's behavior, that is called out
explicitly rather than folded into the general claim.

Second input is this repo's own evidence, drawn from `configs/ay.yaml` and the harness
code (`harness/repl/prompt.py`, `harness/repl/tools.py`):

- `qwen3-coder-flash` emits `<function=list_dir></function>` as message text instead of a
  real tool call, scoring 0/3 on a benchmark, and is excluded from the router's route list
  for exactly that reason (`configs/ay.yaml` lines 38-46).
- Gemini returns a `thought_signature` that must be echoed back or the next call 400s.
- `edit_file` in `harness/repl/tools.py` takes exact replacement text and refuses on an
  absent or ambiguous match, rather than a diff. Tool contract shape is part of prompting.
- `mercury-2` (a diffusion model) fixed a 13-failure suite in 7 seconds and 7 tool calls in
  a recorded session.

## 1. The practices

Each entry: principle, tier, what the source actually claims, and the model-agnostic
wording.

### Be specific about desired output and constraints
**Tier: universal.**
Source claims specificity improves results and gives a before/after example ("Create an
analytics dashboard" vs. one with explicit scope) — a recommendation, not a benchmark.
Model-agnostic wording: state the output format, length, and constraints explicitly;
do not rely on the model inferring "thoroughness" from a short prompt.

### State the goal and the reason behind an instruction
**Tier: universal.**
Source's example is a TTS-formatting rule explained by *why* it matters (the reader can't
pronounce ellipses), asserted to generalize better than a bare rule. No benchmark given,
just a plausibility claim ("Claude is smart enough to generalize from the explanation").
Model-agnostic wording: give the reason a constraint exists, not just the constraint;
small models benefit from this at least as much, since they have less capacity to infer
intent from a bare rule.

### Use a small number of examples (few-shot)
**Tier: universal.**
Source recommends 3-5 examples, relevant, diverse, and structurally distinguishable from
the instructions. No evidence cited beyond "known as few-shot or multishot prompting" —
this is treated as settled practice in the field generally, not an Anthropic-only finding.
Model-agnostic wording: unchanged. Few-shot examples are one of the oldest and most
broadly replicated levers in prompting, and nothing about them depends on Claude.

### Delimit structured regions unambiguously
**Tier: portable with adaptation.** See section 2 below for the full argument.
Source's mechanism is XML tags (`<example>`, `<document>`, `<instructions>`), justified
as reducing "misinterpretation" when a prompt mixes instructions, context, and variable
input. No comparative evidence against other delimiters is given; this is asserted as a
Claude affordance ("Claude excels at" this format elsewhere in Anthropic's docs, though
not on this specific page). The underlying principle — mark the boundaries of distinct
content types so the model does not blend them — is universal. The mechanism is not.

### Give the model a role
**Tier: portable with adaptation.**
Source shows a one-line system message ("You are a helpful coding assistant specializing
in Python.") and asserts "even a single sentence makes a difference," with no effect-size
evidence on this page. The mechanism (a `system` field separate from `user` turns) exists
on most major APIs (OpenAI, Gemini, most OpenAI-compatible endpoints including the ones in
`configs/ay.yaml`) but not universally identically — some local/Ollama-served models fold
system content into the first user turn, and its influence strength varies by how a model
was fine-tuned. Model-agnostic wording: put role/persona and behavioral ground rules in
whatever channel the endpoint treats as highest-priority context (a real `system` role
where supported, otherwise the first block of the first user turn), and keep it short.

### Long-context placement: put long documents before the question
**Tier: universal, with one non-portable number.**
Source states this "improves performance across all models" (their models) and cites an
internal claim of "up to 30 percent" quality improvement for queries placed after
multi-document input, with no methodology shown — treat the percentage as an Anthropic
internal number, not a portable measurement, but the *ordering effect itself* (recency /
proximity bias, information placed right before the query gets weighted highest) is a
well-documented behavior across transformer LLMs generally, not a Claude-specific claim.
Model-agnostic wording: put reference material first, instructions and the actual question
last, regardless of provider.

### Ground long-document answers in extracted quotes first
**Tier: universal.**
Source's technique: ask the model to pull relevant quotes into a delimited block before
answering, to focus it on relevant content. This is a restatement of extraction-before-
synthesis, a generally-observed technique for reducing hallucination on long contexts, not
tied to any Claude mechanism. Portable as-is.

### Model self-knowledge / identity strings
**Tier: Claude-specific / non-portable.**
The exact prompt ("The assistant is Claude, created by Anthropic...") and the practice of
hard-coding a specific model string for downstream API calls is meaningless outside the
Anthropic model family. The general instinct behind it — if your harness needs the model to
identify itself consistently or select a specific downstream model string, say so
explicitly rather than trusting the model's own guess — is worth keeping, but the content
must be rewritten per provider or omitted for a router like this repo's, which serves many
providers behind one persona.

### Tell the model what to do, not what not to do
**Tier: universal.**
Source's example: "Do not use markdown" reworded as "Your response should be composed of
smoothly flowing prose paragraphs." This is standard instruction-following advice, backed
by the general observation (not unique to Anthropic) that negated instructions are weaker
steering signals than positive framing across most instruction-tuned models, since a
negative constraint gives the model no target to move toward.

### Match prompt style to desired output style
**Tier: universal, weakly evidenced.**
Source claims prompt formatting "may influence" response style (hedged language — "may")
and suggests removing markdown from the prompt to reduce markdown in the output. This is a
plausible mirroring effect reported anecdotally, not measured on this page. Worth keeping
as a low-cost thing to try, not a guaranteed lever.

### Explicit tool-triggering instructions
**Tier: universal, mechanism varies.**
Source: models "sometimes provide suggestions rather than implementing them" unless told
to act; gives explicit imperative phrasing ("Change this function...") as the fix, and a
sample `<default_to_action>` block. The underactivation/overactivation problem is general
across tool-calling LLMs — this repo's own qwen3-coder-flash failure is a more severe
version of the same class of problem (the model didn't even try a real tool call, it wrote
pseudo-syntax as text). The fix Anthropic proposes (an explicit "call the tool, don't just
describe it" instruction) is worth adopting broadly, but see section 3 for why it is not
sufficient for qwen3-coder-flash specifically.

### Parallel tool call instructions
**Tier: portable with adaptation.**
Source gives a block encouraging simultaneous independent tool calls. Whether a provider's
API and model actually support emitting multiple tool calls in one turn varies: OpenAI-
compatible endpoints generally support an array of tool calls per turn, but a specific
model's training may not exploit it (weaker or smaller models tend to call one tool, wait,
then call the next, regardless of what the prompt asks). Model-agnostic wording: include
the instruction as a hint, but do not depend on it — build the harness's tool loop to
handle both a single serial call and a batch, and do not assume "make all independent
tool calls in parallel" changes anything for a model that has never been trained on
multi-call turns.

### Explicit self-check / verification instruction
**Tier: universal, with one important caveat.**
Source: "Append something like 'Before you finish, verify your answer against test
criteria.' This catches errors reliably, especially for coding and math." Given as a
confident claim without a cited number. The caveat the source itself raises is instructive:
Claude Opus 5 already over-verifies without being told, so the same instruction that helps
a weaker model can waste tokens/turns on a stronger one. Model-agnostic wording: make
self-check instructions a knob tied to model capability rather than a fixed constant — see
section 5.

### Chain-of-thought scaffolding when native reasoning is off
**Tier: portable with adaptation.**
Source frames this as a fallback for when Claude's adaptive/extended thinking is disabled:
"ask Claude to think through the problem" using delimited `<thinking>`/`<answer>` tags to
separate reasoning from final output. The underlying principle — asking a model to reason
step by step before answering measurably helps on non-trivial tasks, and separating the
reasoning from the delivered answer keeps the reasoning out of the user-facing text — is
one of the best-replicated findings in prompting generally, independent of Claude. The tag
mechanism should follow whatever this harness's delimiter choice is (see section 2), not
literally `<thinking>`.

### State tracking with files, not just context, across long tasks
**Tier: universal.**
Source recommends structured state files (JSON) for machine-checked state like test
results, and unstructured text for progress notes, plus using git as a checkpoint log. None
of this depends on Claude; it is standard advice for any agent whose context window is
smaller than its task. Directly applicable to this harness, which already puts git branch
and dirtiness into the system prompt (`harness/repl/prompt.py` `_git_summary`).

### Confirm before irreversible or externally-visible actions
**Tier: universal, and already implemented here as policy rather than prompt text.**
Source's sample block lists destructive operations (rm -rf, force push, dropping tables)
that should prompt for confirmation. This repo does not rely on the model reading and
obeying such a prompt — it enforces the same list as a hard `denied_commands` gate in
`configs/ay.yaml` (git push, git reset --hard, rm -rf, etc., refused outright regardless of
approval mode). This is strictly better than a prompt-only version of the same idea: a
prompt is a suggestion a model can ignore or a weak model may not parse reliably; a policy
gate is enforced code. Where the harness cannot gate an action in code, the prompt-level
version of this advice is still worth keeping as a second line of defense, not a
replacement for the gate.

### Research: state success criteria before searching
**Tier: universal.**
Straightforward advice — define what a successful answer looks like before starting —
applies to any tool-using research loop regardless of model.

### Subagent orchestration
**Tier: portable with adaptation.**
Source describes Claude "recognizing when tasks would benefit from delegating... proactively
without requiring explicit instruction" — this is presented as an emergent Claude behavior,
not a general capability. Weaker or smaller models are unlikely to spawn subagents
unprompted, and some harnesses (this one included, based on `harness/repl/tools.py`'s flat
tool list) may not even expose a subagent tool. The portable version: if a harness exposes
subagent/delegation tools, describe when to use them and when not to (parallelizable,
isolated-context work vs. simple sequential edits) explicitly, rather than counting on the
model inventing the pattern on its own.

### Reduce unwanted file creation / avoid overengineering / avoid hardcoding to pass tests
**Tier: universal.**
These three (temp-file cleanup, "don't add abstractions or config beyond what's asked,"
"solve the general problem, not the test cases") are general software-discipline
instructions with no Claude-specific mechanism. They read as directly reusable text for any
coding-agent system prompt, and closely match this repo's own `AGENTS.md`/`CLAUDE.md`
convention-loading mechanism (`_conventions` in `harness/repl/prompt.py`) — a natural place
to put them per-repo rather than hardcoding them into the harness-wide base prompt.

### "Investigate before answering" / grounding against hallucination
**Tier: universal.**
"Never speculate about code you have not opened... read the file before answering" is
general good practice for any coding agent with file tools, not a Claude-only finding. This
repo's own `BASE` prompt in `harness/repl/prompt.py` already encodes a version of this
("Read a file before you edit it... Search before you guess").

### Assistant-turn prefill
**Tier: Claude-specific / non-portable — explicitly called out for exclusion.**
The source states plainly: "Starting with Claude 4.6 models... prefilled responses... on
the last assistant turn are no longer supported. Requests... return a 400 error." Even on
Anthropic's own current models this technique is dead, and the source spends a full section
telling users how to migrate off it (structured outputs, direct anti-preamble instructions,
tool calling with an enum). For a harness that targets Qwen, Gemini, Mercury, GPT-family,
DeepSeek, and local models: prefill support is inconsistent across providers even where it
exists (some OpenAI-compatible endpoints accept a trailing assistant message and continue
it, some reject it, some silently ignore it), and Anthropic's own current models reject it
outright. This technique should not appear in a model-agnostic harness at all. Where the
same goal is needed (force a specific output shape, skip a preamble, avoid a bad refusal),
use the migrations the source itself proposes — explicit instruction, tool-call/enum
constraints, or a provider's native structured-output feature where one exists — none of
which are prefill.

### Adaptive/extended thinking, `effort` parameter, `thinking` budget
**Tier: Claude-specific / non-portable.**
The `thinking: {type: "adaptive"}` field, the `effort` parameter, and `budget_tokens` are
Anthropic API request fields with no equivalent shape on OpenAI-compatible endpoints (some
providers, like Gemini's OpenAI-compat surface or reasoning-tuned OSS models, have their
own separate reasoning-effort knobs — e.g. OpenAI's `reasoning_effort`, DeepSeek-R1's
built-in chain-of-thought that cannot be turned off — but the parameter names, defaults,
and behavior do not map 1:1). Model-agnostic version: if the harness needs to control how
much a model reasons before answering, treat it as a provider-specific capability to be
probed and configured per route in `configs/ay.yaml` (similar to how `latency`/`quality`
are already per-route metadata), not as a portable prompt instruction. Where no native
reasoning control exists, fall back to the plain-text CoT scaffolding above.

### Context-window awareness ("track remaining token budget")
**Tier: Claude-specific / non-portable claim, portable intent.**
The source describes a specific capability ("context awareness") on named Sonnet/Haiku
models that lets the model track its own remaining budget. Most other models have no such
introspection and will not benefit from being told "your context window will be
compacted." The portable version of the underlying intent — don't let the model panic-stop
a long task near a context limit — is better implemented by the harness itself (turn/token
budgets, checkpointing, explicit compaction), which is in fact what this repo already does
via `budgets.max_turns` / `max_context_chars` in `configs/ay.yaml`, rather than by asking an
arbitrary model to self-monitor a quantity it may not be able to observe.

## 2. The delimiter question

The source's mechanism is XML tags everywhere: `<example>`, `<document>`,
`<document_content>`, `<thinking>`, `<quotes>`, `<default_to_action>`, and so on, with the
justification that they "help Claude parse complex prompts unambiguously." No comparison to
alternative delimiters is given on this page — this is a preference built on how Claude was
trained (Anthropic's docs elsewhere describe Claude as exposed to heavy XML-structured
data during training), not a claim that XML is optimal across model families.

The repo's own evidence argues against defaulting to XML for tool-calling loops
specifically: `qwen3-coder-flash` emits `<function=list_dir></function>` as plain message
text instead of issuing a real tool call, and the router in `configs/ay.yaml` excludes the
model for exactly this reason ("emits `<function=name>` pseudo-calls as message text
instead of real tool calls, so the loop never sees a tool call and the turn ends having
done nothing"). This is a real, not hypothetical, failure mode: a model whose training
included angle-bracket pseudo-tool-call syntax (common in earlier open tool-calling
formats, ReAct-style scaffolds, and some fine-tuning datasets) can conflate "I am inside an
XML-tagged region" with "I should emit an XML-shaped function call," especially under
pressure from a system prompt that is itself full of angle brackets. A system prompt that
demonstrates `<thinking>`, `<example>`, `<document>` blocks is, from the model's side,
training-distribution evidence that XML-shaped output is what's wanted here, which raises
exactly the risk that produced the flash failure.

Three candidate delimiter styles for a general harness:

1. **XML tags** (`<context>...</context>`). Unambiguous nesting, well supported by models
   trained on Claude-style data or heavy web/code corpora containing XML/HTML. Actively
   risky for models whose pseudo-tool-call syntax is angle-bracket shaped, which is not a
   rare category among open coder models.
2. **Markdown headings and fenced code blocks** (`## Context`, `` ``` ``). Near-universal —
   every instruction-tuned model has seen enormous volumes of Markdown, since it is the
   dominant format in code repositories, chat UIs, and web-scraped training text. Fences
   are unambiguous for verbatim/code content specifically. Headings are weaker for marking
   nested or repeated structures (no natural way to nest `##` sections, and no per-instance
   metadata the way `<document index="2" source="...">` carries it).
3. **Plain labelled sections** (`INSTRUCTIONS:`, `CONTEXT:`, `---`). The lowest-risk option
   for models with unknown or unverified training background: no syntax that overlaps with
   any known pseudo-tool-call or code format. Loses machine-parseability (a harness that
   wants to programmatically extract "the `<quotes>` block" back out of a response has
   nothing to grep for) and gives up nesting almost entirely.

**Recommendation for this harness: Markdown headings plus fenced code blocks as the
default, with a per-route override to XML for providers verified to want it (Claude
proper, and any other model with confirmed strong XML-formatted training).**

Reasoning: Markdown is the closest thing to a universal, low-risk delimiter across the
provider list in `configs/ay.yaml` — Qwen, Gemini, Mercury, and OSS coder models are all
trained overwhelmingly on Markdown-formatted code, docs, and chat transcripts, so headings
and fences carry meaning without introducing syntax that looks like a tool call. It also
fails safe: worst case, a model that ignores the heading structure just reads the prompt as
undifferentiated prose, which is a much smaller failure than a model that mistakes a
`<function>` tag for something to imitate as literal output. XML should remain available as
a per-route configuration for models known to benefit from it and known not to have the
flash failure mode, rather than as the harness-wide default. This matches the pattern
`configs/ay.yaml` already uses (per-route `kind`, `stream`, quality metadata) — the
delimiter choice is exactly the kind of thing that belongs in per-route config, not in a
single hardcoded `BASE` prompt.

For tool-call syntax itself (not prompt delimiting, but worth flagging since it's the same
underlying risk): the fix for the flash failure is not a prompt instruction at all — it's
excluding the model from the router, which is what `configs/ay.yaml` already does. No
system-prompt wording reliably stops a model from repeating a pattern baked into its own
fine-tuning; the working mitigation was route-level exclusion, not prompting.

## 3. What conflicts

- **Prefill** (section 1) is actively broken advice to carry forward — it 400s on
  Anthropic's own current models and is unsupported or inconsistent elsewhere. Must be
  excluded, not adapted.
- **Heavy XML tagging as a system-prompt-wide default** risks the exact failure this repo
  has already observed on `qwen3-coder-flash`. Anthropic's advice to wrap "each type of
  content in its own tag" throughout a prompt is fine for Claude and actively risky for
  a model with angle-bracket pseudo-tool-call training. This is the sharpest concrete
  conflict between the source and this repo's evidence.
- **`thinking`/`effort`/`budget_tokens` request fields** are Anthropic API shape, not
  portable at all; a harness that forwards these fields to an OpenAI-compatible endpoint
  risks a hard error, not a no-op, since some providers reject unknown fields.
  `configs/ay.yaml` does not currently pass any such field, which is correct as it stands.
  If reasoning-effort control is added later, it needs to be per-route (see section 5), not
  a blanket parameter.
  Note also that some OpenAI-compatible providers surface a *different* opaque
  reasoning-adjacent artifact of their own — Gemini's `thought_signature`, which must be
  echoed back on the next call or that call 400s. This is not something Anthropic's guide
  covers at all (it is a different vendor's mechanism), but it is the same category of
  problem as prefill and `budget_tokens`: an API-shape detail that is not a prompting
  practice, does not generalize, and must be handled in the provider adapter, not in prompt
  text.
- **Context-window self-awareness prompting** ("your context window will be automatically
  compacted...") assumes a model capability (tracking its own remaining budget) that most
  models in `configs/ay.yaml` were never trained to have. Telling an arbitrary model this
  is true when it isn't risks it trusting a nonexistent safety net and running past what
  the harness will actually tolerate (`budgets.max_context_chars`, `max_turns`). The
  harness-side budget enforcement already in `configs/ay.yaml` is the correct place for this
  concern, not the prompt.
- **Parallel-tool-call prompting** assumes multi-tool-call turns are something the model
  can act on. For a weak or small model this is at best inert and at worst confusing filler
  in the prompt (more tokens spent on an instruction the model cannot follow). Should be
  gated on a per-route capability flag, not sent unconditionally.
- **Self-verification instructions "for coding and math"** are presented as a universal
  win, but the source's own note about Opus 5 (over-verification once the model is already
  strong at this) shows the effect is capability-dependent, not a fixed constant. A harness
  spanning weak and strong models should not hardcode this instruction into a shared base
  prompt.

## 4. A concrete proposed system prompt skeleton

Model-agnostic, Markdown-delimited, written for a harness like this one that routes across
providers. Blocks are ordered so that stable, cacheable material comes first and volatile,
per-turn material comes last, matching the caching rationale already stated in
`harness/repl/prompt.py`'s module docstring.

```
## Role

You are a coding agent running in a terminal, in the operator's own working directory.
You have tools to read, search, edit, and run things there.

## How to work

- Read a file before you edit it. Edits must match existing text exactly; guessing at
  current contents will fail.
- Prefer targeted edits over full-file rewrites for existing files. A full-file write
  replaces everything, including anything you did not explicitly include.
- Search before you guess. A wrong assumption about where something lives costs more
  than a search.
- When you edit code, run the project's tests or linter afterward if you can, and say
  what the result was.
- Never claim a command passed unless you ran it and saw it pass.
- Never speculate about code you have not opened.

## How to answer

- Be concise and concrete. This is a terminal, not a document.
- Answer the question asked. Do not restate what you just did unless it is not obvious
  from the conversation.
- If you cannot do something, say so plainly and say what you would need.

[OPTIONAL: ## Tool use
Use tools to take action, not just to describe what could be done. If asked to change
something, make the change rather than only describing it.
Only include this block for models/routes observed to under-trigger tools. Omit for
models observed to over-trigger — the fix there is removing emphasis, not adding it.]

[OPTIONAL: ## Reasoning
Think through non-trivial problems step by step before answering. Keep your reasoning
separate from your final answer.
Only include for models with no native reasoning mode (no adaptive-thinking-equivalent
request field). Omit for models with a native reasoning toggle already engaged via the
provider's own request field — duplicating it wastes tokens.]

[OPTIONAL: ## Verification
Before finishing, check your change against the stated requirement.
Gate on model capability tier (see section 5) — a strong model may already do this
unprompted and adding the instruction only adds latency.]

## Environment

Working directory: {root}
Platform: {platform}
{git branch and dirtiness, if a git repo}
Approval mode: {mode} — the harness {mode.label}.
Edits and commands may be refused by the operator. A refusal is final for that action;
do not retry it or try to reach the same effect another way without saying so.
Paths passed to tools are relative to the working directory and cannot escape it.

[OPTIONAL: ## Repository conventions
From {AGENTS.md / CLAUDE.md sources found}. Follow them.
{file contents}
Only included when the repo has one of the configured instruction files. This is also
the natural place for repo-specific "avoid overengineering" / "don't hardcode to pass
tests" text, rather than baking it into the harness-wide base prompt.]

[OPTIONAL: ## Operator instructions
{free-text passed for this session}
Only present when the operator supplied extra instructions for this run.]
```

Notes on the skeleton:
- No XML tags, no `<thinking>` scaffold by default — Markdown `##` headings mark region
  boundaries, chosen for the reasons in section 2.
- The `## Reasoning` and `## Verification` blocks are the two most clearly
  capability-dependent pieces of Anthropic's advice (adaptive thinking / over-verification
  on strong models) rewritten as toggles instead of fixed text.
- This is close to, but not identical to, the current `BASE` prompt in
  `harness/repl/prompt.py`: that prompt already follows most of this shape (role, how to
  work, how to answer, environment, conventions, operator instructions) and does not use
  XML tags. The main gap relative to this skeleton is that it has no tool-use-triggering,
  reasoning, or verification toggles — those are currently fixed (absent) rather than
  configurable per route.

## 5. What should be configurable rather than fixed

- **Delimiter style** (Markdown vs. XML vs. plain-labelled). Argued in section 2. Belongs
  per-route in `configs/ay.yaml`, since the right choice depends on what a specific model
  was trained on, not on the harness as a whole.
- **Tool-triggering emphasis** ("use tools proactively" vs. no extra instruction vs. "ask
  before acting"). The source itself documents this as a dial that has to move in opposite
  directions for different models (Opus 4.5/4.6 need it dialed *back*; a model that
  undertriggers needs it dialed *up*). A fixed instruction is wrong for at least one class
  of model in any multi-provider router.
- **Reasoning/verification instructions.** As in section 3 and 4: whether to add explicit
  CoT scaffolding and self-check instructions should depend on whether the model has a
  native reasoning mode and on its general capability tier, not be sent unconditionally.
  `configs/ay.yaml` already carries a `quality` field per route (e.g. `qwen3-coder-next` at
  3.5, `qwen3.7-max` at 4.0) that is a reasonable existing hook to gate this on.
  For providers whose reasoning surfaces an opaque continuation token (Gemini's
  `thought_signature`), whether that token must be echoed back is also a per-route fact
  that belongs in provider-adapter config, not prompt text.
- **Parallel-tool-call instruction.** Only useful for models/APIs that actually support
  multi-call turns; should be a per-route flag, off by default, rather than assumed.
  `configs/ay.yaml` has no such flag today; it would sit next to `stream` and `local`.
  This is also a case where the fix is architectural rather than promptable: the harness's
  own tool loop should tolerate whatever calling pattern a given model actually produces
  (one call at a time or a batch) rather than depending on a prompt instruction to make a
  weak model batch calls it was never trained to batch.
  A more robust signal than any prompt instruction is a demonstrated benchmark result per
  model, the way `configs/ay.yaml`'s comment block already records pass rates and token
  costs for the qwen routes — that is a stronger basis for a routing decision than trusting
  a general claim from vendor documentation.
- **Verbosity / summary-after-tool-use instructions.** The source notes Claude's latest
  models may skip verbal summaries after tool calls unless told otherwise, while
  Claude Fable 5.1 has the opposite default. Whatever a given model's unprompted default
  turns out to be, the correction is model-specific, so this should be a per-route text
  fragment rather than fixed harness-wide text.
- **Role/system-channel placement.** Whether role and ground rules go in a genuine
  `system` field or get folded into the first user turn depends on what the endpoint
  actually supports; this is provider-adapter configuration, not something to special-case
  in the prompt-assembly code path.
- **Confirmation-before-destructive-action text.** This repo already treats the hard cases
  (rm -rf, force push, git reset --hard) as a policy-level deny-list in `configs/ay.yaml`
  rather than prompt text, which is the right call — a prompt-level version of this should
  stay configurable per operator (some operators may want a wider or narrower set of
  actions gated) but should never be the *only* enforcement mechanism for anything that
  actually matters.
