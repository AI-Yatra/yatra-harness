"""Turning a verified run into a commit, a pushed branch, and a pull request.

This is the only part of the harness that reaches outside the machine, so it
is deliberately the most reluctant. Delivery refuses a run the verifier did
not pass, refuses a run with nothing in it, and asks before every step whose
effect another person can see. Each step is also independently retryable: a
declined push leaves the commit alone so the next attempt has something to
send, and a declined pull request leaves the branch pushed.

The pull request body is assembled from the run's own evidence rather than
from the model's description of its work. A model's account of what it did is
exactly the thing the verifier exists not to trust.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .contracts import RunStatus
from .errors import HarnessError
from .util import atomic_write_json, atomic_write_text
from .workspace import git_environment

MODES = ("commit", "branch", "pr")
SUBJECT_LIMIT = 72


class DeliveryError(HarnessError):
    """A run could not be delivered, or delivery was declined."""


ApproveCallback = Any  # Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    mode: str
    run_id: str
    run_dir: Path
    workspace: Path
    objective: str
    status: RunStatus
    summary: str = ""
    base: str = ""
    remote: str = "origin"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    mode: str
    branch: str
    base: str
    commit: str
    pushed: bool
    pull_request_url: str = ""
    steps: tuple[str, ...] = field(default_factory=tuple)


def deliver(request: DeliveryRequest, *, approve: ApproveCallback) -> DeliveryResult:
    """Commit, push and open a pull request for a completed run."""
    if request.mode not in MODES:
        raise DeliveryError(f"unknown delivery mode {request.mode!r}; expected one of {', '.join(MODES)}")
    if request.status is not RunStatus.COMPLETED:
        raise DeliveryError(
            f"run {request.run_id} ended {request.status.value}; only a COMPLETED run is delivered"
        )
    workspace = Path(request.workspace)
    if _git(("rev-parse", "--git-dir"), cwd=workspace) is None:
        raise DeliveryError(f"run workspace is not a git repository: {workspace}")
    branch = _git(("rev-parse", "--abbrev-ref", "HEAD"), cwd=workspace)
    if not branch or branch == "HEAD":
        raise DeliveryError("run workspace is not on a named branch; nothing to deliver from")
    base = request.base or _default_base(workspace, request.remote)
    steps: list[str] = []

    commit = _commit(request, workspace, base, steps)
    if request.mode == "commit":
        return _record(request, DeliveryResult("commit", branch, base, commit, False, "", tuple(steps)))

    _push(request, workspace, branch, steps, approve)
    if request.mode == "branch":
        return _record(request, DeliveryResult("branch", branch, base, commit, True, "", tuple(steps)))

    url = _pull_request(request, workspace, branch, base, steps, approve)
    return _record(request, DeliveryResult("pr", branch, base, commit, True, url, tuple(steps)))


def _commit(request: DeliveryRequest, workspace: Path, base: str, steps: list[str]) -> str:
    """Stage and commit the run's work, or reuse what is already committed."""
    if _git(("add", "-A"), cwd=workspace) is None:
        raise DeliveryError("could not stage the run's changes")
    staged = _git(("diff", "--cached", "--name-only"), cwd=workspace)
    if staged:
        message = commit_message(request)
        if _git(("commit", "-q", "-F", "-"), cwd=workspace, stdin=message) is None:
            raise DeliveryError("could not commit the run's changes")
        steps.append("commit")
    elif not _ahead_of(workspace, base):
        # Nothing staged and nothing already committed on top of the base:
        # there is genuinely no work here, and pushing an empty branch would
        # produce a pull request with no diff.
        raise DeliveryError(
            "nothing to deliver: the run workspace has no changes over "
            f"{base}"
        )
    head = _git(("rev-parse", "HEAD"), cwd=workspace)
    if not head:
        raise DeliveryError("could not read the delivered commit")
    return head


def _push(
    request: DeliveryRequest, workspace: Path, branch: str, steps: list[str], approve: ApproveCallback
) -> None:
    remote_url = _git(("remote", "get-url", request.remote), cwd=workspace)
    if remote_url is None:
        raise DeliveryError(f"run workspace has no {request.remote!r} remote to push to")
    if not approve(f"push branch {branch} to {request.remote} ({remote_url})"):
        raise DeliveryError(f"push declined; the commit is still on {branch} in the run workspace")
    pushed = _git(("push", "--set-upstream", request.remote, branch), cwd=workspace)
    if pushed is None:
        raise DeliveryError(f"could not push {branch} to {request.remote} ({remote_url})")
    steps.append("push")


