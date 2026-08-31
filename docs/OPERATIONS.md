# Operations

Operator runbook for the mini harness.

## Quick start

```bash
uv sync

# Readiness
uv run harness doctor --config configs/teaching.yaml \
  --task tasks/repair_counter.yaml --skill skills/bugfix.yaml

# Deterministic teaching run (offline, no API key)
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml
```

The teaching route is a replay provider: it runs a scripted scenario that
intentionally produces a failing first patch so the verifier-driven retry loop
is visible. Every run is isolated under `.runs/<run-id>/`.

## Commands

| Command | Purpose |
|---|---|
| `harness doctor` | preflight checks: config, python, git, routes, LLM Light plan, MCP |
| `harness explain <task>` | resolve a task without creating a run |
| `harness tools` | list registered tools and their risk classes |
| `harness routes` | show the LLM Light route plan (no network) |
| `harness run <task>` | create and execute a run |
| `harness resume <run-id>` | resume from the last checkpoint |
| `harness inspect <run-id>` | state summary + event timeline |
| `harness replay <run-id>` | verify and summarize the event ledger |
| `harness list-runs` | list all run bundles |

## Model selection

```bash
# Pin a specific route (bypasses LLM Light ordering)
uv run harness run task.yaml --model teaching

# Add a fallback route
uv run harness run task.yaml --model broken --fallback teaching

# LLM Light profile
uv run harness run task.yaml --config configs/llm_light.yaml --profile budget

# Ad-hoc priorities
uv run harness run task.yaml --config configs/llm_light.yaml \
  --priority cost --priority latency
```

## Local model (Ollama)

```bash
ollama pull qwen2.5-coder:3b
uv run harness run tasks/repair_counter.yaml --config configs/local.yaml \
  --skill skills/bugfix.yaml
```

`configs/local.yaml` uses `local-ollama` as primary with the deterministic
teaching route as fallback, so the workshop works even without a model
installed.

## Remote API

Copy `configs/remote.example.yaml`, set the model name, and export the key:

```bash
export HARNESS_REMOTE_API_KEY=...
uv run harness run tasks/repair_counter.yaml \
  --config configs/remote.example.yaml --skill skills/bugfix.yaml
```

Anthropic-native routes use `ANTHROPIC_API_KEY` (or an explicit
`api_key_env`).

## Fault injection (teaching)

```bash
# One transient model timeout -> retry -> continue
uv run harness run task.yaml --fault model-timeout-once

# Crash after the second durable checkpoint -> resume
uv run harness run task.yaml --fault crash-after-tool=2
uv run harness resume <run-id>
```

## Budgets

```bash
uv run harness run task.yaml --max-turns 3 --max-seconds 120
```

Budget exhaustion always produces an explicit terminal status
(`BUDGET_EXHAUSTED`) with a reason, never a hang.

## Run bundle layout

```
.runs/<run-id>/
├── manifest.json          # frozen inputs digest + source paths
├── inputs/                # frozen config/task/skill YAML
├── workspace/             # the isolated working copy (git repo)
├── state.json             # durable checkpoint
├── events.jsonl           # append-only, sequence-checked trace
├── verification/          # one JSON record per verification attempt
├── patch.diff             # final diff vs baseline
├── result.json            # terminal result
└── summary.md             # human-readable report
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `doctor` fails `model:<route>` | local server not running; start Ollama/vLLM or use the teaching config |
| `doctor` fails `llm_light` | routing config unsatisfiable (e.g. all routes excluded); check constraints |
| run exits `FAILED: all model routes failed` | every route exhausted/errored; check the ledger with `inspect` |
| run exits `BUDGET_EXHAUSTED` | raise `--max-turns`/`--max-seconds` or the config budgets |
| `resume` errors on config | run bundle predates a schema change; check `inputs/config.yaml` |
| MCP tool missing | `doctor` shows `mcp:<name>` failing; check the server command |

## Testing

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check harness tests
```

The suite covers the happy path, verifier-driven repair, fault injection,
crash/resume, budgets, routing, providers, policy, and workspace containment.
