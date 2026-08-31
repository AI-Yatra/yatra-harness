"""A small standards-shaped MCP stdio server exposing read-only repository stats."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"


def response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def repo_stats(root: Path) -> dict[str, Any]:
    files = []
    total_bytes = 0
    suffixes: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        files.append(relative)
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        suffix = path.suffix or "<none>"
        suffixes[suffix] = suffixes.get(suffix, 0) + 1
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "suffixes": dict(sorted(suffixes.items())),
        "sample_files": files[:20],
    }


def handle(message: dict[str, Any], root: Path, initialized: bool) -> tuple[dict[str, Any] | None, bool]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        if requested != PROTOCOL_VERSION:
            return error(request_id, -32602, f"unsupported protocol version {requested!r}"), initialized
        return (
            response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "aiyatra-repo-stats", "version": "1.0.0"},
                },
            ),
            initialized,
        )
    if method == "notifications/initialized":
        return None, True
    if method == "notifications/cancelled":
        return None, initialized
    if not initialized:
        return error(request_id, -32002, "server has not completed initialization"), initialized
    if method == "tools/list":
        return (
            response(
                request_id,
                {
                    "tools": [
                        {
                            "name": "repo_stats",
                            "description": "Return bounded read-only statistics for the current workspace.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            ),
            initialized,
        )
    if method == "tools/call":
        if params.get("name") != "repo_stats":
            return error(request_id, -32601, "unknown tool"), initialized
        stats = repo_stats(root)
        return (
            response(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(stats, sort_keys=True)}],
                    "structuredContent": stats,
                    "isError": False,
                },
            ),
            initialized,
        )
    return error(request_id, -32601, f"method not found: {method}"), initialized


def main() -> int:
    root = Path.cwd().resolve()
    initialized = False
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            reply, initialized = handle(message, root, initialized)
        except (json.JSONDecodeError, ValueError) as exc:
            reply = error(None, -32700, f"parse error: {exc}")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

