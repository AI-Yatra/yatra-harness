# Readiness research: yatra-harness against the state of the art

Scope: what the literature actually shows about agent harness design, what the
four reference tools (Claude Code, Codex CLI, Aider, Cursor/Cline) are
documented to do, what this repository's code actually does, and the ranked
gap between the two. Part 3 was verified by reading the code listed in the
brief, not by reading this repo's own README or docs/ARCHITECTURE.md.

Classification used throughout: **(a) measured** = a benchmark or controlled
study backs the claim. **(b) practice** = a widely repeated engineering claim
with no benchmark behind it, usually from a vendor blog or practitioner
writing. **(c) marketing** = vendor claim with no methodology disclosed, or a
claim that conflicts with a measured result.

## Part 1: the literature

### Agent loop architecture: ReAct, Reflexion, what replaced them

ReAct (Yao et al. 2022) interleaves reasoning text with tool calls and
observations; Reflexion (Shinn et al. 2023) adds a self-critique step that
feeds a text "lesson" back into the next attempt. Both are academic patterns
from small benchmark suites (ALFWorld, HotpotQA, a handful of coding tasks),
not artifacts of production harness engineering.

What ships in production coding agents (Claude Code, Codex CLI, this repo) is
a plain tool-calling loop: send the whole message thread with tool schemas,
run whatever the model asks for, append results, repeat until the model stops
asking. There is no separate "reason" step distinct from the model's own
free-text output, and no distinct Reflexion-style critique phase built into
the harness. This repo's `harness/repl/agent.py` `_drive()` is exactly this
loop: infer, dispatch tool calls, append, repeat, bounded by step/tool/error
limits. **(b) practice.** No harness vendor publishes a benchmark showing
ReAct-with-explicit-thought-blocks beats a bare tool loop with a modern
reasoning model; the field's own explanation (repeated on multiple 2025-2026
practitioner blogs, e.g. "Agentic loops explained," "Stop Hand-Holding Your
Coding Agent," arxiv 2607.00038) is that explicit ReAct/Reflexion scaffolding
was a workaround for models that could not natively plan or critique, and
reasoning-capable models made the external scaffold largely redundant. This is
argued, not measured head-to-head at scale.

### SWE-bench / SWE-bench Verified and the agentless-vs-agent-loop question

