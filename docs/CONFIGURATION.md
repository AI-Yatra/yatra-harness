# Configuration

All harness inputs are strict, versioned YAML. Unknown keys are rejected, so a
typo fails loudly instead of being silently ignored.

## Config file

```yaml
version: 1                       # required, must be 1
runs_dir: ../.runs               # where run bundles live (relative to config)
```

### budgets

| Key | Default | Meaning |
|---|---|---|
| `max_turns` | 12 | model decision turns |
| `max_tool_calls` | 24 | executed tool calls |
| `max_seconds` | 300 | active wall time |
| `max_context_chars` | 24000 | context budget |
| `max_output_chars` | 12000 | per-tool output cap |
| `max_verification_attempts` | 3 | finish attempts before failure |

### model_router

```yaml
model_router:
  primary: teaching              # first route tried
  fallbacks: []                  # ordered routes tried after the primary
  retries_per_route: 1           # transient retries per route
  backoff_seconds: 0.05          # base exponential backoff
  circuit_breaker_failures: 2    # failures before a route is opened
  routes:
    teaching:
      kind: replay               # see providers
      model: deterministic-repair-demo
      script: ../scenarios/repair_demo.yaml
      timeout_seconds: 5
      local: true                # LLM Light decision attribute
      latency: low               # LLM Light decision attribute
      quality: 0.0               # 0-5
      context_window: 32768      # tokens
      cost_per_1m_input: 0.0     # USD
      cost_per_1m_output: 0.0    # USD
      tool_support: true
```

Transport keys: `kind`, `model`, `base_url`, `api_key_env`, `script`,
`timeout_seconds`. Routing keys: `local`, `latency`, `quality`,
`context_window`, `cost_per_1m_input`, `cost_per_1m_output`, `tool_support`.

### llm_light

See [LLM-LIGHT.md](LLM-LIGHT.md). When the section is absent, the configured
`primary`/`fallbacks` are used verbatim, which behaves identically to a harness
without LLM Light.

### policy

| Key | Default | Meaning |
|---|---|---|
| `approval_mode` | `mutations` | `never`, `mutations`, or `always` |
| `allowed_commands` | `[]` | exact command prefixes for `run_command` |
| `denied_commands` | `[]` | patterns refused wherever they appear |
| `network_enabled` | `false` | allow `browser_fetch` |
| `allowed_domains` | `[]` | host allowlist for `browser_fetch` |
| `command_timeout_seconds` | 30 | per-command cap |
| `browser_timeout_seconds` | 10 | per-fetch cap |

`allowed_commands` matches a **prefix**: it answers "may a command of this
shape run at all". `denied_commands` matches a **contiguous subsequence
anywhere** in the command, and is checked first.

Both are needed, and the asymmetry is the point. The dangerous forms are
reachable as arguments to a command that is legitimately allowed -- `python`
has to be on the allowlist for the tests to run, and `python -c "..."` is
arbitrary code. A prefix-only deny rule is dodged by one inserted flag, and a
rule that is trivially dodged is worse than none, because it reads like a
control and is not one.

A denied command is refused before any approver is consulted. A human
clicking yes on a prompt is exactly the mistake the deny-list exists to
prevent.

```yaml
policy:
  allowed_commands:
    - [python, -m, unittest]
    - [git, diff]
  denied_commands:
    - [git, push]     # delivery is the harness's job, not the model's
    - [pip, install]
    - [curl]
```

### mcp

List of MCP stdio servers:

```yaml
mcp:
  - name: repo-stats
    command: ["{python}", -m, harness.mcp_demo.server]
    protocol_version: "2025-11-25"
    timeout_seconds: 10
    enabled: true
```

### search

Backs the `web_search` tool. The tool is `RiskLevel.NETWORK`, so it needs
`network_enabled: true` **and** the skill has to list it; a search backend on
its own enables nothing.

| Key | Default | Meaning |
|---|---|---|
| `kind` | `duckduckgo` | `brave`, `tavily` or `duckduckgo` |
| `endpoint` | the backend's own | override the URL |
| `api_key_env` | `""` | variable holding the backend's key |
| `max_results` | 5 | results returned to the model |

```yaml
search:
  kind: brave
  api_key_env: BRAVE_API_KEY
policy:
  network_enabled: true
```

`brave` and `tavily` need a key; `duckduckgo` parses the HTML endpoint and
needs none, so search works on a workshop laptop without anyone signing up
for anything. It is also the backend most likely to break without notice,
because it depends on a page layout rather than an API.

The key is carried in a header or a request body, never in the URL: a query
string reaches proxy logs and any redirect target, and redaction cannot
follow it there. It is also registered with the `Redactor` alongside the
route credentials, so it is scrubbed from the ledger like any other.

The backend's own host is allowlisted implicitly. Making an operator list
`api.search.brave.com` in `allowed_domains` after configuring it as their
search backend would be a trap rather than a control.

### context

