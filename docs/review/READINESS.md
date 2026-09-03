# Readiness report

A hard review of this harness against Claude Code, Codex CLI and Aider, and
against what the literature actually establishes about agent scaffolds.

Three reviews fed this: `readiness-research.md` (literature and competitive
analysis), `review-repl.md` (the `ay` agent loop and tools),
`review-platform.md` (credentials, execution, config, ledger). Every finding
promoted to this document was re-verified here by running code. Findings that
were reported but not reproduced are marked as such and kept in the source
documents rather than repeated here.

## Verdict

The batch loop is close to production quality. The interactive REPL is a good
agent with a soft floor under it.

That split is the single most important thing in this report and it is not
obvious from the outside, because both are shipped from the same binary. The
batch path (`harness run`) has a Docker sandbox, an MCP client, depth-capped
read-only subagents, an independent verifier and durable checkpoint/resume. The
REPL path (`ay`) has none of them. It executes on the host with the inherited
environment, and its only barrier between a model's tool call and the operator's
disk is the approval gate plus a deny-list that does not hold.

**Do not run `ay --mode full-auto` on a repository you care about until F1 is
fixed.** In `suggest` and `auto-edit` the operator sees each command before it
runs, which is the real protection today.

## The five that will bite you

Each was reproduced by running code in this repository, not inferred.

### F1. The deny-list does not deny. Critical.

`harness/repl/approvals.py`, `Gate._hard_refusal`, via
`harness/execution/policy.py`, `denied_pattern`.

Matching is a contiguous-subsequence test over argv. Anything that puts the
dangerous text inside a single argument, or inserts a flag between matched
words, passes.

```
REFUSED      ['rm', '-rf', '/']
ALLOWED <--  ['bash', '-c', 'rm -rf /']
ALLOWED <--  ['sh', '-c', 'rm -rf /']
ALLOWED <--  ['python', '-c', 'import os; os.system("rm -rf /")']
ALLOWED <--  ['cmd.exe', '/c', 'rm -rf /']
REFUSED      ['git', 'push', '--force']
ALLOWED <--  ['git', '-C', '.', 'push', '--force']
ALLOWED <--  ['git', '--no-pager', 'push', '--force']
```

Both independent reviewers found this. It matters because the code presents the
deny-list as absolute: `approvals.py` states a denied command cannot be run
"with or without approval", and `configs/ay.yaml` says these are "the commands
whose damage is not recoverable by saying no next time". Under
`--mode full-auto` there is nothing else in the way.

The honest framing: the deny-list is a guard against an obvious mistake, not
against an adversary or a confused model that reaches for a shell wrapper. It is
currently documented as the latter.

### F2. Six of seven credential formats are not redacted. High.

`harness/record/redaction.py`, `TOKEN_PATTERNS`.

The provider catalogue carries 17 distinct key prefixes. The redactor matches
`sk-`.

```
redacted    openai     sk-proj-AAAAAA...
LEAKS  <--  groq       gsk_AAAAAAAAAA...
LEAKS  <--  cerebras   csk-AAAAAAAAAA...
LEAKS  <--  inception  sk_AAAAAAAAAAA...
LEAKS  <--  google     AIzaAAAAAAAAAA...
LEAKS  <--  google2    AQ.AbAAAAAAAAA...
LEAKS  <--  nvidia     nvapi-AAAAAAAA...
```

A provider that echoes the offending key in an error body, which several do on
401, writes it verbatim into `events.jsonl`. The `sk_` case is self-inflicted
and recent: the inception provider was added without extending redaction, which
is exactly the failure mode a catalogue-driven redactor would have prevented.

The fix is to derive the patterns from `auth.PROVIDERS` rather than maintain a
second list by hand.

### F3. A model-supplied regex can hang the session forever. High.

`harness/repl/tools.py`, `grep`.

The pattern comes from the model and is compiled and run with no timeout. A
catastrophic-backtracking pattern against a 35-character line does not return:

```
grep {"pattern": "(a+)+$"}   ->   still running after 10s, killed
```

Because matching happens in C inside the same thread, Ctrl-C does not interrupt
it. The session must be killed, losing the conversation. This is not adversarial
input in the usual sense; a model writing a plausible regex can produce it.

### F4. `edit_file` with an empty `old_string` silently corrupts a file. Medium.

`harness/repl/tools.py`, `edit_file`.

```
before:  'x = 1\n'
call:    old_string="", new_string="Z", replace_all=true
after:   'ZxZ Z=Z Z1Z\nZ'
result:  ok=True, detail="+2 -1"
```

It reports success. The empty string matches between every character, so the
replacement is interleaved throughout. `edit_file` is otherwise the best-designed
tool here, refusing absent and ambiguous matches, which makes this gap
conspicuous: the one input it does not reject is the one that destroys the file.

### F5. Streaming disconnect crashes the turn instead of failing over. Medium.

`harness/models/providers.py`, `_read_stream`.

A connection dropped mid-SSE raises `http.client.IncompleteRead`, which is not
in the set `ModelRouter.call` handles (`TransientProviderError`,
`PermanentProviderError`, `ConfigurationError`). It is neither retried nor
failed over to the next route. Reported as confirmed against a fake response
object by the platform reviewer; not independently re-run here.

## Also found, lower severity

- **Orphaned grandchildren on Windows timeout.** `execution/process.py` has no
  job object on Windows, so killing a timed-out child leaves its own children
  running. Confirmed by the platform reviewer via `tasklist`.
- **A torn ledger write bricks a run's history.** `record/events.py` raises
  `StateError` permanently on a truncated final line, so resume and replay both
  fail for that run rather than recovering the intact prefix.
