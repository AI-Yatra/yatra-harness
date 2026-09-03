"""What has to be inside the wheel for an installed `ay` to start.

`ay.py` resolves its default config as `Path(__file__).parent / "configs" /
"ay.yaml"`. From a clone that is the repository, and everything works. From an
installed wheel it is site-packages, and the wheel shipped only `harness/` and
`ay.py`, so a clean install produced:

    error: no such config: .../site-packages/configs/ay.yaml

The whole suite passed while that was true, because every test runs from inside
the checkout where the config sits next to the code. The end-to-end guard is
the `install` job in CI, which installs somewhere else and starts the thing.
This is the cheap version of the same question, so the answer arrives in
milliseconds rather than after a runner boots.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def included() -> list[str]:
    return pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]


class WheelContentsTests(unittest.TestCase):
    def test_the_default_config_is_shipped(self) -> None:
        """Without this an installed `ay` cannot start at all."""
        self.assertIn("configs", included())

    def test_the_package_itself_is_shipped(self) -> None:
        self.assertIn("harness", included())

    def test_the_entry_point_module_is_shipped(self) -> None:
        self.assertIn("ay.py", included())

    def test_every_included_path_exists(self) -> None:
        """A name that no longer exists ships nothing and says nothing."""
        for name in included():
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).exists(), f"{name} is listed but not present")

    def test_the_config_ay_actually_looks_for_is_there(self) -> None:
        import ay

        self.assertTrue(ay.DEFAULT_CONFIG.exists(), f"{ay.DEFAULT_CONFIG} is missing")

    def test_the_default_config_sits_under_a_shipped_directory(self) -> None:
        """It has to be reachable from the wheel, not merely present in git."""
        import ay

        relative = ay.DEFAULT_CONFIG.relative_to(ROOT)
        self.assertIn(relative.parts[0], included())


class InstallerTests(unittest.TestCase):
    """The two scripts are part of the product, so they are checked too."""

    def test_both_installers_exist(self) -> None:
        self.assertTrue((ROOT / "install.sh").is_file())
        self.assertTrue((ROOT / "install.ps1").is_file())

    def test_the_posix_installer_has_no_carriage_returns(self) -> None:
        """A CRLF shebang line makes `sh` fail with a message nobody can read."""
        self.assertNotIn(b"\r\n", (ROOT / "install.sh").read_bytes())

    def test_the_posix_installer_starts_with_a_shebang(self) -> None:
        self.assertTrue((ROOT / "install.sh").read_bytes().startswith(b"#!/bin/sh"))

    def test_both_installers_verify_before_declaring_success(self) -> None:
        """The point of the exercise: prove it runs, do not assume it."""
        for name in ("install.sh", "install.ps1"):
            with self.subTest(name=name):
                self.assertIn("/exit", (ROOT / name).read_text(encoding="utf-8"))

    def test_the_installers_agree_on_the_extra(self) -> None:
        for name in ("install.sh", "install.ps1"):
            with self.subTest(name=name):
                self.assertIn("openpyxl", (ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