| Key | Default | Meaning |
|---|---|---|
| `recent_observations` | 6 | observations kept verbatim |
| `repo_entries` | 120 | repo map entry cap |
| `instruction_files` | `[AGENTS.md, CLAUDE.md]` | repository instruction files read from the workspace root |
| `compaction.kind` | `truncate` | `truncate` or `summarize` |
| `compaction.max_chars` | 240 | size of a compacted entry |
| `compaction.prompt_chars` | 8000 | cap on what the summarizer is shown |
| `max_instruction_chars` | 4000 | cap on that text |

`instruction_files` are read from the run workspace, in the order listed, and
appended to the system prompt labelled with the file each section came from.
A missing, empty or unreadable file is skipped rather than fatal, and an empty
list switches the behaviour off.

The text is capped at `max_instruction_chars` **or** a quarter of
`budgets.max_context_chars`, whichever is smaller, so an oversized `AGENTS.md`
degrades to a truncated one instead of starving the task of context.

This text describes conventions. It is appended after the harness's own
instructions, never before them, and it cannot enable a tool, widen
`allowed_commands`, or satisfy the verifier -- the authority boundary in
[ARCHITECTURE.md](ARCHITECTURE.md) is unchanged by anything a repository
writes about itself. `CONTEXT_BUILT` records which files were used on every
turn.

## Sessions

`harness run --session <id>` reuses one workspace across runs instead of
building a fresh one, so a conversation accumulates. The workspace lives at
`<runs_dir>/sessions/<id>/workspace`; each run still gets its own bundle,
events and checkpoints under `<runs_dir>/<run-id>/`.

Outstanding changes are committed as `harness session turn` before the next
run starts. Without that, `git diff HEAD` for the second run would still
contain the first run's changes and every later run would look productive
whether or not it did anything.

The session's memory (`session.json`) sits beside the workspace, not inside
it. Written into the workspace it would appear as an untracked file and the
verifier would count the harness's own bookkeeping as the run's diff.

### Compaction

An observation that leaves the recent window is folded down. `truncate` keeps
the first `max_chars` of it -- which tool ran, whether it worked, the first
line of what it returned. `summarize` spends one model call to fold the whole
batch into a digest that keeps the facts instead of the shape.

```yaml
context:
  compaction:
    kind: summarize
```

Summarization degrades to truncation whenever it cannot run: a provider
failure, an empty answer, or a config asking for it with no route able to do
it. Compaction is a context optimisation, and taking a run down because the
summarizer is unwell trades a smaller context for no run at all. A summarized
entry is labelled `tool: compaction`, so a model reading its own context can
tell a recorded observation from a paraphrase of several.

`CONTEXT_COMPACTED` records which strategy ran.

## Retrieval

Backs the `retrieve` tool: ranked excerpts of the workspace for a question,
rather than the literal-string match `search_repo` does.

| Key | Default | Meaning |
|---|---|---|
| `kind` | `lexical` | `lexical` (BM25) or `embedding` |
| `endpoint` | — | required for embedding; an OpenAI-compatible `/embeddings` URL |
| `api_key_env` | `""` | variable holding that provider's key |
| `model` | `text-embedding-3-small` | embedding model |
| `lines_per_chunk` | 40 | chunk size |
| `max_file_bytes` | 200000 | files larger than this are skipped |
| `max_chunks` | 4000 | index cap |
| `limit` | 5 | results per query |

```yaml
retrieval:
  kind: embedding
  endpoint: https://api.openai.com/v1/embeddings
  api_key_env: OPENAI_API_KEY
```

`search_repo` is exact and useless when you do not already know the
identifier: "where is the retry backoff decided" finds nothing, because
nobody wrote that sentence in the code. `retrieve` answers that question.

Lexical is the default because it works with no key and no network, so
retrieval is exercised in CI rather than only where someone has a provider.
The embedding backend **falls back to lexical** when the provider is
unreachable: retrieval going quiet is worse than retrieval being approximate,
because the model does not learn that its question failed — it just gets
nothing and reads the wrong files.

Tokens are identifier-aware: `retry_backoff` and `retryBackoff` are both
reachable from "retry backoff", so the operator does not have to guess the
file's naming convention.

The index is cached per workspace and invalidated by a cheap signature (file
count and newest mtime). An agent patches files as it works, so an index
built on turn two is wrong by turn four.

## Sandbox

Where `run_command`, `python_run` and the verifier's acceptance commands
actually execute.

| Key | Default | Meaning |
|---|---|---|
| `kind` | `local` | `local` or `docker` |
| `image` | — | required for docker |
| `network` | `none` | container network mode |
| `memory` | `2g` | container memory cap |
| `cpus` | `2` | container CPU cap |
| `user` | host uid:gid | who the container runs as |
| `selinux_label` | `auto` | `auto`, `always`, `never` — appends `:Z` to the mount |

```yaml
sandbox:
  kind: docker
  image: yatra-harness-sandbox
```

Build the image with `docker build -t yatra-harness-sandbox .`.

