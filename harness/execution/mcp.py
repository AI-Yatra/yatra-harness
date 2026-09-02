"""Minimal production-oriented MCP stdio client for tool discovery and calls.

Implements the stable 2025-11-25 initialize → initialized → operation lifecycle
over newline-delimited UTF-8 JSON-RPC 2.0. The version is configurable so a
server can negotiate a different supported revision explicitly.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from harness.core.errors import MCPError


class MCPStdioClient:
    def __init__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        protocol_version: str = "2025-11-25",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.protocol_version = protocol_version
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._next_id = 1
        self.server_info: dict[str, Any] = {}
        # Bounded: only the most recent stderr lines matter for a diagnosis.
        self._stderr_tail: deque[str] = deque(maxlen=50)

    def __enter__(self) -> MCPStdioClient:
        self.connect()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._process is not None:
            return
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise MCPError(f"could not start MCP server {self.command!r}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        # stderr must be drained: a server that logs enough to fill the pipe
        # buffer would otherwise block on write and hang every request.
        threading.Thread(target=self._read_stderr, daemon=True).start()
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "yatra-harness", "version": "1.0.0"},
            },
        )
        negotiated = result.get("protocolVersion")
        if negotiated != self.protocol_version:
            self.close()
            raise MCPError(
                f"MCP protocol mismatch: requested {self.protocol_version}, server selected {negotiated!r}"
            )
        self.server_info = dict(result.get("serverInfo", {}))
        self.notify("notifications/initialized", {})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise MCPError("MCP tools/list returned a non-list tools value")
        return [dict(tool) for tool in tools]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                self.notify(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": f"timeout after {self.timeout_seconds}s"},
                )
                raise MCPError(f"MCP request {method!r} timed out") from exc
            if message is None:
                tail = "\n".join(self._stderr_tail).strip()
                detail = f"; server stderr:\n{tail}" if tail else ""
                raise MCPError(f"MCP server exited while waiting for {method!r}{detail}")
            if isinstance(message, BaseException):
                raise MCPError(f"MCP reader failed: {message}") from message
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise MCPError(f"MCP {method} failed: {error}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError(f"MCP {method} returned an invalid result")
            return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        # The reader threads exit on EOF, but the pipe objects themselves are
        # only released when closed here; leaving them open leaks a file
        # descriptor per server for the life of the harness process.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MCPError("MCP server is not connected")
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if "\n" in encoded:
            raise MCPError("MCP stdio message unexpectedly contains a literal newline")
        process.stdin.write(encoded + "\n")
        process.stdin.flush()

    def _read_stderr(self) -> None:
        """Consume the server's stderr so it can never block on a full pipe.

        The tail is kept for diagnostics only; it is not part of the protocol.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._stderr_tail.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._messages.put(exc)
                    return
                if isinstance(message, dict):
                    self._messages.put(message)
            self._messages.put(None)
        except BaseException as exc:  # defensive boundary for a background reader
            self._messages.put(exc)

