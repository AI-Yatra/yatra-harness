# Checkpoints, hooks, and plan mode: research and a plan

Scope: the conversational REPL (`ay`, built in `harness/repl/`). The batch loop
(`harness run`) already has durable state (`harness/record/state.py`,
`harness/run/faults.py`) and a bounded read-only sub-agent
(`harness/run/subagents.py`); neither of those is what this document adds to.
The REPL has none of the three features below. This document researches how
the reference tools built them, then designs and stages equivalents that fit
`harness/repl/agent.py`, `harness/repl/tools.py`,
`harness/execution/tools.py`, and `harness/repl/approvals.py` as they exist
today.

Verified claims cite a URL. Everything else is inferred from reading the code
listed above and is marked as such.

## Recommended ordering

1. **Plan mode** first. It is roughly forty lines across two files, touches
   nothing outside the REPL, and every future turn spent in it is a turn
   that cannot silently `write_file` before the operator has seen a plan.
   Highest value, lowest effort, do it first.
2. **Checkpoints and undo** second. It has real design risk (Part 1 below)
   and touches every mutating tool call, but it is the feature operators
   will actually miss day to day: `edit_file` has no `git commit`-per-edit
   safety net today, and a bad multi-file edit currently means reading
   scrollback to reconstruct what changed.
3. **Hooks** third, and only the observe-only half. A blocking `PreToolUse`
   hook is a second approval system next to `Gate` (`harness/repl/approvals.py`)
   and duplicates work `Gate` already does more legibly. An observe-only hook
   (logging, notifications, metrics) is a thin, low-risk addition to
   `event_callback`. See Part 2 for why the blocking half is the wrong
   next feature for this codebase specifically.

## What I would not build

