# Plan: repository -> verified edit -> pull request

## Goal

`ay --repo <path> "<instruction>"` runs the agent against a real git
repository, verifies the change with the existing independent verifier, and
opens a pull request carrying the verification evidence.

## Why the current harness cannot do it

`WorkspaceManager.create` copies a seed directory, drops `.git`, and runs
`git init`. The run workspace therefore has no history and no remote, so
there is nothing to push and nothing to open a PR against. Every other piece
of the pipeline already exists.

## Features, one commit each

1. **Repository-mode workspaces.** A task may name `repository:` instead of
   `workspace_seed:`. The run workspace becomes a clone of that repository on
   a fresh `harness/<run-id>` branch whose `origin` is the source repo's own
   upstream. The source checkout is never touched.

2. **Agent instruction files.** `AGENTS.md` / `CLAUDE.md` found at the
   workspace root are loaded into the system prompt, bounded by a budget.
   This is what tells the agent the repository's conventions.

3. **Delivery.** A new `harness/delivery.py` commits the verified diff,
   pushes the branch, and opens a PR through `gh`. Refuses to run for a
   non-COMPLETED run. Outward-facing steps pass through the approval gate.

4. **CLI and REPL wiring.** `harness run --deliver`, `harness deliver`, and
   `ay --repo` / `/pr`.

5. **This repository's own harness artifacts.** `AGENTS.md`, `init.sh`,
   `feature_list.json`, `PROGRESS.md` -- the instruction, environment and
   state subsystems the course scores, applied to our own tree.

## Non-negotiables

- 143 existing tests keep passing after every commit.
- Seed-mode behaviour is unchanged; repository mode is purely additive.
- Nothing is pushed anywhere without an explicit operator decision.
- Every new behaviour gets a failing test first.

## Found while building, and fixed

6. **A full context deleted the run's own objective.** The dynamic context
   was one sorted-key JSON document truncated from the end, which put `task`
   last. Caught by a live run, not by a test.

7. **A skill for plain edits.** `bugfix` sends the model hunting for a defect
   that a plain edit request does not contain.

8. **`--yes` must not authorise publishing.** It already meant "approve the
   model's tool calls", and `ay` passes it on every run.

## Second pass: the rest of the backlog

9.  Command deny-list, checked before the allowlist and never overridden.
10. `web_search` with a configurable backend.
11. `harness goal`: attempt until the acceptance command passes.
12. Sessions: one workspace and one memory across messages.
13. Pluggable compaction, including a summarizing strategy.
14. Read-only sub-agent delegation, with its own optional model.
15. Container sandbox for tool and acceptance commands.
16. `harness eval`: a benchmark gate, wired into CI.
17. Spans, linking runs in a goal, a session or a delegation.
18. Ranked retrieval with lexical and embedding backends.
19. Streaming.
20. `feature_list.json` and `harness loop` over it.
21. `harness review`: a scored rubric with a verdict.

## Out of scope, and why

Graph orchestration. Lecture 14 of the walkinglabs course says plainly not to
build one before the loops it is meant to coordinate exist and are understood,
and `harness loop` has run exactly once.
