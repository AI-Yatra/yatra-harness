"""Where a tool command actually runs.

Path containment and a command allowlist confine the model's *interface*.
They do not confine the operating system: an allowlisted test runner is still
an ordinary process on the host, with the host's filesystem and the host's
network. docs/SECURITY.md has said so honestly for as long as it has existed,
and recommended a container. This is that container.

The rules a sandbox has to enforce are the ones an application-level gate
cannot: no network unless asked for, no new privileges, not root, bounded
memory and CPU, and nothing mounted except the run workspace. All of that
lives in `docker_command`, which is a pure function -- so those rules are
checked on every machine, and only the test that genuinely starts a container
needs docker installed.

Local execution stays the default. A workshop laptop without docker must
still be able to run the harness, and a teaching tool that refuses to start
teaches nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigurationError
from .process import ProcessResult, run_process

KINDS = ("local", "docker")
CONTAINER_WORKSPACE = "/workspace"


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    kind: str = "local"
    image: str = ""
    network: str = "none"
    memory: str = "2g"
    cpus: str = "2"
    user: str = ""
    docker: str = "docker"
    # "auto" appends :Z on an SELinux host. Without a label an SELinux system
    # denies the container every read of the mounted workspace, which surfaces
    # as an unexplained permission error inside an otherwise correct setup.
    selinux_label: str = "auto"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ConfigurationError(
                f"sandbox.kind must be one of {', '.join(KINDS)}; got {self.kind!r}"
            )
        if self.kind == "docker" and not self.image:
            raise ConfigurationError("sandbox.image is required when sandbox.kind is docker")


class Sandbox(Protocol):
    def run(
        self,
        command: list[str],
        *,
        workspace: Path,
        timeout: float,
        max_output_chars: int,
        environment: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Execute one command against the workspace and return its result."""


def _host_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


class LocalSandbox:
    """The original behaviour: a process on this machine, in the workspace."""

    def run(
        self,
        command: list[str],
        *,
        workspace: Path,
        timeout: float,
        max_output_chars: int,
        environment: dict[str, str] | None = None,
    ) -> ProcessResult:
        return run_process(
            command,
            cwd=workspace,
            timeout=timeout,
            max_output_chars=max_output_chars,
            environment=environment or _host_environment(),
        )


class DockerSandbox:
    """A throwaway container with the workspace mounted and nothing else."""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def run(
        self,
        command: list[str],
        *,
        workspace: Path,
        timeout: float,
        max_output_chars: int,
        environment: dict[str, str] | None = None,
    ) -> ProcessResult:
        argv = docker_command(self.config, command, workspace=workspace, timeout=timeout)
        result = run_process(
            argv,
            # `docker run` is a client; the working directory that matters is
            # --workdir inside the container, set by docker_command.
            cwd=workspace,
            timeout=timeout + 30,  # room for image pull and container setup
            max_output_chars=max_output_chars,
            environment={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
        return result


def docker_command(
    config: SandboxConfig, command: list[str], *, workspace: Path, timeout: float
) -> list[str]:
    """The `docker run` invocation for one command.

    Pure, so every containment rule below is asserted by tests that run
    everywhere rather than only where docker happens to be installed.
    """
    if config.user:
        user = config.user
    elif hasattr(os, "getuid"):
        user = f"{os.getuid()}:{os.getgid()}"
    else:
        user = ""
    argv = [
        config.docker, "run", "--rm",
        # No network unless the operator asked for one. An allowlist cannot
        # stop an allowlisted process from opening a socket; this can.
        "--network", config.network,
        "--security-opt", "no-new-privileges",
        # A container that can gain privileges is not a boundary. Dropping
        # capabilities costs nothing for the commands a harness runs.
        "--cap-drop", "ALL",
        "--memory", config.memory,
        "--cpus", config.cpus,
        "--pids-limit", "512",
    ]
    if user:
        # Matching the host uid keeps files the container writes owned by the
        # operator rather than by root, which would leave a workspace the
        # harness itself could not clean up.
        argv += ["--user", user]
    argv += [
        "--volume", f"{Path(workspace).resolve()}:{CONTAINER_WORKSPACE}{_mount_suffix(config)}",
        "--workdir", CONTAINER_WORKSPACE,
        "--env", "PYTHONNOUSERSITE=1",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "HOME=/tmp",
        config.image,
    ]
    # Appended unchanged. The local runner rewrites `python` to the host's
    # interpreter path, which does not exist inside the image.
    return [*argv, *command]


def _mount_suffix(config: SandboxConfig) -> str:
    """`:Z` where SELinux needs it, nothing where it does not.

    On an SELinux host an unlabelled bind mount is unreadable from inside the
    container, and the symptom -- a permission error on a file the operator
    can plainly see and owns -- points at everything except the real cause.
    `:Z` labels it for this container alone. The option is settable because
    the label suffix is rejected outright on some non-Linux docker hosts.
    """
    if config.selinux_label == "always":
        return ":Z"
    if config.selinux_label == "never":
        return ""
    return ":Z" if Path("/sys/fs/selinux/enforce").exists() else ""


def build_sandbox(config: SandboxConfig) -> Sandbox:
    return DockerSandbox(config) if config.kind == "docker" else LocalSandbox()


def sandbox_config_from_dict(raw: dict[str, Any] | None, path: str = "sandbox") -> SandboxConfig:
    from . import schema  # noqa: PLC0415 - avoids a cycle at import time

    value = schema.mapping(raw or {}, path)
    schema.reject_unknown(
        value,
        {"kind", "image", "network", "memory", "cpus", "user", "docker", "selinux_label"},
        path,
    )

    def text(name: str, default: str) -> str:
        return schema.string(value[name], f"{path}.{name}") if value.get(name) else default

    return SandboxConfig(
        kind=text("kind", "local"),
        image=text("image", ""),
        network=text("network", "none"),
        memory=text("memory", "2g"),
        cpus=text("cpus", "2"),
        user=text("user", ""),
        docker=text("docker", "docker"),
        selinux_label=text("selinux_label", "auto"),
    )
