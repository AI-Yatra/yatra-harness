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

## Out of scope, and why

Docker sandboxing, RAG, streaming and graph orchestration are on the backlog
but none of them are on the path from "edit a repo" to "open a PR".
