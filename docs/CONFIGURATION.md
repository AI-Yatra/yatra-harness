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
| `network_enabled` | `false` | allow `browser_fetch` |
| `allowed_domains` | `[]` | host allowlist for `browser_fetch` |
| `command_timeout_seconds` | 30 | per-command cap |
| `browser_timeout_seconds` | 10 | per-fetch cap |

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

### context

| Key | Default | Meaning |
|---|---|---|
| `recent_observations` | 6 | observations kept verbatim |
| `repo_entries` | 120 | repo map entry cap |
| `instruction_files` | `[AGENTS.md, CLAUDE.md]` | repository instruction files read from the workspace root |
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
