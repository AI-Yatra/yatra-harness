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
uv run ruff check harness tests ay.py
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

| Path | What lives there |
|---|---|
| `harness/runtime.py` | the agent loop, checkpoints, retry, termination |
| `harness/execution/tools.py` | the tool registry and every native tool |
| `harness/execution/policy.py` | risk classes, command allowlist, approvals |
| `harness/run/workspace.py` | seed and repository workspaces, path containment |
| `harness/run/verifier.py` | the independent completion gate |
| `harness/autonomy/delivery.py` | commit, push, pull request |
| `harness/execution/sandbox.py` | local or container execution |
| `harness/run/session.py` | one workspace and memory across messages |
| `harness/run/subagents.py` | read-only delegation |
| `harness/autonomy/goal.py`, `loop.py` | attempt-until-true, and the backlog loop |
| `harness/execution/retrieval.py`, `search.py` | ranked workspace search, web search |
| `harness/record/tracing.py` | spans tying a run to the runs around it |
| `harness/autonomy/evals.py`, `rubric.py` | the eval gate, the scored review |
| `harness/run/context.py` | context budget, compaction, instruction injection |
| `harness/models/auth.py` | credentials; the only module holding a raw key |
| `configs/`, `tasks/`, `skills/` | strict versioned YAML contracts |
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
