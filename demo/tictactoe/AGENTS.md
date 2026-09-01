# AGENTS.md

Noughts and crosses as a library. No UI, no input loop, no dependencies —
`game.py` is pure functions over a nine-cell board, and the tests are the
specification.

## Stack

Python 3.11+, standard library only. Tests are `unittest`, not pytest.

## Verification

```
python -m unittest discover -s tests
```

Run it from the repository root; the tests import `game` from there.

## Hard constraints

- **Do not edit anything under `tests/`.** The tests are the specification.
  Changing one to make it pass changes what the program is supposed to do,
  which is not a fix.
- A board is immutable to callers: `place` returns a new list and the input
  is left alone. Anything new that takes a board must do the same.
- Cells are numbered 0–8, left to right and top to bottom.
- Keep it dependency-free.

## Layout

| Path | What it holds |
|---|---|
| `game.py` | the whole game |
| `tests/test_winner.py` | the rules: placement, winning, draws |
| `tests/test_best_move.py` | the move chooser |
