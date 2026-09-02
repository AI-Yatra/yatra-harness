# AGENTS.md

A coding-agent harness. The model proposes one action at a time; the harness
decides what may run, executes it, records it, recovers it after a crash, and
independently verifies whether the task is actually done.

This file is a map, not a manual. Follow the links rather than expecting
everything here.

## Stack

Python 3.11+ (CI covers 3.11, 3.12, 3.13). Dependencies and entry points are
in `pyproject.toml`; `uv` manages the environment. The only runtime
dependency is PyYAML. `openpyxl` is an extra needed by the spreadsheet task.

Tests are `unittest`, not pytest. Lint is `ruff`.

## Setup and verification

```
uv sync
./init.sh
```

`init.sh` is the whole verification path and the single command to trust:

```
uv run ruff check harness tests ay.py docs/atlas/scripts
uv run lint-imports
uv run python -m unittest discover -s tests
uv run harness doctor --config configs/teaching.yaml
uv run harness run tasks/repair_counter.yaml --config configs/teaching.yaml --skill skills/bugfix.yaml
uv run harness eval evals/teaching.yaml
```

The last two need no API key: `configs/teaching.yaml` runs a scripted local
model, so the same trace appears on every machine.

Run the tests through `uv run`. A bare `python -m unittest` fails, because
the MCP demo server subprocess cannot import `harness` outside the venv.

## Hard constraints

- **Never widen the authority boundary to make something pass.** The skill
  decides which tools exist, the policy engine decides whether a call is
  allowed, the workspace decides which paths are reachable, and the verifier
  decides whether a run succeeded. A change that lets the model bypass any of
  those four is wrong even when the tests are green.
- **`harness/models/auth.py` is the only module that holds a raw credential.** Keys
  must not reach an event, an artifact, or a summary. Anything that resolves a
  credential must also register it with the `Redactor`.
- **Config schemas are strict.** Unknown keys are rejected on purpose, so a
  typo fails loudly. Adding a key means adding it to the `reject_unknown` set
  and to `docs/CONFIGURATION.md`.
- **Never make a run fail for an observability or convenience feature.**
  Tracing, compaction, session memory and retrieval all degrade rather than
  raise. Losing the record of a job must never cost you the job.
- **A test comes before the fix.** Every behaviour change starts with a test
  that fails for the reason being fixed.
- **Do not edit `tests/**` to make an implementation pass.** If a test is
  genuinely wrong, say so rather than quietly changing it.

## Layout

`harness/` is layered. A package may import the ones below it and never the
ones above; `uv run lint-imports` fails the build if that stops being true.

| Layer | Package | What lives there |
|---|---|---|
| 8 | `harness/repl/` | the conversational loop: thread, tools, approvals, rendering |
| 7 | `config.py`, `cli.py`, `runtime.py`, `doctor.py` | the composition root and the things you run |
| 6 | `harness/autonomy/` | goals, backlogs, the loop, evals, review, delivery |
| 5 | `harness/run/` | context, compaction, instructions, verification, subagents, faults, sessions |
| 4 | `harness/execution/` | workspace, policy gate, sandbox, process, tools, MCP, retrieval, search |
| 3 | `harness/record/` | ledger, checkpoints, evidence bundles, spans, replay, redaction |
| 2 | `harness/models/` | credentials, provider adapters, streaming, routing |
| 1 | `harness/core/` | contracts, typed errors, schema helpers, utilities. Depends on nothing. |

`config.py` sits with the entry points rather than in `core` because it is the
composition root: it imports every module it configures. The modules below it
need only its *types*, so they import it under `TYPE_CHECKING`, which
`from __future__ import annotations` makes free at runtime. Two deferred
imports remain, both made inside a function precisely so the cycle does not
exist at import time; they are listed in the contract's `ignore_imports`.

There are two agent loops on purpose. `harness/runtime.py` runs a task
contract against a copied workspace and ends in a verifier's verdict.
`harness/repl/agent.py` runs a conversation in the operator's own directory
and ends when the model stops asking for tools. They share the providers, the
config, the contracts and the command deny-list; they do not share the loop,
because a conversation has no acceptance command.

| Path | What lives there |
|---|---|
| `configs/`, `tasks/`, `skills/` | strict versioned YAML contracts |
| `docs/atlas/` | the architecture canvas, generated from the code |
| `docs/` | architecture, configuration, security, operations, testing |

`docs/ARCHITECTURE.md` maps every module to the run diagram.
`docs/SECURITY.md` states what the harness defends against and where
redaction stops. Read both before changing the runtime.

## Style

Match the surrounding code. Comments explain why a thing is the way it is,
especially where the obvious implementation is wrong; they do not narrate
what the next line does. Commit messages are imperative sentence-case prose,
not conventional-commits.

## Definition of done

- `./init.sh` passes.
- The new behaviour has a test that failed before the change.
- Anything an operator configures is documented in `docs/`.
- No credential can reach a run bundle.
