# ay

A Claude Code-style REPL on top of the Yatra Harness. Type a message,
get a run. Iterate. Inspect past runs. Switch the model. Resume from
checkpoints.

This is a thin wrapper around the existing `harness run` / `harness inspect`
/ `harness resume` / `harness list-runs` subcommands — no changes to the
harness internals.

## Quick start

From the `yatra-harness/` root:

```powershell
# one-time
uv sync
notepad .env
# DASHSCOPE_API_KEY=sk-ws-...

# launch the REPL
# install once, then `ay` works from any directory
uv tool install --editable . --with openpyxl
ay

# or without installing
uv run ay --config configs/palimpsest-config.yaml --skill skills/palimpsest-skill.yaml
```

You'll see:

```
ay -- Claude Code-style REPL on the yatra-harness
config: configs\palimpsest-config.yaml  model: qwen-plus
Type a message to run the agent, or /help for commands.

you>
```

Type a message to run the agent. Slash commands are also available.

## Making a chat run verifiable

By default a chat task is seeded from an empty scratch workspace
(`fixtures/chat_seed`) and its acceptance command is `python -c "print('chat
acceptance ok')"`, which cannot fail. The harness therefore records the run but
does not check it, and `/config` states this. That default is right for
open-ended questions and wrong for anything that produces an artifact.

Three optional flags turn a chat message into a falsifiable task:

| Flag | Effect |
|---|---|
| `--seed DIR` | workspace seed directory (default `fixtures/chat_seed`) |
| `--accept CMD` | real acceptance command, repeatable; also sets `require_non_empty_diff: true` |
| `--protect GLOB` | protected path glob, repeatable |

```bash
uv run ay \
  --config configs/palimpsest-config.yaml \
  --skill skills/palimpsest-skill.yaml \
  --seed fixtures/palimpsest \
  --accept "python verify_contact_workbook.py" \
  --protect "verify_contact_workbook.py" --protect "contact_cards/**"
```

With those set, a message that does no real work now fails verification instead
of reporting success.

## Keys

`ay auth` and `harness auth` are the same command. Store a key once and the
provider is inferred from its prefix:

```
ay auth add sk-ws-...        # or `ay auth add` to be prompted without echo
ay auth status               # redacted, and shows which source won
ay auth verify               # a real call, not a variable check
ay auth remove dashscope
ay auth providers
```

The store lives at `~/.yatra-harness/auth.json`, outside the repository. An
exported environment variable still takes precedence, and `.env` is loaded on
startup.

## Commands

| Command | Effect |
|---|---|
| `<your message>` | Run the agent on the message. Generates a task YAML and invokes `harness run` with live event streaming. |
| `/runs` | List the last 15 runs (status + task id). |
| `/inspect <run_id>` | Show a run's terminal state and the last 30 events. |
| `/resume <run_id>` | Resume a non-terminal run from its durable checkpoint. |
| `/config` | Show the active config path, model, and skill. |
| `/model <name>` | Switch the configured model by editing the config file in place. |
| `/help` | This list. |
| `/exit`, `/quit` | Leave the REPL. |

## How it works

For each message, `ay`:

1. Slugifies the message into a task id (`chat-<slug>-<hash>`).
2. Writes a `tasks/chat/<id>.yaml` with the message as the objective, an
   empty scratch seed (`fixtures/chat_seed/`), and a trivial passing
   acceptance command (`python -c "print('chat acceptance ok')"`).
3. Invokes `python -m harness run <task> --config ... --skill ... --yes`
   as a subprocess, streaming stdout to your terminal line by line.
4. Reports the final exit code, with `/inspect` / `/runs` for follow-up.

The chat seed is empty by design — chat tasks are open-ended and the
acceptance is a trivial pass-through. The operator (you) decides if the
output is right. For tasks that need a real seed (like the Palimpsest
workbook experiment), run the harness directly with `harness run ...`.

## What it's good for

- Ad-hoc Q&A against qwen-plus in a loop, with persistent evidence per
  message under `.runs/`.
- Iterative model exploration: try the same message twice, see the run
  history, compare events.
- Lightweight teaching demos: the REPL is one process, no separate
  server, no chat UI to install.
- Quick "what does the agent do with this prompt?" probes.

## What it's NOT

- Not a multi-turn agent: each message is an independent run. The REPL
  doesn't keep conversation history or context between messages.
- Not a general code editor: the agent runs in an isolated sandboxed
  workspace per run; it can't see or modify your local repo state.
- Not a server: it doesn't accept remote connections, expose a REST API,
  or persist sessions across restarts (runs are persisted, the REPL
  itself is not).
- Not a replacement for `harness run`: for tasks with real seeds,
  acceptance gates, or fault injection, run the harness CLI directly so
  you can pass those flags.

## Files created at runtime

```
yatra-harness/
  tasks/chat/                    one YAML per message
    chat-<slug>-<hash>.yaml
  .runs/                          one evidence bundle per run
    chat-<slug>-<hash>-<ts>-<run_id>/
      events.jsonl
      state.json
      result.json
      summary.md
      patch.diff
      workspace/                  the agent's sandboxed copy
      inputs/                     frozen config + task + skill
      artifacts/                  payloads and verification logs
```

## Troubleshooting

- **`No DASHSCOPE_API_KEY found`**: edit `yatra-harness/.env` and add a line
  `DASHSCOPE_API_KEY=sk-ws-...`.
- **`error: task workspace seed is not a directory`**: the chat seed
  (`fixtures/chat_seed/`) is missing. Re-run `git restore` or pull.
- **The agent says "Added lower-bound handling" for an unrelated task**:
  the trivial acceptance command always passes, so the harness reports
  COMPLETED. Read `summary.md` and `events.jsonl` in the run's directory
  to see what the agent actually did — you decide if it was right.
- **Want to start over**: delete `tasks/chat/` and the relevant entries
  in `.runs/`. The seed and configs stay untouched.

## The boundary

`ay` is intentionally a 300-line shell. It runs the harness
exactly the way `harness run` would — no approval prompts, no streaming
tokens mid-generation, no patch approval flow. The harness is the agent;
the REPL is the operator console. If you want those higher-fidelity
features, build them in `harness/runtime.py` where the loop lives.
