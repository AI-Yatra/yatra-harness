"""The tools a conversational session gives the model.

Shaped for editing rather than for batch patching. The batch path's
`apply_patch` takes a whole unified diff, which asks a model to get line
numbers and context right in one shot; the failure mode is a rejected hunk
and a wasted turn. `edit_file` instead takes the exact text to replace and
refuses when that text is absent or ambiguous, so a mistake comes back as a
specific, fixable message rather than a corrupted file.

Every path goes through `Workspace.resolve`, so containment is the same code
the batch path uses. The difference is only where the workspace root points:
here it is the directory the operator launched in.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.core.contracts import RiskLevel, ToolSpec
from harness.core.errors import ToolError, WorkspaceError
from harness.execution import diagnostics
from harness.execution.policy import ANY_COMMAND, PolicyEngine
from harness.execution.sandbox import build_sandbox
from harness.execution.tools import ToolRegistry
from harness.execution.workspace import Workspace
from harness.record.artifacts import ArtifactStore

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import HarnessConfig

MAX_READ_LINES = 2_000
MAX_LINE_CHARS = 2_000
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build", ".runs",
    ".next", ".idea", ".tox", "target",
}
TEXT_SUFFIX_HINT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".cfg", ".ini", ".sh", ".rs", ".go", ".java", ".c", ".h",
    ".cpp", ".hpp", ".css", ".html", ".sql", ".rb", ".php", ".xml", ".env",
}


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What running a tool produced.

    `display` is for the operator's screen and `content` is for the model.
    They differ on purpose: a 400-line file read is one line on screen
    ("read 400 lines") and 400 lines in the context.
    """

    content: str
    display: str = ""
    detail: str = ""
    ok: bool = True


Handler = Any


#: A quantified group that is itself quantified, the classic exponential
#: backtracking shape. `(a+)+` matches; `(abc)+` and `(a|b)+` do not, because
#: neither repeats anything inside the group.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")

#: Alphabets a runaway pattern is timed against. A pattern built from digits
#: does not backtrack on letters, and `(a|ab)*` needs a two-character cycle
#: before it misbehaves at all.
_PROBE_UNITS = ("a", "ab", "0", "a1")

#: Probe lengths, shortest first, and deliberately short. A single match
#: cannot be interrupted, so the probe's own worst case is the ceiling on how
#: long this check can take: at 22 characters an exponential pattern costs
#: about 2^22 steps, which is a fraction of a second. Longer probes would
#: detect more shapes and could themselves hang, which is the thing being
#: defended against.
_PROBE_LENGTHS = (16, 22)

#: A pattern needing longer than this on a probe this short is backtracking
#: exponentially. An honest pattern answers in microseconds, so the two
#: populations sit orders of magnitude apart and the threshold is not delicate.
_PROBE_BUDGET_SECONDS = 0.01


