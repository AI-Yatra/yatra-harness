# tictactoe

Noughts and crosses as a library, used as a worked example for the
[yatra-harness](../../README.md). No UI and no input loop: `game.py` is pure
functions over a nine-cell board, so a harness run can be judged by running
the tests rather than by someone looking at a screen.

## Run it

```
cd demo/tictactoe
ay
```

No flags. `ay` works in the directory you started it in and reads the
`AGENTS.md` here on its own.

Then type this at the `>` prompt:

```
The test suite is failing. Run the tests, read the failures, fix game.py so
every test passes, then run them again to confirm. Do not edit anything under
tests/.
```

Thirteen tests fail before, thirty pass after. It takes about a minute.

To watch instead of letting it work unattended, type `/mode plan` first and
ask it what is wrong: in that mode it can read but cannot change anything.

Put it back with `git checkout -- demo/tictactoe`.

## Checking by hand

```
python -m unittest discover -s tests
```

The repository is **deliberately incomplete**, in two different ways.

## 1. A logic flaw

`winner()` builds its winning lines procedurally and yields only one
diagonal. A board won from top-right to bottom-left is reported as having no
winner.

```
python -m unittest tests.test_winner
```

One test fails. The fix is in `game.py`.

## 2. A missing feature

`best_move(board, player)` does not exist. `tests/test_best_move.py`
specifies it completely:

1. If a cell wins the game for `player`, take it.
2. Otherwise, if a cell would win for the opponent, take it.
3. Otherwise, take the first free cell in board order.

```
python -m unittest tests.test_best_move
```

Twelve tests fail. The feature goes in `game.py`.

## Why both

They are independent — either can be done first — but they are related, and
the relationship is the interesting part. `best_move` decides what to block by
asking `winner()` what would happen. Add the feature without fixing the flaw
and you get a move chooser that cannot see an anti-diagonal threat: every one
of its own tests passes, and the program is still wrong.