- **`write_file` cannot replace a non-UTF-8 file.** It reads the old contents
  for the diff summary before writing, so replacing a binary file fails with an
  error about the file being replaced.
- **`Agent.cancel()` is dead code.** The cooperative-cancel path is never called
  from `shell.py`; the working interrupt is a bare `KeyboardInterrupt`. Fine
  today, and the reason F3 cannot be interrupted.
- **429 handling ignores `Retry-After`.** Backoff is computed rather than read.

## What is genuinely solid

Stated as plainly as the problems, because a review that only lists faults
misrepresents the thing.

- **Path containment holds.** `Workspace.resolve` was tested against `..`,
  absolute paths, UNC paths and symlink escapes, and refused all of them.
- **Tool subprocess environment is an allowlist**, so provider API keys do not
  reach commands the model runs. This is better than the REPL's own docstring
  suggests.
- **Config validation is strict**: bounds-checked, unknown keys rejected, route
  references resolved at load time. A typo fails before a session starts rather
  than three turns in.
- **Compaction is correct.** Orphaned `tool` messages after a compaction, the
  failure that produces a provider 400 on the next request, are handled.
- **The edit tool's design is right.** Exact-string replace with refusal on
  absent or ambiguous matches is well supported by the published data on
  diff-format failure rates, and is the single choice that most affects whether
  a mid-tier model can edit code at all.
- **The layering is enforced, not intended.** `lint-imports` holds two contracts
  in CI, and a recorded run confirmed zero upward calls at run time.

## Against the reference implementations

Verified by reading this repository's code rather than its README.

| | Claude Code | Codex CLI | Aider | this repo |
|---|---|---|---|---|
| Approval modes | yes | yes | yes | yes, 3 modes |
| OS-enforced sandbox | partial | **yes** | no | batch only, not `ay` |
| Deny-list that holds | yes | yes | n/a | **no, see F1** |
| Checkpoint / undo | yes | partial | **yes, per-edit** | batch only |
| MCP | yes | yes | no | batch only |
| Subagents | yes | no | no | batch only |
| Hooks | yes | no | no | **none anywhere** |
| Session resume | yes | yes | yes | yes |
| Cost visibility | yes | yes | yes | yes, approximate |
| Project instructions | CLAUDE.md | AGENTS.md | CONVENTIONS.md | both |
| Multi-provider | no | no | yes | **yes, 23** |
| Per-model prompt tuning | no | no | no | **yes, unique** |
| Independent verifier | no | no | no | batch only |

Two columns are ahead of the field: the provider catalogue with completion-based
verification, and per-route prompt profiles, which no reference tool documents an
equivalent of.

The pattern in the gaps is consistent and encouraging. Nearly everything missing
from `ay` **already exists in this repository** for the batch loop and was never
wired to the REPL. The sandbox class exists. The MCP client exists. The subagent
system exists. Checkpointing exists. This is integration work, not new
invention, which is a much better position than it looks from the matrix.

Hooks are the exception: `grep -ril hook harness/` returns nothing.

## What the literature actually supports

From `readiness-research.md`, with the evidence tier kept explicit.

- **Measured:** model choice predicts SWE-bench score more than scaffold choice.
  Agentless, a non-agentic pipeline, doubled the prior state of the art, which is
  real evidence against assuming more autonomy is better.
- **Measured:** context degrades well before the window is full, across 18
  models. Compaction is not merely an overflow guard.
- **Measured, and awkward for us:** self-correction without external feedback is
  weak or actively harmful. The `verification` dial in `PromptProfile` asks the
  model to check itself, which this literature says is the weaker form. The batch
  loop's independent verifier is the form the evidence supports, and `ay` does
  not have it.
- **Asserted, not measured:** most multi-agent guidance in either direction.
  Anthropic's 90.2% figure is real but methodology-free and for research fan-out
  rather than code editing; Cognition's argument against multi-agent is a
  position paper.

## On the test suite

16 tests fail on this Windows machine. I have called these environmental all
session, so here is the actual mechanism rather than the assertion.

- **2 sandbox tests**: `sandbox.py:140` adds `--user` only when `os.getuid`
  exists. Windows has no `getuid`, so the flag is absent and the assertion
  fails. On Linux, where Docker actually runs, it is present.
- **11 delivery tests**: fail at "run workspace is not a git repository" in the
  fixture, before reaching the code under test.
- **3 others**: patch recovery and Docker command construction, same shape.

CI on Linux is green on the merge commit, which is the load-bearing evidence.
The correct reading is not "16 known failures" but **"the delivery and sandbox
paths are untested on Windows"**. If you use `harness deliver` from this machine,
it is not covered by a passing test here.

## What to fix, in order

1. **F1, the deny-list.** Inspect interpreter arguments (`-c`, `/c`, `-lc`) and
   match on the normalized command rather than a contiguous argv window. Even
   after fixing, stop describing it as absolute: the honest claim is that the
   sandbox and the gate are the enforcement, and the deny-list is a guard rail.
2. **F2, redaction.** Generate the patterns from `auth.PROVIDERS` so adding a
   provider cannot silently add a leak.
3. **F3, regex timeout.** Cap `grep` execution, or run it in a killable worker.
4. **F4, reject an empty `old_string`.** A three-line guard.
5. **Wire the sandbox into `ay`.** The largest real gap, and the class already
   exists.
6. **Checkpointing for the REPL.** Aider's per-edit commit is the proven design
   and this repo already has the machinery.
7. **F5 and the ledger torn-write**, together, as robustness work.

Items 1 to 4 are small and well understood. They are what stands between this
being a good demonstration and something to trust with an unattended session.
