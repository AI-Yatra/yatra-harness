# Harness Atlas

An interactive canvas for taking this harness apart, module by module.

A codebase arrives as a directory of Python files and stays a black box. The
atlas lays all of it on a single pan and zoom canvas and answers one question:
**where does authority actually live in this harness, and what is the evidence
that nothing routes around it.**

```bash
cd docs/atlas
python3 scripts/scan_harness.py   # writes public/atlas.json from the repository
npm install
npm run dev                       # http://localhost:5173
```

Zero runtime dependencies. `npm run build` produces a static `dist/` that opens
from any path, including a file:// copy.

## Every number is read out of the code

Nothing on the canvas is typed in by hand. Line counts, import edges and public
surface come from the Python AST. Tool names, risk classes and argument names
come from the literal `ToolSpec(...)` calls in `harness/execution/tools.py`. Operator
verbs come from the `add_parser` calls in `harness/cli.py`. Event types come
from the string literals handed to the ledger, including the terminal ones that
are emitted through a status-to-event map. Statuses, action kinds and budget
defaults come from the enums and dataclasses in `harness/core/contracts.py`. Churn
comes from `git log --follow`.

A metric that cannot apply to a module is drawn as hatching and says *n/a*. It
never quietly becomes a zero.

## What is on the canvas

Ten regions, all of them drawings. Where a region has words, they are labels.

| Region | What it shows |
|---|---|
| **start** | the one invariant, the counts, and the key that decodes every other region |
| **map** | fifteen harness-engineering primitives against the two loops; cell colour is depth, a hatched cell means that loop does not do it |
| **turn** | one turn as a sequence over ten actor lanes, each arrow carrying the module that performs it and the ledger event it writes |
| **gates** | the eight ways a call can be refused, as a filter: rule in, verdict out |
| **state** | the run state machine — nine statuses, eleven transitions, five terminal |
| **loops** | the batch and conversational loops side by side, over the modules they share |
| **tools** | every tool by risk class and argument schema, marked for which loop offers it |
| **wall** | all 49 modules, one column per layer, recoloured by the metric in the header |
| **graph** | all 178 import edges, layers as bands, so a backwards dependency is visible as one |
| **mass** | a treemap where area is line count |

The primitive rows are not invented. They follow the design-primitive
catalogues the field has converged on, cross-checked against LangChain's
*Anatomy of an Agent Harness* and OpenAI's account of the Codex harness; three
rows (evals, reliability, retrieval) are added because this repository has
dedicated modules for them.

## Two halves, kept apart

`scripts/scan_harness.py` measures. `scripts/taxonomy.py` judges — which
module implements which primitive, the order of the turn, what each gate
refuses, how the states connect. Splitting them means the canvas can say which
of its claims are counted and which are argued, and it means the argument is
in one file you can disagree with.

The tests check the judgement against the code: every module the taxonomy
names must exist, every event the sequence emits must be one the ledger
actually writes, every status in the state machine must be in the contract,
and neither loop may import the other. A rename breaks the test rather than
quietly turning a covered primitive into a fake gap.

## Controls

| Input | Action |
|---|---|
| drag | pan |
| scroll, pinch | zoom to the cursor |
| click | open the inspector |
| hover a module | dim everything it does not import or get imported by |
| `1`-`9`, `0` | fly to a region, or fit everything |
| `+`, `-` | zoom |
| `Esc` | close the inspector |

## Keeping it current

The data file is committed so the canvas works without running Python. After a
change to the harness, re-run the scanner:

```bash
python3 docs/atlas/scripts/scan_harness.py
python3 docs/atlas/scripts/scan_harness.py --check   # non-zero if stale
```

`tests/test_atlas_scan.py` checks the scanner against the code it reads: every
module lands in exactly one layer, import edges agree in both directions, the
enums match `harness/core/contracts.py`, and every boundary stage names a module that
exists. It deliberately does not assert that `public/atlas.json` is fresh, so
editing the harness does not break the suite. Add `--check` to CI if you would
rather the file never drift.

## Layout

Sections are built at the origin, measured, and only then placed into columns,
so a new module changes the canvas without anyone touching a coordinate.

## Credits

The form is borrowed from [Weight Atlas](https://github.com/alesha-pro/atlas),
which does the same thing for the tensors of a model checkpoint.
