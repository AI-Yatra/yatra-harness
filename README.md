# Yatra Harness

[![ci](https://github.com/AI-Yatra/yatra-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/AI-Yatra/yatra-harness/actions/workflows/ci.yml)

A coding-agent harness you can read in an afternoon. The model proposes one
action at a time. The harness decides what may run, executes it, records it,
recovers it after a crash, and proves whether the task is actually done.

Swapping the model changes one line of YAML. Nothing else moves.

```
TASK -> Task Contract -> Context Engine -> AGENT LOOP -> Model Call -> Model Router
                                                      -> Tool Call  -> Tool Registry
                                                      -> after each turn -> State + Checkpoint -> Verifier
                                                      -> PASS -> DONE | FAIL -> RETRY LOOP
```

## What the harness controls

The model never touches your filesystem, shell, or network. It returns a
proposal, and every side effect passes through four gates: the skill decides
which tools exist, the policy engine decides whether this call is allowed, the
workspace decides which paths are reachable, and the verifier decides whether
the run succeeded.

That last gate matters most. A model claiming completion does not end a run.
The verifier runs the acceptance commands itself, checks the diff is non-empty,
and confirms no protected path changed. Only then does a run report COMPLETED.

## Requirements

Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and `git` on your PATH.
CI covers Python 3.11, 3.12 and 3.13 on Linux. Development happens on Windows
and macOS too.

## Install

Every command below runs unchanged in PowerShell, cmd, bash and zsh. They are
written on single lines for that reason, since backslash continuations break on
Windows and backtick continuations break everywhere else.

```
git clone https://github.com/AI-Yatra/yatra-harness
cd yatra-harness
uv sync
```

That gives you `uv run harness` and `uv run ay`. To get `ay` and `harness` as
plain commands from any directory:

```
uv tool install --editable . --with openpyxl
```

`--with openpyxl` is not optional. The REPL spawns the harness with its own
interpreter, and the workshop's spreadsheet task needs openpyxl available there.

## Your first run needs no API key

The teaching config runs a scripted local model, so every laptop in a room sees
the same trace with no network and no credentials.

```
uv run harness doctor --config configs/teaching.yaml --task tasks/repair_counter.yaml --skill skills/bugfix.yaml
uv run harness run tasks/repair_counter.yaml --config configs/teaching.yaml --skill skills/bugfix.yaml
```

The scripted model writes a wrong patch first. Verification fails, the retry
loop opens, the model repairs its own patch, and the second attempt passes. Watch
for `VERIFICATION_FAILED` followed by `RETRY_LOOP_ENTERED` in the output. That
failure is the point of the exercise.

Each run lands in `.runs/<run-id>/` with its frozen inputs, an append-only event
ledger, durable checkpoints, the patch, and a summary.

## ay, the REPL

`ay` is short for AI Yatra. It wraps the same CLI: your message becomes a
generated task contract, which runs through `harness run` with live output.

```
ay
```

```
   ███████ ███████     ███ ███ ███████ ███████ ███████ ███████
   ███ ███   ███       ███ ███ ███ ███   ███   ███ ███ ███ ███
   ███████   ███       ███████ ███████   ███   ███████ ███████
   ███ ███   ███         ███   ███ ███   ███   ███ ██  ███ ███
   ███ ███ ███████       ███   ███ ███   ███   ███  ██ ███ ███

# Yatra Harness v1.0.0 · ay REPL
# model: qwen3-coder-plus · config: palimpsest-config.yaml
# seed: chat_seed · contract: unverified (acceptance always passes)
# ~\code\yatra-harness

Type a message to run the agent, or /help for commands.

you>
```

Slash commands: `/runs`, `/inspect <id>`, `/resume <id>`, `/config`, `/model`,
`/help`, `/exit`. Full usage lives in [ay.README.md](ay.README.md).

### Read the contract line before you trust a verdict

A plain chat message runs against an empty scratch workspace with an acceptance
command that cannot fail, which is why the banner says
`contract: unverified`. Under that contract a run reports COMPLETED whether or
not the agent did anything. Fine for open questions, useless as evidence.

Give the run a real workspace, a real acceptance command, and protected paths,
and the banner changes to `contract: verified`:

```
ay --seed fixtures/palimpsest --accept "python verify_contact_workbook.py" --protect "verify_contact_workbook.py" --protect "contact_cards/**"
```

Now try asking it to collect the contact cards, exclude Tom, sort them, and
build `contact.xlsx`. Then ask it to do nothing at all and watch that second run
fail. A grader that cannot fail is not a grader.

## Using a live model

Copy the example environment file and add a key.

```
cp .env.example .env                  # macOS, Linux
Copy-Item .env.example .env           # PowerShell
```

`ay` reads `.env` on startup. The `harness` CLI does not, so export the variable
before running it directly:

```
$env:DASHSCOPE_API_KEY = "sk-..."     # PowerShell
export DASHSCOPE_API_KEY="sk-..."     # macOS, Linux
```

```
uv run harness run tasks/palimpsest_task.yaml --config configs/palimpsest-config.yaml --skill skills/palimpsest-skill.yaml --yes
```

Run `harness doctor` first. It fails a route whose API key variable is unset,
which saves you discovering that after a workspace and a run id already exist.

## LLM Light

Declare what you care about and the router orders its routes to match. No code
changes, no rebuild.

```
uv run harness routes --config configs/llm_light.yaml
uv run harness routes --config configs/llm_light.yaml --profile budget
uv run harness routes --config configs/llm_light.yaml --priority cost --priority latency --require-local
```

The five priorities are `privacy`, `quality`, `cost`, `latency` and `context`.
`configs/llm_light.yaml` ships the profiles `offline`, `budget`, `quality`,
`teaching`, `balanced` and `long-context`. Excluded routes are listed with the
reason they were dropped, so a plan is auditable before you spend anything. See
[docs/LLM-LIGHT.md](docs/LLM-LIGHT.md).

## Commands

```
harness doctor      preflight: environment, configs, credentials, adapters
harness explain     resolve a task into its contracts without running it
harness tools       list registered tools with risk classes and provenance
harness routes      show the resolved route plan and what was excluded
harness run         execute a task
harness resume      continue a run from its last checkpoint
harness inspect     terminal state plus the recent event timeline
harness replay      rebuild a run from its ledger and hash it
harness list-runs   list run bundles
```

## Breaking it on purpose

Fault injection is built in, because a harness is only worth having when things
go wrong.

```
uv run harness run tasks/repair_counter.yaml --config configs/teaching.yaml --skill skills/bugfix.yaml --fault model-timeout-once
uv run harness run tasks/repair_counter.yaml --config configs/teaching.yaml --skill skills/bugfix.yaml --fault crash-after-tool=2
uv run harness resume <run-id> --runs-dir .runs
```

The first injects a transient provider timeout and retries the same route. The
second kills the process after the second tool call. Resume picks up from the
durable checkpoint and finishes with the same total tool count as an
uninterrupted run, so nothing is executed twice.

`harness replay <run-id>` reconstructs a finished run from `events.jsonl` and
prints a SHA-256 of the ledger. Edit one event and the hash changes. Delete one
and the sequence check names the gap.

## Providers

| kind | adapter | wire |
|---|---|---|
| `replay` | deterministic script | none |
| `openai_compatible` | OpenAI-compatible | `/chat/completions` |
| `ollama` | OpenAI-compatible | `/v1/chat/completions` |
| `vllm` | OpenAI-compatible | `/v1/chat/completions` |
| `anthropic` | Anthropic Messages API | `/messages` |

The port itself is one method, `complete`. An HTTP adapter subclasses the shared
base and fills in four hooks: endpoint, request body, headers, and how to
normalize the response into an action. See [docs/PROVIDERS.md](docs/PROVIDERS.md).

## Testing

```
uv run python -m unittest discover -s tests -v
uv run ruff check harness tests ay.py
```

84 tests cover the runtime, the tool registry, provider adapters, routing and
the REPL. CI runs them on three Python versions along with `harness doctor` and
a full deterministic run.

## Layout

```text
yatra-harness/
├── harness/            runtime: contracts, context, routing, tools, policy,
│                       workspace, verifier, events, checkpoints, replay
├── ay.py               the ay REPL
├── configs/            teaching, local, remote, llm_light
├── tasks/              task contracts
├── skills/             skill contracts
├── scenarios/          deterministic replay scripts
├── fixtures/           seeded repositories the agent works on
├── tests/              unit and end-to-end suite
├── docs/               architecture and workshop guides
└── .runs/              one evidence bundle per run
```

## Documentation

[Architecture](docs/ARCHITECTURE.md) maps every module to the diagram above.
[Configuration](docs/CONFIGURATION.md) documents the config, task and skill
schemas. [Security](docs/SECURITY.md) states what the harness defends against
and what it does not, including where redaction stops.
[Operations](docs/OPERATIONS.md) is the runbook,
[Testing](docs/TESTING.md) maps tests to acceptance criteria, and
[Workshop](docs/WORKSHOP.md) walks through the material module by module.

## License

MIT. See [LICENSE](LICENSE).
