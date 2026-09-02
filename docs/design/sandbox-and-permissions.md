# OS-level sandboxing and permission rules: research and implementation design

This document has three parts. Part 1 researches how a child process can be
confined without Docker, per platform, and how two real agent harnesses
(OpenAI Codex CLI, Claude Code) do it. Part 2 researches Claude Code's
permission-rule syntax and designs an equivalent for this repo's YAML config.
Part 3 turns both into a staged implementation plan naming real files and
functions in this codebase.

Everything marked "verified" was read directly from primary source (vendor
docs or the `man7.org` kernel page). Everything marked "reported" came from a
secondary source (a blog, a wiki, a search summary) that was not
cross-checked against primary source, usually because the primary source
returned an error (see the Codex Windows note in Part 1). Treat "reported"
claims as good leads, not settled facts, when the implementation stage
depends on them.

## Current state of this repo (baseline, verified by reading source)

- `harness/execution/sandbox.py` defines `LocalSandbox` (a plain
  `subprocess`-based `run_process` call, see `harness/execution/process.py`)
  and `DockerSandbox`. `build_sandbox(config)` picks one by
  `config.kind` (`"local"` or `"docker"` — see `KINDS` at line 31). There is
  no third kind. `LocalSandbox.run` applies no OS-level confinement at all:
  it is `run_process(command, cwd=workspace, ...)`.
- `harness/execution/policy.py` has `PolicyEngine`, a 25-pattern
  `denied_commands` deny-list checked by `denied_pattern()`, a prefix
  `allowed_commands` allowlist consulted only by `_command_allowed`
  (batch path), and `expand_command` / `normalize_command` /
  `carried_code`, which unwrap shell wrappers (`bash -c`, `sudo`, interpreter
  `-c`/`-e` flags) so the deny-list sees what actually executes.
- `harness/repl/approvals.py`'s `Gate` has three `Mode`s (`SUGGEST`,
  `AUTO_EDIT`, `FULL_AUTO`) and a `_hard_refusal` check that runs the same
  `denied_pattern` before any approval prompt. There is no per-command or
  per-path rule system beyond the deny-list and the three coarse modes.
- `harness/config.py` loads `SandboxConfig` and `PolicyConfig` through
  `harness/core/schema.py`'s strict helpers (`schema.reject_unknown` rejects
  any key not in an explicit allow-set — see `sandbox_config_from_dict` at
  `harness/execution/sandbox.py:195` and the `policy` block in
  `load_config` at `harness/config.py:288`).
- `docs/SECURITY.md` already states the honest baseline: "No kernel sandbox
  by default," Docker is the only real boundary, and the allowlist/deny-list
  are application-level, not OS-level.

This design closes that gap for macOS and Linux, and states plainly why it
cannot be closed the same way on Windows.

---

## Part 1: OS-level sandboxing per platform

### macOS: Seatbelt (`sandbox-exec -p <profile>`)

Seatbelt is Apple's kernel-level MAC (mandatory access control) framework,
exposed to unprivileged processes through the `sandbox-exec` CLI and the
`sandbox_init()` C function. `sandbox-exec` launches a child process under a
named or inline S-expression profile.

Verified from search results (Apple's own man page text, quoted by multiple
sources including the GitHub issue on `apple/containerization#737`):
`sandbox-exec` is formally deprecated — the man page says "consider adopting
App Sandbox instead" — but there is no replacement for sandboxing an
arbitrary, non-code-signed CLI subprocess outside the App Store. App Sandbox
requires code-signing with entitlements and an Xcode project; that model does
not fit a Python CLI shelling out to a user's toolchain. This is exactly why
Codex CLI and Claude Code both still use it: it is deprecated but has no
non-Docker successor for this use case. The risk is that Apple could remove
`sandbox-exec` in some future macOS release with no notice beyond the
deprecation warning; there is no committed removal date as of this research.

A profile that allows reads everywhere, restricts writes to one directory
tree, and denies network looks like this (S-expression syntax, standard
Seatbelt idiom confirmed by multiple independent sources):

```
(version 1)
(deny default)
(allow file-read*)
(allow file-write*
  (subpath "/absolute/path/to/workspace"))
(deny network*)
(allow process-fork)
(allow signal (target self))
```

`(deny default)` is the baseline — everything is denied unless a later rule
allows it. `file-read*` and `file-write*` are separate permission classes;
granting one does not grant the other. `subpath` restricts a rule to a
directory subtree. `network*` covers both inbound and outbound; Claude Code's
own sandbox instead does network control via an external proxy rather than a
Seatbelt network rule, because Seatbelt's network primitives are coarse (allow
or deny, not domain-filtered) — domain filtering has to happen at a layer
above the kernel sandbox.

