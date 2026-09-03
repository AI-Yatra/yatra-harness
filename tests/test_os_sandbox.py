"""Kernel confinement without a container.

The command builders are pure, so every containment rule is asserted on every
machine and only the tests that genuinely confine a process need the binary.
That split is what the Docker sandbox already does and the reason it is
testable from Windows at all.

The Windows answer is asserted too. There is no kernel sandbox reachable from
Python there: job objects bound processes and memory but not files or sockets,
and the primitives that would work need Win32 calls. Reporting that plainly is
the feature; claiming confinement that is not happening would be worse than
having none.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import ConfigurationError
from harness.execution.sandbox import (
    KINDS,
    DockerSandbox,
    LocalSandbox,
    OsSandbox,
    SandboxConfig,
    bubblewrap_command,
    build_sandbox,
    detect_mechanism,
    seatbelt_command,
    seatbelt_profile,
)

WORKSPACE = Path("/home/me/proj")


class SelectionTests(unittest.TestCase):
    def test_the_three_kinds(self) -> None:
        self.assertEqual(KINDS, ("local", "os", "docker"))

    def test_each_kind_builds_its_own_sandbox(self) -> None:
        self.assertIsInstance(build_sandbox(SandboxConfig(kind="local")), LocalSandbox)
        self.assertIsInstance(build_sandbox(SandboxConfig(kind="os")), OsSandbox)
        self.assertIsInstance(
            build_sandbox(SandboxConfig(kind="docker", image="python:3.12-slim")), DockerSandbox
        )

    def test_an_unknown_kind_is_refused_at_construction(self) -> None:
        with self.assertRaises(ConfigurationError):
            SandboxConfig(kind="chroot")

    def test_local_stays_the_default(self) -> None:
        """A machine with nothing installed must still run the harness."""
        self.assertEqual(SandboxConfig().kind, "local")


class ProbeTests(unittest.TestCase):
    """Presence is not capability, and assuming it was hid a real failure.

    `--unshare-net` asks the kernel to bring a loopback interface up inside
    the new namespace. In a container, or under a hardened AppArmor profile,
    that is refused and *every* command through the sandbox fails rather than
    running confined. The first CI run that installed bwrap found it at once;
    the suite had been green for as long as bwrap was absent, which was
    everywhere.
    """

    def test_the_probe_answers_both_questions(self) -> None:
        from harness.execution.sandbox import probe_bubblewrap

        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not installed")
        probe = probe_bubblewrap()
        self.assertIsInstance(probe.usable, bool)
        self.assertIsInstance(probe.network, bool)

    def test_a_host_that_cannot_unshare_the_network_still_confines_files(self) -> None:
        """Half a sandbox is worth having; half a sandbox unannounced is not."""
        argv = bubblewrap_command(
            SandboxConfig(kind="os"), ["x"], workspace=WORKSPACE, network=False
        )
        self.assertNotIn("--unshare-net", argv)
        self.assertIn("--bind", argv)
        self.assertIn("--ro-bind-try", argv)

    def test_the_operator_is_told_when_the_network_is_not_confined(self) -> None:
        sandbox = OsSandbox(SandboxConfig(kind="os"))
        if sandbox.mechanism != "bubblewrap" or sandbox.network_confined:
            self.skipTest("this host confines the network")
        self.assertIn("network", sandbox.reason)


class MechanismTests(unittest.TestCase):
    def test_it_names_a_mechanism_or_says_why_not(self) -> None:
        mechanism, reason = detect_mechanism()
        self.assertTrue(mechanism or reason, "silently no sandbox is the one bad answer")

    def test_windows_reports_no_mechanism_and_points_at_docker(self) -> None:
        if sys.platform != "win32":
            self.skipTest("windows only")
        mechanism, reason = detect_mechanism()
        self.assertEqual(mechanism, "")
        self.assertIn("docker", reason)

    def test_the_mechanism_matches_what_is_installed(self) -> None:
        mechanism, _ = detect_mechanism()
        if sys.platform.startswith("linux"):
            self.assertEqual(mechanism, "bubblewrap" if shutil.which("bwrap") else "")
        elif sys.platform == "darwin":
            self.assertEqual(mechanism, "seatbelt" if shutil.which("sandbox-exec") else "")

    def test_a_sandbox_that_cannot_confine_says_so(self) -> None:
        sandbox = OsSandbox(SandboxConfig(kind="os"))
        self.assertTrue(sandbox.mechanism or sandbox.reason)


class BubblewrapTests(unittest.TestCase):
    """Pure command construction, asserted everywhere."""

    def argv(self, **kwargs) -> list[str]:
        config = SandboxConfig(kind="os", **kwargs)
        return bubblewrap_command(config, ["pytest", "-q"], workspace=WORKSPACE)

    def test_the_command_is_appended_unchanged(self) -> None:
        self.assertEqual(self.argv()[-2:], ["pytest", "-q"])

    def test_the_workspace_is_writable(self) -> None:
        argv = self.argv()
        self.assertIn("--bind", argv)
        self.assertEqual(argv[argv.index("--bind") + 1], WORKSPACE.as_posix())

    def test_the_system_is_read_only(self) -> None:
        argv = self.argv()
        pairs = [argv[i + 1] for i, token in enumerate(argv) if token == "--ro-bind-try"]
        self.assertTrue(pairs, "nothing was bound read-only")
        self.assertNotIn(WORKSPACE.as_posix(), pairs)

    def test_the_network_is_off_by_default(self) -> None:
        self.assertIn("--unshare-net", self.argv())

    def test_the_network_can_be_asked_for(self) -> None:
        self.assertNotIn("--unshare-net", self.argv(network="bridge"))

    def test_a_process_cannot_outlive_the_sandbox(self) -> None:
        argv = self.argv()
        self.assertIn("--die-with-parent", argv)
        self.assertIn("--unshare-pid", argv)

    def test_tmp_is_private(self) -> None:
        self.assertIn("--tmpfs", self.argv())

    def test_paths_are_posix_whatever_built_them(self) -> None:
        """So a profile built anywhere is valid on the machine that runs it."""
        self.assertNotIn("\\", " ".join(self.argv()))


class SeatbeltTests(unittest.TestCase):
    def profile(self, **kwargs) -> str:
        return seatbelt_profile(SandboxConfig(kind="os", **kwargs), workspace=WORKSPACE)

    def test_reads_stay_wide(self) -> None:
        """A toolchain the operator installed has to remain readable."""
        self.assertIn("(allow default)", self.profile())

    def test_writes_are_denied_then_allowed_in_the_workspace(self) -> None:
        profile = self.profile()
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(f'(allow file-write* (subpath "{WORKSPACE.as_posix()}"))', profile)

    def test_the_deny_comes_before_the_allow(self) -> None:
        """Seatbelt takes the last matching rule, so order decides."""
        profile = self.profile()
        self.assertLess(
            profile.index("(deny file-write*)"),
            profile.index(f'(allow file-write* (subpath "{WORKSPACE.as_posix()}"))'),
        )

    def test_the_network_is_off_by_default(self) -> None:
        self.assertIn("(deny network*)", self.profile())

    def test_the_network_can_be_asked_for(self) -> None:
        self.assertNotIn("(deny network*)", self.profile(network="bridge"))

    def test_the_command_passes_the_profile_inline(self) -> None:
        argv = seatbelt_command(SandboxConfig(kind="os"), ["pytest"], workspace=WORKSPACE)
        self.assertEqual(argv[0], "sandbox-exec")
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[-1], "pytest")


class LiveConfinementTests(unittest.TestCase):
    """The tests that need the real thing, skipped where it is absent."""

    def setUp(self) -> None:
        mechanism, _ = detect_mechanism()
        if not mechanism:
            self.skipTest("no kernel sandbox on this platform")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.sandbox = OsSandbox(SandboxConfig(kind="os"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_python(self, code: str):
        return self.sandbox.run(
            [sys.executable, "-c", code],
            workspace=self.root,
            timeout=30,
            max_output_chars=4_000,
        )

    def test_a_command_still_runs(self) -> None:
        result = self.run_python("print(2 + 2)")
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn("4", result.output)

    def test_the_workspace_is_writable(self) -> None:
        result = self.run_python(
            "import pathlib; pathlib.Path('inside.txt').write_text('ok'); print('wrote')"
        )
        self.assertEqual(result.returncode, 0, result.output)
        self.assertTrue((self.root / "inside.txt").exists())

    def test_writing_outside_the_workspace_fails(self) -> None:
        """The rule an application-level check cannot enforce."""
        outside = Path(tempfile.gettempdir()) / "escaped-the-sandbox.txt"
        outside.unlink(missing_ok=True)
        self.run_python(
            f"import pathlib; pathlib.Path({str(outside)!r}).write_text('escaped')"
        )
        self.addCleanup(outside.unlink, True)
        self.assertFalse(outside.exists(), "a write escaped the sandbox")


class DockerUnchangedTests(unittest.TestCase):
    """Adding a kind must not disturb the one that already worked."""

    def test_the_docker_command_still_has_its_containment_flags(self) -> None:
        from harness.execution.sandbox import docker_command

        argv = docker_command(
            SandboxConfig(kind="docker", image="python:3.12-slim"),
            ["pytest"],
            workspace=WORKSPACE,
            timeout=30,
        )
        for flag in ("--network", "--cap-drop", "--security-opt", "--pids-limit"):
            self.assertIn(flag, argv)

    def test_docker_still_requires_an_image(self) -> None:
        with self.assertRaises(ConfigurationError):
            SandboxConfig(kind="docker")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
