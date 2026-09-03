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


#: Things `cmd` interprets itself rather than loading from disk. A model on
#: Windows reaches for these constantly, and none of them is a file, so
#: executing directly finds nothing to run.
CMD_BUILTINS = frozenset({
    "del", "dir", "copy", "move", "ren", "rename", "erase", "type", "cls",
    "echo", "md", "mkdir", "rd", "rmdir", "set", "start", "call", "ver",
})

#: The conventional exit code for a command that could not be found.
NOT_FOUND = 127


def _not_found(program: str) -> str:
    """Why nothing ran, in terms the caller can act on.

    An agent asked to tidy up ran `del`, got "unexpected tool failure:
    FileNotFoundError: [WinError 2] The system cannot find the file
    specified", tried again with absolute paths, got the same, and gave up --
    leaving the files it was deleting behind. The error named no program and
    called routine a thing that happens whenever a model guesses a name.
    """
    if os.name == "nt" and program.strip().lower() in CMD_BUILTINS:
        return (
            f"{program!r} is not a program. Windows runs it inside cmd, and commands here "
            f"are executed directly with no shell, so there is nothing to interpret it. "
            f"Run 'cmd /c {program} ...' if you need it, or use the file tools."
        )
    return f"{program!r} is not a program on this system, or is not on PATH."


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
    if not normalized:
        # Windows raises a bare OSError 87 for this rather than the
        # FileNotFoundError the empty case morally deserves.
        return ProcessResult(
            command=(), returncode=NOT_FOUND, output="no command was given.",
            timed_out=False, truncated=False,
        )
    try:
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
    except (FileNotFoundError, NotADirectoryError):
        # A name that is not a program is ordinary information, the same shape
        # as any other failing command, so every caller already knows how to
        # read it. Raising sent it to the loop's catch-all instead, which
        # reports "unexpected tool failure" and names nothing.
        return ProcessResult(
            command=normalized,
            returncode=NOT_FOUND,
            output=_not_found(normalized[0] if normalized else ""),
            timed_out=False,
            truncated=False,
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

