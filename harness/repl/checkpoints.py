"""Undo for a conversation, without touching the operator's history.

An agent editing files in a real working directory needs an undo that is not
"hope it was committed". Three shapes were considered and the third is here.

Committing to the operator's own repository, which is what Aider does, gives a
perfect undo and puts the agent's intermediate states in the history the
operator will publish. Snapshotting only through the edit tools, which is what
Claude Code does, is cheap and misses everything a command changed, so a run
that formats the tree or regenerates a lockfile leaves nothing to go back to.

A shadow repository gets both. It is an ordinary git repository whose work tree
is the operator's directory and whose `.git` is somewhere else, so it records
full states of the tree without ever writing to the real history, and because
it snapshots after tool calls rather than inside them it catches what commands
did as well as what edits did. It also works in a directory that is not a git
repository at all, which is where an undo is needed most.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.execution.process import run_process
from harness.execution.workspace import git_environment

#: Never snapshotted. The first two would recurse, and the rest are rebuildable
#: output whose size would dominate every commit.
EXCLUDED = (
    ".git/",
    ".ay/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    "dist/",
    "build/",
    "target/",
    ".next/",
    ".runs/",
    "*.pyc",
)

#: How many to keep. Old ones are dropped rather than the repository growing
#: without bound; git's own packing keeps the cost of the kept ones low.
KEEP = 50

#: Anything slower than this and a snapshot is worse than no snapshot, because
#: it is paid on every tool call. A large tree that cannot be captured quickly
#: turns the feature off rather than slowing the session down.
TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One recorded state of the working tree."""

    ref: str
    label: str
    when: str

    @property
    def short(self) -> str:
        return self.ref[:8]


class Checkpoints:
    """A shadow git repository over the operator's working directory."""

    def __init__(self, root: Path, store: Path) -> None:
        self.root = root
        self.store = store
        self.enabled = True
        self.reason = ""
        self._ready = False

    # ---------------------------------------------------------------- plumbing

    def _git(self, *arguments: str, timeout: float = TIMEOUT_SECONDS):
        return run_process(
            [
                "git",
                "--git-dir",
                str(self.store),
                "--work-tree",
                str(self.root),
                *arguments,
            ],
            cwd=self.root,
            timeout=timeout,
            max_output_chars=8_000,
            environment=git_environment(),
        )

    def _prepare(self) -> bool:
        """Create the shadow repository on first use.

        Deferred so a session that never edits anything never creates it, and
        so a directory where git is unavailable costs a notice rather than a
        failure to start.
        """
        if self._ready:
            return True
        if not self.enabled:
            return False
        try:
            if not (self.store / "HEAD").exists():
                self.store.parent.mkdir(parents=True, exist_ok=True)
                created = run_process(
                    ["git", "init", "--bare", "--quiet", str(self.store)],
                    cwd=self.root,
                    timeout=TIMEOUT_SECONDS,
                    max_output_chars=4_000,
                    environment=git_environment(),
                )
                if created.returncode != 0:
                    return self._disable(created.output.strip() or "git init failed")
            # These are on top of the operator's own .gitignore, which is
            # honoured. Bypassing it would need `add --force`, and that turns
            # off every exclude including these, so the choice is all or
            # nothing rather than a dial.
            #
            # Honouring it is the better default. What people gitignore is
            # build output, virtualenvs and `.env`, so ignoring it would copy
            # secrets into a second repository and grow the store without
            # bound. The cost is real and worth stating: a change to a
            # gitignored file cannot be undone from here.
            info = self.store / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "exclude").write_text("\n".join(EXCLUDED) + "\n", encoding="utf-8")
        except OSError as exc:
            return self._disable(str(exc))
        self._ready = True
        return True

    def _disable(self, reason: str) -> bool:
        self.enabled = False
        self.reason = reason
        return False

    # ------------------------------------------------------------------ record

    def record(self, label: str) -> Checkpoint | None:
        """Snapshot the working tree. None when there was nothing to record."""
        if not self._prepare():
            return None
        staged = self._git("add", "--all", ".")
        if staged.returncode != 0:
            self._disable(staged.output.strip() or "git add failed")
            return None
        committed = self._git(
            "commit",
            "--quiet",
            "--allow-empty",
            "--no-verify",
            "-m",
            label or "checkpoint",
        )
        if committed.returncode != 0:
            self._disable(committed.output.strip() or "git commit failed")
            return None
        self._trim()
        found = self.list(limit=1)
        return found[0] if found else None

    def _trim(self) -> None:
        """Keep the newest KEEP checkpoints and let git reclaim the rest."""
        listed = self._git("rev-list", "--count", "HEAD")
        try:
            total = int(listed.output.strip() or "0")
        except ValueError:
            return
        if total <= KEEP:
            return
        base = self._git("rev-parse", f"HEAD~{KEEP}")
        if base.returncode != 0:
            return
        # Re-root the branch so the older commits become unreachable, then let
        # git drop them. Rewriting rather than deleting keeps the kept range
        # continuous, which `undo` depends on.
        self._git("update-ref", "refs/heads/trimmed", base.output.strip())
        self._git("update-ref", "-d", "refs/heads/trimmed")

    # -------------------------------------------------------------------- read

    def list(self, limit: int = 20) -> list[Checkpoint]:
        if not self._ready or not self.enabled:
            return []
        listed = self._git(
            "log", f"--max-count={limit}", "--format=%H%x1f%s%x1f%cr", "HEAD"
        )
        if listed.returncode != 0:
            return []
        found: list[Checkpoint] = []
        for line in listed.output.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                found.append(Checkpoint(parts[0], parts[1], parts[2]))
        return found

    def changed_since(self, ref: str) -> list[str]:
        """Paths that differ between *ref* and the tree as it is now.

        Shown before restoring, because between two turns the operator may have
        edited something themselves, and a restore that silently discards their
        work would be the worst possible behaviour for an undo.
        """
        if not self._ready:
            return []
        self._git("add", "--all", ".")
        diff = self._git("diff", "--name-only", ref)
        if diff.returncode != 0:
            return []
        return [line.strip() for line in diff.output.splitlines() if line.strip()]

    # ----------------------------------------------------------------- restore

    def restore(self, ref: str) -> bool:
        """Put the working tree back to *ref*, keeping the history intact.

        Files created after the checkpoint are removed, which is what makes it
        an undo rather than a merge. `checkout -- .` alone does not do that:
        it rewrites the content of files present in the checkpoint and leaves
        anything added since exactly where it was, so the tree ends up in a
        state that never existed.

        The history is deliberately not rewound. Moving the branch back to the
        checkpoint would throw away the states after it, and an undo that
        cannot itself be undone is a trap. The tree goes back, the tip stays
        where it was, and the next snapshot records the revert as one more
        state, so going forward again is just another restore.

        The caller is expected to have shown `changed_since` first.
        """
        if not self._prepare():
            return False
        tip = self._git("rev-parse", "HEAD")
        if tip.returncode != 0:
            return False
        if self._git("reset", "--hard", "--quiet", ref).returncode != 0:
            return False
        # Untracked leftovers are not the checkpoint's business but are still
        # the agent's mess. `info/exclude` keeps this off .git, .ay and the
        # build directories.
        self._git("clean", "-fdq")
        self._git("reset", "--soft", tip.output.strip())
        return True
