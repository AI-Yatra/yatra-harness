"""Typed tool registry and policy-governed native/MCP tool implementations."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harness.core.contracts import RiskLevel, SkillContract, ToolResult, ToolSpec
from harness.core.errors import MCPError, ToolError, WorkspaceError
from harness.core.util import truncate
from harness.execution.mcp import MCPStdioClient
from harness.execution.policy import PolicyEngine
from harness.execution.process import run_process
from harness.execution.retrieval import (
    BM25Index,
    EmbeddingIndex,
    embedding_request,
    iter_chunks,
    parse_embeddings,
    render_hits,
    workspace_signature,
)
from harness.execution.sandbox import build_sandbox
from harness.execution.search import build_request, parse_results, render
from harness.execution.workspace import Workspace
from harness.models import auth
from harness.record.artifacts import ArtifactStore

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import HarnessConfig, MCPServerConfig

ToolHandler = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(
        self,
        policy: PolicyEngine,
        *,
        max_output_chars: int,
        artifacts: ArtifactStore,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.policy = policy
        self.max_output_chars = max_output_chars
        self.artifacts = artifacts
        self.event_callback = event_callback or (lambda _event, _payload: None)
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ToolError(f"duplicate tool registration: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(item.spec for item in sorted(self._tools.values(), key=lambda item: item.spec.name))

    def execute(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        registered = self._tools.get(name)
        if registered is None:
            return ToolResult(call_id, name, False, "", error=f"unknown tool: {name}")
        try:
            validate_json_schema(arguments, registered.spec.input_schema, f"tool.{name}.arguments")
        except ToolError as exc:
            return ToolResult(call_id, name, False, "", error=str(exc))
        decision = self.policy.evaluate(registered.spec, arguments)
        self.event_callback(
            "POLICY_DECISION",
            {
                "call_id": call_id,
                "tool": name,
                "allowed": decision.allowed,
                "requires_approval": decision.requires_approval,
                "reason": decision.reason,
            },
        )
        if not decision.allowed:
            return ToolResult(call_id, name, False, "", error=decision.reason)
        try:
            content, metadata = registered.handler(arguments)
            bounded, was_truncated = truncate(content, self.max_output_chars)
            if was_truncated:
                reference = self.artifacts.write_payload(f"tool-{name}", content)
                metadata = {**metadata, "truncated": True, "artifact_ref": reference}
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=True,
                content=bounded,
                metadata=metadata,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except (ToolError, WorkspaceError, MCPError) as exc:
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                content="",
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # isolate unexpected tool failures from the loop
            return ToolResult(
                call_id=call_id,
                name=name,
                ok=False,
                content="",
                error=f"unexpected tool failure: {type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )


def validate_json_schema(value: Any, spec: dict[str, Any], path: str) -> None:
    expected = spec.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolError(f"{path} must be an object")
        required = spec.get("required", [])
        for key in required:
            if key not in value:
                raise ToolError(f"{path}.{key} is required")
        properties = spec.get("properties", {})
        if spec.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ToolError(f"{path} contains unknown fields: {', '.join(unknown)}")
        for key, item in value.items():
            if key in properties:
                validate_json_schema(item, properties[key], f"{path}.{key}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ToolError(f"{path} must be an array")
        item_spec = spec.get("items", {})
        for index, item in enumerate(value):
            validate_json_schema(item, item_spec, f"{path}[{index}]")
        return
    if expected == "string" and not isinstance(value, str):
        raise ToolError(f"{path} must be a string")
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ToolError(f"{path} must be an integer")
    if expected == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise ToolError(f"{path} must be a number")
    if expected == "boolean" and not isinstance(value, bool):
        raise ToolError(f"{path} must be a boolean")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError(f"{path} must be one of {spec['enum']!r}")


def _register_delegate(
    registry: ToolRegistry,
    config: HarnessConfig,
    workspace: Workspace,
    dispatcher: Any,
) -> None:
    agents = ", ".join(sorted(config.subagents.agents))
    registry.register(
        ToolSpec(
            "delegate",
            "Ask a read-only sub-agent a question about this workspace and get "
            f"its report back. Available agents: {agents}.",
            _object_schema(
                {"agent": {"type": "string"}, "objective": {"type": "string"}},
                ("agent", "objective"),
            ),
            # CONTROL rather than EXECUTE: delegation spends budget and starts
            # another run, but it cannot itself change the workspace.
            RiskLevel.CONTROL,
        ),
        lambda args: dispatcher(str(args["agent"]), str(args["objective"])),
    )


def build_registry(
    config: HarnessConfig,
    skill: SkillContract,
    workspace: Workspace,
    artifacts: ArtifactStore,
    policy: PolicyEngine,
    event_callback: EventCallback | None = None,
    dispatcher: Any = None,
) -> ToolRegistry:
    registry = ToolRegistry(
        policy,
        max_output_chars=config.budgets.max_output_chars,
        artifacts=artifacts,
        event_callback=event_callback,
    )
    _register_native(registry, config, workspace)
    if config.subagents.enabled and dispatcher is not None:
        _register_delegate(registry, config, workspace, dispatcher)
    for server in config.mcp_servers:
        if server.enabled:
            _register_mcp(registry, server, workspace)
    missing = sorted(set(skill.allowed_tools) - {spec.name for spec in registry.specs()})
    if missing:
        raise ToolError(f"skill enables tools that are not registered: {', '.join(missing)}")
    return registry


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _register_native(registry: ToolRegistry, config: HarnessConfig, workspace: Workspace) -> None:
    object_schema = _object_schema
    registry.register(
        ToolSpec(
            "repo_tree",
            "List a bounded tree of files in the run workspace.",
            object_schema({"max_entries": {"type": "integer"}}),
            RiskLevel.READ,
        ),
        lambda args: _repo_tree(workspace, args, config.context_repo_entries),
    )
    registry.register(
        ToolSpec(
            "search_repo",
            "Search UTF-8 text files in the workspace for a literal string.",
            object_schema(
                {"query": {"type": "string"}, "max_matches": {"type": "integer"}},
                ("query",),
            ),
            RiskLevel.READ,
        ),
        lambda args: _search_repo(workspace, args),
    )
    registry.register(
        ToolSpec(
            "read_file",
            "Read a UTF-8 file inside the run workspace.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                ("path",),
            ),
            RiskLevel.READ,
        ),
        lambda args: _read_file(workspace, args),
    )
    registry.register(
        ToolSpec(
            "apply_patch",
            "Apply a unified git patch inside the run workspace.",
            object_schema({"patch": {"type": "string"}}, ("patch",)),
            RiskLevel.WRITE,
        ),
        lambda args: _apply_patch(workspace, args, config.budgets.max_output_chars),
    )
    registry.register(
        ToolSpec(
            "run_command",
            "Run an allowlisted command without a shell in the workspace.",
            object_schema(
                {"command": {"type": "array", "items": {"type": "string"}}}, ("command",)
            ),
            RiskLevel.EXECUTE,
        ),
        lambda args: _run_command(workspace, args, config),
    )
    registry.register(
        ToolSpec(
            "python_run",
            "Run a workspace-relative Python script in isolated interpreter mode.",
            object_schema(
                {
                    "path": {"type": "string"},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                },
                ("path",),
            ),
            RiskLevel.EXECUTE,
        ),
        lambda args: _python_run(workspace, args, config),
    )
    registry.register(
        ToolSpec(
            "git_diff",
            "Return the current workspace diff and changed paths.",
            object_schema({}),
            RiskLevel.READ,
        ),
        lambda args: _git_diff(workspace, config),
    )
    registry.register(
        ToolSpec(
            "browser_fetch",
            "Fetch one allowlisted public HTTP(S) URL without following redirects.",
            object_schema({"url": {"type": "string"}}, ("url",)),
            RiskLevel.NETWORK,
        ),
        lambda args: _browser_fetch(args, config),
    )
    registry.register(
        ToolSpec(
            "retrieve",
            "Find the parts of the workspace most relevant to a question, "
            "ranked, as excerpts with their file and line range.",
            object_schema(
                {"query": {"type": "string"}, "limit": {"type": "integer"}}, ("query",)
            ),
            RiskLevel.READ,
        ),
        lambda args: _retrieve(workspace, args, config),
    )
    registry.register(
        ToolSpec(
            "web_search",
            "Search the public web and return titles, URLs and snippets.",
            object_schema({"query": {"type": "string"}}, ("query",)),
            RiskLevel.NETWORK,
        ),
        lambda args: _web_search(args, config),
    )
    registry.register(
        ToolSpec(
            "finish",
            "Submit a completion claim. The harness will independently verify it.",
            object_schema({"summary": {"type": "string"}}, ("summary",)),
            RiskLevel.CONTROL,
        ),
        lambda args: (args["summary"], {"control": "finish"}),
    )


def _repo_tree(
    workspace: Workspace, arguments: dict[str, Any], default_entries: int
) -> tuple[str, dict[str, Any]]:
    maximum = min(max(arguments.get("max_entries", default_entries), 1), 500)
    entries = []
    for path in sorted(workspace.root.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(workspace.root).as_posix()
        if path.is_symlink():
            entries.append(relative + " -> <symlink>")
        elif path.is_dir():
            entries.append(relative + "/")
        elif path.is_file():
            entries.append(relative)
        if len(entries) >= maximum:
            break
    return "\n".join(entries), {"entries": len(entries), "capped": len(entries) == maximum}


def _search_repo(workspace: Workspace, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    query = arguments["query"]
    if not query:
        raise ToolError("search query cannot be empty")
    maximum = min(max(arguments.get("max_matches", 50), 1), 200)
    matches = []
    scanned = 0
    for path in sorted(workspace.root.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        scanned += 1
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if query in line:
                        matches.append(
                            f"{path.relative_to(workspace.root).as_posix()}:{line_number}:{line.rstrip()}"
                        )
                        if len(matches) >= maximum:
                            return "\n".join(matches), {
                                "matches": len(matches),
                                "files_scanned": scanned,
                                "capped": True,
                            }
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(matches), {"matches": len(matches), "files_scanned": scanned, "capped": False}


def _read_file(workspace: Workspace, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    path = workspace.resolve(arguments["path"], must_exist=True)
    if path.is_symlink() or not path.is_file():
        raise ToolError("read_file requires a regular non-symlink file")
    start = max(arguments.get("start_line", 1), 1)
    end = arguments.get("end_line", start + 399)
    if end < start or end - start > 2_000:
        raise ToolError("invalid or excessive line range")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError(f"could not read UTF-8 file: {exc}") from exc
    selected = [f"{index:5d} | {lines[index - 1]}" for index in range(start, min(end, len(lines)) + 1)]
    return "\n".join(selected), {
        "path": workspace.relative(path),
        "start_line": start,
        "end_line": min(end, len(lines)),
        "total_lines": len(lines),
    }


def _patch_header_paths(patch: str) -> tuple[str, ...]:
    paths = []
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        if not value:
            raise ToolError("patch contains an empty file path")
        paths.append(value)
    if not paths:
        raise ToolError("patch does not contain unified diff file headers")
    return tuple(dict.fromkeys(paths))


def _recount_patch_hunk_headers(patch: str) -> str:
    """Recount old/new line counts in unified diff hunk headers.

    LLMs frequently emit patches with off-by-one errors in the hunk header
    counts (e.g. ``@@ -5,7 +5,8 @@`` when the hunk actually contains 6 old
    lines), and they sometimes omit the leading ``+`` on every line of a
    new-file patch (writing bare file content after ``@@ -0,0 +1,N @@``).
    Git's ``apply`` is strict and rejects both cases with ``error: corrupt
    patch at <stdin>:N``. This function rewrites the header so the declared
    counts match the body, preserving the optional function-section label
    (the trailing text after the second ``@@`` in ``@@ -X,Y +A,B @@ label``),
    and it adds the missing ``+`` prefix to bare lines inside new-file hunks.
    """
    import re

    hunk_header = re.compile(
        r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
    )

    lines = patch.splitlines(keepends=True)
    out: list[str] = []
    in_hunk = False
    is_new_file = False  # True when the previous file header was `--- /dev/null`
    old_count = 0
    new_count = 0
    header_idx = -1  # index in `out` of the most recent hunk header line
    label = ""

    for line in lines:
        match = hunk_header.match(line.rstrip("\n").rstrip("\r"))
        if match is not None:
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            label = match.group(5) or ""
            old_count = 0
            new_count = 0
            in_hunk = True
            header_idx = len(out)
            out.append(f"@@ -{old_start} +{new_start} @@{label}\n")
            continue
        if not in_hunk:
            # Track whether the previous file header was a new-file patch.
            if line.startswith("--- /dev/null"):
                is_new_file = True
            elif line.startswith("--- "):
                is_new_file = False
            out.append(line)
            continue
        # A diff/--- /+++ line terminates the current hunk.
        if line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("+++ "):
            if header_idx >= 0:
                old_start_re = re.match(r"^@@ -(\d+)", out[header_idx])
                new_start_re = re.match(r"^@@ -\d+ \+(\d+)", out[header_idx])
                if old_start_re and new_start_re:
                    out[header_idx] = (
                        f"@@ -{old_start_re.group(1)},{old_count} "
                        f"+{new_start_re.group(1)},{new_count} @@{label}\n"
                    )
            old_count = 0
            new_count = 0
            in_hunk = False
            header_idx = -1
            out.append(line)
            continue
        if line.startswith("+"):
            new_count += 1
            out.append(line)
        elif line.startswith("-"):
            old_count += 1
            out.append(line)
        elif line.startswith("\\"):
            # "\ No newline at end of file" does not change line counts.
            out.append(line)
        elif is_new_file:
            # New-file hunk: every bare line is an addition; prefix with `+`.
            new_count += 1
            out.append("+" + line)
        else:
            # Context line (or blank, which is also a context line).
            old_count += 1
            new_count += 1
            out.append(line)

    if in_hunk and header_idx >= 0:
        old_start_re = re.match(r"^@@ -(\d+)", out[header_idx])
        new_start_re = re.match(r"^@@ -\d+ \+(\d+)", out[header_idx])
        if old_start_re and new_start_re:
            out[header_idx] = (
                f"@@ -{old_start_re.group(1)},{old_count} "
                f"+{new_start_re.group(1)},{new_count} @@{label}\n"
            )
    return "".join(out)


def _apply_patch(
    workspace: Workspace, arguments: dict[str, Any], max_output: int
) -> tuple[str, dict[str, Any]]:
    raw_patch = arguments["patch"]
    patch = _recount_patch_hunk_headers(raw_patch)
    # Some models emit unified diffs whose last line lacks a terminating
    # newline. `git apply` then reads the trailing line together with the
    # hunk header and refuses with "corrupt patch at <stdin>:N". Append a
    # newline if missing so the patch is always well-formed.
    if not patch.endswith("\n"):
        patch = patch + "\n"
    paths = _patch_header_paths(patch)
    for relative in paths:
        workspace.ensure_writable(relative)
    # Write the patch to a temp file rather than passing it on stdin.
    # subprocess.Popen with text=True on Windows translates \n to \r\n
    # on the way to the child, which `git apply` treats as a context
    # mismatch against the LF file on disk. Using a file path preserves
    # the bytes the model produced.
    patch_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        suffix=".diff",
        delete=False,
    )
    try:
        patch_file.write(patch)
        patch_file.close()
        patch_path = patch_file.name
        check = run_process(
            ["git", "apply", "--check", "--whitespace=nowarn", patch_path],
            cwd=workspace.root,
            timeout=20,
            max_output_chars=max_output,
        )
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass
    if check.returncode != 0:
        # Re-open the file (it's already deleted) and re-create it for the reverse check.
        patch_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            suffix=".diff",
            delete=False,
        )
        patch_file.write(patch)
        patch_file.close()
        patch_path = patch_file.name
        try:
            reverse = run_process(
                ["git", "apply", "--reverse", "--check", "--whitespace=nowarn", patch_path],
                cwd=workspace.root,
                timeout=20,
                max_output_chars=max_output,
            )
        finally:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
        if reverse.returncode == 0:
            return "patch was already applied; treated as an idempotent success", {
                "paths": list(paths),
                "already_applied": True,
            }
        # First fallback: 3-way merge. Tolerant of whitespace and small
        # context mismatches, and uses the git index to resolve changes.
        patch_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            suffix=".diff",
            delete=False,
        )
        patch_file.write(patch)
        patch_file.close()
        patch_path = patch_file.name
        # `git apply --3way` writes conflict markers into the file and leaves
        # unmerged entries in the index *before* exiting non-zero. Reporting
        # that failure honestly is not enough: the workspace is damaged, every
        # later `git diff` is misleading, and the next turn reads a file full
        # of `<<<<<<< ours`. So take a snapshot first and put it back unless
        # the merge came out clean.
        snapshot = _snapshot(workspace, paths)
        try:
            three_way = run_process(
                ["git", "apply", "--check", "--3way", "--whitespace=nowarn", patch_path],
                cwd=workspace.root,
                timeout=20,
                max_output_chars=max_output,
            )
            applied_3way = None
            if three_way.returncode == 0:
                applied_3way = run_process(
                    ["git", "apply", "--3way", "--whitespace=nowarn", patch_path],
                    cwd=workspace.root,
                    timeout=20,
                    max_output_chars=max_output,
                )
        finally:
            try:
                os.unlink(patch_path)
            except OSError:
                pass
        merged_cleanly = (
            three_way.returncode == 0
            and applied_3way is not None
            and applied_3way.returncode == 0
            and not _unmerged_paths(workspace)
        )
        if not merged_cleanly:
            conflicted = bool(_unmerged_paths(workspace))
            _restore(workspace, snapshot)
            if conflicted:
                raise ToolError(
                    "patch conflicts with the current contents and was not applied; "
                    "the workspace is unchanged. Read the file as it is now and "
                    "write a patch against that."
                )
        if merged_cleanly:
            return (
                f"applied patch to {len(paths)} path(s) via 3-way merge: "
                f"{', '.join(paths)}"
            ), {
                "paths": list(paths),
                "already_applied": False,
                "applied_via": "three_way_merge",
            }
        raise ToolError(
            f"patch failed validation: {check.output.strip()}"
        )
    # The first apply succeeds, so write the patch file again to apply it
    # (the previous temp file was deleted after the check).
    patch_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        suffix=".diff",
        delete=False,
    )
    patch_file.write(patch)
    patch_file.close()
    patch_path = patch_file.name
    try:
        applied = run_process(
            ["git", "apply", "--whitespace=nowarn", patch_path],
            cwd=workspace.root,
            timeout=20,
            max_output_chars=max_output,
        )
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass
    if applied.returncode != 0:
        raise ToolError(f"patch could not be applied: {applied.output.strip()}")
    return f"applied patch to {len(paths)} path(s): {', '.join(paths)}", {
        "paths": list(paths),
        "already_applied": False,
    }


def _snapshot(workspace: Workspace, paths: tuple[str, ...]) -> dict[str, bytes | None]:
    """The exact bytes of each path, or None where the path does not exist.

    Bytes rather than a git operation on purpose. Restoring from HEAD would
    discard whatever the agent had already done and not committed, which in a
    normal run is everything it has done.
    """
    captured: dict[str, bytes | None] = {}
    for relative in paths:
        try:
            path = workspace.resolve(relative)
        except WorkspaceError:
            continue
        try:
            captured[relative] = path.read_bytes()
        except (OSError, ValueError):
            captured[relative] = None
    return captured


def _unmerged_paths(workspace: Workspace) -> tuple[str, ...]:
    """Paths git considers conflicted, which a `--3way` failure leaves behind."""
    result = run_process(
        ["git", "ls-files", "--unmerged", "--"],
        cwd=workspace.root,
        timeout=20,
        max_output_chars=8_000,
    )
    if result.returncode != 0:
        return ()
    return tuple({line.split("\t")[-1] for line in result.output.splitlines() if line})


def _restore(workspace: Workspace, snapshot: dict[str, bytes | None]) -> None:
    """Put the snapshotted paths back and clear the conflicted index."""
    for relative, content in snapshot.items():
        try:
            path = workspace.resolve(relative)
        except WorkspaceError:
            continue
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except OSError:
            continue
    if snapshot:
        # `git reset` on the paths drops the unmerged stage entries; without
        # it the index stays conflicted even though the files are correct.
        run_process(
            ["git", "reset", "-q", "--", *snapshot],
            cwd=workspace.root,
            timeout=20,
            max_output_chars=4_000,
        )


def _normalize_command(command: list[str], config: HarnessConfig) -> list[str]:
    """Point `python` at the interpreter that will actually be used.

    On the host that is this venv's interpreter, so a command reaches the same
    Python the harness runs under. Inside a container it is emphatically not:
    the host's absolute venv path does not exist there, and substituting it
    produces a "no such file" that looks nothing like its cause.
    """
    if config.sandbox.kind != "local":
        return list(command)
    if command and command[0] in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    return list(command)


def _run_command(
    workspace: Workspace, arguments: dict[str, Any], config: HarnessConfig
) -> tuple[str, dict[str, Any]]:
    command = _normalize_command(arguments["command"], config)
    result = build_sandbox(config.sandbox).run(
        command,
        workspace=workspace.root,
        timeout=config.policy.command_timeout_seconds,
        max_output_chars=config.budgets.max_output_chars,
    )
    metadata = {
        "command": list(arguments["command"]),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
    }
    if result.timed_out:
        raise ToolError(f"command timed out after {config.policy.command_timeout_seconds}s")
    if result.returncode != 0:
        raise ToolError(f"command exited {result.returncode}:\n{result.output}")
    return result.output, metadata


def _python_run(
    workspace: Workspace, arguments: dict[str, Any], config: HarnessConfig
) -> tuple[str, dict[str, Any]]:
    script = workspace.resolve(arguments["path"], must_exist=True)
    if script.suffix != ".py" or script.is_symlink() or not script.is_file():
        raise ToolError("python_run requires a regular workspace-relative .py file")
    extra = arguments.get("arguments", [])
    # Inside a container the script is addressed by its workspace-relative
    # path and run by the image's own `python`; the host's interpreter path
    # and the host's absolute script path both mean nothing there.
    if config.sandbox.kind == "local":
        argv = [sys.executable, "-I", str(script), *extra]
    else:
        argv = ["python", "-I", workspace.relative(script), *extra]
    result = build_sandbox(config.sandbox).run(
        argv,
        workspace=workspace.root,
        timeout=config.policy.command_timeout_seconds,
        max_output_chars=config.budgets.max_output_chars,
    )
    if result.timed_out:
        raise ToolError("python script timed out")
    if result.returncode != 0:
        raise ToolError(f"python script exited {result.returncode}:\n{result.output}")
    return result.output, {"path": workspace.relative(script), "returncode": result.returncode}


def _git_diff(
    workspace: Workspace, config: HarnessConfig
) -> tuple[str, dict[str, Any]]:
    diff = run_process(
        ["git", "diff", "--", "."],
        cwd=workspace.root,
        timeout=20,
        max_output_chars=config.budgets.max_output_chars,
    )
    names = run_process(
        ["git", "diff", "--name-only", "--", "."],
        cwd=workspace.root,
        timeout=20,
        max_output_chars=config.budgets.max_output_chars,
    )
    if diff.returncode != 0 or names.returncode != 0:
        raise ToolError("git diff failed")
    paths = [line for line in names.output.splitlines() if line]
    return diff.output, {"changed_paths": paths, "count": len(paths)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _validate_public_url(url: str, allowed_domains: tuple[str, ...]) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("browser_fetch requires an http or https URL with a host")
    hostname = parsed.hostname.rstrip(".").lower()
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
        raise ToolError(f"URL host is not allowlisted: {hostname}")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ToolError(f"URL host could not be resolved: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any(
            (
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        ):
            raise ToolError(f"URL resolved to a non-public address: {address}")
    return parsed


def _browser_fetch(arguments: dict[str, Any], config: HarnessConfig) -> tuple[str, dict[str, Any]]:
    url = arguments["url"]
    _validate_public_url(url, config.policy.allowed_domains)
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "yatra-harness/1.0"})
    try:
        with opener.open(request, timeout=config.policy.browser_timeout_seconds) as response:
            raw = response.read(config.budgets.max_output_chars + 1)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            if len(raw) > config.budgets.max_output_chars:
                text = text[: config.budgets.max_output_chars]
            return text, {
                "url": url,
                "status": response.status,
                "content_type": content_type,
                "bytes": len(raw),
                "redirects_followed": 0,
            }
    except urllib.error.HTTPError as exc:
        raise ToolError(f"HTTP request failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"HTTP request failed: {exc.reason}") from exc


# Keyed by workspace and backend, and invalidated by a cheap signature of the
# tree. The agent patches files as it works, so an index built on turn two is
# wrong by turn four.
_INDEX_CACHE: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}
# Bounded because the process outlives the run. `harness loop` and
# `harness goal` create a workspace per feature or per attempt, and an
# unbounded cache would hold every one of their chunk sets -- with the
# embedding backend, every one of their vector sets -- until the process
# exited. A run only ever queries its own workspace, so a handful is plenty.
_INDEX_CACHE_LIMIT = 4


def _remember_index(
    key: tuple[str, str], signature: tuple[int, int], index: Any
) -> None:
    _INDEX_CACHE[key] = (signature, index)
    while len(_INDEX_CACHE) > _INDEX_CACHE_LIMIT:
        # Insertion-ordered, so the oldest workspace leaves first.
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))


def _retrieve(
    workspace: Workspace, arguments: dict[str, Any], config: HarnessConfig
) -> tuple[str, dict[str, Any]]:
    """Rank workspace chunks against a question.

    The index is built once per workspace and reused. Rebuilding it on every
    call would re-read the repository -- and, with the embedding backend,
    re-embed all of it -- for each question the agent asks.
    """
    settings = config.retrieval
    key = (str(workspace.root), settings.kind)
    signature = workspace_signature(workspace.root, settings)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        index = cached[1]
    else:
        chunks = iter_chunks(workspace.root, settings)
        if settings.kind == "embedding":
            index = EmbeddingIndex(chunks, lambda texts: _embed(texts, config))
        else:
            index = BM25Index(chunks)
        _remember_index(key, signature, index)
    limit = int(arguments.get("limit") or settings.limit)
    hits = index.search(str(arguments["query"]), limit=max(1, min(limit, 20)))
    return render_hits(hits), {
        "backend": settings.kind,
        "query": str(arguments["query"]),
        "hits": len(hits),
        "paths": [f"{hit.chunk.path}:{hit.chunk.start_line}" for hit in hits],
    }


def _embed(texts: list[str], config: HarnessConfig) -> list[list[float]]:
    """One /embeddings call, batched, with the key in a header."""
    settings = config.retrieval
    secret = auth.resolve_env(settings.api_key_env).key if settings.api_key_env else ""
    request = embedding_request(settings, texts, key=secret)
    host = urllib.parse.urlparse(request.url).hostname or ""
    _validate_public_url(request.url, (*config.policy.allowed_domains, host))
    http_request = urllib.request.Request(
        request.url,
        data=request.body.encode("utf-8"),
        headers={"User-Agent": "yatra-harness/1.0", **request.headers},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            http_request, timeout=max(config.policy.browser_timeout_seconds, 30)
        ) as response:
            payload = response.read().decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as exc:
        # The status, never the body: a failed embeddings call can echo the
        # key back and must not reach an observation.
        raise ToolError(f"embeddings request failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"embeddings request failed: {exc.reason}") from exc
    return parse_embeddings(payload, len(texts))


def _web_search(arguments: dict[str, Any], config: HarnessConfig) -> tuple[str, dict[str, Any]]:
    """Run one search through the configured backend.

    The search endpoint is reached under the same SSRF and allowlist rules as
    browser_fetch, with one addition: the backend's own host is allowlisted
    implicitly. Requiring an operator to also list `api.search.brave.com` in
    allowed_domains after configuring it as their search backend would be a
    trap, not a control -- they have already said where search goes.
    """
    settings = config.search
    key = ""
    if settings.api_key_env:
        key = auth.resolve_env(settings.api_key_env).key
    request = build_request(settings, str(arguments["query"]), key=key)
    domains = (*config.policy.allowed_domains, settings.host) if settings.host else config.policy.allowed_domains
    _validate_public_url(request.url, domains)
    opener = urllib.request.build_opener(_NoRedirect())
    http_request = urllib.request.Request(
        request.url,
        data=request.body.encode("utf-8") if request.body else None,
        headers={"User-Agent": "yatra-harness/1.0", **request.headers},
        method=request.method,
    )
    try:
        with opener.open(http_request, timeout=config.policy.browser_timeout_seconds) as response:
            raw = response.read(config.budgets.max_output_chars * 4)
            charset = response.headers.get_content_charset() or "utf-8"
            payload = raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        # The status is the useful part; the body of a failed search request
        # can echo the key back and must not reach an observation.
        raise ToolError(f"search request failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"search request failed: {exc.reason}") from exc
    results = parse_results(settings, payload)
    return render(results), {
        "backend": settings.kind,
        "query": " ".join(str(arguments["query"]).split()),
        "results": len(results),
        "urls": [result.url for result in results],
    }


def _expanded_mcp_command(command: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sys.executable if part in {"{python}", "python", "python3"} and index == 0 else part for index, part in enumerate(command))


def _register_mcp(registry: ToolRegistry, server: MCPServerConfig, workspace: Workspace) -> None:
    command = _expanded_mcp_command(server.command)
    with MCPStdioClient(
        command,
        cwd=workspace.root,
        protocol_version=server.protocol_version,
        timeout_seconds=server.timeout_seconds,
    ) as client:
        discovered = client.list_tools()
    for raw in discovered:
        name = raw.get("name")
        description = raw.get("description", "MCP tool")
        input_schema = raw.get("inputSchema", {"type": "object"})
        if not isinstance(name, str) or not isinstance(description, str) or not isinstance(input_schema, dict):
            raise MCPError(f"MCP server {server.name!r} returned an invalid tool definition")

        def handler(arguments: dict[str, Any], *, tool_name: str = name) -> tuple[str, dict[str, Any]]:
            with MCPStdioClient(
                command,
                cwd=workspace.root,
                protocol_version=server.protocol_version,
                timeout_seconds=server.timeout_seconds,
            ) as active_client:
                result = active_client.call_tool(tool_name, arguments)
            if result.get("isError"):
                raise MCPError(f"MCP tool {tool_name!r} returned an error result")
            text_parts = [
                item.get("text", "")
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = "\n".join(text_parts)
            if not content and "structuredContent" in result:
                content = json.dumps(result["structuredContent"], sort_keys=True)
            return content, {
                "source": "mcp",
                "server": server.name,
                "structured_content": result.get("structuredContent"),
            }

        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema,
                risk=RiskLevel.READ,
                source=f"mcp:{server.name}",
            ),
            handler,
        )
