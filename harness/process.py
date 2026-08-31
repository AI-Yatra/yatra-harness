"""Bounded subprocess execution without a shell and with process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .util import truncate


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
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            output, _ = process.communicate()
    bounded, was_truncated = truncate(output or "", max_output_chars)
    return ProcessResult(
        command=normalized,
        returncode=process.returncode if process.returncode is not None else -1,
        output=bounded,
        timed_out=timed_out,
        truncated=was_truncated,
    )

