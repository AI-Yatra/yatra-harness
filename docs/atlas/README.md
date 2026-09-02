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

## The recorded session

Every other region measures the package at rest. The `trace` region measures it
in motion, from one real conversation:

```bash
python3 docs/atlas/scripts/trace_session.py --route inception
```

That copies `demo/tictactoe` to a temporary directory, runs its test suite to
get a starting score, hands the failing suite to a real model over a real
provider, and runs the suite again afterwards. The subject has a flaw and a gap
on purpose: `winning_lines()` omits the anti-diagonal, and `best_move()` does
not exist. Thirteen tests fail at the start. `tests/` is write-protected, so the
only way to turn them green is to fix the code.

Nothing is instrumented by hand. A `sys.setprofile` hook watches every call and
keeps the ones that cross from one component into another, which is what makes
the diagram worth trusting: a component that stops being on the path stops being
in the picture, without anyone remembering to update a drawing.

Three things it shows that the static regions cannot:

- **The path.** Which component handed to which, in what order, with call
  counts. Across the recorded run, of thirteen edges, none runs back up the
  layer stack. That is the import contract holding at run time and not only
  under `lint-imports`.
- **The cost.** Self time per component, with nested calls subtracted so the
  parts sum to the wall clock rather than exceeding it. In the committed run,
  96% of seven seconds is blocked on the provider and 3% on the test subprocess
  the model started. The harness's own work comes to 21ms.
- **The result.** The before and after summaries are the test runner's own
  words, captured from a subprocess either side of the conversation. The region
  reports a failed run as readily as a successful one.

`public/trace.json` is optional. Without it the canvas drops the region and
everything else works. `tests/test_atlas_trace.py` covers the recorder, most
importantly the timing arithmetic: summing per-component durations without
subtracting nested calls once produced a harness that spent 14.4s inside a 7.2s
session, and no reader would have caught that from the picture.

## Layout

Sections are built at the origin, measured, and only then placed into columns,
so a new module changes the canvas without anyone touching a coordinate.

## Credits

The form is borrowed from [Weight Atlas](https://github.com/alesha-pro/atlas),
which does the same thing for the tensors of a model checkpoint.
