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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.core.contracts import RiskLevel, ToolSpec
from harness.core.errors import ToolError, WorkspaceError
from harness.execution.process import run_process
from harness.execution.workspace import Workspace

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


class ReplToolset:
    """The registry, bound to one workspace root and one config."""

    def __init__(self, workspace: Workspace, config: HarnessConfig) -> None:
        self.workspace = workspace
        self.config = config
        self._handlers: dict[str, Handler] = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "glob": self.glob,
            "grep": self.grep,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_command": self.run_command,
        }

    # ------------------------------------------------------------ declaration

    def specs(self) -> tuple[ToolSpec, ...]:
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

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolOutcome(f"No such tool: {name}", ok=False)
        if "__parse_error__" in arguments:
            return ToolOutcome(str(arguments["__parse_error__"]), ok=False)
        try:
            return handler(arguments)
        except (ToolError, WorkspaceError) as exc:
            return ToolOutcome(str(exc), ok=False)
        except OSError as exc:
            return ToolOutcome(f"{type(exc).__name__}: {exc}", ok=False)

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
        try:
            expression = re.compile(raw)
        except re.error as exc:
            raise ToolError(f"not a valid regular expression: {exc}") from exc
        limit = int(arguments.get("limit") or 100)
        scope = self.workspace.root
        if arguments.get("path"):
            scope = self._resolve(str(arguments["path"]), must_exist=True)
        file_glob = str(arguments.get("glob") or "")
        hits: list[str] = []
        files = 0
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
        before = _read_text(path) if existed and path.is_file() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        added, removed = _count_changes(before, content)
        verb = "updated" if existed else "created"
        return ToolOutcome(
            f"{verb} {self._relative(path)} ({len(content.splitlines())} lines)",
            display=self._label(path),
            detail=f"+{added} -{removed}",
        )

    def edit_file(self, arguments: dict[str, Any]) -> ToolOutcome:
        path = self._resolve(arguments.get("path"), must_exist=True)
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("edit_file needs string old_string and new_string")
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
        return ToolOutcome(
            f"edited {self._relative(path)} in {where} (+{added} -{removed})",
            display=self._label(path),
            detail=f"+{added} -{removed}",
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
        result = run_process(
            command,
            cwd=self.workspace.root,
            timeout=timeout,
            max_output_chars=self.config.budgets.max_output_chars,
            environment=_command_environment(),
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


def _command_environment() -> dict[str, str]:
    """The environment a model-run command gets.

    Inherited rather than stripped: the point of this REPL is to run the
    operator's real toolchain in their real directory, and a command without
    PATH, HOME or a virtualenv is not that. Secrets are the operator's own
    and were already readable by anything they ran themselves.
    """
    return dict(os.environ)