**(a) measured**, with caveats on comparability. SWE-bench Verified is 500
human-filtered instances of the original 2,294-task SWE-bench, built by
OpenAI specifically to remove ambiguous specs and broken tests
(https://openai.com/index/introducing-swe-bench-verified/). On the original
benchmark, Agentless (Xia et al.) — a fixed localize-repair-validate pipeline
with **no autonomous agent loop at all** — roughly doubled the prior
open-source state of the art (from about 16% to over 30% resolve rate),
demonstrating that a well-engineered non-agentic pipeline can beat an
under-engineered agent loop. That result is real evidence against "more agent
autonomy is always better," but it is a comparison of *specific*
implementations from one era (2024), not a general law.

Since then, cross-scaffold studies (e.g. "Inside the Scaffold," arxiv
2604.03515) report that on recent SWE-bench Verified runs, **the underlying
model predicts task success more than the scaffold does** — the same model
posts materially different scores across SWE-agent, OpenHands, and Agentless,
but the spread across models on a fixed scaffold is larger than the spread
across scaffolds for a fixed model. The practical implication for harness
design: scaffold choices (tool shape, context handling, retry budget) move
scores by single-digit to low-double-digit points; model choice moves them
more. Harness engineering is real but second-order.

### Tool design: exact-string-replace vs. diff/patch application

**(b) practice, with one directly relevant measured data point.** Aider's own
published edit-format leaderboard (https://aider.chat/docs/leaderboards/edit.html)
is the closest thing to controlled evidence: it measures, per model, the rate
at which a model's proposed edit successfully re-applies to the file with no
human intervention, across whole-file rewrite, unified diff, and
search/replace ("exact string block") formats. The stated trade is not that
one format is more *correct*, but that whole-file is easiest for the model to
get right (highest apply-success) at the cost of tokens per edit, and diff
formats are token-cheap but fail to apply more often when the model gets an
anchor line wrong. A separate study ("To Diff or Not to Diff?," arxiv
2604.27296) found unified-diff and similar formats achieve near-perfect
round-trip exact match when the search anchor is unique, but search/replace
formats degrade to 0.81-0.94 success when the anchor string is ambiguous
(appears more than once) — which is exactly the failure mode this repo's
`edit_file` tool refuses outright rather than guessing
(`harness/repl/tools.py` lines 320-332: an `old_string` matching more than
once is a hard error unless `replace_all` is set). The repo's own docstring
argument — that a rejected exact-match hunk produces a specific, fixable error
message where a rejected diff hunk produces a wasted turn — matches this
literature's finding about *where* diff formats fail (ambiguous or
slightly-wrong context), but there is no controlled study isolating "exact
match with a required-unique-match refusal" as its own format; it is a
reasonable engineering inference from adjacent measured data, not a directly
benchmarked claim.

### Context management: compaction, retrieval, context rot

**(a) measured.** Chroma's 2025 study (https://www.trychroma.com/research/context-rot)
tested 18 frontier models (GPT-4.1, Claude 4, Gemini 2.5, Qwen3 among them) on
controlled tasks at increasing input length and found every model's
reliability degrades well before the stated context-window limit is reached —
a 200K-token model can show measurable degradation at 50K tokens on simple
retrieval and repetition tasks, and the degradation is worse for tasks with
more reasoning steps. This is distinct from context-window overflow (a hard
token-count failure) and is the empirical basis for the "lost in the middle"
finding (Liu et al., Stanford), which showed retrieval accuracy dropping for
facts placed mid-context versus at the start or end.

Compaction (Anthropic's own term, described in
"Effective context engineering for AI agents,"
https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
— summarizing the older part of a thread and continuing from the summary — is
the practitioner response, but its own effectiveness is **(b) practice**:
Anthropic describes the technique and how Claude Code implements it, but does
not publish a benchmark showing compacted sessions solve more tasks than
uncompacted ones cut off at the same token budget. This repo implements
compaction the same way Anthropic describes it: summarize by the same model
with tools disabled, replace the older messages, keep a safe tail
(`harness/repl/agent.py` `compact()`; `harness/repl/conversation.py`
`compact()`, `_trim_to_safe_start()`). The batch path additionally supports a
cheaper deterministic alternative, per-observation truncation, and falls back
to it if the summarizing call fails (`harness/run/compaction.py`). Neither
this repo nor Anthropic's post cites a benchmark quantifying how much task
success compaction preserves versus a hard truncation.

### Verification and self-correction

**(a) measured, and the measured result is skeptical of naive self-correction.**
Huang et al., "Large Language Models Cannot Self-Correct Reasoning Yet" (ICLR
2024, arxiv 2310.01798) found that asking a model to check and fix its own
output *without external feedback* frequently does not improve, and sometimes
degrades, accuracy. A 2026 follow-up, "The Self-Correction Illusion: LLMs
Correct Others but Not Themselves" (arxiv 2606.05976), sharpens this: models
can correctly judge the same claim when it is presented as coming from
someone else, but fail to apply the same judgment to their own
just-generated text — re-presenting a model's own claim under an external
role lifted correction rates by 23-93 percentage points across seven model
families. The practical implication for a harness: a "review your own diff"
instruction is weak; a genuinely external check (a test suite, a second
independent process, a different model) is what the evidence supports. This
repo's batch path enforces exactly the external-check version — verification
is a separate `Verifier` class that reruns acceptance commands and diffs
against protected paths, independent of the agent that made the changes
(`harness/run/verifier.py`) — but the REPL side has no equivalent: `ay`'s
prompt-profile `verification` dial (`harness/models/prompting.py`) only adds
a system-prompt instruction telling the model to check its own work, which is
precisely the self-correction shape the cited research finds weak. The gap
between the batch path's externally-verified design and the REPL's
prompted-self-check design is real and visible in the code, not just in the
docs.

### Sandboxing and permission models

**(a) measured for the security research, (b) practice for which model is
"best."** A 2026 systematization paper on execution-security research for AI
coding agents (arxiv 2607.05743) treats sandboxing as its own research area,
covering microVMs (Firecracker), syscall-interception sandboxes (gVisor),
hypervisor-based containers (Kata), and notes that permission-scoping and
runtime-approval mechanisms are common in deployed systems but
under-studied academically relative to their real-world use. There is no
published controlled comparison showing, e.g., that Codex's
sandbox-mode-plus-independent-approval-policy design catches more real
incidents than Claude Code's single ask-every-time gate; the design
differences below are documented behavior, not benchmarked safety outcomes.

### Multi-agent / subagent orchestration

**(a) measured for the negative case, (b)/(c) for the positive case.**
Cognition's "Don't Build Multi-Agents" (March 2025,
https://cognition.com/blog/dont-build-multi-agents) is a vendor blog, not a
benchmark, but its central claim is a specific, checkable failure mode: two
subagents working on the same deliverable in parallel with no shared state
produce genuinely incompatible outputs (their cited example: one subagent
built a Mario-style background, the other a realistic bird, for the same
Flappy Bird clone), because there is no fast-changing shared store for them
to coordinate through. Anthropic's own multi-agent research post reports an
internal eval where an orchestrator-plus-parallel-subagents architecture beat
a single Opus 4 agent by 90.2% on a research task
(https://www.anthropic.com/engineering/managed-agents-adjacent post family) —
this is **(c) marketing-adjacent**: it is a real internal number but with no
public methodology, no comparison harness released, and it is for
open-ended *research* (breadth-first fan-out over independent web
sub-questions), a task shape that tolerates independent subagent judgments far
better than a single shared code artifact does. Cognition's own later product
("Devin can now Manage Devins," 2026) shows the same company adopting
supervised multi-agent orchestration once each sub-Devin runs in its own
isolated VM with no interleaved shared-file editing — consistent with the
original critique rather than contradicting it: the failure mode was
concurrent uncoordinated edits to one shared artifact, not delegation itself.
This repo's own subagent design (`harness/run/subagents.py`) is built the
same way the evidence points: sub-agents are read-only (`FORBIDDEN_TOOLS`
blocks `apply_patch`, `python_run`, `browser_fetch`, `web_search`, and
further delegation), work from a copy of the workspace, and produce a report
rather than a diff — this sidesteps the exact "two writers collide" failure
Cognition documented, because there is only ever one writer. This is a sound
design choice given the literature, but it exists only on the batch (`harness
run`) path; the conversational REPL (`ay`) has no delegation tool at all (see
Part 3).

## Part 2: reference implementations

Sources: vendor docs current as of the search date, and the Codex and Aider
public repositories/docs where they gave a clearer answer than marketing copy.

| Capability | Claude Code | Codex CLI | Aider |
|---|---|---|---|
| Permission model | Three modes toggled live (default ask-per-call, auto-accept-edits, plan-mode read-only); `PreToolUse` hooks can programmatically approve/deny/modify calls before the built-in gate runs. | Two independent dials: sandbox mode (`read-only` / `workspace-write` / `danger-full-access`, OS-enforced) and approval policy (`untrusted` / `on-request` / `never`), so what the agent *can* touch and when it *must ask* are separate settings. | No live approval gate; safety is git-centric — every accepted edit is its own commit, and `/undo` reverts the last one. Aider will ask before running shell commands it proposes, but there is no per-edit approval prompt in the tool-call sense. |
| Context / compaction | `/compact` summarizes history and continues from the summary; documented as dropping tool transcripts but preserving decisions and file state. | Not separately documented in public sources found; Codex relies on model context window and repo-level `AGENTS.md` rather than a publicized compaction algorithm. | Repo map: a PageRank-ranked graph of every symbol definition/reference in the repo, token-budgeted, sent alongside the prompt instead of raw file dumps — a retrieval strategy, not a compaction strategy (there is little long-running single-session state to compact; each request is closer to stateless-per-edit). |
| Tool surface | File read/edit/write, bash, web fetch/search, and any MCP-exposed tools; schemas not fully public but documented via the tool list in Claude Code's own docs. | Shell execution plus file edit through the same sandbox; MCP client support documented. | No general tool-calling loop in the harness sense — edit formats (search/replace, unified diff, whole-file) *are* the tool surface; shell commands are proposed as text the user (or `/run`) executes. |
| Subagent support | Yes: named subagents with their own prompt, tool restrictions, and permission mode; inherit and can be narrowed from the main tool set; used for parallel bounded work. | Not part of the documented core CLI feature set as of the search date. | None. |
| Hooks / extensibility | Documented hook points (`PreToolUse`, `PostToolUse`, etc.) that run shell commands or scripts keyed to tool-call events. | Not documented as a first-class hook system in the sources found. | None documented; extension is via config flags and edit-format/model choice. |
| MCP | Full client support, plus the ability to add MCP servers project- or user-wide. | Documented MCP client support. | No MCP support found in current docs. |
| Session persistence / resume | Sessions persist and can be resumed. | Documented session resume. | No multi-turn "session" concept beyond the chat history in one run; git history is the persistent record. |
| Checkpointing / undo | Checkpoints are part of the broader tool ecosystem (Cursor and Cline, not Claude Code itself, are the ones documented with automatic pre-edit snapshots — see below); Claude Code's main undo mechanism is that edits are ordinary file writes the user's own git/VCS covers. | Sandboxed workspace-write mode limits blast radius but is not itself a checkpoint/undo feature; relies on the user's VCS. | Strong here: every AI edit is an automatic git commit, and `/undo` reverts the most recent one with a diff shown — the most granular, agent-native undo of the four. |
| Cost / token visibility | Not the focus of found docs; usage is visible through the Anthropic Console rather than a live in-session meter in most descriptions. | Not prominent in docs found. | Not prominent in docs found. |
| Config / instruction files | `CLAUDE.md`, generated by `/init`, read at session start. | `AGENTS.md`, Codex's direct equivalent, also generated by `/init`. | `.aider.conf.yml` / CLI flags for behavior; no equivalent auto-generated persistent instruction file documented as a first-class feature. |

Cursor and Cline/Roo, covered briefly since the brief asked for observable
capabilities rather than exhaustive treatment: Cursor's agent auto-creates
codebase checkpoints before significant changes and offers `@codebase`
semantic search over a maintained workspace index; Cline creates a
restorable snapshot at every tool call and has an explicit Plan/Act mode
separation; Roo Code (a Cline fork) adds "context condensing," which is the
same compaction idea as Claude Code's `/compact` — summarize what fell out of
the recent window, replace it, continue. All three of Cursor, Cline, and Roo
are documented with some form of automatic, granular, tool-call-level
checkpoint/restore. This is the single capability most consistently present
across reference tools that this repo's REPL entirely lacks (see Part 3 and
Part 4).

## Part 3: this repo, verified by code

Verified by reading `harness/repl/{agent,tools,approvals,conversation,model,
shell,prompt,banner}.py`, `harness/models/{auth,prompting}.py`,
`harness/execution/{tools,policy,sandbox,mcp,workspace}.py`,
`harness/run/{subagents,compaction,verifier}.py`, `harness/record/state.py`,
`harness/cli.py`, and `configs/ay.yaml`. This repo actually ships **two**
harnesses that share infrastructure but diverge sharply in capability: the
interactive REPL (`ay`, `harness/repl/`) and the batch task runner (`harness
run`, `harness/execution/` + `harness/run/`). The matrix below marks both
where they differ, because collapsing them would misrepresent either.

| Capability | Claude Code | Codex CLI | Aider | yatra-harness (`ay` REPL) | yatra-harness (`harness run` batch) |
|---|---|---|---|---|---|
| Permission model | 3 live modes + hooks | sandbox mode x approval policy, both OS/process-enforced | git-commit-per-edit, no live gate | 3 modes (`suggest`/`auto-edit`/`full-auto`) plus a hard, non-overridable deny-list checked before any approval path (`harness/repl/approvals.py` `_hard_refusal`); "allow always" is remembered per tool or per command head for the session | Same deny-list mechanism plus a positive allowlist of command prefixes (`harness/execution/policy.py`); `approval_mode` config knob (`never`/`always`/`mutations`) |
| Sandbox enforcement | not the mechanism; relies on OS trust | **OS-enforced** sandbox modes | none | **None.** `ReplToolset.run_command` calls `harness.execution.process.run_process` directly with the operator's full inherited environment (`harness/repl/tools.py` line ~358-364, `_command_environment()` returns `dict(os.environ)` verbatim). The Docker sandbox class exists in the codebase (`harness/execution/sandbox.py`) but the REPL never imports or calls it. | **Present.** `execution/tools.py` calls `build_sandbox(config.sandbox)` and the batch verifier does the same (`harness/run/verifier.py`); `sandbox.kind: docker` runs commands in a `--network none --cap-drop ALL --pids-limit 512` throwaway container (`harness/execution/sandbox.py` `docker_command`). |
| Context compaction | documented, undisclosed mechanics | undocumented | not applicable | **Present, model-driven.** Same model, same thread, tools disabled, summarizes and replaces all but a safety-trimmed tail of 6 messages (`harness/repl/agent.py` `compact()`, `harness/repl/conversation.py` `compact()`). Triggered automatically at 80% of the route's declared context window (`needs_compaction`). | **Present, two strategies.** `TruncatingCompactor` (deterministic, free, default) or `SummarizingCompactor` (model call, falls back to truncation on failure) (`harness/run/compaction.py`). |
| Tool surface | read/edit/write/bash/web/MCP | shell/edit + MCP | edit-format-as-tool-surface | 7 tools: `read_file`, `list_dir`, `glob`, `grep`, `write_file`, `edit_file`, `run_command`. `edit_file` is exact-string-replace with a hard refusal on zero or ambiguous (>1, without `replace_all`) matches (`harness/repl/tools.py` lines 311-341). No web/network tool of any kind — network tools are entirely batch-path (`harness/repl/prompt.py`: "browser_fetch and web_search are batch-path tools"). | Larger and pluggable: native tools plus MCP-discovered tools normalized into the same registry (`harness/execution/tools.py`, `harness tools` CLI command lists "normalized native and MCP capabilities"). |
| Subagent / delegation | yes, first-class | not documented | none | **Absent.** `ReplToolset._handlers` has no `delegate` entry; there is no code path from the REPL to `harness/run/subagents.py` at all. | **Present, deliberately constrained.** A sub-agent is read-only by construction (`FORBIDDEN_TOOLS` blocks writes, execution outside the allowlist, network, and further delegation), runs against a *copy* of the workspace with the git history preserved, depth-capped at 1 by default, call-capped at 3 per run, and its deliverable is a report, never a diff (`harness/run/subagents.py`). |
| MCP | yes | yes | no | **Absent from the REPL toolset.** The client exists (`harness/execution/mcp.py`, a real stdio JSON-RPC 2025-11-25 implementation) and is used by the batch path (`execution/tools.py` imports and calls `MCPStdioClient`, and iterates `config.mcp_servers`), but `harness/repl/tools.py` never references it — `ay` cannot reach an MCP server. | **Present.** Newline-delimited JSON-RPC over stdio, full initialize/initialized lifecycle, stderr drained on a background thread so a chatty server cannot deadlock the pipe (`harness/execution/mcp.py`). |
| Hooks | yes (`PreToolUse`/`PostToolUse`) | not documented | none | **Absent.** A repo-wide grep for "hook"/"hooks" across `harness/` returns zero matches. There is no hook mechanism anywhere in this codebase, REPL or batch. | Same: absent. |
| Session persistence / resume | yes | yes | git history only | **Persistence, not true resume-with-validation.** `Conversation.save`/`Conversation.load` round-trip the message list to a JSON file per session id (`harness/repl/conversation.py`); `ay --resume` reopens the most recent one. There is no schema/state validation beyond a version check and dropping orphaned leading tool messages (`_trim_to_safe_start`). | **Full checkpoint/resume.** `harness/record/state.py` is described in its own docstring as "atomic durable checkpoints and resume validation"; `harness run resume` is a first-class CLI command (`harness/cli.py`), backed by an append-only event ledger that `harness replay` can reconstruct and verify. |
| Checkpointing / undo of file edits | not Claude Code itself; ecosystem tools (Cursor, Cline) do this | sandbox limits blast radius, not undo | **best-in-class**: every AI edit is its own git commit, `/undo` reverts one | **Absent.** No automatic commit-per-edit, no snapshot-per-tool-call, no `/undo` command. `edit_file` and `write_file` mutate the file directly; the only recovery path is the operator's own git discipline (the system prompt tells the model the git branch and dirty-file count, `harness/repl/prompt.py` `_git_summary`, but takes no snapshot itself). | **Partial, at the run level.** The verifier and delivery machinery work against git diffs and can detect protected-path violations after the fact (`harness/run/verifier.py` `_changed_paths`), and checkpoints capture run *state*, but there is no per-edit rollback of the workspace itself — a bad edit inside an allowed run is not automatically reverted, only detected. |
| Cost / token visibility | console-based, not prominent in-session | not prominent | not prominent | **Present, per-session.** `/cost` reports input/output tokens and compaction count for the session; `/context` renders a filled-bar meter against the route's declared window (`harness/repl/shell.py` `_show_context`). Costs are estimated with a fixed 4-chars-per-token heuristic (`harness/repl/conversation.py` `estimate_tokens`), explicitly documented as "wrong for code and wrong for prose, in opposite directions," used only to decide when to compact, not billed accurately. | Route configs carry `cost_per_1m_input`/`cost_per_1m_output` (`configs/ay.yaml`), but no code path was found in `harness/run/` that sums these into a dollar total for a completed run — the number exists in config, not as a reported metric. |
| Config / instruction files | `CLAUDE.md`, `/init` | `AGENTS.md`, `/init` | none first-class | **Present and matched to the reference tools.** `AGENTS.md` and `CLAUDE.md` are both read at session start if present, concatenated into the system prompt, capped at 6,000 chars (`configs/ay.yaml` `context.instruction_files`/`max_instruction_chars`; `harness/repl/prompt.py` `_conventions`). `/init` asks the model to write one (`harness/repl/shell.py` `_command`, the `init` branch). | Same instruction-file mechanism, read once at run start (`harness/run/instructions.py`, referenced from config). |
| Multi-model routing / fallback | single model per session (model switch is manual) | single model per session | single model per session | **Present and more elaborate than any reference tool found.** `RouteChain` tries the chosen route, then configured fallbacks, then every other route with a live credential, switching only on errors the *route itself* owns (quota, dead key, outage — never a 400) and staying switched (`harness/repl/model.py` `RouteChain`). `configs/ay.yaml` documents 10 routes across 8 providers plus local Ollama, with per-route prompting profiles chosen by measured quality (see `harness/models/prompting.py` `for_route`). | Same routing infrastructure, plus retries and a circuit breaker count in config (`model_router.circuit_breaker_failures`). |
| Prompt tuning per model | fixed system prompt design | fixed system prompt design | fixed system prompt design | **Unusual and real.** A `PromptProfile` dataclass with 7 independent dials (delimiter style, tool-call emphasis, self-verification instruction, planning scaffold, parallel-tool hint, post-tool narration, state-file discipline), five named presets, switchable live with `/profile`, and picked automatically per route by a declared `quality` score if not stated explicitly (`harness/models/prompting.py`). No reference tool researched documents anything this granular; it is a genuine differentiator, not a gap. |

## Part 4: the gap list, ranked

Ranked by user-facing severity for someone relying on this as a daily coding
agent, in the `ay` REPL specifically (the batch path is close to parity with
the reference tools on several of these already, as the matrix shows).

**1. No sandbox enforcement in the REPL, ever.** `run_command` in
`harness/repl/tools.py` runs directly on the host with the operator's full
environment, regardless of `configs/*.yaml` sandbox settings — the
`DockerSandbox` class the batch path uses is simply never called from the
REPL. Why it matters: this is the one gap the literature has a dedicated
research area for (arxiv 2607.05743 on isolation for coding agents), and it
is the one difference between Codex CLI (OS-enforced sandbox, independent of
approval policy) and this tool that is architecturally, not just
configurationally, different — Codex cannot accidentally run a destructive
command with full host access from a misconfigured approval mode; this repo
currently can, in `full-auto` mode, because the sandbox layer that exists in
the codebase is wired to one execution path and not the other. Difficulty:
moderate. `ReplToolset.run_command` already delegates to
`harness.execution.process.run_process`; swapping in
`harness.execution.sandbox.build_sandbox(self.config.sandbox).run(...)` is a
small, local change, since the sandbox abstraction and Docker plumbing
already exist and are tested for the batch path. The real cost is deciding
default behavior (a REPL without Docker installed must still run) and testing
the Windows case, since this repo's dev environment is Windows and Docker
mounts/SELinux labels are Linux-shaped code paths. This is measured-important,
not cargo cult.

**2. No checkpoint/undo of file edits.** Every reference tool researched
(Aider via auto-commit, Cursor and Cline via automatic snapshots) gives the
operator a one-command way to revert the agent's last file change. This repo
has none for the REPL: `write_file`/`edit_file` mutate directly, and the only
safety net is whatever git discipline the operator already has, which the
system prompt merely reports (`_git_summary` in `harness/repl/prompt.py`)
rather than enforces. Why it matters: this is the single highest-frequency
recovery action in real agent-assisted coding sessions — undoing one bad
multi-file edit is far more common than needing a sandbox, and its absence
here is a genuine capability gap, not a design choice defended anywhere in
this repo's docs. Difficulty: low-to-moderate. The workspace already has git
context available (`_git_summary` runs `git status`/`rev-parse`); an
`/undo` that shells out to `git stash`/`git checkout` for tracked files, or a
lighter "snapshot file contents before write/edit, restore on `/undo`"
scheme scoped to files this session touched, is buildable without new
infrastructure. This is a measured user need (every competitor implements
some version of it) rather than cargo cult.

**3. No MCP access from the REPL.** The MCP client
(`harness/execution/mcp.py`) is a complete, tested stdio JSON-RPC
implementation, and the batch path uses it, but `harness/repl/tools.py` has
no code path to it at all — an operator using `ay` cannot add a filesystem,
database, or search MCP server the way they can in Claude Code or Codex CLI.
Why it matters: MCP is the mechanism by which a coding agent reaches tools
the harness author didn't anticipate (internal wikis, ticket trackers, other
company-specific tools); its absence caps `ay` to exactly the 7 built-in
tools, permanently. Difficulty: low. The hard part (the protocol client) is
already written and used elsewhere in this codebase; the work is exposing
discovered MCP tools through `ReplToolset.specs()`/`dispatch()` the same way
`execution/tools.py` already does for the batch path, plus deciding how MCP
tool risk levels map onto the REPL's approval `Mode` enum. This is a
measured-valuable capability (every reference tool researched has it except
Aider) with a working local implementation to reuse, so it is close to free
relative to the other gaps.

**4. No subagent/delegation from the REPL.** `harness/run/subagents.py` is a
careful, literature-aligned design — read-only sub-agents, workspace copies,
depth and call caps, report-not-diff deliverables — exactly the shape the
Cognition critique argues for (no concurrent writers). But it is entirely a
batch-path (`harness run`) feature; there is no `delegate` tool in
`ReplToolset._handlers`, so an interactive `ay` session cannot spin off a
bounded sub-task at all. Why it matters less than gaps 1-3: subagents are the
one capability where the literature is genuinely split (Cognition's negative
case vs. Anthropic's internal 90.2% figure for research-shaped, not
code-editing-shaped, tasks), and the repo's own reasoning for keeping
sub-agents read-only in the batch path applies with equal force to a REPL
version — so building it is not obviously higher-value than getting the
existing correct design exposed. Difficulty: moderate-to-high. It requires
either running a second, nested `Agent` instance inside a running REPL turn
(threading and cancellation get more complex — the existing `Interrupted`/
`_cancel` machinery in `harness/repl/agent.py` would need to compose) or
shelling out to a full `harness run` invocation from inside the REPL and
parsing its result, which is closer to how the batch path already thinks
about it. Worth doing after 1-3, not before.

**5. No hooks anywhere in the codebase.** Zero matches for "hook" across
`harness/`. Claude Code's `PreToolUse`/`PostToolUse` hooks let an operator
enforce project-specific policy (auto-format on write, block a path pattern,
log every command) without forking the harness. Why it matters: this is the
extensibility mechanism that lets a team encode its own house rules without
waiting on this repo's maintainers, and its absence means every such rule
currently has to be a code change to `harness/repl/approvals.py` or
`harness/execution/policy.py` directly. Difficulty: moderate. The gate
(`Gate.check` in `harness/repl/approvals.py`) and the policy engine
(`PolicyEngine.evaluate` in `harness/execution/policy.py`) already have a
single, well-defined decision point each tool call passes through — adding a
shell-out hook call before/after that decision is architecturally clean, but
needs its own scoping decision (what should a hook be allowed to change:
allow/deny only, or arguments too) and a security review, since a hook is
itself an arbitrary command the operator configures. This is a genuine gap
against the literature's own observation that permission-scoping and runtime
policy are common in *deployed* systems even though under-studied
academically — hooks are exactly that deployed pattern, and this repo has
none of it. Not cargo cult; it is table stakes for team adoption specifically
(a single operator running `ay` alone feels this gap far less than a team
standardizing on it would).

**6. Self-verification in the REPL is prompted, not enforced.** The
`verification` dial in `harness/models/prompting.py` adds a system-prompt
instruction asking the model to check its own work; this is precisely the
self-correction-without-external-feedback shape that Huang et al. (ICLR 2024)
and the 2026 "Self-Correction Illusion" paper found weak or counterproductive.
The batch path already has the answer — an independent `Verifier` that reruns
acceptance commands (`harness/run/verifier.py`) — but nothing analogous
exists for the REPL, where there is no task contract or acceptance command to
check against in the first place; a REPL session is open-ended by design.
Why it matters less than gaps 1-4: this is partly inherent to what a
conversational REPL is for (there is no fixed "done" criterion to verify
against, unlike a batch task with an acceptance command), so closing this gap
fully would change what `ay` is, not just add a feature. Difficulty: high if
"real" verification is wanted (would require the REPL to know what "done"
means, which it structurally does not), low if the ask is narrower — e.g. an
optional `/verify <command>` that runs a test command and reports pass/fail,
independent of the model's own narration. The literature supports the
narrower version; it does not clearly support trying to bolt full
batch-style verification onto an open-ended chat loop.

**7. Token/cost accounting is approximate and not dollar-denominated.**
`estimate_tokens` in `harness/repl/conversation.py` explicitly documents
itself as "deliberately rough" (4 chars/token, wrong for code and prose in
opposite directions) and is used only to decide when to compact and to draw
the `/context` meter — it is not a billing-accurate count, and `/cost` never
converts token counts into the `cost_per_1m_input`/`cost_per_1m_output`
figures already present in `configs/ay.yaml` for every route. Why it matters:
lower severity than the above — none of the reference tools researched are
documented as having prominent in-session cost meters either, so this is not
a competitive gap so much as an internally inconsistent one (the cost data
exists in config and is unused at runtime). Difficulty: low. `/cost` already
has `self.total_in`/`self.total_out` and `self.route.cost_per_1m_input` is
one attribute lookup away (`harness/repl/shell.py` `_command`, the `cost`
branch); multiplying and formatting is a small change. This is not
cargo-cult work, it is finishing a feature that is already half-built.

## Summary

The strongest parts of this repo relative to the field are the exact-match
edit tool with a required-unique-match refusal (aligned with what the diff-
format literature says actually fails), the batch path's independent
verifier and read-only, workspace-copy, depth-capped sub-agent design (both
directly consistent with the measured self-correction and multi-agent
literature), and the per-route prompt-profile system, which has no documented
equivalent in any reference tool researched. The weakest parts are
concentrated entirely in the interactive REPL, which currently ships without
sandboxing, MCP, subagents, hooks, or edit-level undo — every one of which
either already exists as working code on the batch side of this same
repository, or (hooks) is a self-contained addition to a decision point that
already exists. The gap is real but shallower than it looks: most of the
missing REPL capability is infrastructure this codebase has already built
once for `harness run` and simply never connected to `ay`.
