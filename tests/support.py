"""Shared test fixtures.

Only one thing lives here so far, and it earned its place by being wrong in
both copies. The delivery tests each carried their own `gh` stub written as a
`#!/bin/sh` script, which Windows cannot execute -- so on Windows the stub was
never found, the tests reached the developer's *real* `gh`, and thirteen of
them failed against real GitHub auth. They passed in CI only because the
runner has no credentials, which is the worst way for a test to be green.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def install_gh_stub(
    directory: Path, calls: Path, *, url: str = "https://example.invalid/pr/1", exit_code: int = 0
) -> Path:
    """Write a fake `gh` into *directory* and return the script's path.

    Two flavours, because an extension-less shell script is not executable on
    Windows. `shutil.which` there consults PATHEXT, so the stub has to be a
    `.cmd` -- the same mechanism that made a real `gh.cmd` shim invisible to
    delivery until it started resolving the executable instead of naming it.

    The caller puts *directory* on PATH; doing it here would leave the
    environment changed with no matching cleanup.
    """
    directory.mkdir(parents=True, exist_ok=True)
    newline = chr(10)
    if os.name == "nt":
        script = directory / "gh.cmd"
        script.write_text(
            newline.join(
                [
                    "@echo off",
                    f'>>"{calls}" echo %*',
                    f"echo {url}",
                    f"exit /b {exit_code}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return script
    script = directory / "gh"
    script.write_text(
        newline.join(
            [
                "#!/bin/sh",
                f'printf "%s" "$*" >> "{calls}"',
                f'printf "{newline}" >> "{calls}"',
                f'echo "{url}"',
                f"exit {exit_code}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script
