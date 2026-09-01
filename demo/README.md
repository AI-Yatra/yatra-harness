# Demo

A small game repository with two things wrong with it, and the commands that
put the harness to work on them.

The game is [tictactoe](tictactoe/): pure functions over a nine-cell board,
no UI and no input loop, so a run can be judged by running the tests rather
than by somebody watching a screen.

| | |
|---|---|
| **Logic flaw** | `winner()` yields only one diagonal, so a win from top-right to bottom-left is not seen. One test fails. |
| **Feature addition** | `best_move(board, player)` does not exist. Twelve tests fail. |

They are independent — run either first — and related, which is the part
worth pointing at. `best_move` decides what to block by asking `winner()`
what would happen, so adding the feature without fixing the flaw produces a
move chooser that cannot see an anti-diagonal threat while passing every one
of its own tests.

Every task protects `tests/**`. The agent cannot make the tests pass by
editing the tests, and it does not get to decide that it is finished: the
verifier runs the acceptance command itself.

## Before you start

```
uv sync
ay auth add sk-...        # or export DASHSCOPE_API_KEY
uv run harness doctor --config demo/config.yaml
```

`demo/config.yaml` is the demo's own: bigger budgets, because the feature is
a dozen tests' worth of work, and **no fallback route**, because falling back
to a replay script written for another task would turn a failed demo into a
COMPLETED run doing unrelated work.

## 1. Fix the logic flaw

```
uv run harness run demo/tasks/fix-winner.yaml --config demo/config.yaml --skill skills/repo-edit.yaml --yes
```

Expect a two-line patch to `game.py` and `status: COMPLETED`. The interesting
part is what it took: read the failing test, read the code, patch, and then
the harness — not the model — ran `python -m unittest tests.test_winner` to
decide.

## 2. Add the feature

```
uv run harness run demo/tasks/add-best-move.yaml --config demo/config.yaml --skill skills/repo-edit.yaml --yes
```

`tests/test_best_move.py` is the whole specification, and the agent has to
read it before it can write anything.

## 3. Both, as a conversation

```
uv run ay --repo <path> --config demo/config.yaml --skill skills/repo-edit.yaml --accept "python -m unittest discover -s tests"
```

Use `demo/setup.sh` for the path (below). Ask for the fix in one message and
the feature in the next: the second message edits what the first one wrote,
because the session keeps one workspace and remembers what already happened.

## 4. Both, without you

```
uv run harness loop demo/tictactoe/feature_list.json --seed demo/tictactoe --config demo/config.yaml --skill skills/repo-edit.yaml --yes
```

`--yes` approves the model's edits inside the run workspace. Leave it off and
the loop still runs, but every patch is refused and the model is told so —
worth seeing once, and the reason this line has the flag.

The backlog says what to do and how each item is checked. The loop takes the
next unfinished one, pursues it, marks it against the run that earned it, and
goes round again.

Both features end up in **one** workspace — the loop runs in a session, so the
second feature builds on the first — and the path is printed when it stops.
The whole suite passes there, and `tests/` is byte-identical to the original,
which is the protected-paths gate having held.

Running this **edits `feature_list.json`**; that is what a backlog is for, and
`git checkout demo/tictactoe/feature_list.json` resets it.

## 5. All the way to a pull request

```
eval "$(demo/setup.sh)"
uv run ay --repo "$DEMO_REPO" --config demo/config.yaml --skill skills/repo-edit.yaml --accept "python -m unittest discover -s tests" --deliver pr
```

`setup.sh` builds a throwaway git repository from `demo/tictactoe` with a
local bare remote, so the delivery path can be rehearsed without opening a
pull request on anybody's account. Everything except `gh pr create` works
against it; point `--repo` at a real checkout when you want the last step
too.

Without `--deliver-yes` it stops and asks twice — once before pushing the
branch, once before opening the pull request. Answer `n` to either and it
stops with the work intact.

## Watching it fail

Worth doing at least once, because a gate you have never seen refuse anything
is not evidence of a gate.

```
uv run harness run demo/tasks/fix-winner.yaml --config demo/config.yaml --skill skills/repo-edit.yaml --yes --max-turns 2
```

Two turns is not enough to read the test and write the patch. The run ends
`BUDGET_EXHAUSTED` rather than pretending, and the bundle records exactly how
far it got.
