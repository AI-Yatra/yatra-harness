# The screen

What `ay` looks like, and why. Two modules: `harness/repl/theme.py` holds the
palette and the grid constants, `harness/repl/render.py` does the drawing.

## The problem

A terminal program does not know where it is running. The background may be
white, black, or Solarized Dark. Colour may be stripped by a pipe, by
`NO_COLOR`, or by a terminal that reports itself as dumb. The window may be 40
columns or 400. Unicode may not encode at all. Every one of those is normal,
and the interface has to stay readable through all of them.

## One grid

Every line shares a two-column gutter. Marks hang in it; content starts after
it. Nested detail sits exactly one gutter deeper.

```
  AI-Yatra 1.0.0
  model: MiniMaxAI/MiniMax-M3 · full-auto · standard profile

  I'll start by exploring the repository structure and understanding what's
    there, which decides where the tests live.

⏺ Run(python -m unittest discover -s tests)
  ⎿ Ran 30 tests in 0.003s
    FAILED (failures=13)
⏺ Edit(game.py)
  ⎿ @@ -58,6 +58,7 @@
         lines.append((0, 4, 8))
    +    lines.append((2, 4, 6))
```

The wordmark, the header, model prose, tool names, notices and errors all
begin at column 2. Tool results and diffs begin at column 4, under the `⎿`
that marks them. A wrapped line hangs at column 4 so the eye can tell a
continuation from a new point.

**Alignment carries the hierarchy, colour only reinforces it.** That ordering
is deliberate: alignment is the only part that survives a pipe. The grid is
asserted in `tests/test_render.py` with colour turned off, which is the state
where nothing else is left.

Prose is not run to the window edge, and does not grow past 100 columns on a
wide terminal. Past roughly that measure the eye loses the start of the next
line on the way back.

## Five roles, measured

Colour is information, not decoration, so there are five roles and no more.
Anything without a meaning stays at the terminal's own foreground, the only
colour guaranteed to be readable.

| role | what it marks | colour | worst-case contrast |
|---|---|---|---|
| `accent` | the agent's marks, and anything you can act on | 32 `#0087d7` | 3.86 |
| `muted` | arguments, counts, paths, hints | 244 `#808080` | 3.80 |
| `success` | a thing that worked; diff additions | 65 `#5f875f` | 3.66 |
| `failure` | a thing that did not, or a state that should worry you | 167 `#d75f5f` | 3.69 |
| `strong` | emphasis | bold | n/a |

The colours were not chosen by eye. Each was picked by maximising its *worst*
WCAG contrast ratio across white, black and Solarized Dark, searching the whole
256-colour cube. The arithmetic ceiling for a single colour that has to work on
both white and black is `sqrt(21)`, about 4.58, reached only at relative
luminance 0.179; every value above is within 0.9 of that ceiling.

`tests/test_render.py` recomputes those ratios from the SGR codes, so a colour
retuned to something unreadable fails the suite instead of shipping.

### Three things deliberately not used

**SGR 2, `dim`/faint.** It has no defined appearance: some terminals ignore it
entirely, leaving no hierarchy at all, and others render it unreadable on a
dark background. The previous renderer used it for most of its output, which
put the majority of the interface at the mercy of a code with no agreed
meaning. A measured grey replaces it.

**The 16 ANSI colours.** They have no standard. Each terminal theme picks its
own, so `blue` may be anything, and is famously illegible on black in several
popular themes. The 256-colour cube is fixed, so a chosen colour is the colour
that appears.

**Yellow, for warnings.** It measures under 1.6 against a white terminal. A
state that should worry the operator uses `failure` instead: one colour for
"wrong or dangerous" rather than two, one of which cannot be read.

## Prose does not depend on transport

Streamed text used to be written straight to the terminal, unwrapped and
unstyled, while a response that arrived in one piece went through the markdown
renderer. The same model saying the same words produced two different screens
depending on whether the route had `stream: true`.

`Prose` is now the single renderer both paths feed. A line is emitted as soon
as it is known to be complete, at a newline or at the last space that fits, so
streaming still arrives live. Feeding it a whole response, or the same
response one character at a time, or in random chunks, produces byte-identical
output; the test asserts that across 25 random chunkings.

Only enough markdown to read comfortably. Headings lose their hashes and gain
weight, bullets get a real bullet, and fenced code is left exactly as written
and clipped rather than wrapped, because a reflowed code line is a wrong code
line. The fence itself is not drawn: the bar down the left already says where
the block starts and stops.

## Degrading

A console that cannot encode `⏺⎿│·─•` falls back to `* \`- | - - -`, chosen once
at startup. The set is all-or-nothing, because half Unicode and half ASCII
looks like a bug rather than a fallback. A stream that raises mid-write is
caught and the text re-encoded with replacements, so a session is never lost
over decoration.
