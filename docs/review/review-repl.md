# Hard adversarial review: ay REPL

Scope: harness/repl/agent.py, conversation.py, model.py, tools.py, approvals.py,
shell.py, prompt.py, blocks.py, render.py, banner.py, harness/models/prompting.py,
ay.py. Config/auth/execution internals are out of scope except where called
directly from these files.

Findings are ranked by how likely they are to bite a daily user. Each says
CONFIRMED (ran code and observed the result) or PLAUSIBLE (read the code, did
not execute the failing path).

## 1. The deny-list is trivially bypassed by any interpreter or shell (CONFIRMED, critical)

File: harness/repl/approvals.py, `Gate._hard_refusal` (lines 170-187), backed
by `denied_pattern` in harness/execution/policy.py.

The gate's "absolute, cannot be overridden" deny-list
(`configs/ay.yaml: policy.denied_commands`, e.g. `[rm, -rf]`, `[git, push]`)
only matches a *contiguous* subsequence of literal argv tokens. `run_command`
takes an argument array with no shell, so a model can put the dangerous
command inside a single array element handed to an interpreter, and the
pattern never sees `rm` and `-rf` as adjacent tokens.

Verified directly against `Gate.check` with the shipped `configs/ay.yaml`:

```
gate.check(spec, {'command': ['rm', '-rf', '/']})
  -> Decision(allowed=False, reason="... deny-list pattern 'rm -rf' ...")

gate.check(spec, {'command': ['bash', '-c', 'rm -rf /']})
  -> Decision(allowed=True, reason='full-auto mode')

gate.check(spec, {'command': ['python', '-c', 'import os; os.system("rm -rf /")']})
  -> Decision(allowed=True, reason='full-auto mode')

gate.check(spec, {'command': ['cmd.exe', '/c', 'rm -rf /']})
  -> Decision(allowed=True, reason='full-auto mode')
```

`ReplToolset.run_command` (harness/repl/tools.py) does nothing to close this:
its only shell-metacharacter check applies solely when the model sends
`command` as a *string*, not when it is already an array (the normal,
documented shape). Confirmed the array form dispatches and actually runs:

```
ts.run_command({'command': ['python', '-c', 'print(1+1)']})
  -> ToolOutcome(content='2', ok=True)
```

A second, narrower instance of the same weakness: inserting any flag between
the two pattern words also evades a match, even for patterns that *are*
adjacent in the naive case:

```
gate.check(spec, {'command': ['git', '-C', '.', 'push']})       -> allowed=True
gate.check(spec, {'command': ['git', '--no-pager', 'push']})    -> allowed=True
gate.check(spec, {'command': ['git', 'push', '--force-with-lease']}) -> denied (control case, matches)
```

Impact: in `full-auto` mode this is silent and immediate. In `suggest` /
`auto-edit` mode it degrades an "absolute, no human can override" rule into
an ordinary approval prompt that shows `Run bash -c rm -rf /?` — recoverable
only if the operator reads the full command text carefully before typing `1`.
The code's own docstring says the deny-list "is refused and is never offered
for approval, because a human clicking yes on a prompt is exactly the mistake
the deny-list exists to prevent" — for any command routed through an
interpreter, that guarantee does not hold.

Fix direction: `denied_pattern` needs to see through common wrapper forms
(`bash -c`, `sh -c`, `python -c`, `cmd /c`, `powershell -Command`, etc.) by
also matching against the interpreter's inline script argument, not just the
outer argv tokens; and/or the allowlist model from the batch path's
`PolicyEngine` (which the REPL's `Gate` does not use at all) should be applied
here too instead of relying on a pure blocklist.

## 2. `grep` runs an unbounded, uncapped regex search (CONFIRMED, high)

File: harness/repl/tools.py, `ReplToolset.grep` (lines 250-290).

`pattern` is compiled with `re.compile(raw)` straight from model input and
run per-line with `.search()` across every text file under the search root,
with no timeout and no complexity guard. Python's `re` engine backtracks
catastrophically on ordinary-looking patterns:

```
pattern = re.compile(r'(a+)+$')
pattern.search('a' * 35 + '!')   # times out; still running after 8s wall clock
```

A pattern like `(a+)+$` (or any of the many equivalent classic ReDoS shapes)
against a single matching line anywhere in the repository hangs that `grep`
call indefinitely. There is no per-call timeout the way `run_command` has
one, so the whole turn — and the whole shell, since everything is
single-threaded and synchronous — stalls with no visible error and no
progress indication beyond the spinner. Unlike `run_command`, this cannot be
killed by the platform's process timeout because it isn't a subprocess; the
only way out is killing the `ay` process itself.

This is squarely a resource-limit gap: any model that guesses a plausible but
pathological regex (not even maliciously — `(\w+\s?)+` and other "reasonable"
patterns are also classic ReDoS shapes) can wedge the session.

## 3. `write_file` cannot replace a non-UTF-8 existing file, and the error is misleading (CONFIRMED, medium)

File: harness/repl/tools.py, `ReplToolset.write_file` (lines 294-309).

```python
before = _read_text(path) if existed and path.is_file() else ""
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(content, encoding="utf-8")
```

`before` is read purely to compute a `+N -M` line-count summary for the tool
card, but the read happens unconditionally before the write, and `_read_text`
raises `ToolError` for any file that isn't valid UTF-8. The write then never
happens. Confirmed:

```
f = 'x.bin' containing arbitrary bytes
ts.dispatch('write_file', {'path': 'x.bin', 'content': 'now text'})
  -> ToolOutcome(ok=False, content='x.bin is not a UTF-8 text file')
f.read_bytes()  # unchanged, still the old binary content
```

`write_file`'s whole documented purpose is "creating it or replacing it
entirely" — but it cannot replace an existing file unless that file happens
to already be valid UTF-8 text. The error message also talks about the
*existing* file's encoding, which has nothing to do with the (valid,
string) content the model is trying to write, so it reads as a confusing
non-sequitur to a model trying to fix a corrupted or binary-looking file.