Path containment and the command allowlist confine the model's *interface*.
They do not confine the operating system: an allowlisted test runner is still
an ordinary process on the host, with the host's filesystem and network. The
container is what makes that containment real.

Acceptance commands run in the same sandbox the tools did. A change proved to
work on the host and never tried in the environment it will actually run in
has not been proved to work.

`selinux_label` matters more than it looks. On an SELinux host an unlabelled
bind mount is unreadable from inside the container, and the symptom — a
permission error on a file the operator owns and can plainly see — points at
everything except the cause. `auto` detects it; `never` exists because some
non-Linux docker hosts reject the suffix outright.

Local stays the default: a workshop laptop without docker must still be able
to run the harness, and a teaching tool that refuses to start teaches nothing.

## Sub-agents

Delegation is off unless `subagents.agents` names at least one agent. When it
is on, a `delegate` tool appears in the registry and a skill can enable it.

```yaml
subagents:
  max_depth: 1        # a sub-agent may not delegate further
  max_calls: 3        # per parent run
  max_turns: 6        # each sub-agent's own turn budget
  agents:
    explore: skills/explore.yaml
    review:
      skill: skills/review.yaml
      config: configs/reviewer.yaml   # optional: its own model
```

A sub-agent is **read-only**. Its deliverable is a report, not an edit, and a
report needs no verifier because it changes nothing — so the completion gate
stays exactly where it was: one agent makes changes, one verifier decides
whether they worked. It works from a *copy* of the parent's workspace, so a
reviewer that runs the test suite cannot leave artifacts the parent is then
judged on.

Giving a sub-agent its own `config` is the point rather than a convenience.
The argument the verifier embodies — the author of a piece of work is the
worst judge of it — applies again one level down: a reviewer running the same
model as the writer shares its blind spots.

Every delegation is a full run with its own bundle, ledger and checkpoints,
so a sub-agent that misbehaves is as inspectable as its parent.
`SUBAGENT_STARTED` and `SUBAGENT_FINISHED` appear in the parent's ledger.

The depth cap is the guard that matters: without it a delegating agent can
spawn a delegating agent and the per-run budget stops bounding anything. A
sub-agent also inherits no approver, so a nested run cannot spend an
operator's yes on something they never saw.

### Environment overrides

- `HARNESS_RUNS_DIR` overrides `runs_dir` (used by tests and CI).
- Route API keys come from the env var named in `api_key_env`, or the
  provider's conventional variable (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
  when unset.

## Task file

```yaml
version: 1
id: repair-counter-boundary
objective: >-
  Repair the clamp function ...
workspace_seed: ../fixtures/buggy_counter
constraints:
  - Inspect the repository before modifying it.
  - Do not modify tests.
protected_paths:
  - tests/**
acceptance:
  commands:
    - [python, -m, unittest, discover, -s, tests, -v]
  require_non_empty_diff: true
  timeout_seconds: 30
metadata: {}
```

`workspace_seed` is copied into the run workspace; the source is never
modified. `protected_paths` are glob patterns; any change to them fails
verification.

### Seed mode and repository mode

A task names **exactly one** of `workspace_seed` or `repository`. Naming both
is an error, because a run would have two answers to the question of where it
came from.

| | `workspace_seed` | `repository` |
|---|---|---|
| Workspace is | a copy of the directory | a clone of the repository |
| History | one `harness baseline` commit | the repository's real history |
| Branch | `master`/`main`, fresh | `harness/<run-id>` |
| Remote | none | the source's own `origin` |
| Can end in a pull request | no | yes |

```yaml
repository: ../some-checkout
base_ref: main          # optional; defaults to the repository's HEAD
```

`base_ref` accepts a branch, a tag or a commit sha, and only applies in
repository mode. It is resolved inside the clone, so a branch name is read
through `origin/<name>`.

Repository mode reads the source checkout and never writes to it. Because a
clone starts from a commit, uncommitted work in the source is not carried
into the run. The clone's `origin` is repointed from the local path git would
otherwise use at the source repository's own upstream, so a later push
targets the remote the pull request needs rather than the operator's
checkout.

## Shipped skills

| Skill | For |
|---|---|
| `skills/bugfix.yaml` | repairing a defect the acceptance command detects |
| `skills/repo-edit.yaml` | making a specific requested change to a repository |
| `skills/palimpsest-skill.yaml` | the workshop's artifact-building task |

The distinction between the first two matters more than it looks. `bugfix`
tells the model to find and repair a defect, so given a plain edit request it
goes looking for a bug that is not there and asks for clarification instead
of working. `repo-edit` states that the objective *is* the request.

## Skill file

```yaml
version: 1
id: bounded-bugfix
instructions: >-
  Explore before editing. Prefer the smallest implementation-only patch ...
allowed_tools:
  - repo_tree
  - read_file
  - apply_patch
  - run_command
  - finish
```

`allowed_tools` is the capability gate: the policy engine denies any tool the
skill does not enable.
