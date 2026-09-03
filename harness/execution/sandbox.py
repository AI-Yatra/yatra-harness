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

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.core.errors import ConfigurationError
from harness.execution.process import ProcessResult, run_process

KINDS = ("local", "os", "docker")

#: Bound read-only so a command can find its interpreter and libraries. Bound
#: with `--ro-bind-try`, so one missing on a given distribution is skipped
#: rather than failing the whole sandbox.
SYSTEM_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt", "/nix")
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


class OsSandbox:
    """Confinement from the kernel, without a container.

    Docker is a strong boundary and a heavy one: an image to pull, a daemon to
    run, and a second filesystem where the operator's toolchain is not. That is
    right for an unattended run and wrong for a conversation in someone's own
    checkout, which is exactly where the harness previously had nothing.

    Both mechanisms here are the ones the reference implementations use.
    Neither exists on Windows in a form reachable from Python: job objects
    bound processes and memory but not files or sockets, and the primitives
    that would work need Win32 calls this codebase has no business making. So
    Windows falls back to running locally and says so, rather than reporting a
    confinement it is not providing.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        self.mechanism, self.reason = detect_mechanism()
        #: False when this host confines the filesystem but refuses to unshare
        #: the network. Recorded rather than silently accepted: an operator who
        #: asked for `network: none` and did not get it has to be told, because
        #: the whole value of a sandbox is knowing what it actually did.
        probe = probe_bubblewrap() if self.mechanism == "bubblewrap" else None
        self.network_confined = probe.network if probe else True
        if probe and not probe.network:
            self.reason = (
                "the filesystem is confined but this host refuses to unshare the "
                f"network, so a command can still reach it ({probe.reason})"
            )

    def run(
        self,
        command: list[str],
        *,
        workspace: Path,
        timeout: float,
        max_output_chars: int,
        environment: dict[str, str] | None = None,
    ) -> ProcessResult:
        root = Path(workspace).resolve()
        if self.mechanism == "bubblewrap":
            argv = bubblewrap_command(
                self.config, command, workspace=root, network=self.network_confined
            )
        elif self.mechanism == "seatbelt":
            argv = seatbelt_command(self.config, command, workspace=root)
        else:
            argv = list(command)
        return run_process(
            argv,
            cwd=root,
            timeout=timeout,
            max_output_chars=max_output_chars,
            environment=environment or _host_environment(),
        )


def detect_mechanism() -> tuple[str, str]:
    """Which kernel sandbox is available here, and why not when there is none.

    Reported rather than assumed, because a sandbox that silently is not one
    is worse than no sandbox: the operator relaxes on the strength of it.
    """
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec"):
            return "seatbelt", ""
        return "", "sandbox-exec not found"
    if sys.platform.startswith("linux"):
        if not shutil.which("bwrap"):
            return "", "bubblewrap (bwrap) is not installed"
        if not probe_bubblewrap().usable:
            return "", f"bubblewrap is installed but cannot run here: {probe_bubblewrap().reason}"
        return "bubblewrap", ""
    return "", f"no kernel sandbox is available on {sys.platform}; use kind: docker"


@dataclass(frozen=True, slots=True)
class Probe:
    """What bubblewrap can actually do on this host, as opposed to whether it exists."""

    usable: bool
    #: False when the filesystem can be confined but the network cannot.
    network: bool
    reason: str = ""


def _bwrap_ok(*, network: bool) -> tuple[bool, str]:
    """Run the real sandbox command over a scratch directory and see if it works.

    Built from `bubblewrap_command` rather than hand-written, because every
    hand-written probe so far has been wrong in a different direction. The
    first used `--ro-bind / /`, which is not a valid root, so bubblewrap
    refused it for a reason the real command never hits. The second used
    `--dev-bind / /`, which asks for *more* privilege than the real command --
    device access across the whole root -- and failed with "setting up uid
    map: Permission denied" on a host where the real command is fine.

    A probe that does not run what the sandbox runs is answering a different
    question. This one runs `true` through exactly the argv a tool call would
    get, so a pass means tool calls will work and a failure is the failure.
    """
    with tempfile.TemporaryDirectory() as scratch:
        argv = bubblewrap_command(
            SandboxConfig(kind="os"), ["true"], workspace=Path(scratch), network=network
        )
        try:
            result = subprocess.run(  # noqa: S603 - argv built by this module, no shell
                argv, capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return True, ""
    message = (result.stderr or result.stdout).strip().splitlines()
    return False, message[-1] if message else f"exit {result.returncode}"


@functools.lru_cache(maxsize=1)
def probe_bubblewrap() -> Probe:
    """Run bubblewrap once and find out what it can do here.

    Presence is not capability, and assuming it was cost us a sandbox nobody
    had ever exercised. `--unshare-net` asks the kernel to bring up a loopback
    interface in the new namespace, and inside a container or under a hardened
    AppArmor profile that is refused -- `Failed RTM_NEWADDR: Operation not
    permitted` -- so *every* command through the sandbox failed rather than
    running confined. The first CI run that installed bwrap found it
    immediately; a year of green suites had not, because the check skipped
    wherever bwrap was absent, which was everywhere.

    So the network is probed separately from the filesystem. A host that can
    confine files but not sockets still gets file confinement, and is told in
    those words rather than left to assume it got both.
    """
    full, reason = _bwrap_ok(network=True)
    if full:
        return Probe(usable=True, network=True)
    partial, plain_reason = _bwrap_ok(network=False)
    if partial:
        return Probe(usable=True, network=False, reason=reason)
    return Probe(usable=False, network=False, reason=plain_reason or reason)


def seatbelt_profile(config: SandboxConfig, *, workspace: Path) -> str:
    """An Apple Seatbelt profile allowing reads, writes only in the workspace.

    `sandbox-exec` is formally deprecated and has been for years, with no
    replacement offered for this use, which is why Codex still uses it. The
    risk is that a future macOS removes it; `detect_mechanism` notices that as
    a missing binary and falls back rather than failing.
    """
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{Path(workspace).as_posix()}"))',
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))',
    ]
    if config.network == "none":
        rules.append("(deny network*)")
    return "\n".join(rules)


def seatbelt_command(
    config: SandboxConfig, command: list[str], *, workspace: Path
) -> list[str]:
    """The `sandbox-exec` invocation for one command. Pure, so it is testable."""
    return [
        "sandbox-exec",
        "-p",
        seatbelt_profile(config, workspace=workspace),
        *command,
    ]


def bubblewrap_command(
    config: SandboxConfig, command: list[str], *, workspace: Path, network: bool = True
) -> list[str]:
    """The `bwrap` invocation for one command.

    Bubblewrap rather than Landlock as the primary mechanism, for a reason
    worth writing down: Landlock cannot restrict the network at all before ABI
    v4, which needs kernel 6.7, so on most machines in use it would give
    filesystem confinement and silently no network confinement. Bubblewrap's
    namespaces work far further back and cover both.

    The filesystem is assembled rather than subtracted: the system is bound
    read-only, the workspace read-write, and nothing else is visible.
    """
    argv = [
        "bwrap",
        "--die-with-parent",
        # A new pid namespace means a process that outlives the timeout dies
        # with the sandbox instead of being reparented and left running.
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--proc", "/proc",
        "--dev", "/dev",
        # A tmpfs rather than the host's /tmp, so scratch files a command
        # writes are gone when it ends and cannot be read by anything else.
        "--tmpfs", "/tmp",  # noqa: S108 - a private tmpfs is the point

    ]
    # `--ro-bind-try` rather than checking each path here. Distributions
    # differ on which of these exist, and asking the machine that builds the
    # command would make the answer depend on where it was built, which is the
    # property that makes these functions testable anywhere.
    for path in SYSTEM_PATHS:
        argv += ["--ro-bind-try", path, path]
    root = Path(workspace).as_posix()
    argv += ["--bind", root, root, "--chdir", root]
    if config.network == "none" and network:
        argv.append("--unshare-net")
    return [*argv, *command]


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
    if config.kind == "docker":
        return DockerSandbox(config)
    if config.kind == "os":
        return OsSandbox(config)
    return LocalSandbox()


def sandbox_config_from_dict(raw: dict[str, Any] | None, path: str = "sandbox") -> SandboxConfig:
    from harness.core import schema  # noqa: PLC0415 - avoids a cycle at import time

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
