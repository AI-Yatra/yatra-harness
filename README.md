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

`ay` is short for AI Yatra. It is a coding agent in your terminal, working in
the directory you launched it in.

```
cd ~/my-project
ay
```

```
  ay  yatra-harness 1.0.0
  qwen3-coder-plus · suggest · ~/my-project
  /help for commands, @file to include a file, !cmd to run one

> the auth test fails on windows. why?

⏺ Grep(def test_.*windows)
  ⎿ 3 matches in 2 files
⏺ Read(harness/models/auth.py)
  ⎿ 131 lines

The lookup in `_resolve` is case-sensitive, but Windows environment
variables are not, so a lowercase `dashscope_api_key` never matches.

> fix it and run the test

⏺ Edit(harness/models/auth.py)
  ⎿ Edit harness/models/auth.py?
       1. Yes   2. Yes, and edit_file any file for the rest of this session
       3. No, tell the model what to do instead
  ⎿ +4 -2
⏺ Run(python -m unittest tests.test_auth)
  ⎿ 14 lines

Fixed. `_resolve` folds the name before comparing. All 12 tests pass.
```

One conversation, one working directory, many turns. Reads happen freely;
edits and commands ask first, and a deny-list refuses the unrecoverable ones
with or without approval. Full usage is in [ay.README.md](ay.README.md).

Slash commands: `/model`, `/mode`, `/approvals`, `/tools`, `/context`,
`/cost`, `/compact`, `/clear`, `/init`, `/config`, `/help`, `/exit`. In the
prompt, `@path` inlines a file, `!command` runs one yourself, and Ctrl-C stops
a turn without ending the session.

### Two shapes, one harness

`ay` is the conversational half. `harness run` is the batch half: a task
contract, a copied workspace, a budget, and an independent verifier that
decides whether the job is actually done. Use the REPL when the work is a
conversation and the batch path when you need a verdict you can trust.

```
ay                          # conversation, here, now
ay run task.yaml            # task contract, copied workspace, verified
```

Anything `ay` does not read as a message is handed to the harness CLI
unchanged, so `ay auth`, `ay inspect` and `ay run` are the same code path as
`harness ...`.

### Approval modes

| Mode | Behaviour |
|---|---|
| `suggest` (default) | asks before every edit and every command |
| `auto-edit` | edits freely, still asks before running anything |
| `full-auto` | asks about nothing |

Answering *"Yes, and …"* grants that shape of action for the rest of the
session. The deny-list in `configs/ay.yaml` is never offered for approval at
all: `rm -rf`, `git push`, `git reset --hard`, `sudo`, `pip install` and the
rest are refused outright, and a pattern matches anywhere in the command so
`sudo rm -rf` is caught by the `rm -rf` rule.

### Sessions

A session is one message history, written to `.ay/<id>.json` after every turn.
`ay --resume` reopens the most recent one here; `--session <name>` names it.
When the context window fills, the earlier conversation is summarised and
replaced; `/context` shows how close you are and `/compact` does it on demand.

## Keys

Store a key once and every route that needs it resolves without exporting
anything. The provider is inferred from the key's prefix where the prefix
says anything: a bare `sk-` is issued by OpenAI, DeepSeek, Moonshot and
OpenCode alike, so in that case the candidates are asked which of them the
key actually authenticates against, rather than one of them being guessed.
Name it yourself with `--provider` to skip that, or `--no-probe` to refuse
rather than ask.

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

`verify` makes a real call. A variable being set is not evidence that the key
works, which is how an exhausted quota used to reach the agent loop before
anyone noticed. For most providers it lists the models, which is free and
requires the key. For the aggregator gateways it cannot: OpenCode Zen and
Command Code both serve their model list unauthenticated and answer 200 to a
key that is pure nonsense, so `verify` sends them a one-token completion
against a free model instead.

### Providers

Thirteen are built in. These three are worth calling out:

| Provider | Aliases | Variable | Endpoint |
|---|---|---|---|
| `google` | `gemini`, `aistudio` | `GEMINI_API_KEY`, `GOOGLE_API_KEY` | `generativelanguage.googleapis.com/v1beta/openai` |
| `opencode` | `zen` | `OPENCODE_API_KEY`, `OPENCODE_ZEN_API_KEY` | `opencode.ai/zen/v1` |
| `commandcode` | `cmd`, `command-code` | `COMMAND_CODE_API_KEY`, `CMD_API_KEY` | `api.commandcode.ai/provider/v1` |

