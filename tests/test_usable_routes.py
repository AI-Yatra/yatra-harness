"""What to say when the default route has no key.

An operator set the harness up on a fresh Linux machine, stored a GMI key, and
ran `ay`. They were told:

    No credential for DASHSCOPE_API_KEY, which route 'qwen' needs.

which is true, and useless. `qwen` is the configured primary, so it is the
only route the check ever looked at. Three routes were ready to run on the key
they had just stored and nothing mentioned them. Their reply was "even after
adding the key something is wrong".
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from harness.config import load_config
from harness.repl.shell import usable_routes

ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(ROOT / "configs" / "ay.yaml")


class Resolved:
    def __init__(self, available: bool) -> None:
        self.available = available


def only(*variables: str):
    """A credential store holding exactly these variables."""
    return mock.patch(
        "harness.repl.shell.auth.resolve_route",
        side_effect=lambda variable, _base: Resolved(variable in variables),
    )


class UsableRouteTests(unittest.TestCase):
    def test_a_gmi_key_names_the_gmi_routes(self) -> None:
        with only("GMI_API_KEY"):
            self.assertEqual(
                usable_routes(config(), exclude="qwen"), ["gmi", "gmi-m27", "gmi-router"]
            )

    def test_no_credentials_names_nothing(self) -> None:
        with only():
            self.assertEqual(usable_routes(config(), exclude="qwen"), [])

    def test_the_route_that_failed_is_not_offered_back(self) -> None:
        with only("DASHSCOPE_API_KEY"):
            self.assertNotIn("qwen", usable_routes(config(), exclude="qwen"))

    def test_a_sibling_sharing_the_variable_is_still_offered(self) -> None:
        """`qwen-max` uses the same key as `qwen` and is a different model."""
        with only("DASHSCOPE_API_KEY"):
            self.assertIn("qwen-max", usable_routes(config(), exclude="qwen"))

    def test_a_keyless_route_is_never_called_ready(self) -> None:
        """`local` needs no key but does need a server this cannot check."""
        with only("GMI_API_KEY"):
            self.assertNotIn("local", usable_routes(config(), exclude="qwen"))

    def test_every_name_offered_is_a_real_route(self) -> None:
        known = set(config().router.routes)
        with only("GMI_API_KEY", "GROQ_API_KEY"):
            self.assertTrue(set(usable_routes(config())) <= known)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
