# AGENTS.md

Stock levels for a very small shop. Python 3.11+, standard library only,
`unittest` rather than pytest.

## Verification

```
python -m unittest discover -s tests
```

Run it from this directory; the tests import `inventory` from here.

## Hard constraints

- **Do not edit anything under `tests/`.** The tests are the specification.
  Changing one to make it pass changes what the shop is supposed to do.
- **`data/stock.json` is not the fix.** It is the shop's current numbers, the
  way a real till's database would be. If a count in it looks wrong, that is
  the symptom of a bug in `inventory.py`; correcting the number by hand leaves
  the bug in place and loses the evidence. Treat it as read-only.
- `add` and `remove` return a new dict and leave the one they were given
  alone, so a caller can compare before and after.
- A mistake a person could make at a till is a `ValueError` with a sentence in
  it, not a crash. `KeyError: 'cheese'` is a crash.

## Layout

| Path | What it holds |
|---|---|
| `inventory.py` | the counting, and the command line |
| `data/stock.json` | the shop's current numbers |
| `tests/test_inventory.py` | what the shop is allowed to do |