def _backtracks(expression: re.Pattern[str]) -> bool:
    r"""Whether *expression* blows up on a short string.

    Timing rather than reading the pattern, because a structural check cannot
    see every shape: `(a|a)*` repeats nothing inside its group and is still
    exponential. Measuring catches it however it is written.

    This has to happen before the search rather than during it. Matching runs
    inside C and does not release the GIL, so a worker thread cannot be joined
    with a timeout and a signal cannot be delivered. Once a catastrophic match
    starts, nothing in the process can stop it, and the only usable defence is
    to decline to start.

    The same constraint bounds how good this can be. Detecting a shape whose
    blow-up only appears on longer input would need a longer probe, and that
    probe could itself hang. `(\d|\d\d)*$` is the documented survivor. The
    complete fix is to match in a separate process that can be killed; that is
    not done here because the pattern comes from the model rather than from an
    attacker, and the shapes a model actually writes are covered.
    """
    for length in _PROBE_LENGTHS:
        for unit in _PROBE_UNITS:
            probe = (unit * (length // len(unit) + 1))[:length] + "!"
            started = time.perf_counter()
            try:
                expression.search(probe)
            except (re.error, RecursionError):
                return True
            if time.perf_counter() - started > _PROBE_BUDGET_SECONDS:
                return True
    return False


class _EphemeralArtifacts:
    """Somewhere for oversized output to go when there is no run directory.

    The batch loop writes artifacts under the run it belongs to. A
    conversation may not have one, and dropping the overflow instead would
    make the truncation notice a dead end: the model is told the output was
    cut and neither it nor the operator can see the rest.

    The directory is made on first use, so a session that never overflows
    never creates one.
    """

    def __init__(self) -> None:
        self._store: ArtifactStore | None = None

    def write_payload(self, name: str, content: str) -> str:
        if self._store is None:
            self._store = ArtifactStore(Path(tempfile.mkdtemp(prefix="ay-artifacts-")))
        return self._store.write_payload(name, content)


class ReplToolset:
    """The conversational tool set, executed by the shared registry.

    The tools here are shaped for a conversation, but running them is not a
    separate mechanism. Every call goes through `ToolRegistry`, which is the
    same object the batch loop uses, so a conversation gets the things that
    were previously only wired to `harness run`: arguments validated against
    the declared schema before a handler sees them, oversized output spilled to
    an artifact instead of into the context, every decision written to the
    event ledger, and an unexpected exception isolated to the call rather than
    the session.

    It also means a tool only has to be registered once to be available to
    both. `extra_tools` is how MCP servers and delegation reach a
    conversation, rather than by being reimplemented here.
    """

    def __init__(
        self,
        workspace: Workspace,
        config: HarnessConfig,
        *,
        artifacts: ArtifactStore | None = None,
        event_callback: Any = None,
        extra_tools: Sequence[tuple[ToolSpec, Any]] = (),
    ) -> None:
        self.workspace = workspace
        self.config = config
        #: Problems with the checker itself, for the shell to show the
        #: operator once. Collected rather than raised, and deliberately not
        #: shown to the model: a missing linter is the operator's problem, and
        #: a model told about one tries to install it.
        self.notices: list[str] = []
        self._handlers: dict[str, Handler] = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "glob": self.glob,
            "grep": self.grep,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_command": self.run_command,
        }
        self.registry = self._build_registry(artifacts, event_callback, extra_tools)

    def _build_registry(
        self,
        artifacts: ArtifactStore | None,
        event_callback: Any,
        extra_tools: Sequence[tuple[ToolSpec, Any]],
    ) -> ToolRegistry:
        """Assemble the shared registry for a conversation.

        Two parts of the shared policy are stood down here, both because the
        operator is present and neither because a conversation deserves less
        care.

        Approval is left to `Gate`. A second approver inside the registry would
        prompt twice, or refuse what the operator had just allowed.

        The command allowlist steps aside for the reason `configs/ay.yaml`
        already gives: this loop asks about commands rather than requiring them
        to be listed in advance, and a list written to cover everything the
        operator might agree to stops meaning anything. The deny-list, the
        network rule and the schema check all still apply, and they are the
        same code the batch loop runs, so a refusal cannot be reached by going
        around the gate.
        """
        specs = self._native_specs() + tuple(spec for spec, _ in extra_tools)
        policy = PolicyEngine(
            replace(
                self.config.policy,
                approval_mode="never",
                allowed_commands=(ANY_COMMAND,),
            ),
            tuple(spec.name for spec in specs),
        )
        registry = ToolRegistry(
            policy,
            max_output_chars=self.config.budgets.max_output_chars,
            artifacts=artifacts or _EphemeralArtifacts(),
            event_callback=event_callback,
        )
        for spec in self._native_specs():
            registry.register(spec, self._adapt(spec.name))
        for spec, handler in extra_tools:
            registry.register(spec, handler)
        return registry

    def _repair(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fix the argument shapes models reliably get wrong, before validation.

        The declared schema says what the model should send, and it should keep
        saying that: widening it to accept a string would teach every model
        that a string is fine, and would have to survive whatever each provider
        does with a union type.

        Repairing beforehand keeps both. A model that sends `"pytest -q"`
        instead of `["pytest", "-q"]` gets what it meant, and one that sends
        shell syntax is told plainly that there is no shell, rather than having
        a pipe character handed to a program as a filename.
        """
        if name != "run_command":
            return arguments
        command = arguments.get("command")
        if not isinstance(command, str):
            return arguments
        if any(character in command for character in "|&;<>$`"):
            raise ToolError(
                "command must be an array of arguments and cannot use shell syntax "
                "(pipes, redirection, substitution). Run the pieces separately."
            )
        return {**arguments, "command": command.split()}

    def _adapt(self, name: str) -> Any:
        """Wrap a conversational handler in the registry's calling convention.

        The registry deals in `(content, metadata)` and decides success by
        whether the handler raised. A conversation needs more than that: a
        failing test command has useful output and a non-zero exit, and is not
        a tool failure. The screen labels travel the same way, so the terminal
        can show `Read(game.py)` while the model receives the file.
        """

        def handler(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            outcome = self._handlers[name](arguments)
            return outcome.content, {
                "display": outcome.display,
                "detail": outcome.detail,
                "ok": outcome.ok,
            }

        return handler

    # ------------------------------------------------------------ declaration

    def specs(self) -> tuple[ToolSpec, ...]:
        """Every tool this conversation may call, natives and extras alike."""
        return self.registry.specs()

    def _native_specs(self) -> tuple[ToolSpec, ...]:
        obj = _object_schema
        return (
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file from the working directory. Returns the "
                "contents with line numbers. Read a file before editing it.",
                obj(
                    {
                        "path": {"type": "string", "description": "Path relative to the working directory."},
                        "offset": {"type": "integer", "description": "First line to read, 1-based."},
                        "limit": {"type": "integer", "description": "How many lines to read."},
                    },
                    ("path",),
                ),
                RiskLevel.READ,
            ),
            ToolSpec(
                "list_dir",
                "List the entries of a directory. Directories are marked with a "
                "trailing slash. Build and cache directories are skipped.",
                obj({"path": {"type": "string", "description": "Directory, relative to the working directory. Defaults to the root."}}),
                RiskLevel.READ,
            ),
            ToolSpec(
                "glob",
                "Find files by glob pattern, for example 'src/**/*.py'. Returns "
                "paths sorted by how recently they were modified.",
                obj({"pattern": {"type": "string"}, "limit": {"type": "integer"}}, ("pattern",)),
                RiskLevel.READ,
            ),
            ToolSpec(
                "grep",
                "Search file contents with a regular expression. Returns matching "
                "lines with their file and line number.",
                obj(
                    {
                        "pattern": {"type": "string", "description": "Python regular expression."},
                        "glob": {"type": "string", "description": "Restrict to files matching this glob."},
                        "path": {"type": "string", "description": "Restrict to this subdirectory."},
                        "limit": {"type": "integer"},
                    },
                    ("pattern",),
                ),
                RiskLevel.READ,
            ),
            ToolSpec(
                "write_file",
                "Write a file, creating it or replacing it entirely. Use edit_file "
                "to change part of an existing file.",
                obj({"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content")),
                RiskLevel.WRITE,
            ),
            ToolSpec(
                "edit_file",
                "Replace an exact block of text in a file. old_string must appear "
                "exactly once unless replace_all is true, and must match the file "
                "byte for byte including indentation. Read the file first.",
                obj(
                    {
                        "path": {"type": "string"},
                        "old_string": {"type": "string", "description": "Exact text to replace. Include enough surrounding lines to be unique."},
                        "new_string": {"type": "string", "description": "Replacement text. Empty string deletes."},
                        "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one."},
                    },
                    ("path", "old_string", "new_string"),
                ),
                RiskLevel.WRITE,
            ),
            ToolSpec(
                "run_command",
                "Run a command in the working directory without a shell. Pass the "
                "command as an array of arguments. Output and exit code are "
                "returned; a non-zero exit is reported, not hidden.",
                obj(
                    {
                        "command": {"type": "array", "items": {"type": "string"}},
                        "timeout": {"type": "number", "description": "Seconds before the command is killed."},
                    },
                    ("command",),
                ),
                RiskLevel.EXECUTE,
            ),
        )

    def dispatch(self, name: str, arguments: dict[str, Any], call_id: str = "") -> ToolOutcome:
        """Run one tool call and describe what happened.

        Arguments that never parsed are answered before the registry sees
        them, because a schema check on a JSON fragment the provider truncated
        would report a missing field rather than the truncation.
        """
        if "__parse_error__" in arguments:
            return ToolOutcome(str(arguments["__parse_error__"]), ok=False)
        try:
            arguments = self._repair(name, arguments)
        except ToolError as exc:
            return ToolOutcome(str(exc), ok=False)
        result = self.registry.execute(call_id or name, name, arguments)
        metadata = result.metadata or {}
        if not result.ok:
            # A refusal or a raised error. The registry has already turned it
            # into a sentence; the model reads that and tries something else.
            return ToolOutcome(result.error or "tool failed", ok=False)
        return ToolOutcome(
            result.content,
            display=str(metadata.get("display", "")),
            detail=str(metadata.get("detail", "")),
            ok=bool(metadata.get("ok", True)),
        )

    # ------------------------------------------------------------------ reads

    def read_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = self._resolve(arguments.get("path"), must_exist=True)
        if path.is_dir():
            raise ToolError(f"{arguments['path']} is a directory; use list_dir")
        text = _read_text(path)
        lines = text.splitlines()
        offset = max(1, int(arguments.get("offset") or 1))
        limit = int(arguments.get("limit") or MAX_READ_LINES)
        chosen = lines[offset - 1 : offset - 1 + max(1, limit)]
        if not chosen:
            return ToolOutcome(
                f"(no lines at offset {offset}; the file has {len(lines)})",
                display=f"{self._label(path)} · empty range",
            )
        width = len(str(offset + len(chosen) - 1))
        body = "\n".join(
            f"{str(offset + i).rjust(width)}│{line[:MAX_LINE_CHARS]}"
            for i, line in enumerate(chosen)
        )
        shown = len(chosen)
        more = len(lines) - (offset - 1) - shown
        if more > 0:
            body += f"\n\n({more} more lines; read again with offset={offset + shown})"
        detail = f"{shown} line{'s' if shown != 1 else ''}"
        return ToolOutcome(body, display=self._label(path), detail=detail)

    def list_dir(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw = str(arguments.get("path") or ".")
        path = self.workspace.root if raw in {".", ""} else self._resolve(raw, must_exist=True)
        if not path.is_dir():
            raise ToolError(f"{raw} is not a directory")
        entries = sorted(
            (e for e in path.iterdir() if e.name not in SKIP_DIRS),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
        if not entries:
            return ToolOutcome("(empty directory)", display=self._label(path), detail="empty")
        listed = [f"{e.name}/" if e.is_dir() else f"{e.name}" for e in entries[:400]]
        body = "\n".join(listed)
        if len(entries) > 400:
            body += f"\n({len(entries) - 400} more entries)"
        return ToolOutcome(body, display=self._label(path), detail=f"{len(entries)} entries")

    def glob(self, arguments: dict[str, Any]) -> ToolOutcome:
        pattern = str(arguments.get("pattern") or "").strip()
        if not pattern:
            raise ToolError("glob needs a pattern")
        limit = int(arguments.get("limit") or 200)
        matches = [
            p for p in _walk(self.workspace.root)
            if fnmatch.fnmatch(self._relative(p), pattern)
            or fnmatch.fnmatch(p.name, pattern)
        ]
        # Most recently touched first: in a live repository that is almost
        # always the file the operator is asking about.
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return ToolOutcome(f"No files match {pattern}", display=pattern, detail="no matches")
        body = "\n".join(self._relative(p) for p in matches[:limit])
        if len(matches) > limit:
            body += f"\n({len(matches) - limit} more)"
        return ToolOutcome(body, display=pattern, detail=f"{len(matches)} files")

    def grep(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw = str(arguments.get("pattern") or "")
        if not raw:
            raise ToolError("grep needs a pattern")
        if _NESTED_QUANTIFIER.search(raw):
            # Rejected before it runs rather than after it hangs. A quantified
            # group that is itself quantified is the classic shape whose
            # backtracking is exponential in the length of the line.
            raise ToolError(
                f"the pattern {raw!r} nests a quantifier inside a quantified group, which "
                "can take exponential time on a long line. Rewrite it without the nesting, "
                "for example (a+)+ as a+."
            )
        try:
            expression = re.compile(raw)
        except re.error as exc:
            raise ToolError(f"not a valid regular expression: {exc}") from exc
        if _backtracks(expression):
            raise ToolError(
                f"the pattern {raw!r} backtracks exponentially: it took too long on a "
                "22-character probe, so on a real line it would not finish. Rewrite it "
                "without nested or overlapping repetition."
            )
        limit = int(arguments.get("limit") or 100)
        scope = self.workspace.root
        if arguments.get("path"):
            scope = self._resolve(str(arguments["path"]), must_exist=True)
        file_glob = str(arguments.get("glob") or "")
        hits: list[str] = []
        files = 0

        def search() -> None:
            nonlocal files
            for path in _walk(scope):
                relative = self._relative(path)
                if file_glob and not (
                    fnmatch.fnmatch(relative, file_glob) or fnmatch.fnmatch(path.name, file_glob)
                ):
                    continue
                try:
                    text = _read_text(path)
                except (ToolError, OSError):
                    continue
                matched = False
                for number, line in enumerate(text.splitlines(), start=1):
                    if expression.search(line):
                        matched = True
                        hits.append(f"{relative}:{number}: {line.strip()[:300]}")
                        if len(hits) >= limit:
                            break
                files += 1 if matched else 0
                if len(hits) >= limit:
                    break

        search()
        if not hits:
            return ToolOutcome(f"No matches for {raw}", display=raw, detail="no matches")
        body = "\n".join(hits)
        if len(hits) >= limit:
            body += f"\n(stopped at {limit} matches)"
        return ToolOutcome(body, display=raw, detail=f"{len(hits)} matches in {files} files")

    # ----------------------------------------------------------------- writes

    def write_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = self._resolve(arguments.get("path"))
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolError("write_file needs a string content")
        existed = path.exists()
        if existed and path.is_dir():
            raise ToolError(f"{self._relative(path)} is a directory, not a file")
        # The old contents are read only to count the change, so failing to
        # read them must not fail the write. A file that is binary or in
        # another encoding is exactly the one a caller most wants to replace,
        # and refusing with an error about the file being overwritten reads as
        # though the new content were at fault.
        before = ""
        if existed and path.is_file():
            try:
                before = _read_text(path)
            except (ToolError, OSError, UnicodeDecodeError):
                before = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        added, removed = _count_changes(before, content)
        verb = "updated" if existed else "created"
        return self._checked(
            path,
            ToolOutcome(
                f"{verb} {self._relative(path)} ({len(content.splitlines())} lines)",
                display=self._label(path),
                detail=f"+{added} -{removed}",
            ),
        )

    def edit_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = self._resolve(arguments.get("path"), must_exist=True)
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("edit_file needs string old_string and new_string")
        if not old:
            # `"".count()` is one per character plus one, and replacing the
            # empty string interleaves the replacement throughout the file.
            # Without this guard the edit reports success on a file it has
            # destroyed, which is the worst shape a failure can take.
            raise ToolError(
                "old_string is empty. It must be the exact text to replace. To create a "
                "file use write_file; to add to one, include the surrounding line in "
                "old_string and repeat it in new_string."
            )
        if old == new:
            raise ToolError("old_string and new_string are identical; nothing to do")
        before = _read_text(path)
        occurrences = before.count(old)
        if occurrences == 0:
            raise ToolError(
                f"old_string was not found in {self._relative(path)}. It must match the file "
                "exactly, including indentation and line breaks. Read the file and copy the "
                "text to replace from what you read."
            )
        if occurrences > 1 and not arguments.get("replace_all"):
            raise ToolError(
                f"old_string appears {occurrences} times in {self._relative(path)}. Include "
                "more surrounding lines so it matches exactly one place, or pass "
                "replace_all: true to change every occurrence."
            )
        after = before.replace(old, new) if arguments.get("replace_all") else before.replace(old, new, 1)
        path.write_text(after, encoding="utf-8")
        added, removed = _count_changes(before, after)
        where = f"{occurrences} places" if arguments.get("replace_all") and occurrences > 1 else "1 place"
        return self._checked(
            path,
            ToolOutcome(
                f"edited {self._relative(path)} in {where} (+{added} -{removed})",
                display=self._label(path),
                detail=f"+{added} -{removed}",
            ),
        )

    def _checked(self, path: Path, outcome: ToolOutcome) -> ToolOutcome:
        """Add the project checker's report to a successful write.

        `ok` is never touched. A diagnostic is not a failed edit, and an agent
        that reads one as a failure writes the same change again -- which is a
        bug another agent shipped, not a hypothetical.
        """
        settings = getattr(self.config, "diagnostics", None)
        relative = self._relative(path)
        if settings is None or not settings.applies_to(relative):
            return outcome
        report = diagnostics.check(settings, self.workspace.root, relative)
        if report.broken:
            # The operator's problem, not the model's. Telling the model its
            # linter is missing makes it try to install one.
            first = report.output.splitlines()[0] if report.output else "could not run"
            note = f"diagnostics: {first}"
            if note not in self.notices:
                self.notices.append(note)
            return outcome
        if report.clean:
            return outcome
        return replace(
            outcome,
            content=diagnostics.attach(outcome.content, report),
            detail=f"{outcome.detail}, checker reported".strip(", "),
        )

    def run_command(self, arguments: dict[str, Any]) -> ToolOutcome:
        command = arguments.get("command")
        if isinstance(command, str):
            # A model that sends a string instead of an array is common enough
            # that guessing is better than refusing, but only when the string
            # has no shell metacharacters -- there is no shell to interpret them.
            if any(ch in command for ch in "|&;<>$`"):
                raise ToolError(
                    "command must be an array of arguments and cannot use shell syntax "
                    "(pipes, redirection, substitution). Run the pieces separately."
                )
            command = command.split()
        if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
            raise ToolError("command must be a non-empty array of strings")
        timeout = float(arguments.get("timeout") or self.config.policy.command_timeout_seconds)
        # The same execution path the batch loop uses, so the sandbox is a
        # configuration choice rather than a property of which entry point the
        # operator happened to start. `kind: local` runs on this machine, which
        # is what a conversation in the operator's own directory wants;
        # `kind: docker` puts the same call in a container with no network.
        result = build_sandbox(self.config.sandbox).run(
            command,
            workspace=self.workspace.root,
            timeout=timeout,
            max_output_chars=self.config.budgets.max_output_chars,
            environment=_command_environment(self.config),
        )
        printable = " ".join(command)
        if result.timed_out:
            return ToolOutcome(
                f"Command timed out after {timeout:g}s.\n{result.output}",
                display=printable,
                detail=f"timed out after {timeout:g}s",
                ok=False,
            )
        # A non-zero exit is information, not a failure of the tool. The batch
        # path raises here; in a conversation the model needs to read the
        # failing test output, which raising would replace with an exception.
        body = result.output.strip() or "(no output)"
        head = f"exit code {result.returncode}\n" if result.returncode != 0 else ""
        lines = len(result.output.splitlines())
        return ToolOutcome(
            f"{head}{body}",
            display=printable,
            detail=(
                f"exit {result.returncode}"
                if result.returncode
                else f"{lines} line{'s' if lines != 1 else ''}"
            ),
            ok=result.returncode == 0,
        )

    # ---------------------------------------------------------------- helpers

    def _resolve(self, raw: Any, *, must_exist: bool = False) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("a path is required")
        candidate = raw.strip()
        # An absolute path inside the working directory is a natural thing for
        # a model to echo back after a read, so it is accepted and relativized
        # rather than refused; one outside it is still a containment error.
        if os.path.isabs(candidate):
            try:
                candidate = str(Path(candidate).resolve().relative_to(self.workspace.root))
            except ValueError as exc:
                raise WorkspaceError(
                    f"path is outside the working directory: {raw}"
                ) from exc
        return self.workspace.resolve(candidate, must_exist=must_exist)

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.workspace.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _label(self, path: Path) -> str:
        return self._relative(path) or "."


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _walk(root: Path) -> Iterator[Path]:
    """Every file under *root*, skipping build and vendor directories.

    os.walk with in-place pruning rather than rglob: rglob descends into
    node_modules and .git first and filters afterwards, which on a real
    repository is the difference between instant and unusable.
    """
    for base, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            yield Path(base) / name


def _read_text(path: Path) -> str:
    if path.stat().st_size > 8_000_000:
        raise ToolError(f"{path.name} is larger than 8 MB; read a slice with offset and limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        if path.suffix.lower() in TEXT_SUFFIX_HINT:
            raise ToolError(f"{path.name} is not valid UTF-8") from exc
        raise ToolError(f"{path.name} is not a UTF-8 text file") from exc


def _count_changes(before: str, after: str) -> tuple[int, int]:
    """Lines added and removed, for the one-line summary under a tool card."""
    import difflib  # noqa: PLC0415 - only needed when something actually changed

    added = removed = 0
    for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), lineterm="", n=0
    ):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def unified_diff(before: str, after: str, path: str, context: int = 3) -> str:
    """A diff for the approval prompt, so consent is informed by the change."""
    import difflib  # noqa: PLC0415

    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=path,
            tofile=path,
            lineterm="",
            n=context,
        )
    )


def _command_environment(config: HarnessConfig) -> dict[str, str] | None:
    """The environment a model-run command gets.

    Inherited rather than stripped when the sandbox is local: the point of this
    REPL is to run the operator's real toolchain in their real directory, and a
    command without PATH, HOME or a virtualenv is not that. The secrets in it
    are the operator's own and were already readable by anything they ran
    themselves.

    Under a real sandbox that reasoning does not hold, because the whole point
    is that the command is not trusted with the operator's environment.
    Returning None hands the decision to the sandbox, which builds a minimal
    one of its own.
    """
    if config.sandbox.kind != "local":
        return None
    return dict(os.environ)