What Seatbelt can and cannot enforce:
- **Filesystem read**: yes, per-subpath, via `file-read*`.
- **Filesystem write**: yes, per-subpath, via `file-write*`.
- **Network**: yes, but only allow/deny, not per-domain. Domain-level
  filtering needs a proxy in front of the allowed network path.
- **Process spawn**: `process-fork` / `process-exec` rules exist but a forked
  child inherits the same profile, so this is about permitting the fork
  itself, not escaping the sandbox by forking.
- **Failure mode when unsupported**: not a kernel-support question on macOS
  the way it is on Linux — Seatbelt has shipped on every shipping macOS
  version relevant here. The realistic failure mode is a syntax error in the
  profile, which makes `sandbox-exec` refuse to start the child at all (fails
  closed), or a profile that is technically valid but omits a rule a tool
  needs, which shows up as an EPERM inside the sandboxed process rather than
  at launch.

Sources: [mxc/docs/macos-support/seatbelt-backend.md](https://github.com/microsoft/mxc/blob/main/docs/macos-support/seatbelt-backend.md), [apple/containerization#737](https://github.com/apple/containerization/issues/737), [Sandboxing an AI Harness on macOS](https://alejandromp.com/development/blog/sandboxing-an-ai-harness-on-macos/), [A deep dive on agent sandboxes](https://pierce.dev/notes/a-deep-dive-on-agent-sandboxes).

### Linux: Landlock and seccomp

Verified from `man7.org/linux/man-pages/man7/landlock.7.html` (primary
source, fetched directly):

Landlock is an unprivileged LSM, usable by any process without root, that
restricts what *that process and its descendants* can do — it can only
narrow rights the process already has, never grant new ones. It is
ABI-versioned, and each ABI version added a capability the previous one
lacked:

| ABI | Kernel | Adds |
|---|---|---|
| v1 | 5.13 | filesystem: execute, read/write files, read directories, create/remove file types |
| v2 | 5.19 | `LANDLOCK_ACCESS_FS_REFER` — linking/renaming across directories |
| v3 | 6.2 | `LANDLOCK_ACCESS_FS_TRUNCATE` |
| v4 | 6.7 | **network**: `LANDLOCK_ACCESS_NET_BIND_TCP`, `LANDLOCK_ACCESS_NET_CONNECT_TCP` |
| v5 | 6.10 | `LANDLOCK_ACCESS_FS_IOCTL_DEV` |
| v6 | (reported, not independently verified from the man page fetch) | scoping for signals and abstract Unix sockets |

**Landlock cannot restrict network before ABI v4 (kernel 6.7).** On any
kernel between 5.13 and 6.6, Landlock is filesystem-only; a program relying
on it for network denial on those kernels gets no network restriction at all
unless it also uses something else (see below). This matters directly for
this repo's target: Docker images and CI runners frequently pin kernels well
below 6.7, and a developer's WSL2 or a stock Ubuntu LTS may not have v4
either — Ubuntu 24.04 ships a 6.8 kernel, so it is close to the line but not
guaranteed, and this must be probed at runtime, not assumed from the distro
name.

Failure mode when the kernel does not support Landlock at all:
`landlock_create_ruleset()` returns `-1` (fails closed, i.e., returns an
error the caller must handle — it does not silently no-op). A correct caller
checks this and falls back explicitly; it does not get silent unrestricted
execution. This is the single most important correctness property for this
design: **the fallback path must be an explicit code branch, not "the syscall
failed so nothing happened."**

Alternatives and how they combine, from the same research:
- **seccomp-bpf**: restricts which syscalls a process may make at all (not
  path-scoped — a much blunter tool than Landlock). Useful as a second layer:
  block `socket()` outright for "no network" regardless of Landlock ABI.
- **bubblewrap (`bwrap`)**: an unprivileged sandboxing tool built on
  namespaces (mount, PID, user, network). It does the "no network" job on
  every kernel that supports user namespaces (widely available since ~2013,
  well before Landlock existed) by simply not creating a network namespace
  interface, i.e. `--unshare-net` with no veth — a much older and more
  portable technique than Landlock ABI v4. Claude Code's own Linux/WSL2
  sandbox uses bubblewrap as primary, not Landlock, for exactly this reason
  (verified: "the sandbox relies on two packages: bubblewrap... and socat,"
  from `code.claude.com/docs/en/sandboxing`).
- **`unshare` (the syscall and the CLI)**: the lower-level primitive
  bubblewrap is built on. Usable directly (`unshare --net --map-root-user`)
  but bubblewrap adds the mount-namespace filesystem view on top, which is
  what "read everywhere, write only in workspace" actually needs — plain
  `unshare` alone does not give you selective filesystem read/write, only
  namespace isolation.

Recommended combination for this repo: **bubblewrap for filesystem+network
namespace isolation, unconditionally available on any Linux with unprivileged
user namespaces enabled (the same requirement Claude Code documents); Landlock
as an optional additional layer when ABI v4+ is present, for defense in
depth; seccomp as a belt-and-suspenders network blocker.** This mirrors what
both Codex (reported: bubblewrap primary, Landlock as an explicit legacy
fallback via `features.use_legacy_landlock`, seccomp for
`PR_SET_NO_NEW_PRIVS` and a network filter) and Claude Code (verified:
bubblewrap primary) actually ship, not a novel design.

One operational gotcha, verified from Claude Code's own docs: on Ubuntu
24.04+, the default AppArmor policy blocks bubblewrap from creating the user
namespaces it needs (`kernel.apparmor_restrict_unprivileged_userns=1`), and
needs an AppArmor profile drop-in to unblock — this is a real deployment
trap, not a hypothetical.

Sources: [landlock(7)](https://man7.org/linux/man-pages/man7/landlock.7.html), [Landlock kernel docs](https://docs.kernel.org/userspace-api/landlock.html), [code.claude.com/docs/en/sandboxing](https://code.claude.com/docs/en/sandboxing).

### Windows: honestly, very little from pure Python

This is the platform where a hopeful answer would be actively wrong, so it
gets stated plainly first: **there is no pure-Python, no-native-code
mechanism on Windows that gives filesystem/network confinement comparable to
Seatbelt or Landlock+bwrap.** Every real option either needs a compiled
extension / ctypes calls into Win32 security APIs, or is not a per-command
sandbox at all.

What exists, reported from search results (the primary source,
`openai.com/index/building-codex-windows-sandbox/`, returned HTTP 403 to
WebFetch and could not be independently verified — everything below is
secondary-sourced and should be treated as a lead, not a settled fact, until
someone reads that page directly or reads `codex-rs`'s Windows sandbox crate):

- **Job objects** (`CreateJobObject` / `AssignProcessToJobObject`, reachable
  from Python via `pywin32`'s `win32job` module without any custom native
  code — this part *is* pure-Python-reachable). Job objects enforce
  **process lifetime and count limits, and optionally a memory ceiling**.
  They do **not** restrict filesystem or network access at all. This is the
  Windows analogue of Docker's `--pids-limit` and `--memory`, not of its
  filesystem or network isolation.
- **Restricted tokens** (`CreateRestrictedToken` via `ctypes` calls into
  `advapi32.dll` — no `pywin32` wrapper ships this by default, so this
  requires either hand-written `ctypes` bindings or a small native helper).
  A write-restricted token adds an extra access check on write operations:
  the normal ACL check still applies, *and* at least one SID in the token's
  restricted-SID list must independently be granted access. Combined with a
  synthetic SID that has no ACL entries anywhere except the workspace
  directory, this can approximate "write only inside the workspace" — but
  building and threading a custom token through `CreateProcessAsUser` is
  meaningfully more Win32-API surface than this codebase currently touches
  anywhere, and it does not touch network access at all (a write-restricted
  token does not restrict `socket()`).
- **AppContainer**: the real Windows analogue of Seatbelt/Landlock — genuine
  filesystem and registry virtualization plus network isolation via
  capability SIDs. This is what Chromium and (reported) Codex CLI actually
  use for meaningful isolation. It requires `CreateAppContainerProfile` and a
  capability-SID list, callable via `ctypes`/`win32security`, but setting it
  up correctly (profile creation, ACLing the allowed directories for the
  AppContainer SID, handling firewall rules for network denial) is
  substantial native-API work — not something to build from scratch inside
  this harness's Python codebase as a first pass. The Codex Windows writeup
  (reported) also mentions synthetic SIDs, dedicated sandbox users, and
  firewall segmentation on top of AppContainer, which reinforces that this is
  a multi-primitive system, not one call.
- **Windows Sandbox** (`WSB`, the lightweight-VM feature): genuine full
  isolation, but it is a whole separate desktop session that takes seconds to
  boot per invocation, requires Windows Pro/Enterprise/Education and Hyper-V,
  and is not designed to run one shell command and return — it is
  architecturally the same shape as the existing `DockerSandbox`, not a
  lighter alternative to it. If Windows containment matters more than "no
  Docker," this is the thing to reach for, not raw Win32 APIs.

**Conclusion for this repo, stated as the design constraint it is: on
Windows, Docker remains the only sandbox this harness should claim to
provide.** A `LocalSandbox` running directly on Windows should not claim any
OS-level confinement, exactly as it does not today. Building a
job-object-plus-restricted-token approximation is possible but would give a
false sense of safety — it stops neither network exfiltration (job objects
don't touch it, restricted tokens don't touch it) nor determined filesystem
escape (a restricted token can be built wrong in ways that are hard to test).
Codex CLI reportedly ships real Windows containment, but by their own
description it required dedicated native Win32 engineering (synthetic SIDs,
AppContainer, firewall rules) well beyond a "confine a subprocess" patch —
this is consistent with Claude Code's own documented decision to not support
native Windows at all and instead require WSL2 (verified: "Native Windows is
not supported. On Windows, run Claude Code inside a WSL2 distribution," from
`code.claude.com/docs/en/sandboxing`). That is the industry's actual answer
to "how do you sandbox on Windows without Docker": you don't; you use WSL2
(which is a Linux kernel, so it's the Linux answer in a compatibility box) or
a container.

Sources (reported, not independently verified — primary source blocked the
fetch): [Building a safe, effective sandbox to enable Codex on Windows](https://openai.com/index/building-codex-windows-sandbox/) (403 on fetch), [The Windows Sandbox Deep Dive — Codex CLI](https://codex.danielvaughan.com/2026/07/18/codex-cli-windows-sandbox-architecture-powershell-ast-safety-elevated-unelevated-appcontainer-restricted-tokens/), [A Deep Dive into Codex Windows Sandbox](https://jonny-johnson.medium.com/a-deep-dive-into-codex-windows-sandbox-a2489bf4ae91). Verified directly: [AppContainer for legacy apps — Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-for-legacy-applications-), job-object API shape from [pywin32 win32job.i](https://github.com/mhammond/pywin32/blob/main/win32/src/win32job.i).

### How Codex CLI does it (github.com/openai/codex)

Reported/partially verified via `codex-rs` repository fetches:

- Rust codebase (~95% Rust, reported), with a dedicated sandboxing layer per
  platform rather than one cross-platform abstraction. `CODEX_SANDBOX=seatbelt`
  is set as an env marker on the sandboxed child on macOS.
- **Linux** (`codex-rs/linux-sandbox`, fetched directly): bubblewrap is the
  primary and default filesystem sandbox, with the first `bwrap` on `PATH`
  preferred and a bundled fallback if none is found; Landlock is available
  only as an explicit legacy opt-in (`features.use_legacy_landlock`); seccomp
  applies `PR_SET_NO_NEW_PRIVS` and a network filter in-process, and blocks
  new `AF_UNIX`/socketpair creation in managed-proxy mode; user and PID
  namespaces are explicitly unshared (`--unshare-user`, `--unshare-pid`), and
  network namespace unshared (`--unshare-net`) when network is restricted.
  This is the same shape recommended above independent of reading Codex's
  code: bubblewrap first, Landlock as a bonus layer, not the primary
  mechanism.
- **macOS**: Seatbelt, same `sandbox-exec -p` mechanism described above.
- **Windows**: restricted tokens, synthetic SIDs, AppContainer, firewall
  segmentation (reported, not independently verified — see caveat above).

Source: [codex-rs linux-sandbox (fetched directly)](https://github.com/openai/codex/tree/main/codex-rs/linux-sandbox), [openai/codex AGENTS.md](https://github.com/openai/codex/blob/main/AGENTS.md).

### What Claude Code documents (code.claude.com/docs/en/sandboxing — fetched directly, verified)

- Platforms: **macOS, Linux, WSL2. Native Windows is explicitly not
  supported**; the documented answer for Windows users is to run inside
  WSL2.
- macOS: Seatbelt, nothing to install.
- Linux/WSL2: **bubblewrap** for filesystem isolation plus **socat** for
  routing sandboxed network traffic through an external proxy process; an
  optional seccomp filter (`@anthropic-ai/sandbox-runtime`) adds Unix-domain-socket
  blocking.
- Filesystem model: default write access is the working directory + added
  directories + session temp dir; default *read* access is "the entire
  computer" except explicitly denied paths — read is intentionally broad,
  write is intentionally narrow. This is a materially different default from
  Docker's "nothing mounted except workspace" model that `DockerSandbox`
  documents in this repo, and is worth adopting: broad read + narrow write
  matches how a real toolchain (compilers, linters reading site-packages,
  language servers reading `~/.cache`) actually behaves, without needing an
  explicit allowlist of every library path.
- Network model: **not enforced by Seatbelt/Landlock network primitives
  directly** — enforced by an external proxy process that all sandboxed
  traffic is routed through, with a per-domain allow/deny list evaluated
  against the requested hostname. By default TLS is not terminated/inspected,
  so this is hostname-based filtering, not content filtering — the docs
  explicitly warn this is bypassable via domain fronting if the threat model
  needs stronger guarantees.
- Explicit warning content worth carrying into this repo's own docs: sandbox
  strength depends on *both* layers together — filesystem isolation without
  network isolation lets a compromised process exfiltrate secrets it can
  still read; network isolation without filesystem isolation lets it plant a
  backdoor (modify `~/.bashrc`, a `$PATH` executable) that a later,
  unsandboxed run will execute.

Source: [code.claude.com/docs/en/sandboxing](https://code.claude.com/docs/en/sandboxing).

---

## Part 2: permission rules

### Claude Code's syntax and semantics (fetched directly from `code.claude.com/docs/en/permissions`, verified)

Core facts, condensed to what a design needs:

- Rule shape: `Tool` (bare, matches every use) or `Tool(specifier)`.
  `Bash(git push *)` is a specifier rule: `*` matches any text including
  spaces, exactly one wildcard span per position, and **the words before the
  first `*` are what the rule actually restricts to** — `Bash(git * main)`
  matches every git subcommand (the `*` stands in for the subcommand, so it
  is nearly unrestricted), while `Bash(git log * main)` is meaningfully
  narrower. This is a sharp edge the docs call out explicitly with a warning,
  and it is exactly the kind of mistake a permissive-looking rule can hide.
- Three buckets: `allow`, `ask`, `deny`, each a list of rules.
- **Precedence: deny first, then ask, then allow. First match wins, and rule
  specificity never overrides this order** — a broad `Bash(aws *)` deny
  blocks a narrower `Bash(aws s3 ls)` allow. This is the single most
  important semantic to carry over: specificity does not win, list order
  (deny > ask > allow) does.
- A **bare tool name** deny (`"deny": ["Bash"]`) removes the tool from the
  model's context entirely — the model never sees it as callable. A
  **scoped** deny (`"deny": ["Bash(rm *)"]`) leaves the tool available and
  blocks only matching calls.
- Bash-specific matching is command-string-aware, not naive substring: it
  understands shell operators (`&&`, `||`, `;`, `|`, `&`, newlines) and
  requires *each* subcommand in a compound command to independently clear a
  rule; it strips a fixed set of process wrappers (`timeout`, `time`, `nice`,
  `nohup`, `stdbuf`, shell `command`/`builtin`) before matching, and strips a
  leading known-safe env-var assignment for allow rules only (`FOO=bar rm`
  still matches a deny rule for `rm`, because deny/ask always match past a
  leading assignment while allow does not). This is structurally very close
  to what `harness/execution/policy.py`'s `expand_command` /
  `normalize_command` / `COMMAND_PREFIXES` already do in this repo — the
  repo's unwrapping logic (`sudo`, `env`, shell `-c`, interpreter `-c`/`-e`)
  is the same idea, independently arrived at.
- Read/Edit rules use gitignore-style path patterns with anchor prefixes
  (`//` = filesystem root, `~/` = home, `/` = settings-source root, bare =
  cwd-relative).
- Scoping: rules live in settings files at multiple levels — user
  (`~/.claude/settings.json`), project (`.claude/settings.json`, checked into
  the repo and shared), project-local (`.claude/settings.local.json`,
  gitignored, personal), and managed/enterprise (highest precedence, cannot
  be overridden by any lower level, not even CLI flags). **Deny from any
  scope beats allow from any other scope** — a user-level deny blocks a
  project-level allow.
- Deny/ask rules interact with hooks: a `PreToolUse` hook cannot override a
  deny or ask rule; hook decisions are consulted but the rule evaluation
  still runs afterward for deny/ask.

### Equivalent design for this repo

The repo's config style is strict YAML with `schema.reject_unknown` at every
level (verified in `harness/config.py` and `harness/core/schema.py`) — no
free-form JSON blob, no implicit keys. The rule design below is shaped to fit
that, not to reproduce Claude Code's JSON verbatim.

**Rule syntax.** A rule is a mapping, not a compact string, because the
schema helpers validate mappings field-by-field and a compact
`"Bash(git push *)"` string would need its own parser invented from scratch
inside this repo, duplicating what `expand_command`/`_matches` already do for
space-separated token lists:

```yaml
policy:
  rules:
    - tool: run_command
      match: [git, push, "*"]
      action: deny
    - tool: run_command
      match: [git, commit, "*"]
      action: allow
    - tool: run_command
      match: [npm, run, "*"]
      action: allow
    - tool: write_file
      path: "src/**"
      action: ask
    - tool: write_file
      path: "tests/**"
      action: deny
```

- `tool` names a registered `ToolSpec` (`run_command`, `write_file`,
  `edit_file`, `browser_fetch`, ...) — validated against the same tool
  registry the skill gate already checks (`PolicyEngine.evaluate` at
  `harness/execution/policy.py:308` already knows `tool.name`).
- `match` (for `run_command`) is a token list reusing the existing
  `schema.command_list`/`string_list` shape and the existing `_matches`
  ordered-subsequence semantics from `policy.py:241` — `"*"` as a bare token
  is a new wildcard the matcher does not currently support and is the one
  real addition `_matches` needs (see Part 3, Stage 2).
- `path` (for file tools) is a glob matched with `pathlib.PurePath.match` or
  `fnmatch`, anchored at the workspace root the same way `write_file`/
  `edit_file` already resolve paths (need to check the existing path
  containment code — this reuses it rather than adding a second resolver).
- `action` is `allow | ask | deny`.

**Matching semantics.** For `run_command`, `match` is checked against every
candidate `expand_command()` produces (so `bash -c "git push"` is caught by a
rule written for `git push`, exactly as the existing deny-list already is).
For file tools, `path` is checked against the argument path after the
existing containment/normalization step.

**Precedence.** Deny beats ask beats allow, first match in that order wins,
list order does not matter beyond bucket — this is a direct port of Claude
Code's rule, because it is the right rule (specific allow rules should never
be able to carve exceptions out of a deny), and because getting it wrong is
the exact mistake `docs/SECURITY.md` already warns the existing deny-list
guards against ("a human clicking yes on a prompt is exactly the mistake it
exists to prevent" — `harness/repl/approvals.py:8`).

Concretely, `rules` is evaluated as three implicit buckets by `action`, in
this order, first match wins:
1. Any rule with `action: deny` matching the call → refuse, no approval
   offered (same shape as today's `denied_pattern` hard refusal).
2. Any rule with `action: ask` matching the call → force the approval
   gate even in `FULL_AUTO` mode (a new capability — today `FULL_AUTO`
   cannot be made to ask about anything).
3. Any rule with `action: allow` matching the call → skip the approval gate
   even in `SUGGEST` mode (a new capability — today only the mode governs
   this, per-command).
4. No rule matches → fall through to the existing `Mode`-based behavior
   unchanged.

**Interaction with the existing deny-list.** `policy.denied_commands` stays
exactly as it is and is checked *before* `rules` — it is the one list no
config file, no rule, and no approval can override (this is already true and
should stay true; it is the safety rail for "damage not recoverable by
saying no next time," per the existing docstring in `harness/execution/policy.py:1`).
`rules` sits between the hard deny-list and the `Mode`-driven default,
narrower in scope than the deny-list (which matches anywhere in a command)
and more expressive (it can also `allow` and `ask`, which the deny-list
cannot).

**Interaction with the three approval modes.** `Mode` remains the default
when no rule matches. A matching `allow` or `deny` rule overrides the mode
outright (this mirrors Claude Code: a sandbox boundary or a rule substitutes
for the mode-driven prompt). A matching `ask` rule forces a prompt regardless
of mode, including `FULL_AUTO` — this is new behavior `Gate._must_ask`
(`harness/repl/approvals.py:163`) does not have today, since today `FULL_AUTO`
always returns `False` unconditionally.

**Scoping.** Two tiers fit this repo's existing file layout without
inventing a new settings-discovery mechanism:
- **Project**: `rules:` inside the config file passed to `ay` or
  `harness run` (`configs/ay.yaml` today). Checked into git, shared.
- **User**: an optional `~/.yatra-harness/rules.yaml` (same directory
  `docs/SECURITY.md` already documents for `auth.json`), loaded and merged
  ahead of project rules, so a user-level deny cannot be overridden by a
  project-level allow — same "deny from any scope wins" principle as Claude
  Code, implemented as a merge-then-evaluate step rather than a settings
  hierarchy, since this repo has no existing multi-file settings precedence
  system to hook into.

This repo has no enterprise/managed-settings tier and none is proposed —
there is no multi-developer fleet management surface here to justify one,
and adding it would be scope beyond what the task needs.

---

## Part 3: staged implementation plan for this codebase

Ground rule for every stage below: **CI runs Linux, the developer's machine
is Windows.** Every stage must be testable on Windows even though its
sandboxing effect is Linux/macOS-only, which means every stage's *pure logic*
(profile string construction, rule matching, config parsing) has to be
covered by tests with no OS dependency, exactly the same pattern
`docker_command()` already uses today — it is a pure function tested on every
machine, and only the test that starts a real container needs Docker
installed (`harness/execution/sandbox.py:1-19`, `130-172` explains this
design choice already).

### Stage 0: nothing changes for Windows — verify the boundary claim

Before adding any code, add one thing to `docs/SECURITY.md`: an explicit
statement that `sandbox.kind: local` on native Windows provides no OS-level
confinement beyond what is already true today, and that Docker is the only
sandbox with real teeth on Windows. This is not a code change, but it should
land before or alongside Stage 1 so the new macOS/Linux capability does not
imply a Windows capability that does not exist. (Out of scope for this task
per the instructions — "do NOT modify any other file" — noted here as the
first real stage of a follow-up PR, not something this document's author
does.)

### Stage 1: `SandboxConfig.kind` gains `"os"`, platform dispatch, Linux-first

Files: `harness/execution/sandbox.py`, `harness/config.py` (no changes
needed there beyond what `sandbox_config_from_dict` already validates, since
`kind` is already a free-standing string check).

- Add `"os"` to `KINDS` (line 31).
- Add `OsSandbox` class alongside `LocalSandbox`/`DockerSandbox`, with a
  `run()` that dispatches on `platform.system()`:
  - Linux: build a `bwrap` argv (analogous to `docker_command()`, and just as
    pure/testable — construct the argv list, do not invoke `bwrap` in the
    function under test). Bind-mount the workspace read-write, bind-mount
    `/usr`, `/lib`, `/lib64`, `/bin`, `/etc/resolv.conf` (or similar) read-only
    for toolchain access, `--unshare-net` unless `network` is enabled,
    `--unshare-pid`, `--die-with-parent`. New config field
    `sandbox.network: none|host` mirrors the Docker config's existing
    `network` field for consistency.
  - macOS: build a Seatbelt profile string (pure function, e.g.
    `seatbelt_profile(config, workspace) -> str`), invoke via
    `sandbox-exec -p <profile> <command...>`.
  - Windows (and any platform without `bwrap`/`sandbox-exec` available):
    `OsSandbox.run` raises `ConfigurationError` at construction time (in
    `build_sandbox`), not silently degrading to `LocalSandbox` — silent
    degradation is the exact Landlock-fail-open trap Part 1 called out.
    `build_sandbox` should tell the operator plainly: "`sandbox.kind: os` is
    not available on Windows; use `sandbox.kind: docker` or `local`."
- `build_sandbox(config)` gains the `"os"` branch and the availability check
  above.

**Risk**: `bwrap` binding the wrong set of read-only system paths breaks
tools that need to read something not on the list (this is precisely the
class of failure Claude Code's own troubleshooting section documents
repeatedly — `jest`+watchman, Go TLS verification, `docker` itself all fail
under a too-strict sandbox). Mitigate by defaulting to Claude Code's model —
broad read, narrow write — rather than Docker's narrow-everything model:
allow read of the whole filesystem except an explicit deny list
(`~/.ssh`, `~/.aws`), restrict write to the workspace + temp dir only.

**Testing on Windows while CI is Linux**: the argv/profile-string builders
(`bwrap_command()`, `seatbelt_profile()`) are pure functions — same pattern
as `docker_command()` — so they get unconditional unit tests
(`tests/test_sandbox_os_linux.py`, `tests/test_sandbox_os_macos.py`) that run
everywhere, asserting the exact argv/profile text for representative configs.
A second test tier (`tests/test_sandbox_os_linux_live.py`) actually shells
out to `bwrap` and is skipped when `shutil.which("bwrap")` is `None` — passes
in CI (Linux, once `bwrap` is confirmed present or installed as a CI
dependency), skipped on the Windows dev machine automatically, matching the
existing pattern `DockerSandbox` tests already use for `docker`.

### Stage 2: policy rules — `"*"` wildcard in `_matches`, and the `rules` list

Files: `harness/execution/policy.py`, `harness/config.py`.

- Extend `_matches()` (`policy.py:241`) to treat a literal `"*"` token in the
  pattern as "any single or multi-token span," matching Claude Code's
  semantics: `*` at the end with a preceding space also matches the bare
  prefix (`git log *` matches `git log`). This is an incremental change to an
  existing pure function with existing test coverage
  (`tests/test_*policy*.py` — confirm exact file name), so the new behavior
  gets new cases added to the same suite rather than a new matcher built from
  scratch.
- Add `PolicyRule` dataclass: `tool: str`, `match: tuple[str, ...] | None`
  (for command tools), `path: str | None` (for file tools), `action:
  Literal["allow", "ask", "deny"]`.
- Add `rules_from_dict` parser mirroring `sandbox_config_from_dict`'s shape:
  `schema.sequence` → `schema.mapping` per item → `schema.reject_unknown`
  against `{"tool", "match", "path", "action"}` → validate `action` is one of
  the three literals, validate exactly one of `match`/`path` is present
  depending on whether `tool` is `run_command` or a file tool.
- Add `PolicyConfig.rules: tuple[PolicyRule, ...] = ()` field, wired through
  `load_config` at `harness/config.py:288-327` the same way
  `denied_commands`/`allowed_commands` already are, and added to the
  `reject_unknown` key set at `harness/config.py:292-299`.
- `PolicyEngine.evaluate()` (`policy.py:308`) and `Gate.check()`
  (`approvals.py:122`) both gain a `_rule_decision(tool, arguments)` call
  inserted after the existing hard-deny check and before the mode-driven
  default, implementing the deny→ask→allow precedence from Part 2. Both call
  sites need it, mirroring how `denied_pattern` is already called from both
  `PolicyEngine._command_denied` and `Gate._hard_refusal` — the same
  "REPL and batch loop must not drift" comment that already exists at
  `policy.py:280` (`"Module-level so the conversational REPL and harness run
  cannot drift on what counts as dangerous"`) applies here.

**Risk**: two separate call sites (`PolicyEngine`, `Gate`) evaluating the
same rule set is exactly the drift risk the module-level-function comment in
`policy.py` already flags for the deny-list. Mitigate by putting
`_rule_decision` as a plain function in `policy.py`, not a method on either
class, called by both — same shape as `denied_pattern`.

**Testing**: entirely pure-logic, no OS dependency, runs identically on
Windows and Linux CI. New cases in the existing policy test module covering:
wildcard matching edge cases from the Claude Code table (`git log * main` vs
`git * main` vs trailing-`*`-matches-bare-prefix), deny-beats-ask-beats-allow
precedence, and the interaction with `Mode.FULL_AUTO` (an `ask` rule must
force a prompt even in full-auto).

### Stage 3: config schema and `configs/ay.yaml` documentation

Files: `harness/config.py` (schema wiring only, covered in Stage 2),
`configs/ay.yaml` (example rules block, comments explaining precedence,
following the existing prose style — see the `denied_commands` comment block
at lines 233-246 as the tone to match).

This stage is documentation-shaped but touches the shipped config, so it is
listed separately: writing `rules:` examples into `configs/ay.yaml` is how an
operator actually discovers the feature, the same way the file today is the
canonical example of the deny-list rather than a separate doc page.

### Stage 4: wire `OsSandbox` into `configs/ay.yaml` as an opt-in, not a default

`sandbox.kind: local` stays the default (same reasoning `docker_command`'s
module docstring already gives: "A workshop laptop without docker must still
be able to run the harness" — the same is true a fortiori for a bwrap/Seatbelt
dependency that plenty of Linux/macOS machines also won't have installed by
default). `sandbox.kind: os` becomes the documented middle option between
`local` (no confinement) and `docker` (full confinement, needs Docker
installed): meaningful confinement on macOS/Linux with nothing to install
beyond `bubblewrap`+`socat`-equivalent (or their macOS no-install case),
still `ConfigurationError`s honestly on Windows.

### What is explicitly NOT attempted

- **No Windows job-object/restricted-token/AppContainer implementation.**
  Per Part 1, job objects don't touch filesystem/network, restricted tokens
  need hand-rolled `ctypes` Win32 calls this codebase has no precedent for
  and cannot easily unit-test the security properties of, and AppContainer
  is a multi-primitive system (profile + capability SIDs + firewall rules)
  that would be a project of its own, not a stage of this one. Building a
  partial version — say, job objects alone — would give a
  false sense of confinement (memory/CPU limits, yes; filesystem/network
  escape, no) while looking like a peer of the Linux/macOS work. That is
  worse than the current honest state, which is why it is out.
- **No content-level TLS inspection for network filtering**, matching Claude
  Code's own stated default and its own explicit warning about domain
  fronting. Hostname-based allow/deny only, same caveat documented.
- **No enterprise/managed-settings precedence tier** for rules (Part 2) —
  no fleet-management surface exists in this repo to justify it.
- **No automatic Landlock-primary design on Linux.** Per Part 1's ABI table,
  Landlock cannot restrict network before kernel 6.7, which is too recent to
  rely on as the sole mechanism; bubblewrap is primary, Landlock stays an
  optional additional layer, matching what both Codex and Claude Code
  actually ship rather than a novel Landlock-first design that would silently
  lose network confinement on older kernels.
