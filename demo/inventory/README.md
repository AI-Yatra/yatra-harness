# inventory

Stock levels for a very small shop, and the third worked example for the
[yatra-harness](../../README.md).

The first two demos are about the code. **This one is about the harness.** The
bug is deliberately dull — `remove` does not check anything — because the point
is the session you run, not the puzzle you solve.

## Run it

```
cd demo/inventory
ay
```

No flags. `ay` works in the directory you started it in and reads the
`AGENTS.md` here on its own, so the rules of the exercise travel with the
repository instead of the command line.

## The bug

```
$ python inventory.py report
apples  3
bread   1
milk    2

$ python inventory.py remove apples 99
apples  -96
```

Minus ninety-six apples, written to `data/stock.json`. `remove` subtracts
whatever it is given from whatever is there. Asking for something the shop
never stocked crashes with a `KeyError` instead of saying so.

```
python -m unittest discover -s tests
```

Four tests fail. The fix is in `inventory.py`.

## The session

Everything below is typed at the `>` prompt. One step, one feature.

### 1. Plan mode: look, do not touch

```
/mode plan
why does the stock go negative?
```

It reads the code and explains. It cannot edit in this mode — if it tries, the
attempt is refused and you see the refusal. Use it when you want a diagnosis
before you want a change.

### 2. The approval gate

```
/mode suggest
fix it
```

`suggest` is the default mode and it asks before every edit and every command.
Say **1** to one of them and **3** to the next: *3* is not just "no", it sends
your reason back to the model, which then tries something else.

### 3. A constraint it has to respect

```
the numbers in data/stock.json are wrong, just correct them
```

`AGENTS.md` says the data file is not the fix. It should decline and repair
`inventory.py` instead. This one is instruction-following rather than
enforcement: the model chooses to obey, and it is worth watching whether it
does.

### 4. Undo

```
/undo
/checkpoints
```

`/undo` puts the files back to before the last change. `/checkpoints` lists
every state this session can return to. Undo does not rewind history — the
undo itself is a change, so it can be undone too.

### 5. Let it finish

```
/mode auto-edit
finish the fix and run the tests
```

`auto-edit` stops asking about file edits but still asks before commands that
leave the workspace. All fourteen tests should pass.

### 6. What it cost

```
/cost
/context
/approvals
/tools
```

Tokens in and out, how full the context window is, what you blanket-approved
this session, and every tool the model can call.

## If you only want it fixed

Skip the tour and paste this instead:

Paste the whole block at once. `ay` reads lines that arrive together as one
message, so a multi-line prompt stays a single turn.

```
The tests are failing. Run them, fix inventory.py so every test passes, then
run them again. Do not edit anything under tests/, and do not edit
data/stock.json.
```

Four tests fail before, fourteen pass after.

## Other things worth trying

| | |
|---|---|
| `@inventory.py what does remove do?` | pull a file into your message |
| `!python -m unittest discover -s tests` | run a command yourself, without the model |
| `/model` | every configured route and whether it has a key |
| `/model gmi` | switch model mid-session, keeping the conversation |
| `/compact` | summarise the conversation to free context |
| `/init` | write an AGENTS.md for a repository that has none |

## Putting it back

```
git checkout -- demo/inventory
```
