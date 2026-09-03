# Project state

Three things that follow the repository rather than the install: settings,
memory, and the checker that runs after an edit.

They exist because of one gap. The harness knew nothing about the directory it
was started in. It read one config from its own install path, kept a
transcript but never a fact, and edited files without ever being told whether
the result still compiled.

## Settings

`ay` now discovers settings by walking up from the directory it was started
in, stopping at a `.git` or a `.yatra`. A session started three folders inside
a repository still finds the repository's settings.

```
--config                        what you typed wins outright
.yatra/settings.local.yaml      machine-local, gitignored
.yatra/settings.yaml            the project's, committed, shared
~/.yatra-harness/settings.yaml  your defaults for every project
configs/ay.yaml                 what ships
```

Anything absent is simply skipped. `load_config` only discovers when it is
given a project root, so every batch caller still reads exactly the file it
named.

### Refusals do not follow that order

`deny` and `ask` lists **merge across every layer, and a refusal written at
any layer survives all of them.** If a project bans `git push`, a personal
file cannot quietly re-enable it, and an empty list in a narrower file does
not clear a wider one.

`allow` lists are replaced like any other value. The asymmetry is the point:
widening a refusal is safe, and widening a permission grants something the
operator never wrote.

`/config` prints which files were applied, because an operator debugging a
rule needs to know which one set it.

## Memory

`.yatra/memory.md` — what earlier sessions learned about this repository,
loaded into the system prompt at the start of every session.

A markdown file, not a vector store, and that is a decision rather than a
shortcut. A file can be read, corrected and deleted by the person it
describes; it diffs; a team can commit it or ignore it. For the tens of lines
a repository actually needs remembered, an embedding index would be machinery
standing in for a paragraph.

Three failure modes shape it.

**Staleness.** A remembered fact outlives the thing it describes. "The tests
live in `spec/`" survives the day somebody renames the directory. Every entry
carries its date and arrives as `(52d ago, may be out of date)`. Stale entries
are marked rather than dropped, because dropping one hides that the agent
believes something wrong.

**Growth.** Capped at 40 entries; the oldest fall off. A memory that only
grows becomes the context problem it was meant to solve.

**Junk.** The expensive failure is not forgetting, it is remembering something
that was true once. The `remember` tool's description says what is *not* worth
keeping: anything already in `AGENTS.md`, anything true only of the change
being made now, any summary of the conversation.

Memory is keyed to the project, not the session, because the project is the
stable identity. It sits *after* `AGENTS.md` in the prompt: what a person
wrote down outranks what the agent worked out for itself.

`forget()` exists so a wrong memory can be corrected without opening a file.

## Diagnostics

The project's own checker, run over a file straight after the agent writes it.
Off unless configured.

```yaml
diagnostics:
  command: [ruff, check, --output-format, concise, "{file}"]
  suffixes: [.py]
```

`{file}` is the changed path, appended if the token is absent. `suffixes`
keeps a Python type checker away from a Markdown edit.

### This is not a hook

A hook observes, and its output deliberately **never reaches the model**,
because a formatter that is not installed is the operator's problem and a
model told about it tries to fix it. A diagnostic is the opposite: the report
is about the model's own edit, it is exactly what the model needs, and it is
useless to anybody else. One mechanism could not carry both rules.

### A diagnostic is not a failed edit

Everything here is shaped by that sentence. Another agent shipped this exact
bug: its model read the diagnostics attached to a successful edit, concluded
the edit had not applied, and wrote the same change again.

So the edit's own result comes first and stands alone, the report follows
behind a separator that says *the edit above was applied*, and the tool's
success flag never moves because a checker had something to say. A checker
that cannot run at all becomes an operator notice and never reaches the model.

The exit code is the signal, not the output. Checkers announce success in
prose — ruff prints `All checks passed!` — and reading that as a finding would
attach a report to every clean edit and teach the model to ignore the section.

### Why a CLI checker and not a language server

A language server means a client, a lifecycle, a protocol and a per-language
install. The projects that most want this already have `ruff`, `tsc` or `mypy`
configured and know how to run them. The tradeoff is real and is stated rather
than hidden: no go-to-definition, no call graph, no find-all-references.

## What is deliberately not here

**Type-ahead steering** — typing a correction while the model is still
working. Interrupting already works well: `Ctrl-C` stops the turn after the
current tool rather than inside it, every dangling tool call is answered so
the thread stays valid, the model is told it was interrupted, and the session
is persisted. You can redirect immediately with the whole context intact.

The remaining piece is accepting keystrokes *during* a turn, which needs a
thread blocking on stdin while the spinner writes to the same terminal. On
Windows that read cannot be cancelled cleanly. That is a race in the one place
a terminal program cannot afford one, in exchange for saving a keypress.
