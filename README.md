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

### Messages in a session build on each other

Every `ay` conversation gets a session: one workspace and one written memory,
so the second message edits what the first one wrote and knows it happened.

```
you> create a file called SCRATCH.md containing the single word hello
you> now create SCRATCH2.md containing the same word that is in SCRATCH.md
```

The workspace lives at `.runs/sessions/<id>/workspace` and the memory at
`session.json` beside it. Outstanding work is committed before each new turn
begins, so a turn's diff is its own and the session's history is a sequence
of commits rather than one blob. Failures are remembered as well as
successes, because a memory that only holds what worked teaches the next turn
to repeat what did not.

`--session <id>` resumes a named session later. `--stateless` restores the
old behaviour of a fresh workspace per message.

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

## Keys

Store a key once and every route that needs it resolves without exporting
anything. The provider is inferred from the key's prefix.

```
ay auth add sk-ws-...
```

```
stored dashscope key sk-ws-H...i7C4 (116 chars)
  file:  ~/.yatra-harness/auth.json
  routes naming DASHSCOPE_API_KEY now resolve without exporting it
```

Omit the key to be prompted for it instead, which keeps it out of your shell
history:

```
ay auth add
```

Keys are shown redacted here and everywhere else.

```
ay auth status              # what is configured, and which source won
ay auth verify              # a real call to every configured provider
ay auth verify dashscope
ay auth remove dashscope
ay auth providers
```

`verify` asks the provider to list its models. A variable being set is not
evidence that the key works, which is how an exhausted quota used to reach the
agent loop before anyone noticed.

Resolution order is the environment variable first, then the stored file, and
`ay auth status` prints which one won. A stale exported variable shadowing a
stored key becomes visible instead of mysterious. Ollama and vLLM report ready
without a key.

The store sits in your home directory rather than the repository, so you cannot
commit it by accident. Keys resolved from it are scrubbed from the event ledger
exactly like exported ones.

`harness auth` and `ay auth` run the same code, and both load `.env` on
startup -- the nearest one at or above the working directory. It accepts a
leading `export` and strips surrounding quotes, so a line pasted out of a
shell profile works. An exported variable always wins over the file.

A route resolves its credential by the variable it names, and failing that by
its endpoint. That way a route naming a non-standard variable -- as
`configs/teaching.yaml` does with `HARNESS_REMOTE_API_KEY` -- can still find a
key stored for the provider that endpoint belongs to. A stored key is only
ever offered to its own provider's endpoint.

## From a repository to a pull request

Point a run at a real repository and it works on a clone of it, on its own
branch, with the repository's history and remote intact.

```
ay --repo . --skill skills/repo-edit.yaml --accept "./init.sh" --deliver pr
```

```
you> fix the typo in the installation section of README.md
```

The agent edits the clone. The verifier runs the acceptance command itself.
Only if that passes does delivery start: commit the diff, ask before pushing
the branch, ask again before opening the pull request. The body is built from
the run's verification record, not from the model's description of its work.

Use `skills/repo-edit.yaml` for this rather than `skills/bugfix.yaml`.
`bugfix` tells the model to find and repair a defect, so given a plain edit
request it goes looking for a bug that is not there.

`--deliver commit` stops after the local commit and `--deliver branch` stops
after the push, so you can watch each step before allowing the next. Nothing
leaves the machine without an explicit yes -- an unattended run with no
terminal denies the push rather than performing it.

Publishing has its own flag. `--yes` approves what the model may do inside
the workspace; `--deliver-yes` approves sending the result somewhere other
people can see. Conflating them would make `ay`, which always passes `--yes`
so tool calls are not gated mid-conversation, push without being asked.

The same thing from the CLI, or afterwards from a finished run:

```
uv run harness run tasks/fix_typo.yaml --config configs/remote-qwen.yaml --skill skills/repo-edit.yaml --deliver pr --deliver-yes
uv run harness deliver <run-id> --mode pr
```

Opening a pull request needs the [GitHub CLI](https://cli.github.com/)
authenticated (`gh auth login`). The branch is pushed before `gh` is called,
so if that step fails the work is still on the remote and the pull request
can be opened by hand.

## Running against a live model

```
uv run harness run tasks/palimpsest_task.yaml --config configs/palimpsest-config.yaml --skill skills/palimpsest-skill.yaml --yes
```

Run `harness doctor` first. It fails a route whose credential is missing and
names the variable, which saves you discovering that after a workspace and a run
id already exist.

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
harness auth        add, inspect, verify and remove provider credentials
harness doctor      preflight: environment, configs, credentials, adapters
harness explain     resolve a task into its contracts without running it
harness tools       list registered tools with risk classes and provenance
harness routes      show the resolved route plan and what was excluded
harness run         execute a task
harness goal        attempt a goal until its acceptance command passes
harness deliver     commit, push and open a pull request for a completed run
harness resume      continue a run from its last checkpoint
harness inspect     terminal state plus the recent event timeline
harness replay      rebuild a run from its ledger and hash it
harness list-runs   list run bundles
```

## Goal mode

A run is one attempt. A goal is "keep attempting until this is true".

```
uv run harness goal "make the counter tests pass" --seed fixtures/buggy_counter --accept "python -m unittest discover -s tests" --config configs/teaching.yaml --skill skills/bugfix.yaml
```

```
== attempt 1 ==
   COMPLETED: acceptance criteria passed

goal: ACHIEVED
reason: acceptance criteria passed
attempts: 1
record: .runs/goal-make-the-counter-tests-pass-c39270/goal.json
```

`--accept` is required and is the whole point: it is the stopping condition,
and it is not the model's opinion. A failed attempt is retried with the
reason carried into the next one's constraints, so attempt two is told what
attempt one hit. A `BLOCKED` run stops the pursuit instead — the model asked
a question, and asking it again unchanged cannot produce a different answer.

`--max-attempts` and `--max-seconds` bound the whole pursuit rather than each
try. Add `--repo` and `--deliver pr` to end an achieved goal with a pull
request.

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

## Sub-agents

The agent can ask a second agent a question instead of reading its way to the
answer.

```
uv run harness run tasks/repair_counter.yaml --config configs/delegation.yaml --skill skills/bugfix-delegating.yaml
```

That run delegates "where is the clamp lower bound handled?", gets a report
back, and applies the repair the sub-agent pointed at. Both sides are replay
scripts, so it needs no key.

A sub-agent is read-only and works from a copy of the workspace: its
deliverable is findings, not an edit. It gets its own run bundle, so it is as
inspectable as its parent, and it can be given its own config — a reviewer
running the same model as the writer shares its blind spots. See
[Configuration](docs/CONFIGURATION.md).

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

247 tests cover the runtime, the tool registry, provider adapters, routing,
repository workspaces, delivery and the REPL. CI runs them on three Python versions along with `harness doctor` and
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
