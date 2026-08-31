# Workshop guide

This guide maps the 3.5-hour workshop plan to concrete commands. The teaching
path is fully offline and deterministic; the local-model path is the authentic
demonstration of model independence.

## Module 1 — Model vs agent vs harness (15 min)

Run the naive baseline: a model with raw shell access is a chat, not an agent.
Then run the harness and note what is *missing* from the naive version:
contracts, context, policy, state, verification.

```bash
uv run harness doctor --config configs/teaching.yaml \
  --task tasks/repair_counter.yaml --skill skills/bugfix.yaml
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml
```

## Module 2 — Contracts and architecture (20 min)

```bash
uv run harness explain tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml
```

Look at `harness/contracts.py`: `TaskContract`, `ActionProposal`,
`ToolResult`, `HarnessEvent`, `RunResult`. Every boundary is a dataclass.

## Module 3 — Model independence and context (25 min)

```bash
uv run harness tools --config configs/teaching.yaml
uv run harness routes --config configs/llm_light.yaml --profile budget
uv run harness routes --config configs/llm_light.yaml --priority cost --priority latency

# Deterministic replay run (every laptop sees the same trace)
uv run harness run tasks/repair_counter.yaml \
  --config configs/llm_light.yaml --skill skills/bugfix.yaml --profile teaching

# Real local model (if Ollama is running)
uv run harness run tasks/repair_counter.yaml \
  --config configs/local.yaml --skill skills/bugfix.yaml
```

Key idea: the run loop, tools, policy, and verifier are identical in both
cases. Only the route changed.

## Module 4 — Agent loop and typed tools (35 min)

```bash
uv run harness tools --source native
uv run harness tools --source mcp
```

Watch the loop with:

```bash
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml
uv run harness inspect <run-id>
```

Compare how a native tool (`repo_tree`) and an MCP tool (`repo_stats`) pass
through the same schema, policy, event, and observation contracts.

## Module 5 — Workspace, policy, approvals (35 min)

The teaching config uses `approval_mode: never`. Try the mutations config with
a scripted approval:

```bash
uv run harness run tasks/repair_counter.yaml \
  --config configs/local.yaml --skill skills/bugfix.yaml --yes
```

Trigger a policy denial by asking for a tool the skill does not enable, or a
command that is not on the allowlist, and inspect the `POLICY_DECISION` event.

## Module 6 — Verification and self-repair (30 min)

The teaching scenario intentionally fails its first patch. Watch the flow:

```bash
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml
uv run harness inspect <run-id>
```

The ledger shows: `VERIFICATION_FAILED` → `RETRY_LOOP_ENTERED` → repair patch
→ tests pass → `VERIFICATION_PASSED` → `RUN_COMPLETED`.

## Module 7 — Reliability and budgets (25 min)

```bash
# Transient timeout -> retry -> continue
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --fault model-timeout-once

# Primary failure -> fallback
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --model broken --fallback teaching

# Crash -> resume
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --fault crash-after-tool=2
uv run harness resume <run-id> --runs-dir .runs

# Budgets
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --max-turns 3
```

## Module 8 — Traces, replay, capstone (15 min)

```bash
uv run harness list-runs
uv run harness inspect <run-id>
uv run harness replay <run-id>
```

The capstone run produces the sequence from the plan:

```text
task accepted
→ context compiled
→ model proposes inspection
→ read/search tools execute
→ model proposes patch
→ policy confines write to workspace
→ verifier fails
→ failure becomes an observation
→ model repairs patch
→ transient model/tool fault is retried
→ verifier passes
→ harness writes evidence bundle and declares COMPLETE
```

## LLM Light exercise (optional extension)

1. Run `harness routes` with different profiles and predict the order before
   running.
2. Change a route's `quality` in `configs/llm_light.yaml` and re-run `routes`
   — only the config changed, never the harness.
3. Add a constraint (`--require-local`, `--max-cost 0.30`) and watch excluded
   routes appear with reasons.
4. Run the same task through two profiles and diff the `MODEL_ROUTES_RESOLVED`
   events in `harness inspect`.

## Troubleshooting

- `doctor` fails `model:local-ollama`: Ollama is not running; use the
  teaching config or start `ollama serve`.
- `doctor` fails `llm_light`: the routing constraints exclude every route;
  relax a constraint or fix a profile's `allowed` list.
- A run ends `BUDGET_EXHAUSTED`: raise `--max-turns` or the config budgets.