- **A dedicated "plan" document/artifact type**, the way Claude Code composes
  a distinct plan message and offers "approve with auto-accept edits" /
  "approve and review each edit" as two different post-approval states
  ([docs](https://code.claude.com/docs/en/permission-modes)). Our `Mode` enum
  already has three tiers (`SUGGEST`, `AUTO_EDIT`, `FULL_AUTO`); approving a
  plan can just mean "the operator ran `/mode auto-edit`". Building a second
  approval object that wraps the first buys nothing here.
- **HTTP, MCP-tool, and prompt-type hooks** (Claude Code has four hook
  handler types beyond a plain command,
  [docs](https://code.claude.com/docs/en/hooks)). A command hook covers the
  cases this repo has: run a linter, ping a webhook via `curl` inside that
  command, log to a file. Building a typed HTTP hook client duplicates
  `harness/execution/tools.py`'s existing `browser_fetch`/SSRF-guarded HTTP
  path for a use case (an operator's own webhook) that a shell command
  already reaches.
- **A general blocking hook engine** as a *second* authorization layer next
  to `Gate`. See Part 2's design section for the full argument; the short
  version is that `Gate` already is this repo's `PreToolUse`, and a
  competing mechanism that can also say no is two sources of truth for one
  decision.
- **Skills as a separate mechanism.** Out of scope for this document, but
  worth stating since it is adjacent: the harness already reads `AGENTS.md`
  at session start and skills are, in every reference implementation, a
  packaged instruction file plus an optional tool allowlist. `configs/ay.yaml`
  and the skill contract in `harness/config.py` already do the tool-allowlist
  half for sub-agents (`harness/run/subagents.py`). The marginal value of a
  fourth way to hand the model instructions is low until a concrete need
  appears.

---

## Part 1: checkpoints and undo

### How the references do it

**Aider.** Verified from
[aider.chat/docs/git.html](https://aider.chat/docs/git.html) and a web
search of the same page: aider commits directly to the user's real
repository, not a shadow one. Every successful edit becomes its own commit
with a model-generated message. Before aider's own first edit in a session,
if the tree is already dirty, aider commits the operator's pending changes
first, as a separate commit, so its own commits never mix the operator's
uncommitted work with its own. Aider marks its commits by appending
`(aider)` to the git committer name (configurable), and offers
`--attribute-co-authored-by` as an alternative to changing the author field.
`/undo` reverts the most recent aider commit. The design bet is: git is
already the undo mechanism every operator has, so use it directly and rely
on commit boundaries and authorship metadata to keep aider's history
distinguishable from the operator's, rather than inventing a second store.
This only works because aider assumes a real, already-initialized git repo.

**Claude Code.** Verified from
[code.claude.com/docs/en/checkpointing](https://code.claude.com/docs/en/checkpointing):
checkpoints are per-*prompt*, not per-tool-call: "checkpointing automatically
captures the state of your code before each user prompt." Only edits made
through Claude's own file-editing tools are tracked; files changed by a
`Bash` command (`rm`, `mv`, `cp`) are explicitly **not** trackable, and the
docs say so as a named limitation. Subagent edits are excluded except for
one narrow case (a foreground-forked skill). Checkpoints live alongside the
session transcript, so `/rewind` can restore code, conversation, or both,
independently, and a `/rewind` menu lets you jump to any earlier prompt.
Retention is capped at the 100 most recent checkpoints per session, and
checkpoints (with sessions) are swept after 30 days by default
(`cleanupPeriodDays`). Symlinked and hard-linked paths are skipped on
restore, with a warning naming which files were skipped. The docs are
explicit that this is not a version-control replacement: "For permanent
version history and collaboration, continue using version control."
Concurrent edits (operator hand-edits a file between two agent turns) are
handled by scope, not by conflict detection: "Checkpointing only tracks
files that have been edited within the current session. Manual changes you
make to files outside of Claude Code... are normally not captured, unless
they happen to modify the same files as the current session" — the doc does
not describe a diff/warn step; restoring appears to just overwrite tracked
files with the snapshot's content.

**Cline / Cursor.** Verified from a web search summarizing
[docs.cline.bot/core-workflows/checkpoints](https://docs.cline.bot/core-workflows/checkpoints)
and Cline's own repo docs: Cline maintains a **shadow git repository**,
separate from the project's real `.git`, and commits the full working tree
to it after every tool use (finer grain than Claude Code's per-prompt
model). Because it is a plain `git commit` in a repo that ignores nothing,
it captures files the project's own `.gitignore` would exclude, and files
that were never tracked at all. The user's real git history is never
touched. This is the same "reuse git's object store" bet as Aider, but
pointed at a repo the operator never sees, which sidesteps Aider's problem
of polluting real history and sidesteps Claude Code's problem of needing a
bespoke snapshot format, at the cost of a second `.git` directory on disk
per project.

### The hard questions, answered for this repo

**What to snapshot.** A shadow git repository, Cline-style, not a
content-addressed blob store we write ourselves and not commits to the
operator's real repo (Aider-style). Reasoning:

- Git's object store is already a deduplicating, content-addressed blob
  store with a mature CLI (`git show`, `git diff`, `git gc`). Building our
  own duplicates it for no gain.
- Aider's approach requires a real, already-initialized repo and accepts
  mixing into the operator's actual history (mitigated by author-name
  tagging, not prevented). Our REPL runs in "the operator's real working
  directory" (`harness/repl/shell.py`, `Workspace(self.root, ())`), which is
  not always a git repo (see next point), so this cannot be the only path.
- A shadow repo answers "what about gitignored files" for free (git in the
  shadow repo can be told to ignore nothing) and answers "what about a
  directory with no git repo" for free (a git repo can be initialized
  anywhere; it does not have to be the operator's repo).

**Repository that is not a git repo at all.** Initialize the shadow repo
regardless. `git --git-dir=<shadow>/.git --work-tree=<root>` does not
require `<root>` to have its own `.git`; a bare `git init --separate-git-dir`
into `.ay/checkpoints/<session_id>/` (next to the existing
`sessions_dir: Path = field(default_factory=lambda: Path(".ay"))` in
`harness/repl/shell.py`'s `Options`) works whether or not `self.root` is
itself a repo. This also sidesteps a real risk: pointing the shadow repo's
work-tree at a directory that already has a `.git` and running `git add -A`
inside the *shadow* repo's index never touches the real repo's index or
`HEAD`, because they are entirely separate git directories that happen to
share a work-tree.

**Files ignored by git.** The shadow repo should have its own empty
ignore list (or `git add -A --force` style behavior) so it captures
everything, matching Cline's behavior. This is a deliberate tradeoff to
flag, not hide: `.env` and other operator secrets that are gitignored in
the real repo would be captured verbatim in the shadow repo's objects,
which live under `.ay/` and are not encrypted. Mitigation: reuse the
harness's own denylist idiom, `SKIP_DIRS` and a small path-glob denylist
(`.env`, `*.pem`, `id_rsa*`) applied before `git add -A`, documented as
"checkpoints do not protect secrets any better than the rest of `.ay/`."

**Bounding disk growth.** Cap retained checkpoints per session (Claude
Code's number, 100, is a reasonable starting point) and run `git gc
--auto` in the shadow repo periodically, the same way Claude Code prunes:
"Discarding an older checkpoint deletes the snapshot files that no
remaining checkpoint references." Practically: keep a ring of the last N
commit hashes in the session's saved JSON (`Conversation.save`, see
`harness/repl/conversation.py`), and on eviction just let the objects
become unreachable and let `git gc` reclaim them — no separate
enumeration of blobs needed, because git already does reachability-based
GC.

**Restore semantics when the operator has hand-edited files between
turns.** Neither Aider nor Claude Code does true three-way merge; Claude
Code's docs describe scope-based avoidance ("only tracks files edited in
session") rather than conflict detection, and explicitly disclaim being a
merge tool. The tractable rule for this repo: before restoring, diff the
current working tree against the checkpoint the agent left behind at the
end of its last recorded write (not the checkpoint being restored to, the
*most recent* one). If they match, the restore is safe and silent. If they
differ, the operator hand-edited something after the agent's last write;
show which paths differ and ask for confirmation before overwriting them,
the same "ask, do not silently clobber" posture `Gate.check` already uses
for every other side effect in this REPL. This is a strictly better
guarantee than either reference tool gives, and it costs one extra `git
diff` before a restore.

### Design for this repo

New module `harness/repl/checkpoints.py`:

- `CheckpointStore(root: Path, shadow_dir: Path)` wraps `git
  --git-dir=<shadow_dir> --work-tree=<root>` via the existing
  `harness/execution/process.py:run_process` helper (already used this way
  by `_apply_patch`'s snapshot/restore in `harness/execution/tools.py`, so
  the calling convention is established in this codebase already).
- `snapshot(label: str) -> str` — `git add -A -- . ':!<denylist globs>'` then
  `git commit --allow-empty-message -m <label>`, returns the commit hash.
  Empty-message commits are fine; the label lives in a git note or in the
  session JSON, not the commit subject, so nothing here depends on a model
  writing a good commit message the way Aider's flow does.
- `restore(commit: str, paths: Sequence[str] | None) -> RestoreResult` —
  `git checkout <commit> -- <paths or .>` against the shadow work-tree,
  with the pre-restore diff-and-confirm behavior above.
- `diverged_paths(commit: str) -> tuple[str, ...]` — the confirmation check.

Wiring into the turn loop (`harness/repl/agent.py`):

- `Agent._run_tool` is the one place every tool call, batch or REPL, is not
  routed through — it is specifically the REPL's call point
  (`self.toolset.dispatch(call.name, call.arguments)` at line 211). Take a
  snapshot **after** `dispatch` returns, for any `spec.risk in
  {RiskLevel.WRITE, RiskLevel.EXECUTE}`, labeled with the tool name and
  call id. After, not before: this is what lets a `run_command` that
  happens to `rm` a file be captured too, which is Claude Code's named
  limitation ("Bash command changes not tracked") and does not need to be
  ours, since the shadow repo does not care which tool touched the tree.
- One checkpoint per user prompt is *also* worth keeping (label
  `"prompt:<n>"`, taken in `Agent.send` before `self.conversation.add_user`),
  so `/rewind`-style "back to before this prompt" stays possible even if
  the operator wants coarser granularity than every tool call.
- Tie checkpoint commit hashes to `Conversation` message indices so a
  restore can offer "code only", "conversation only", or "both", matching
  Claude Code's three restore options — the conversation half is nearly
  free since `Conversation` already supports truncation for `/compact`
  (`harness/repl/agent.py:compact`).

New slash commands in `harness/repl/shell.py:_command`: `/checkpoints` (list,
newest first, with label and short hash) and `/undo [n]` (restore the nth
most recent checkpoint, default 1 — "undo my last change"). No new
`/rewind`-style picker UI is proposed for the first cut; a numbered list
plus `/undo <n>` is enough value for the implementation cost, and a fuller
picker can follow if operators ask for it.

Config: new `checkpoints:` block in `configs/ay.yaml` (`enabled: true`,
`retain: 100`, `denylist: [".env", "*.pem", "id_rsa*"]`), read into a new
`CheckpointConfig` dataclass in `harness/config.py` next to the existing
`PolicyConfig`/`BudgetSpec` pattern.

Risks: git subprocess overhead on every write in a large repo (mitigate:
`git add -A` on a shadow index is still `O(changed files)` after the first
commit, since git diffs against its own index, not the whole tree, so this
should stay cheap in practice — worth a benchmark on a large repo before
shipping); the denylist under-covering a secret (documented limitation
above, not solvable generically); Windows path handling for `--git-dir`
outside the work-tree (git supports it, but `run_process`'s argv-only,
no-shell contract in this codebase, per `harness/execution/tools.py`'s
`_run_command`, means this is a plain argv list, not a shell string, so no
new quoting risk).

Testing: a new `tests/test_repl_checkpoints.py` exercising `snapshot`/
`restore`/`diverged_paths` directly against a `tmp_path`, both when `root`
already has its own `.git` (must prove the two repos stay independent —
`git -C root status` before and after a shadow commit must be identical)
and when it does not; an `Agent`-level test (alongside the existing
`tests/test_repl_agent.py`) asserting a checkpoint is taken after a
`write_file` tool call and that `/undo` restores the prior content.

---

## Part 2: hooks

### How the references do it

**Claude Code.** Verified from
[code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks):
hooks fire on a wide event set — once per session (`SessionStart`,
`SessionEnd`), once per turn (`UserPromptSubmit`, `Stop`, `StopFailure`),
and once per tool call (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`PermissionRequest`, `PermissionDenied`), plus more specialized ones.
A command hook receives a JSON payload on stdin: session id, transcript
path, cwd, the active `permission_mode`, the event name, and for tool
events the tool name, its input, and a `tool_use_id`. Only some events can
block: `PreToolUse` can prevent the tool from running,
`UserPromptSubmit` can reject the prompt, `Stop` can prevent the turn from
ending; `PostToolUse` and `PostToolUseFailure` fire only after the fact and
cannot undo anything. Blocking is via exit code: `0` = proceed (JSON output,
if any, is still honored); `2` = block, with the reason taken from stderr or
a JSON `decision` field; any other nonzero code is a non-blocking error —
the action proceeds and the failure is just surfaced. Hooks are configured
in `settings.json` at several scopes (`~/.claude/settings.json`,
`.claude/settings.json`, `.claude/settings.local.json`, managed/org
policy, plugin manifests, and skill/subagent frontmatter), each entry
naming a `matcher` (tool-name regex) and a list of hook definitions —
`command`, `http`, `mcp_tool`, `prompt`, or `agent` type. Security posture:
hooks in project settings only run after the workspace is explicitly
trusted; organizations can force `allowManagedHooksOnly`; HTTP hooks are
constrained to an allowlist of URLs and only allowlisted environment
variables are interpolated into headers; the docs state plainly that "exit
code 2 is the only reliable way to enforce policies."

**Codex CLI.** From a web search (I did not fetch OpenAI's own hooks doc
directly; treat this paragraph as less firmly verified than the Claude Code
section above): Codex configures hooks in `hooks.json` and, as of the
version referenced by the search results (0.150.1), registers twelve
lifecycle events including a `PreToolUse`/`PostToolUse` pair modeled
directly on Claude Code's, covering `Bash`/`exec_command`, `apply_patch`
edits, and MCP tool calls. Codex requires reviewing and trusting a hook's
exact definition before it is allowed to run at all (a stronger
first-run gate than Claude Code's per-workspace trust). One documented gap:
the `notify` hook fires only on turn completion, not on an approval
request, which several users have filed as a feature request — worth
noting because it is exactly the kind of asymmetry ("some events observe,
some can gate, and it is not always obvious which") that makes a hook
system easy to design wrong.
Sources:
[github.com/openai/codex/issues/11808](https://github.com/openai/codex/issues/11808),
[agenticcontrolplane.com/blog/codex-cli-hooks-reference](https://agenticcontrolplane.com/blog/codex-cli-hooks-reference).

### Is `ToolRegistry`'s `event_callback` the right seam?

Partly. Read `harness/execution/tools.py:ToolRegistry.execute` (lines
80–133): `event_callback` is invoked exactly once per call, with
`"POLICY_DECISION"` and a payload that includes `allowed` — but the
callback's return value is discarded (`self.event_callback(...)` on line
90 is a bare statement) and the decision was already computed by
`self.policy.evaluate(...)` on the line before. So today's
`event_callback` is Claude Code's `PostToolUse` shape (observe, cannot
change the outcome), not `PreToolUse`'s (can veto). It fires after policy
evaluation but before the handler runs, and it never fires with the
handler's result, so it cannot do `PostToolUse` logging of output either —
it is narrower than either.

**Answer: it is the right seam for an observe-only hook, and the wrong
place to bolt on a blocking one.** For observe-only: rename nothing, just
add a second call, `self.event_callback("TOOL_RESULT", {...ok, duration_ms,
truncated...})`, right after the handler returns (both success and
exception paths in `execute`), and let an operator register a callback via
the existing `ReplToolset(..., event_callback=...)` constructor argument
(`harness/repl/tools.py` line 175) or `build_registry(...,
event_callback=...)` (`harness/execution/tools.py` line 203) — both already
plumb it through from `Shell.__init__` / the batch builder, so a REPL-level
hook needs zero new wiring to *receive* events, only a place to configure
what runs on them.

For blocking: **do not build a second authorization path.** `Gate` in
`harness/repl/approvals.py` already is this repo's `PreToolUse` — it
inspects `(ToolSpec, arguments)`, can refuse absolutely (`_hard_refusal`,
the deny-list), can ask the operator, and can remember a standing
approval. A parallel hook engine that can also emit `deny` creates the
question "if `Gate` says yes and a hook says no, or vice versa, which
wins, and does the model get a consistent story either way" — that is
exactly the ambiguity Codex's `notify`-vs-approval-request gap and Claude
Code's five-way blocking/non-blocking exit code table exist to manage, and
both of those are mature products with dedicated docs pages for the
distinction. This repo does not need to reproduce that complexity to get
the actual value operators want from hooks, which is closer to "run
`pytest` after every edit and tell me if it broke" than "let a shell
script override my approval policy."

If a blocking hook is wanted later regardless, the extension point is
`PolicyEngine.evaluate` in `harness/execution/policy.py`
(not read in full for this document, but it is what `ToolRegistry.execute`
calls to get `decision` before `event_callback` fires) or a new optional
`pre_execute: Callable[[str, dict], str | None]` parameter on
`ToolRegistry.execute` that runs after `decision.allowed` is confirmed
true and can still return a deny reason — deliberately *after* `Gate`, so
a hook can only add a "no" on top of an approved action, never override a
"no" from the harness's own policy into a "yes". That asymmetry (hooks can
tighten, never loosen) is the one design choice from this section worth
carrying into any future implementation even if the rest is deferred.

### Design for this repo (observe-only, staged now)

- New `HookConfig` in `harness/config.py`: `events: list[str]` (initially
  just `"post_tool_use"`), `command: list[str]` (argv, no shell — matching
  every other command surface in this codebase, see `_repair` in
  `harness/repl/tools.py` refusing shell syntax for `run_command`),
  `matcher: str` (tool-name regex, default match-all), `timeout: float`.
- New `harness/repl/hooks.py`: `run_hooks(hooks: Sequence[HookConfig],
  event: str, payload: dict) -> None`, called from the `event_callback`
  passed into `ReplToolset` in `Shell.__init__`. Each hook runs via
  `run_process` with the JSON payload on stdin, matching Claude Code's
  input contract loosely (`tool_name`, `tool_input`, `ok`, `duration_ms`)
  since operators moving from Claude Code will recognize the shape.
  Failures (nonzero exit, timeout) are logged as a `self.render.notice(...)`
  and otherwise swallowed — an observe-only hook must never be able to
  break a turn, which is the whole reason it is not on the blocking path.
- Config surfaces in `configs/ay.yaml` under a new `hooks:` key, loaded the
  same way `subagents:` is (`subagent_config_from_dict` in
  `harness/run/subagents.py` is the pattern to copy: `schema.mapping`,
  `schema.reject_unknown`, explicit fields, fail at load rather than at
  first use).
- Explicit opt-in and a startup notice: hooks are silent code execution
  from a config file, so on session start (`Shell._banner` in
  `harness/repl/shell.py`), if any hooks are configured, print their
  commands once, the same way `self._startup_notices` already surfaces
  "optional tools unavailable" — visibility, not a trust prompt, since this
  harness has no ambient project-level config auto-discovery to defend
  against (the operator already named `--config` explicitly).

Testing: `tests/test_repl_hooks.py` — a fake hook script (a small Python
file `run_process` can invoke) asserting it receives the right payload
shape and that a hook exception/timeout does not propagate into the turn.

---

## Part 3: plan mode

### How Claude Code does it

Verified from
[code.claude.com/docs/en/permission-modes](https://code.claude.com/docs/en/permission-modes).
Plan mode ("Claude reads files, runs shell commands to explore, and writes
a plan, but does not edit your source") is one of several permission
modes cycled with `Shift+Tab` or entered via `--permission-mode plan` /
prefixing a prompt with `/plan`. Reads and read-only commands run freely;
in the base product (without the newer "auto mode" classifier layer, which
is Claude-specific infrastructure well beyond this repo's scope) any
command outside a fixed read-only allowlist prompts for approval same as
in Manual mode. Edits are blocked outright, not merely asked about, until
the plan is approved. When Claude presents a plan, the operator picks one
of: approve and auto-accept edits from here, approve and review edits
individually, or reject and keep planning. Approving exits plan mode and
switches to whichever follow-up mode was chosen. The mechanism is entirely
mode-scoped: there is no separate "plan" object with its own approval
state machine, the plan is just the assistant's message, and "approval" is
a mode transition triggered by a specific reply to a specific prompt UI.

### Design for `harness/repl/approvals.py`

Add a fourth member to `Mode`:

```python
class Mode(StrEnum):
    SUGGEST = "suggest"
    AUTO_EDIT = "auto-edit"
    FULL_AUTO = "full-auto"
    PLAN = "plan"

    @property
    def label(self) -> str:
        return {
            Mode.SUGGEST: "asks before edits and commands",
            Mode.AUTO_EDIT: "edits freely, asks before commands",
            Mode.FULL_AUTO: "does not ask",
            Mode.PLAN: "read-only: explores and proposes, cannot edit or run commands",
        }[self]
```

`Gate._must_ask` currently has two branches (`FULL_AUTO` never asks,
`AUTO_EDIT` asks only for `EXECUTE`/`NETWORK`, everything else asks for
`WRITE`/`EXECUTE`/`NETWORK`). Plan mode is not a third point on that
"how much do we ask" spectrum — it is a hard stop, the same shape as the
existing `_hard_refusal` deny-list check, not the same shape as `_must_ask`.
Add it there instead, ahead of the existing refusal check in `Gate.check`:

```python
def check(self, tool: ToolSpec, arguments: dict[str, Any]) -> Decision:
    refusal = self._hard_refusal(tool, arguments)
    if refusal is not None:
        return Decision(False, refusal)

    if self.mode is Mode.PLAN and tool.risk not in {RiskLevel.READ, RiskLevel.CONTROL}:
        return Decision(
            False,
            "plan mode: read-only until the operator approves a plan. "
            "Describe what you would do instead of doing it, and finish "
            "your turn so the operator can review and approve.",
        )
    ...
```

`RiskLevel.CONTROL` (the `delegate` and `finish` tools,
`harness/execution/tools.py` lines 173–194 and 416–424) stays allowed in
plan mode: delegation is already read-only by construction
(`FORBIDDEN_TOOLS` in `harness/run/subagents.py`), and refusing `finish`
would make it impossible for the model to end a planning turn cleanly.

`prompt.py` needs no change — `_environment` already interpolates
`mode.label` into the system prompt (`harness/repl/prompt.py:59`), so
`Mode.PLAN`'s label is enough for the model to be told what plan mode
means without a new prompt branch.

`Shell._switch_mode` in `harness/repl/shell.py` needs no change either;
`Mode(argument.strip().lower())` already accepts any valid enum value, so
`/mode plan` works the moment the enum has the member. "Approving a plan"
is then just the operator typing `/mode auto-edit` or `/mode full-auto`
after reading the assistant's proposal — no new UI, no new data structure,
consistent with the "what I would not build" note above. The one addition
worth making is a one-line nudge in `HELP` (`harness/repl/shell.py`):

```
  /mode [name]       approval mode: suggest, auto-edit, full-auto, plan
```

(replace the existing line, which currently omits `plan`), plus updating
`Options.mode: Mode = Mode.SUGGEST`'s docstring-adjacent comment nowhere
needed since it is just a default.

Risks: a model in plan mode that tries a write tool anyway gets a refusal
message and, per the existing loop in `Agent._run_tool`, that counts
against `Limits.max_consecutive_errors` (`harness/repl/agent.py`) the same
as any other denial — worth confirming in testing that a model reliably
stops proposing edits after 1-2 refusals rather than burning the error
budget, since a model that has not internalized "plan mode" from the
system prompt text alone will hit this. If that turns out to be a real
failure mode, the fix is a clearer refusal string, not a code change.

Testing: extend `tests/test_repl_agent.py` (or add
`tests/test_repl_plan_mode.py`) with a fake model that emits a `write_file`
call while `Mode.PLAN` is active, asserting the call is denied and the
denial reason is surfaced via `on_tool_denied` rather than silently
dropped; a `Gate`-level unit test asserting `RiskLevel.READ` and
`RiskLevel.CONTROL` pass in `PLAN` while `WRITE`/`EXECUTE`/`NETWORK` do
not, regardless of `_must_ask`'s own logic.

---

## Part 4: staged implementation plan

### Stage 0 — plan mode (do first)

Files: `harness/repl/approvals.py` (`Mode` enum, `Gate.check`),
`harness/repl/shell.py` (`HELP` text only). No config keys. No new
dependencies.

Test: unit tests on `Gate.check` per risk level in `PLAN` mode; one
`Agent`-level test with a fake model attempting a write in plan mode.

Risk: low. The only behavioral risk is model confusion (noted above), which
is observable and fixable by editing prompt text, not architecture.

### Stage 1 — checkpoints and undo

Files: new `harness/repl/checkpoints.py`; `harness/repl/agent.py`
(`Agent._run_tool`, `Agent.send`) to trigger snapshots; `harness/repl/shell.py`
(`_command` for `/checkpoints` and `/undo`, `HELP` text);
`harness/config.py` (new `CheckpointConfig`); `configs/ay.yaml` (new
`checkpoints:` block).

New config keys: `checkpoints.enabled` (bool, default true),
`checkpoints.retain` (int, default 100), `checkpoints.denylist` (list of
globs, default `[".env", "*.pem", "id_rsa*", "*.key"]`).

Risks: subprocess overhead per mutating tool call (benchmark before
shipping on a repo the size of this one); secrets captured by the shadow
repo despite the denylist (document, do not attempt to fully solve);
Windows `git --git-dir` path quoting (mitigated by argv-only `run_process`,
same as everywhere else in this codebase).

Test: `tests/test_repl_checkpoints.py` for the store in isolation
(including the "root already has its own `.git`" case, asserting zero
interference); an `Agent`-level test asserting a snapshot follows a
`write_file` call and `/undo` restores prior content; a divergence test
where the operator's own edit between two agent turns is detected and
confirmed rather than silently overwritten.

### Stage 2 — hooks (observe-only)

Files: new `harness/repl/hooks.py`; `harness/repl/shell.py` (wiring
`event_callback` into `ReplToolset(...)` construction, startup notice in
`_banner`); `harness/config.py` (new `HookConfig`); `configs/ay.yaml`
(new `hooks:` block).

New config keys: `hooks.enabled` (bool, default false — opt-in, since this
is arbitrary command execution from a config file), `hooks.post_tool_use`
(list of `{matcher, command, timeout}`).

Risks: a hook that hangs (mitigated by `timeout`, same pattern as
`command_timeout_seconds` elsewhere in `PolicyConfig`); a hook that writes
to stdout/stderr the operator did not expect to see mid-session
(mitigated by routing hook output through `self.render.notice`, capped,
rather than passing it through raw).

Test: `tests/test_repl_hooks.py` with a fixture command hook (small Python
script) asserting payload shape and that hook failure never raises out of
`Agent._run_tool`.

### Explicitly deferred

Blocking hooks (`PreToolUse`-equivalent) are deferred indefinitely as
designed in Part 2, pending a concrete operator request that `Gate` cannot
already serve — the asymmetric "hooks can only add a no" extension point on
`ToolRegistry.execute` is documented above if that day comes, but nothing
should be built against it speculatively.
