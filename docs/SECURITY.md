# Security model

The harness is a **teaching tool**, not a production security boundary. Its
containment is honest about what it is: process and path isolation, not a
kernel sandbox. This document states what the harness protects, how, and what
it does not claim to protect against.

## Layered defenses

| Layer | What it does | Bypass risk |
|---|---|---|
| Skill gate | The model may only request tools the skill enables | Low (config is operator-owned) |
| Tool registry | Only registered, typed tools exist; schemas validated | Low |
| Policy engine | Risk classes, command allow/deny lists, network toggle, approvals | Medium (allowlist is prefix-based) |
| Workspace | Canonical-path containment; all paths resolve inside the run copy | Low (symlink resolution is explicit) |
| Protected paths | Glob patterns for immutable files (e.g. `tests/**`) | Low |
| Subprocess | `shell=False`, no shell interpolation, process-group kill on timeout | Low |
| Sandbox | optional container: no network, no new privileges, capabilities dropped, non-root, bounded memory/CPU/pids, only the workspace mounted | Low when enabled |
| Output caps | Every tool result is truncated; oversized output goes to artifact store | Low |
| Redaction | Secrets scrubbed from events and artifacts | Medium (best-effort patterns) |
| Verifier | Acceptance commands + non-empty diff + protected-path integrity | Low |

## What the model cannot do

- Read or write anything outside the run workspace (path resolution rejects
  absolute and escaping paths).
- Execute arbitrary commands: only allowlisted command prefixes pass the
  policy gate, and there is no shell involved.
- Modify protected paths (e.g. the test suite).
- Make network requests: `browser_fetch` and `web_search` are disabled unless
  `network_enabled: true`, and even then only allowlisted domains are
  reachable, with SSRF guards against private/loopback/link-local addresses.
- Claim completion: `finish` always triggers the independent verifier.

## Provider security

- Credentials resolve through `harness/auth.py` and nowhere else. That module
  is the only one that holds a raw key, and it hands it to the provider adapter
  alone. `harness doctor` and the runner call the same function, so they cannot
  disagree about whether a route is ready.
- Precedence is the environment variable first, then the stored file.
  `harness auth status` prints which source won.
- The store is `~/.yatra-harness/auth.json`, outside the repository so it cannot
  be committed. It is written with mode 0600 on POSIX; on Windows it inherits
  the user profile ACL, which grants the owner, SYSTEM and Administrators only.
- A key resolved from the store is added to the `Redactor` exactly like an
  exported one, so it is scrubbed from the event ledger and artifacts.
- `harness auth add` accepts the key as an argument or prompts for it without
  echo. Passing a secret as an argument records it in shell history, so the
  prompt is the safer path on a shared machine.
- Credentials are never read from config files.
- `api_key_env` names the variable; the conventional `OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY` are used when unset.
- LLM Light (`profile_from_route`) strips endpoints and credential variable
  names from routing decisions, so the plan can be logged freely.
- The `Redactor` scrubs known key shapes (`sk-...`, `Bearer ...`, `gh*_...`)
  and sensitive field names from `events.jsonl`, `manifest.json`, the
  verification records, `result.json` and `summary.md`.
- Two files are deliberately **not** redacted, and both are local-only:
  `state.json`, because `completed_tool_calls` is replayed verbatim by
  `resume` and rewriting it would corrupt recovery; and `patch.diff`, because
  it must stay byte-exact to remain appliable. Treat a run directory as
  sensitive: share `summary.md`, not the whole bundle.

## Sub-agents

A delegated sub-agent is read-only, and the harness enforces it in two
places rather than trusting the skill author. A sub-agent skill enabling a
writing, executing-outside-the-allowlist or network tool is refused at config
load; and at run time a sub-agent runs under an approver that permits reading
and allowlisted commands and refuses writing and network. It works from a
copy of the parent workspace whose git remotes have been stripped, so it can
neither change the parent's tree nor publish from its own.

It inherits nothing from the operator's approval decisions. A nested run must
not be able to spend a yes given for something the operator never saw.

## Known limitations (stated honestly)

- **No kernel sandbox by default.** With `sandbox.kind: local`, a malicious
  model that gets an allowlisted command (e.g. a test runner with a clever
  argument) could in principle escape the workspace: the harness confines
  *the model's interface*, not the OS. Setting `sandbox.kind: docker` closes
  this — tool commands and acceptance commands then run in a throwaway
  container with no network, no new privileges, all capabilities dropped, a
  non-root uid, bounded memory/CPU/pids, and only the run workspace mounted.
  Local stays the default because a workshop laptop without docker must still
  be able to run the harness.
- **Prefix allowlist.** `allowed_commands` matches command prefixes, so a
  pathological model can reach a dangerous form as an argument to a command
  that is legitimately allowed. `denied_commands` closes the specific holes
  you can name -- it matches anywhere in the command, is checked first, and
  is never overridden by an approver -- but a deny-list only refuses what it
  has been told about. Production deployments should still run the harness in
  a container.
- **Best-effort redaction.** The redactor recognizes common secret shapes;
  it cannot guarantee no leakage from arbitrary model output, and it does not
  cover `state.json` or `patch.diff` (see above).
- **No network egress control at the OS level.** The policy gate is
  application-level.

## Recommended production hardening

1. Set `sandbox.kind: docker` with the shipped `Dockerfile`, and run the
   harness itself inside a container too.
2. Restrict egress with a firewall or network policy.
3. Replace the prefix allowlist with exact command hashes.
4. Use `approval_mode: always` with a human approver for write/execute tools.
5. Rotate keys and use short-lived credentials.