def _pull_request(
    request: DeliveryRequest,
    workspace: Path,
    branch: str,
    base: str,
    steps: list[str],
    approve: ApproveCallback,
) -> str:
    body_path = _write_body(request, workspace, base)
    if not approve(f"open a pull request from {branch} into {base}"):
        raise DeliveryError(f"pull request declined; {branch} is pushed and can be opened by hand")
    command = [
        "gh", "pr", "create",
        "--base", base,
        "--head", branch,
        "--title", subject(request.objective),
        "--body-file", str(body_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            env=_gh_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise DeliveryError(
            "the GitHub CLI `gh` is not installed or not on PATH; "
            "the branch is pushed, so the pull request can be opened by hand"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise DeliveryError(f"gh pr create failed: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise DeliveryError(f"gh pr create failed: {output}")
    steps.append("pull-request")
    return _first_url(output)


def commit_message(request: DeliveryRequest) -> str:
    """A commit that says what was asked for and which run produced it."""
    lines = [subject(request.objective), ""]
    if request.summary.strip():
        lines.extend([request.summary.strip(), ""])
    lines.append(f"Harness-Run: {request.run_id}")
    return "\n".join(lines) + "\n"


def subject(objective: str) -> str:
    """One line, from what the operator asked for rather than what the model said.

    Cut at a word boundary and left without an ellipsis, because this is a git
    subject line: the full request is in the body directly below it, so a
    trailing marker adds nothing and reads badly in a log.
    """
    text = " ".join(objective.split())
    sentence = text.split(". ", 1)[0].rstrip(".") or "Harness change"
    if len(sentence) <= SUBJECT_LIMIT:
        return sentence
    head = sentence[: SUBJECT_LIMIT + 1]
    cut = head.rfind(" ")
    trimmed = (head[:cut] if cut > SUBJECT_LIMIT // 2 else sentence[:SUBJECT_LIMIT]).rstrip()
    return trimmed.rstrip(",;:-") or "Harness change"


def _write_body(request: DeliveryRequest, workspace: Path, base: str) -> Path:
    directory = Path(request.run_dir) / "delivery"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pull-request.md"
    atomic_write_text(path, pull_request_body(request, workspace, base), mode=0o644)
    return path


def pull_request_body(request: DeliveryRequest, workspace: Path, base: str) -> str:
    verification = _latest_verification(Path(request.run_dir))
    changed = verification.get("changed_paths") or _changed_against(workspace, base)
    lines = [
        "## What was asked for",
        "",
        " ".join(request.objective.split()),
        "",
        "## What changed",
        "",
    ]
    if changed:
        lines.extend(f"- `{path}`" for path in changed)
    else:
        lines.append("_No files listed._")
    lines.extend(["", "## Verification", ""])
    commands = verification.get("commands") or []
    if commands:
        lines.append(
            "The harness ran these acceptance commands itself, in an isolated "
            "clone, after the change was made:"
        )
        lines.append("")
        for entry in commands:
            command = " ".join(entry.get("command", []))
            outcome = "passed" if entry.get("returncode") == 0 and not entry.get("timed_out") else "failed"
            lines.append(f"- `{command}` — {outcome}")
        lines.append("")
        lines.append(f"Verifier result: **{verification.get('summary', 'unknown')}**.")
    else:
        # Said plainly rather than omitted: a reviewer needs to know the
        # difference between "verified" and "no evidence recorded".
        lines.append("_No verification record was found in the run bundle._")
    lines.extend(
        [
            "",
            "---",
            "",
            f"Produced by the yatra-harness run `{request.run_id}`. The model proposed "
            "the change; the harness verified it independently before this branch "
            "was pushed.",
            "",
        ]
    )
    return "\n".join(lines)


def _record(request: DeliveryRequest, result: DeliveryResult) -> DeliveryResult:
    directory = Path(request.run_dir) / "delivery"
    directory.mkdir(parents=True, exist_ok=True)
    value = asdict(result)
    value["steps"] = list(result.steps)
    value["run_id"] = request.run_id
    atomic_write_json(directory / "delivery.json", value)
    return result


def _latest_verification(run_dir: Path) -> dict[str, Any]:
    directory = run_dir / "artifacts" / "verification"
    if not directory.is_dir():
        return {}
    attempts = sorted(directory.glob("attempt-*.json"))
    if not attempts:
        return {}
    try:
        value = json.loads(attempts[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _changed_against(workspace: Path, base: str) -> list[str]:
    for candidate in (f"origin/{base}", base):
        output = _git(("diff", "--name-only", f"{candidate}...HEAD"), cwd=workspace)
        if output is not None:
            return [line for line in output.splitlines() if line]
    return []


def _default_base(workspace: Path, remote: str) -> str:
    """The branch a pull request should target, read from the remote itself."""
    head = _git(("symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"), cwd=workspace)
    if head and head.startswith(f"{remote}/"):
        return head[len(remote) + 1 :]
    return "main"


def _ahead_of(workspace: Path, base: str) -> bool:
    for candidate in (f"origin/{base}", base):
        count = _git(("rev-list", "--count", f"{candidate}..HEAD"), cwd=workspace)
        if count is not None:
            return count.strip() not in {"", "0"}
    # The base cannot be resolved, so "is there anything new here" has no
    # answer. Treating that as "yes" keeps an unusual repository deliverable
    # rather than silently undeliverable.
    return True


def _first_url(output: str) -> str:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
    return ""


def _gh_environment() -> dict[str, str]:
    """`gh` needs the operator's credentials; it gets nothing else."""
    environment = {"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0"}
    for name in ("HOME", "USERPROFILE", "APPDATA", "LANG", "SYSTEMROOT",
                 "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "XDG_CONFIG_HOME"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _git(arguments: tuple[str, ...], *, cwd: Path, stdin: str | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=git_environment(),
            check=False,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