**Google AI Studio.** Get a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) and run
`ay auth add` -- both current key formats are recognised on their own. Google
is partway through replacing the long-standing `AIza` keys with `AQ.` ones, and
a newly created key comes out in the new format. `AQ.` keys are rejected on
some native paths that take `?key=`, but work over `Authorization: Bearer`
against the OpenAI-compatible surface, which is the one this route uses.

That surface is at `/v1beta/openai`; the bare `/v1beta` is the native
`generateContent` API and does not speak `chat/completions`.

Gemini 3 models return an encrypted `thought_signature` on every function call
and reject the *next* request if it does not come back. The REPL carries it
through automatically, including across a saved session -- see
[ay.README.md](ay.README.md#gemini-and-thought-signatures).

**OpenCode Zen** and **Command Code** are gateways: one key, many vendors'
models. Neither publishes a key prefix, so name the provider when you store
one:

```
ay auth add --provider opencode <key>        # from opencode.ai/auth
ay auth add --provider commandcode <key>     # from Command Code Studio
```

Then pick them in the REPL, where `configs/ay.yaml` already has a route for
each:

```
/model gemini
/model opencode
/model commandcode
```

Command Code also serves an Anthropic-shaped endpoint at the same base URL, so
a route with `kind: anthropic` and that `base_url` reaches
`/provider/v1/messages` and authenticates with `x-api-key`.

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
uv run harness goal "fix the typo in the installation section of README.md" --repo . --skill skills/repo-edit.yaml --accept "./init.sh" --config configs/remote-qwen.yaml --deliver pr
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
harness eval        run an eval suite and gate on its pass rate
harness loop        work a feature_list.json backlog until it is done or stuck
harness review      score a completed run against a fixed rubric
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

## Three demos to try it on

Each is a small repository broken on purpose, with the exact prompt to paste in
its README. No flags: `ay` works in the directory you start it in.

| | what it teaches | run |
|---|---|---|
| [demo/tictactoe](demo/tictactoe) | a logic flaw and a missing feature, judged by tests | `cd demo/tictactoe && ay` |
| [demo/loginpage](demo/loginpage) | a UI you can look at: enumeration, labels, an invisible error | `cd demo/loginpage && ay` |
| [demo/inventory](demo/inventory) | the harness itself: plan mode, approvals, `/undo`, cost | `cd demo/inventory && ay` |

Put any of them back with `git checkout -- demo/`.

## Finding things in a large repository

`grep` and `glob`, and deliberately nothing more.

A ranked semantic `retrieve` tool lived here and was removed. It was measured
against six live sessions and chosen zero times: the model reached for `grep`
every time, and on the questions asked grep was right. The literature agrees --
lexical search measures uniformly stronger than dense retrieval under inline
delivery across four harnesses and five models -- and the tool cost a 148-package
dependency in a harness whose promise is that a laptop with nothing installed
still runs it. The reasoning and the numbers are in the commit that removed it.

## Running tools in a container

```
docker build -t yatra-harness-sandbox .
uv run harness run tasks/repair_counter.yaml --config configs/sandboxed.yaml --skill skills/bugfix.yaml
```

Every `run_command`, `python_run` and acceptance command then runs in a
throwaway container: no network, no new privileges, all capabilities dropped,
a non-root uid, bounded memory and CPU, and only the run workspace mounted.

The allowlist confines what the model may *ask* for. This confines what the
resulting process can *reach*. `docs/SECURITY.md` has always said the harness
had the first and not the second; `sandbox.kind: docker` is the second.

Local execution stays the default, so a laptop without docker still works.

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

540 tests cover the runtime, the tool registry, provider adapters, routing,
repository workspaces, delivery, sessions, sub-agents, streaming,
the sandbox and the REPL. Tests that need docker skip themselves where it is
absent. CI runs them on three Python versions along with `harness doctor` and
a full deterministic run.

## Working a backlog on its own

Goal mode still needs you to say what the goal is. `harness loop` reads the
goals from a file.

```
uv run harness loop examples/feature_list.json --seed fixtures/buggy_counter --config configs/teaching.yaml --skill skills/bugfix.yaml
```

```
=== clamp-lower-bound: clamp() returns the lower bound for values below it.
  attempt 1
  ACHIEVED: acceptance criteria passed

loop: COMPLETE
reason: nothing left to do; every feature passes
```

Each feature in `feature_list.json` carries its own acceptance commands, and
the loop is *refused* a feature that has none: without one there is nothing to
stop on, and "done" falls back to whoever last read the diff.

A completed feature is marked against evidence — the run id and the reason —
and a failed one is written down rather than erased, because a backlog that
forgets its failures sends the loop round the same wall until the budget runs
out. A failed feature is skipped rather than retried immediately: goal mode
already retried it, and going straight round again would let one hard feature
eat the whole run while the rest of the backlog stayed untouched.

Running that example **edits the file**: the feature is marked passing with
the run id that earned it. That is the point of a backlog, and it also means
`git checkout examples/feature_list.json` is how you run it twice.

Three endings, each named: the backlog is finished, the feature budget is
spent, or everything left has already failed. A loop that cannot say why it
stopped is not autonomous, it is unattended.

## Scoring a run

```
uv run harness review <run-id> --config configs/remote-qwen.yaml
```

```
  correctness        2/2
  verification       2/2
  scope              2/2
  maintainability    2/2
verdict: ACCEPT  (average 2.00)
```

The reviewing agent did not write the change and does not decide whether the
run is done — the verifier already did that. It works from a copy of the run's
workspace that keeps its git history, so `git_diff` shows the change under
review.

Prose is fine to read and impossible to gate on. Fixing the dimensions in
advance means the reviewer scores what it was asked to score rather than
whatever it happened to notice. A dimension it does not score counts as
**zero** — defaulting to full marks would let a reviewer pass anything by
saying less — and the verdict uses a floor per dimension rather than an
average, because an average lets a perfect score somewhere hide a total
failure somewhere else.

## The demo

[`demo/`](demo/) is a small game repository with two things wrong with it — a
logic flaw and a missing feature — and the commands that put the harness to
work on them, up to and including a pull request.

## Evals

```
uv run harness eval evals/teaching.yaml
```

```
suite: teaching  cases: 3
PASS  repair-counter                       COMPLETED             254ms
PASS  broken-acceptance-is-refused         FAILED                203ms
PASS  delegation                           COMPLETED             208ms

pass rate: 100% (threshold 100%)
```

The unit suite proves each part behaves. It cannot answer the question that
matters when a prompt, a model or a budget changes: does a run still finish
the task. `harness eval` runs a set of cases, records the outcome of each, and
exits non-zero when the pass rate falls below the suite's threshold. CI runs
it on every push.

The middle case is expected to **fail**, and that is the point. Its acceptance
command always exits non-zero, so a harness whose verifier stopped verifying
would turn it green — while every unit test in this repository stayed green
too. That case is the one that notices.

## Layout

```text
yatra-harness/
├── harness/            runtime: contracts, context, routing, tools, policy,
│                       workspace, verifier, events, checkpoints, replay,
│                       sandbox, sessions, sub-agents, delivery, goal, loop,
│                       search, tracing, evals, rubric
├── ay.py               the ay REPL entry point
├── harness/repl/       the conversational agent behind it
├── configs/            teaching, local, remote, llm_light
├── tasks/              task contracts
├── skills/             skill contracts
├── scenarios/          deterministic replay scripts
├── fixtures/           seeded repositories the agent works on
├── tests/              unit and end-to-end suite
├── demo/               a game repo with a bug and a gap, for demonstrations
├── evals/              eval suites run in CI
├── docs/               architecture and workshop guides
└── .runs/              one evidence bundle per run
```

## Documentation

[Architecture](docs/ARCHITECTURE.md) maps every module to the diagram above.
[Configuration](docs/CONFIGURATION.md) documents the config, task and skill
schemas. [Security](docs/SECURITY.md) states what the harness defends against
and what it does not, including where redaction stops.
[Interface](docs/INTERFACE.md) is the grid and the palette `ay` draws itself
with, and how each was measured.
[Project state](docs/PROJECT-STATE.md) covers the three things that follow the
repository rather than the install: layered settings, what one session
remembers for the next, and the checker that runs after every edit.
[Operations](docs/OPERATIONS.md) is the runbook,
[Testing](docs/TESTING.md) maps tests to acceptance criteria, and
[Workshop](docs/WORKSHOP.md) walks through the material module by module.

[Harness Atlas](docs/atlas/README.md) is the same architecture as an
interactive pan and zoom canvas: every module, the real import graph, the
authority chain, the run loop and the tool surface, with every number read out
of the repository by a scanner rather than typed in.

```bash
cd docs/atlas && python3 scripts/scan_harness.py && npm install && npm run dev
```

## License

MIT. See [LICENSE](LICENSE).
