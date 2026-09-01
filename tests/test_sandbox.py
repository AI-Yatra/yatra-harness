"""Where a command actually runs.

Path containment and a command allowlist confine the model's *interface*.
They do not confine the operating system: an allowlisted test runner is still
a process on the host with the host's filesystem and network. A container is
the layer that makes the containment real, and docs/SECURITY.md has been
honest about its absence for as long as it has existed.

The command construction is a pure function, so the rules that matter -- no
network, no new privileges, non-root, bounded memory, only the workspace
mounted -- are tested on every machine. Only the one test that actually
starts a container needs docker.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.errors import ConfigurationError
from harness.sandbox import (
    DockerSandbox,
    LocalSandbox,
    SandboxConfig,
    build_sandbox,
    docker_command,
)

HAS_DOCKER = shutil.which("docker") is not None and (
    subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    if shutil.which("docker")
    else False
)


def config(**kwargs) -> SandboxConfig:
    defaults = {"kind": "docker", "image": "python:3.12-slim"}
    return SandboxConfig(**{**defaults, **kwargs})


class DockerCommandTests(unittest.TestCase):
    def command(self, argv=("python", "-m", "unittest"), **kwargs) -> list[str]:
        return docker_command(
            config(**kwargs), list(argv), workspace=Path("/runs/r1/workspace"), timeout=30
        )

    def test_the_container_is_removed_after_the_run(self) -> None:
        self.assertIn("--rm", self.command())

    def test_the_network_is_off_by_default(self) -> None:
        # A command allowlist cannot stop an allowlisted process from opening
        # a socket. This can.
        self.assertIn("--network", self.command())
        self.assertIn("none", self.command())

    def test_the_network_can_be_enabled_deliberately(self) -> None:
        argv = self.command(network="bridge")
        self.assertIn("bridge", argv)
        self.assertNotIn("none", argv)

    def test_privilege_escalation_is_refused(self) -> None:
        self.assertIn("--security-opt", self.command())
        self.assertIn("no-new-privileges", " ".join(self.command()))

    def test_the_process_does_not_run_as_root(self) -> None:
        self.assertIn("--user", self.command())

    def test_only_the_workspace_is_mounted(self) -> None:
        argv = self.command()
        mounts = [argv[index + 1] for index, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(len(mounts), 1, mounts)
        self.assertTrue(mounts[0].startswith("/runs/r1/workspace:/workspace"), mounts[0])

    def test_an_selinux_host_gets_a_labelled_mount(self) -> None:
        # Without the label an SELinux host denies the container every read of
        # the workspace, and the symptom points at everything but the cause.
        argv = self.command(selinux_label="always")
        mount = argv[argv.index("--volume") + 1]
        self.assertTrue(mount.endswith(":Z"), mount)

    def test_the_label_can_be_switched_off(self) -> None:
        # Some non-Linux docker hosts reject the suffix outright.
        argv = self.command(selinux_label="never")
        mount = argv[argv.index("--volume") + 1]
        self.assertFalse(mount.endswith(":Z"), mount)

    def test_the_working_directory_is_the_workspace(self) -> None:
        argv = self.command()
        self.assertIn("--workdir", argv)
        self.assertIn("/workspace", argv)

    def test_memory_and_cpu_are_bounded(self) -> None:
        joined = " ".join(self.command())
        self.assertIn("--memory", joined)
        self.assertIn("--cpus", joined)

    def test_the_image_is_the_configured_one(self) -> None:
        self.assertIn("python:3.12-slim", self.command())

    def test_the_command_follows_the_image(self) -> None:
        argv = self.command(("pytest", "-x"))
        self.assertEqual(argv[-2:], ["pytest", "-x"])
        self.assertLess(argv.index("python:3.12-slim"), argv.index("pytest"))

    def test_the_host_interpreter_path_is_not_leaked_into_the_container(self) -> None:
        # `python` on the host is an absolute venv path. Inside the container
        # that path does not exist, so the substitution the local runner makes
        # must not happen here.
        argv = self.command(("python", "-c", "print(1)"))
        self.assertEqual(argv[-3:], ["python", "-c", "print(1)"])

    def test_an_unknown_sandbox_kind_is_refused_at_config_time(self) -> None:
        with self.assertRaises(ConfigurationError):
            SandboxConfig(kind="chroot")

    def test_docker_without_an_image_is_refused_at_config_time(self) -> None:
        with self.assertRaises(ConfigurationError):
            SandboxConfig(kind="docker", image="")


class SelectionTests(unittest.TestCase):
    def test_the_default_is_local(self) -> None:
        self.assertIsInstance(build_sandbox(SandboxConfig()), LocalSandbox)

    def test_docker_is_selected_when_configured(self) -> None:
        self.assertIsInstance(build_sandbox(config()), DockerSandbox)


class LocalSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-sandbox-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def test_a_command_runs_and_returns_its_output(self) -> None:
        sandbox = LocalSandbox()
        result = sandbox.run(
            ["python", "-c", "print('hello sandbox')"],
            workspace=self.workspace, timeout=30, max_output_chars=4_000,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello sandbox", result.output)

    def test_a_failing_command_reports_its_status(self) -> None:
        result = LocalSandbox().run(
            ["python", "-c", "raise SystemExit(3)"],
            workspace=self.workspace, timeout=30, max_output_chars=4_000,
        )
        self.assertEqual(result.returncode, 3)


@unittest.skipUnless(HAS_DOCKER, "docker is not available on this machine")
class DockerSandboxIntegrationTests(unittest.TestCase):
    IMAGE = "python:3.12-slim"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-docker-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "hello.py").write_text("print('from the container')\n", encoding="utf-8")

    def sandbox(self, **kwargs) -> DockerSandbox:
        return DockerSandbox(config(image=self.IMAGE, **kwargs))

    def test_a_command_runs_inside_the_container(self) -> None:
        result = self.sandbox().run(
            ["python", "hello.py"], workspace=self.workspace, timeout=120, max_output_chars=4_000
        )
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn("from the container", result.output)

    def test_the_workspace_is_writable_from_inside(self) -> None:
        result = self.sandbox().run(
            ["python", "-c", "open('written.txt','w').write('x')"],
            workspace=self.workspace, timeout=120, max_output_chars=4_000,
        )
        self.assertEqual(result.returncode, 0, result.output)
        self.assertTrue((self.workspace / "written.txt").is_file())

    def test_nothing_outside_the_workspace_is_visible(self) -> None:
        result = self.sandbox().run(
            ["python", "-c", "import os; print(os.path.exists('/etc/shadow-should-not-matter'))"],
            workspace=self.workspace, timeout=120, max_output_chars=4_000,
        )
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn("False", result.output)

    def test_the_host_home_directory_is_not_mounted(self) -> None:
        result = self.sandbox().run(
            ["python", "-c", "import pathlib; print(sorted(p.name for p in pathlib.Path('/').iterdir()))"],
            workspace=self.workspace, timeout=120, max_output_chars=8_000,
        )
        self.assertNotIn(str(Path.home()), result.output)

    def test_the_network_is_unreachable_by_default(self) -> None:
        result = self.sandbox().run(
            ["python", "-c",
             "import socket,sys\\n"
             "try:\\n socket.create_connection(('1.1.1.1',53),timeout=3); print('REACHED')\\n"
             "except OSError: print('BLOCKED')"],
            workspace=self.workspace, timeout=120, max_output_chars=4_000,
        )
        self.assertIn("BLOCKED", result.output)

    def test_the_process_is_not_root(self) -> None:
        result = self.sandbox().run(
            ["python", "-c", "import os; print(os.getuid())"],
            workspace=self.workspace, timeout=120, max_output_chars=4_000,
        )
        self.assertNotIn("\\n0\\n", "\\n" + result.output)


HAS_IMAGE = HAS_DOCKER and subprocess.run(
    ["docker", "image", "inspect", "yatra-harness-sandbox"],
    capture_output=True, timeout=60,
).returncode == 0


@unittest.skipUnless(HAS_IMAGE, "the yatra-harness-sandbox image is not built here")
class SandboxedRunTests(unittest.TestCase):
    """A whole run whose tools and acceptance execute in the container."""

    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self) -> None:
        import os

        self.runs = Path(tempfile.mkdtemp(prefix="harness-sandboxed-"))
        self.addCleanup(shutil.rmtree, self.runs, True)
        self.environment = {**os.environ, "HARNESS_RUNS_DIR": str(self.runs)}

    def harness(self, *arguments: str):
        import sys

        return subprocess.run(
            [sys.executable, "-m", "harness", *arguments],
            cwd=self.ROOT, capture_output=True, text=True, timeout=600, env=self.environment,
        )

    def test_a_sandboxed_run_completes(self) -> None:
        result = self.harness(
            "run", "tasks/repair_counter.yaml",
            "--config", "configs/sandboxed.yaml", "--skill", "skills/bugfix.yaml",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status: COMPLETED", result.stdout)

    def test_acceptance_really_runs_inside_the_container(self) -> None:
        # The proof has to come from inside: the host's home directory does
        # not exist in the container, and /workspace does.
        import json as json_module

        task = self.runs / "containment.yaml"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(
            "version: 1\n"
            "id: containment-check\n"
            "objective: Repair the clamp lower bound.\n"
            f"workspace_seed: {self.ROOT / 'fixtures' / 'buggy_counter'}\n"
            "acceptance:\n"
            "  commands:\n"
            '    - [python, -c, "import os,sys; sys.exit(0 if os.path.isdir(\'/workspace\') '
            "and not os.path.isdir(os.path.expanduser('~/.yatra-harness')) else 1)\"]\n"
            "  require_non_empty_diff: true\n"
            "  timeout_seconds: 120\n",
            encoding="utf-8",
        )
        result = self.harness(
            "run", str(task), "--config", "configs/sandboxed.yaml", "--skill", "skills/bugfix.yaml",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundle = sorted(p for p in self.runs.iterdir() if p.is_dir())[0]
        record = json_module.loads(
            (bundle / "artifacts" / "verification" / "attempt-01.json").read_text(encoding="utf-8")
        )
        self.assertTrue(record["passed"], record)


if __name__ == "__main__":
    unittest.main()
