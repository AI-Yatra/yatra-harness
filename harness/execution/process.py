"""Bounded subprocess execution without a shell and with process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.core.util import truncate


def _stop_group(process: subprocess.Popen, *, force: bool) -> None:
    """Stop the whole process group, tolerating a process that already died.

    The group can vanish between the timeout expiring and the kill landing.
    That race is a success -- the process is gone -- not an error worth
    surfacing, and raising from it turns a clean timeout into a crash.
    POSIX signals the session created by `start_new_session`, which reaches
    every descendant. Windows has no such session, and `terminate()` reaches
    only the direct child: a command that spawned its own subprocess left that
    grandchild running after a timeout, holding the file handles and the port
    the timeout was supposed to release. `taskkill /T` walks the parent-child
    tree Windows records and is the available equivalent, so it is tried first
    and the direct kill is what happens when it is not there.
    """
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        if _taskkill(process.pid, force=force):
            return
        if force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _taskkill(pid: int, *, force: bool) -> bool:
    """Kill a Windows process tree. False when taskkill is unavailable."""
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 128 is "no such process", which means the tree is already gone and the
    # caller has nothing left to do.
    return completed.returncode in (0, 128)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool
    truncated: bool


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    max_output_chars: int,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ProcessResult:
    normalized = tuple(str(part) for part in command)
    process = subprocess.Popen(
        normalized,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
        start_new_session=os.name == "posix",
    )
    timed_out = False
    try:
        output, _ = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_group(process, force=False)
        try:
            output, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _stop_group(process, force=True)
            output, _ = process.communicate()
    bounded, was_truncated = truncate(output or "", max_output_chars)
    return ProcessResult(
        command=normalized,
        returncode=process.returncode if process.returncode is not None else -1,
        output=bounded,
        timed_out=timed_out,
        truncated=was_truncated,
    )

