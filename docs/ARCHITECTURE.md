# Architecture

This document describes the runtime architecture of the harness, mapping
each piece of the workshop diagram to a concrete module.

## The authority boundary

The central rule of the harness is an authority boundary:

> The model proposes the next action. The harness decides what may run,
> executes it, records it, recovers it, and proves whether the task is
> complete.

Every side effect must pass through:

```
Task Contract -> Context -> Model Router -> Policy -> Tool Registry
     -> Workspace -> Event Log -> Checkpoint -> Verifier
```

No module bypasses this chain. The model only ever receives a bounded context
and returns a normalized `ActionProposal` (a tool call, a finish claim, or a
clarification request). It has no direct filesystem, shell, or network access.

## Package layout

The `harness/` package is grouped by responsibility. The four modules at the
root are the ones an operator meets first; everything else lives in a themed
subpackage, and the grouping mirrors the authority chain above.

| Location | Holds |
|---|---|
| `harness/` (root) | `cli.py` (operator commands), `config.py` (versioned loading), `doctor.py` (preflight), `runtime.py` (the agent loop) |
| `harness/core/` | The shared vocabulary: `contracts`, `errors`, `schema`, `util` |
| `harness/models/` | The model side: `auth`, `providers`, `streaming`, `llm_light`, `model_router` |
| `harness/execution/` | The tool side: `tools`, `policy`, `mcp`, `process`, `sandbox`, `search`, `retrieval` |
| `harness/run/` | One run's anatomy: `workspace`, `session`, `context`, `compaction`, `instructions`, `verifier`, `faults`, `subagents` |
| `harness/record/` | The durable record: `state`, `events`, `artifacts`, `tracing`, `redaction`, `replay` |
| `harness/autonomy/` | Multi-run modes: `goal`, `backlog`, `loop`, `evals`, `rubric`, `delivery` |
| `harness/mcp_demo/` | The demo MCP server the teaching config starts |

## Module map

| Module | Responsibility | Diagram node |
|---|---|---|
| `cli.py` | Operator interface: `doctor`, `explain`, `tools`, `routes`, `run`, `resume`, `inspect`, `replay`, `list-runs` | Task intake |
| `config.py` | Strict, versioned YAML loading for config, tasks, and skills | Task contract |
| `core/contracts.py` | The normalized data contracts: `TaskContract`, `ActionProposal`, `ToolResult`, `HarnessEvent`, `RunState` | Contracts |
| `run/context.py` | Bounded prompt construction, repo map, observation compaction | Context engine |
| `models/providers.py` | Provider adapters: replay, OpenAI-compatible (also Ollama/vLLM), Anthropic | Model call |
| `models/llm_light.py` | Priority-based route ordering: cost, latency, privacy, quality, context | Model router (policy) |
| `models/model_router.py` | Reliability: retries, backoff, circuit breaking, fallback, plan resolution | Model router (mechanism) |
| `execution/tools.py` | Typed tool registry, native tools, JSON-schema validation | Tool registry |
| `execution/mcp.py` | MCP stdio client and lifecycle for external tools | Python/MCP |
| `execution/policy.py` | Capability authorization: risk classes, command allowlist, approvals | Policy gate |
| `run/workspace.py` | Per-run workspace copy, containment, protected paths | Shell/Git, Browser/Files |
| `run/verifier.py` | Acceptance commands + diff + protected-path integrity | Verifier |
| `record/state.py` | Atomic checkpoints | State + checkpoint |
| `record/events.py` | Append-only, sequence-checked JSONL ledger | Trace |
| `run/faults.py` | Deterministic fault injection | Reliability |
| `record/artifacts.py` | Evidence bundle: `summary.md`, `patch.diff`, verification records | Evidence |
| `doctor.py` | Preflight diagnostics | Readiness |
| `record/replay.py` | Side-effect-free event ledger reconstruction | Replay |

## Control flow

1. **Task intake.** `harness run task.yaml --config config.yaml --skill skill.yaml`
   freezes the config, task, and skill into the run directory as
   `inputs/*.yaml`. The run id is derived from the task id and a timestamp.
2. **Workspace.** The seed repository is copied into
   `.runs/<run-id>/workspace` and initialized as a git repository with a
   baseline commit. All tool access is confined to this copy.
3. **Context.** The `ContextEngine` builds a system prompt from the skill, a
   repo map, recent observations, and budget state. Older observations are
   compacted to summaries with artifact references.
4. **Route plan.** The `ModelRouter` resolves the ordered route plan once per
   run (see [LLM Light](LLM-LIGHT.md)). The plan is frozen so every turn of a
   run uses the same ordering.
5. **Model call.** The provider adapter for the current route normalizes the
   model response into an `ActionProposal`.
6. **Tool dispatch.** The proposal is validated against the tool's JSON
   schema, checked against policy, and executed. The result becomes an
   observation in the next context.
7. **Checkpoint.** State is durably written after every model response and
   tool result.
8. **Verification.** A `finish` proposal triggers the verifier. Passing
   produces `COMPLETED`; failing becomes a new observation and the loop
   continues (retry loop) until attempts or budgets are exhausted.

## The agent loop

```
observe (context) -> propose (model) -> validate (policy) -> act (tool)
        -> observe (result) -> verify on finish -> repair on failure
```

The loop is generic: it contains no model-specific, tool-specific, or
task-specific logic. Everything the model may do is declared in the skill's
`allowed_tools` and the config's policy.

## Reliability model

- **Retries** re-attempt transient provider failures per route.
- **Circuit breaking** opens a route after a configured failure count.
- **Fallback** moves to the next route in the plan.
- **Checkpoints** allow `resume` after a crash without repeating completed
  mutations.
- **Budgets** bound turns, tool calls, wall time, context size, and output
  size, each with an explicit terminal reason.

## State and events

`state.json` is a single atomic checkpoint updated with `os.replace` and
`fsync` after each boundary. `events.jsonl` is an append-only ledger with
monotonic sequence numbers, correlation ids, and redacted payloads.

## The verifier

The verifier is deliberately independent of the model:

1. Runs every acceptance command in the workspace.
2. Requires a non-empty diff (unless disabled).
3. Ensures no protected path changed.

Only the verifier may produce `COMPLETED`. A `finish` claim that fails
verification re-enters the loop as an observation.
