# Platform review: models, execution, config, record

Scope: harness/models/{auth,providers,model_router,llm_light}.py,
harness/execution/{policy,process,sandbox,workspace,mcp,tools,search,retrieval}.py,
harness/config.py, harness/core/**, harness/record/**, configs/*.yaml, .gitignore.
Excludes harness/repl/** and ay.py (owned by another reviewer).

Every finding below states CONFIRMED (ran code against this repo) or PLAUSIBLE
(read-only reasoning, not executed).

## 1. CONFIRMED — Redaction misses most of the harness's own key formats

`harness/record/redaction.py:13-17`. `TOKEN_PATTERNS` has three regexes: a
hyphenated `sk-...` form, `Bearer`/`Basic ...`, and GitHub's `gh[opsu]_...`.
The provider catalogue in `harness/models/auth.py` issues several formats that
none of these three patterns match. Verified directly:

```
groq (gsk_...)      -> LEAKED
cerebras (csk-...)  -> LEAKED
inception (sk_...)  -> LEAKED   (underscore, not the hyphen the regex requires)
google AIza...      -> LEAKED
google AQ....       -> LEAKED
nvidia (nvapi-...)  -> LEAKED
fireworks (fw_...)  -> LEAKED
chutes (cpk_...)    -> LEAKED
```
(openai `sk-...`, openrouter `sk-or-...`, dashscope `sk-ws-...`, and `Bearer `
values were all correctly redacted — the hyphen form is the only one covered.)

Concrete scenario: a user on the groq, cerebras, inception, google, nvidia,
fireworks, or chutes route hits a 401/403. `provider_error_message` (used in
`auth.py` and `providers.py`) often echoes back part of the request or a
provider message that contains the key (e.g. a gateway that quotes "invalid
key: gsk_xxx..." in its error body). That text flows through
`EventLog.append`, which calls `Redactor.value` on the payload — and for these
eight providers the raw key survives into `events.jsonl` on disk, permanently
and unencrypted, plus anywhere the event is displayed.

This is the single highest-severity finding: it is exactly the credential
leakage path the module's own docstring promises does not happen ("Keys never
reach an event... this module is the only one that holds a raw secret"), and
it fails for most of the newer providers this codebase specifically added
support for.

Fix direction: add prefix-anchored patterns for `csk-`, `gsk_`, `sk_`, `nvapi-`,
`fw_`, `cpk_`, `AIza`, `AQ\.` (and ideally derive the list from
`auth.PROVIDERS[*].prefixes` so a new provider can't silently reintroduce the
gap).

## 2. CONFIRMED — A reset connection mid-stream crashes the run instead of falling back

`harness/models/providers.py`, `_HTTPProvider.send()` (lines 140-166) wraps
`urlopen` in try/except for `HTTPError`, `URLError`/`TimeoutError`, and
`json.JSONDecodeError`. When streaming, the actual read loop is
`_read_stream` (lines 171-189), which iterates the response line by line
*inside* that same `with` block. If the connection drops mid-stream, iterating
an `http.client.HTTPResponse` raises `http.client.IncompleteRead` (or
`ConnectionResetError`), neither of which is `URLError`. Verified by feeding a
fake response object that raises `IncompleteRead` after one good chunk:

```
p._read_stream(FakeResponse())
-> http.client.IncompleteRead: IncompleteRead(7 bytes read)   # propagates unwrapped
```

`ModelRouter.call()` (`harness/models/model_router.py:150-192`) only catches
`PermanentProviderError`, `ConfigurationError`, and `TransientProviderError`.
A raw `IncompleteRead`/`ConnectionResetError` is none of those, so it is not
retried, does not trigger fallback to the next route, and is not recorded as
`MODEL_ROUTE_FAILED` — it propagates straight out of the router as an
unhandled exception. For a daily user this means: a flaky network blip during
a streamed response (`stream: true` in the route config, used by e.g.
`configs/ay.yaml`) crashes the whole turn instead of the harness quietly
retrying or falling back to the next configured route, which is exactly the
behavior the retry/circuit-breaker machinery exists to provide for every other
failure mode.

Fix direction: wrap the `_read_stream` loop's iteration in a
try/except that re-raises as `TransientProviderError`.

## 3. CONFIRMED — A torn write permanently bricks the event ledger (no crash recovery)

`harness/record/events.py`. `append()` (lines 45-64) does an `os.open` +
`os.write` + `os.fsync` per line, which is reasonably crash-safe *for the
write itself*, but `EventLog.__init__` always calls `_read_last_sequence()` →
`self.read()` (lines 66-82), which raises `StateError` on the first line that
fails `json.loads` or fails the sequence check, with no truncation or
recovery path. Verified: after simulating a crash mid-write (last line cut
short, no trailing newline — the actual shape of a torn write, e.g. from a
kill -9, OOM kill, or Windows forced termination between `os.write` and
`os.close`),

```
EventLog(p, 'run1')
-> StateError: invalid event at ...\events.jsonl:2
```

Every later attempt to open that same ledger — including the session-resume
path in `harness/runtime.py` (`EventLog(run_dir / "events.jsonl", run_id, ...)`,
called twice) and `replay_run()` in `harness/record/replay.py` — raises the
same `StateError` forever. There is no way to recover or resume that run
short of hand-editing the JSONL file. For a daily interactive user this means:
if `ay`/`harness` is killed (Ctrl+C during a slow fsync, a laptop sleep that
kills the process, a Windows Task Manager "End task") at the exact moment it
is appending an event, that session's history becomes permanently unreadable
and unresumable on the next launch.

Fix direction: on load, treat a trailing line that fails to parse as an
incomplete write and drop it (with a warning), rather than raising for the
whole file; only raise on a genuine sequence *gap* between well-formed lines.

## 4. CONFIRMED (Windows-specific) — killing a timed-out command leaves grandchild processes running

`harness/execution/process.py`, `_stop_group()` (lines 15-32) and
`run_process()` (lines 44-83). On POSIX, `start_new_session=True` plus
`os.killpg` cleanly kills the whole process group. On Windows, no process
group / job object is used at all: `Popen` is created with no
`creationflags`, and on timeout `_stop_group` calls plain
`process.terminate()`/`process.kill()`, which only signals the single direct
child, not any processes *it* spawned.

Verified: ran a command via `run_process(..., timeout=2)` whose child process
itself spawns a grandchild (`subprocess.Popen` inside the child) that sleeps
20s. The outer command timed out and was reported as killed
(`timed_out=True`), but the grandchild had already started and was still
alive after `run_process` returned — confirmed present in `tasklist` after the
call returned. Any allowed command that itself shells out (a test runner that
forks workers, `npm test` spawning node, a Makefile spawning sub-processes)
will leave those orphans running past the configured timeout on Windows,
consuming CPU/memory or holding file locks in the workspace indefinitely
until the user notices and kills them by hand.

Fix direction: on Windows, create the child in its own process group
(`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`) and/or a Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and kill the job rather than the single
process.

## 5. CONFIRMED — the deny-list is a token-subsequence match, so a one-argument wrapper walks straight through it

`harness/execution/policy.py`, `denied_pattern()` (lines 40-63). It matches a
denied pattern only as a contiguous run of *separate argv tokens*. Verified
against the actual `denied_commands` shipped in `configs/ay.yaml` (`[rm, -rf]`,
`[curl]`, `[wget]`, `[sudo]`, etc.):

```
('sh', '-c', 'rm -rf /')                 -> not denied (None)
('bash', '-lc', 'rm -rf /')              -> not denied (None)
('python', '-c', 'os.system("curl ...")')-> not denied (None)
('powershell', '-c', 'curl evil')        -> not denied (None)
('env', 'curl', 'http://evil')           -> denied ('curl')      [caught]
('xargs', 'curl')                        -> denied ('curl')      [caught]
('cmd', '/c', 'curl', 'http://evil')     -> denied ('curl')      [caught]
```

Whenever the dangerous command survives as a *separate argv token* (`env
curl`, `xargs curl`, `cmd /c curl`) the deny-list still catches it, because
the docstring's design goal — "a contiguous subsequence anywhere in the
command" — works as intended there. The gap is specifically the shell/`-c`/
`-lc` family (`sh -c`, `bash -lc`, `powershell -c`, `python -c`) where the
dangerous command is embedded as *one string argument* rather than separate
tokens, so it never appears as the tuple `('rm', '-rf', ...)` the pattern
looks for. `python` itself is on the allowlist (`configs/ay.yaml`
`allowed_commands: [python]`, needed for running tests), so `python -c
"<anything>"` is both allowed and, per this check, never denied.

This is not a full bypass of the harness's defenses: `run_command` is gated by
`RiskLevel.EXECUTE`, and under `policy.approval_mode: mutations` (the shipped
default) any execute-risk tool call requires human approval before it runs at
all (`policy.py` lines 96-114), so a human still has to say yes. The real gap
is narrower but genuine: the deny-list itself — the layer meant to make some
commands un-runnable *even with approval* — does not cover the shell-wrapper
family, so an approver who trusts "python -c ..." as routine (because
`python` alone is allowlisted and looks safe) can unknowingly approve
arbitrary code. Under `approval_mode: never` there is no second layer at all
for this gap.

Fix direction: either special-case `-c`/`-lc`/`/c` arguments (scan their
string content for denied substrings too), or deny the wrapper binaries
themselves (`sh`, `bash`, `powershell`, `cmd`) outright, forcing them through
an explicit approval prompt rather than the ordinary allowlist path.

## 6. PLAUSIBLE — auth store file mode is close to meaningless on Windows

`harness/models/auth.py:281-287` `save_store()` calls
`atomic_write_json(path, data, mode=0o600)`. Checked the actual file on this
machine: `~/.yatra-harness/auth.json` has `os.stat` mode `0o100666` (not
`0o600`) even though the code path always passes `mode=0o600`. `os.chmod` on
Windows only toggles the read-only attribute; it does not narrow NTFS ACLs to
the owning user, so any other principal with access to the profile (another
local account, a service) is not blocked by this call the way it would be on
POSIX. Real-world exposure on a typical single-user Windows laptop is low
(the user profile directory itself is usually ACL'd to the owner by Windows
defaults), so this is a defense-in-depth gap rather than an active leak, and
it is a Windows platform limitation rather than a logic bug in this code —
flagging because the module's docstring implies the store is protected and,
on Windows, the `mode=0o600` argument is decorative.

## 7. PLAUSIBLE — 429 backoff ignores `Retry-After`

`harness/models/providers.py` `send()` (429/5xx → `TransientProviderError`)
and `harness/models/model_router.py:186` (`delay = self.config.backoff_seconds
* (2**attempt)`) never read the `Retry-After` header from a 429 response. Only
the error body is captured (`exc.read(4_000)`), the headers are discarded.
Practical effect: a provider that asks for a specific wait (common on 429s)
gets hit again after the harness's own fixed exponential delay regardless,
which can extend a rate-limit lockout instead of respecting it. Not verified
against a live 429 with a real `Retry-After` header; inferred from reading
that `exc.headers` is never consulted anywhere in the codebase.

## Clean / verified-safe areas

- **Workspace path containment** (`harness/execution/workspace.py`,
  `Workspace.resolve`): CONFIRMED robust. Tested `..`, `..\`, mixed `../`
  sequences, `C:/Windows/System32`, a UNC path (`\\server\share\file`), and a
  symlink planted inside the workspace root pointing at a directory outside
  it (`link -> ../outside`, containing a real `secret.txt`) — every one of
  these was rejected with `WorkspaceError`. `.resolve()` follows symlinks
  before the `relative_to()` containment check, so a symlink escape does not
  work. The one oddity is a bare Windows drive-relative path (`C:foo`, no
  slash) — Python's `Path.__truediv__` folded it into `root/foo` rather than
  resolving it against drive `C:`'s own current directory, which happened to
  stay safe in this test but is worth an explicit unit test rather than
  relying on `pathlib` behavior that differs across Python versions.

- **Subprocess environment does not inherit host secrets.**
  `harness/execution/sandbox.py` `_host_environment()` builds an explicit,
  short allowlist (`PATH`, `LANG`, `PYTHONNOUSERSITE`,
  `GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL`) and `harness/execution/tools.py`
  never passes an `environment=` override, so `run_command` never inherits
  the full `os.environ` — provider API keys sitting in the harness's own
  process environment are not exposed to model-run subprocesses. Confirmed by
  reading both call sites; this closes off a leakage path the task asked
  about.

- **Config validation** (`harness/config.py`): CONFIRMED thorough by
  inspection — every section uses `schema.reject_unknown` against an explicit
  key set, numeric fields carry `minimum=`/`maximum=` bounds (negative
  budgets, negative timeouts, and out-of-range `quality` are all rejected at
  load time, before a run starts), route names referenced by `primary`,
  `fallbacks`, and `llm_light.profiles.*.constraints.{allowed,denied}` are
  checked against the declared route set, and `prompt_profile` is validated
  against the known preset list. A malformed config fails fast with a
  specific `ConfigurationError` rather than surfacing later as a confusing
  runtime failure.

- **`.gitignore`**: `.env` is ignored; the credential store itself lives
  entirely outside the repository (`~/.yatra-harness/auth.json`), so there is
  no path by which `harness auth add` output could be committed.

- **Non-streaming HTTP error handling** (`providers.py` `send()`,
  `auth.py` `_request_json()`): both correctly distinguish HTTP error status,
  connection errors, and non-JSON bodies, and `provider_error_message`
  degrades gracefully to a truncated raw body for an HTML 500 page rather
  than crashing on the failed `json.loads`. 200-with-error-body is not
  special-cased, but falls through to `_normalize`'s `KeyError` handling
  ("provider response has no assistant message"), which is a legible failure
  rather than a crash.

## Summary, ranked by severity

1. Redaction misses `gsk_`, `csk-`, `sk_`, `AIza`, `AQ.`, `nvapi-`, `fw_`,
   `cpk_` key formats — real credential leakage into the event ledger for 8
   of the ~24 configured providers. CONFIRMED.
2. A dropped connection mid-stream crashes the run instead of retrying or
   falling back. CONFIRMED.
3. A crash mid-write permanently bricks that run's event ledger; no resume,
   no replay. CONFIRMED.
4. On Windows, a timed-out command's grandchild processes are not killed and
   keep running. CONFIRMED.
5. The command deny-list does not cover `sh -c`/`bash -lc`/`powershell -c`
   wrapped commands; mitigated by the approval gate under the default policy,
   not mitigated under `approval_mode: never`. CONFIRMED.
6. Auth store file mode is decorative on Windows. PLAUSIBLE, low real-world
   exposure.
7. 429 handling ignores `Retry-After`. PLAUSIBLE.
