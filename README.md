# Yatra Harness

A provider-neutral coding-agent harness that implements the workshop flow from
task intake through bounded context, model routing, policy-governed tools,
durable checkpoints, independent verification, repair, and evidence capture.

```
TASK -> Task Contract -> Context Engine -> AGENT LOOP -> Model Call -> Model Router
                                                      -> Tool Call  -> Tool Registry
                                                      -> after each turn -> State + Checkpoint -> Verifier
                                                      -> PASS -> DONE | FAIL -> RETRY LOOP
```

## Requirements

- Python 3.11+ (tested on 3.13 and 3.14)
- [uv](https://docs.astral.sh/uv/) for dependency management
- `git` on PATH
- A model API key in `.env` for live-model configs (Qwen / OpenAI / Anthropic / Ollama / vLLM)

## Quick start

```bash
uv sync
cp .env.example .env   # then put your key in .env
# DASHSCOPE_API_KEY=sk-ws-...

# Preflight: validate environment, configs, and provider reachability
uv run harness doctor \
  --config configs/teaching.yaml \
  --task tasks/repair_counter.yaml \
  --skill skills/bugfix.yaml

# Run a deterministic workshop task (no API key needed)
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml \
  --skill skills/bugfix.yaml

# Run a live model (Qwen Cloud / DashScope)
uv run harness run tasks/palimpsest_task.yaml \
  --config configs/palimpsest-config.yaml \
  --skill skills/palimpsest-skill.yaml --yes
```

The teaching route is deterministic, local, offline, and requires no API key.
It intentionally produces a failing first patch so the verifier-driven retry
loop is visible. Every run is isolated under `.runs/<run-id>/`.

## CLI

```bash
harness doctor      # preflight checks
harness explain     # resolve a task without running
harness tools       # list registered tools and risk classes
harness routes      # show the LLM Light route plan
harness run         # execute a task run
harness resume      # resume from the last checkpoint
harness inspect     # state + event timeline
harness replay      # verify and summarize the event ledger
harness list-runs   # list run bundles
```

## REPL (Claude Code-style)

`ay` (short for AI Yatra) is a thin REPL on top of the same CLI. Each message becomes
a generated `tasks/chat/<id>.yaml` and runs through `harness run` with live
stdout streaming. Slash commands: `/runs`, `/inspect`, `/resume`, `/config`,
`/model`, `/help`, `/exit`. See `ay.README.md` for full usage.

Install once, then `ay` runs from any directory:

```bash
uv tool install --editable . --with openpyxl
```

```bash
ay                                                             # repo defaults
ay --config configs/teaching.yaml --skill skills/bugfix.yaml   # offline route
```

Or run it without installing:

```bash
uv run ay \
  --config configs/palimpsest-config.yaml \
  --skill skills/palimpsest-skill.yaml
```

By default a chat task runs against an empty scratch workspace with an
acceptance command that always passes, so the harness records the run but does
not check it; `/config` says so explicitly. To make a chat run verifiable, give
it a real seed, acceptance command, and protected paths:

```bash
uv run ay \
  --config configs/palimpsest-config.yaml \
  --skill skills/palimpsest-skill.yaml \
  --seed fixtures/palimpsest \
  --accept "python verify_contact_workbook.py" \
  --protect "verify_contact_workbook.py" --protect "contact_cards/**"
```

## LLM Light

LLM Light is a priority-based model router: declare what you care about and
the harness orders its model routes accordingly — no code changes.

```bash
# Preview the route plan (no network, no credentials)
uv run harness routes --config configs/llm_light.yaml

# Named profile
uv run harness run tasks/repair_counter.yaml \
  --config configs/llm_light.yaml --skill skills/bugfix.yaml --profile budget

# Ad-hoc priorities and constraints
uv run harness run tasks/repair_counter.yaml \
  --config configs/llm_light.yaml --skill skills/bugfix.yaml \
  --priority cost --priority latency --require-local
```

Priorities: `privacy` (local first), `quality`, `cost`, `latency`, `context`.
Profiles in `configs/llm_light.yaml`: `offline`, `budget`, `quality`,
`teaching`, `balanced` (weighted), `long-context`.
See [docs/LLM-LIGHT.md](docs/LLM-LIGHT.md).

## Providers

| kind | adapter | wire |
|---|---|---|
| `replay` | deterministic script | none |
| `openai_compatible` | OpenAI-compatible | `/chat/completions` |
| `ollama` | OpenAI-compatible | `/v1/chat/completions` |
| `vllm` | OpenAI-compatible | `/v1/chat/completions` |
| `anthropic` | Anthropic Messages API | `/messages` |

See [docs/PROVIDERS.md](docs/PROVIDERS.md).

## Reliability exercises

```bash
uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --fault model-timeout-once

uv run harness run tasks/repair_counter.yaml \
  --config configs/teaching.yaml --skill skills/bugfix.yaml --fault crash-after-tool=2
uv run harness resume <run-id> --runs-dir .runs
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map and control flow
- [docs/LLM-LIGHT.md](docs/LLM-LIGHT.md) — priority-based routing
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — config, task, skill schemas
- [docs/PROVIDERS.md](docs/PROVIDERS.md) — provider port and adapters
- [docs/SECURITY.md](docs/SECURITY.md) — defense layers and honest limits
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — runbook and troubleshooting
- [docs/TESTING.md](docs/TESTING.md) — test suite and acceptance mapping
- [docs/WORKSHOP.md](docs/WORKSHOP.md) — module-by-module workshop guide

## Testing

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check harness tests
```

## Layout

```text
yatra-harness/
├── harness/            # runtime modules
├── ay.py               # the `ay` REPL (Claude Code-style front end)
├── configs/            # teaching, local, remote, llm_light
├── tasks/              # task contracts
├── skills/             # skill contracts
├── scenarios/          # deterministic replay scripts
├── fixtures/           # seeded buggy repositories
├── tests/              # unit + end-to-end suite
├── docs/               # architecture and workshop documentation
└── .runs/              # one evidence bundle per run
```