## 4. `edit_file` with an empty `old_string` and `replace_all` silently mangles the file (CONFIRMED, low-medium)

File: harness/repl/tools.py, `ReplToolset.edit_file` (lines 311-341).

`old_string == ""` is accepted (only `old == new` is rejected). `str.count("")`
on an N-character file returns N+1, so the "appears N times, pass
`replace_all`" guidance effectively invites exactly the failure mode:

```
file contains 'hello'
edit_file(old_string='', new_string='X', replace_all=True)
  -> ToolOutcome(ok=True, content='edited x.txt in 6 places (+1 -1)')
file now contains 'XhXeXlXlXoX'
```

The tool reports success with a `+1 -1` diff summary that wildly
understates what happened (it inserted `X` between every character). This
is a narrow trigger — a model would have to pass an empty `old_string` — but
nothing in the schema or the handler rejects it, and the result is silent
data corruption with a misleading "done" report rather than a caught error.

## 5. `Agent.cancel()` / `Interrupted` is dead code (PLAUSIBLE, informational)

Files: harness/repl/agent.py (`Agent.cancel`, `_check_cancelled`, the
`Interrupted` exception) and harness/repl/shell.py.

`shell.py` never calls `self.agent.cancel()` anywhere; grepping the whole
`harness/repl` tree confirms no caller sets `Agent._cancel`. The interrupt
mechanism that actually works today is a bare `KeyboardInterrupt` propagating
synchronously out of whatever blocking call (HTTP request, subprocess) is in
progress, caught in `shell.py:_run_turn`. That path is fine and is exercised
by `_answer_dangling_calls`, which correctly patches up any assistant
message left with unanswered tool calls after a `KeyboardInterrupt` /
`Interrupted` (I read this carefully — it walks every assistant message's
`tool_calls`, not just the last one, so a multi-tool-call turn cut mid-loop
is handled correctly). But the cooperative-cancellation path
(`Agent.cancel()` / `_check_cancelled()`) that the code is visibly built
around is simply never wired up, so between-tool-call and between-step
cancellation only happens by accident of where `KeyboardInterrupt` happens to
land. Not user-visible today since the fallback works, but it means a future
caller (a GUI, a second thread) that calls `agent.cancel()` expecting a clean
stop will find it does nothing, and a `KeyboardInterrupt` raised while inside
a tool's C-level code (e.g. mid-regex, see finding 2) is exactly the case
cooperative cancellation exists for and does not cover.

## Categories checked with no confirmed defect

- **Compaction correctness**: `Conversation.compact` / `_trim_to_safe_start`
  were exercised by reasoning through every place a `keep_recent` tail slice
  could land mid-tool-call-group, and by the existing test suite
  (`tests/test_repl_agent.py`, `tests/test_repl_end_to_end.py`, all passing).
  The trimming correctly drops an entire orphaned run of `tool` messages
  regardless of how many there are, and the system prompt is never part of
  `messages` so it can't be dropped by compaction. No confirmed bug.
- **Path containment**: absolute paths, `..`, and Windows-style traversal
  (`..\..\Windows\win.ini`) were all confirmed rejected by
  `ReplToolset._resolve` with a clear "path escapes workspace" error.
- **Interrupt bookkeeping**: `_answer_dangling_calls` in shell.py correctly
  answers every unanswered tool call after an interrupt, not just the last
  one; this was read in detail and is correct.
- **Streaming**: `StreamAccumulator` reassembly lives in
  harness/models/providers.py, outside this review's file list, so it was
  not exercised; `model.py`'s consumption of the assembled payload
  (`_read_openai`, `_read_anthropic`) tolerates missing/malformed
  `tool_calls`, missing `usage`, and non-dict content blocks without raising.
- **Unicode/console output**: `ay.py` reconfigures stdout/stderr to UTF-8
  with `errors="replace"` at startup, and `Console.write` in render.py has a
  fallback `encode("ascii", "replace")` path if that still fails, so a
  cp1252 Windows console does not appear able to crash the session on model
  output. Not independently reproduced on an actual cp1252 console, but the
  double fallback reads as sound.
- **run_command test suite**: all 123 tests in
  tests/test_repl_agent.py, tests/test_repl_tools.py, tests/test_repl_shell.py,
  and tests/test_repl_end_to_end.py pass on this machine.

## File written

docs/review/review-repl.md (this file). No source file outside docs/review/
was modified.
