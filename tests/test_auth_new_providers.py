"""Credential-catalog coverage for OpenCode and Command Code."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.models import auth


class FakeResponse:
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"data": []}).encode("utf-8")


class NewProviderAuthTests(unittest.TestCase):
    ENV_NAMES = (
        "YATRA_HARNESS_AUTH_FILE",
        "OPENCODE_API_KEY",
        "COMMAND_CODE_API_KEY",
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-new-auth-")
        self.addCleanup(self.temporary.cleanup)
        self.previous = {name: os.environ.get(name) for name in self.ENV_NAMES}
        self.addCleanup(self._restore_environment)
        for name in self.ENV_NAMES:
            os.environ.pop(name, None)
        os.environ["YATRA_HARNESS_AUTH_FILE"] = str(
            Path(self.temporary.name) / "auth.json"
        )

    def _restore_environment(self) -> None:
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_catalog_contains_official_endpoints_and_environment_variables(self) -> None:
        opencode = auth.get_provider("opencode")
        self.assertEqual(opencode.env, ("OPENCODE_API_KEY",))
        self.assertEqual(opencode.base_url, "https://opencode.ai/zen/v1")
        self.assertEqual(opencode.api, auth.API_OPENAI)

        commandcode = auth.get_provider("commandcode")
        self.assertEqual(commandcode.env, ("COMMAND_CODE_API_KEY",))
        self.assertEqual(
            commandcode.base_url, "https://api.commandcode.ai/provider/v1"
        )
        self.assertEqual(commandcode.api, auth.API_OPENAI)

    def test_provider_aliases_are_human_friendly(self) -> None:
        self.assertEqual(auth.get_provider("opencode-zen").name, "opencode")
        self.assertEqual(auth.get_provider("zen").name, "opencode")
        self.assertEqual(auth.get_provider("command-code").name, "commandcode")
        self.assertEqual(auth.get_provider("cmd").name, "commandcode")

    def test_command_code_key_prefix_can_be_inferred(self) -> None:
        self.assertEqual(auth.detect("user_command-code-secret").name, "commandcode")

    def test_generic_sk_key_is_not_misclassified_as_opencode(self) -> None:
        self.assertEqual(auth.detect("sk-opencode-secret").name, "openai")
        record = auth.add("sk-opencode-secret", provider="opencode")
        self.assertEqual(record["provider"], "opencode")

    def test_environment_and_store_resolution_work_for_both_providers(self) -> None:
        os.environ["OPENCODE_API_KEY"] = "sk-opencode-environment"
        self.assertEqual(auth.resolve("opencode").source, auth.SOURCE_ENV)

        auth.add("user_command-code-stored")
        credential = auth.resolve_env("COMMAND_CODE_API_KEY")
        self.assertEqual(credential.provider, "commandcode")
        self.assertEqual(credential.source, auth.SOURCE_STORED)

    def test_official_endpoints_resolve_custom_route_variables(self) -> None:
        auth.add("sk-opencode-stored", provider="opencode")
        opencode = auth.resolve_route(
            "MY_OPENCODE_KEY", "https://opencode.ai/zen/v1"
        )
        self.assertEqual(opencode.provider, "opencode")

        auth.add("user_command-code-stored")
        commandcode = auth.resolve_route(
            "MY_COMMAND_CODE_KEY", "https://api.commandcode.ai/provider/v1"
        )
        self.assertEqual(commandcode.provider, "commandcode")

    def test_opencode_go_endpoint_can_be_registered_as_an_override(self) -> None:
        auth.add(
            "sk-opencode-go",
            provider="opencode",
            base_url="https://opencode.ai/zen/go/v1",
        )
        credential = auth.resolve_route(
            "MY_OPENCODE_GO_KEY", "https://opencode.ai/zen/go/v1"
        )
        self.assertEqual(credential.provider, "opencode")

    def test_verification_honors_stored_base_url_override(self) -> None:
        auth.add(
            "sk-opencode-go",
            provider="opencode",
            base_url="https://opencode.ai/zen/go/v1",
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse()
        ) as urlopen:
            result = auth.verify("opencode")
        self.assertTrue(result.ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://opencode.ai/zen/go/v1/models"
        )

    def test_verification_calls_each_official_models_endpoint_with_bearer_auth(self) -> None:
        cases = (
            ("opencode", "sk-opencode", "https://opencode.ai/zen/v1/models"),
            (
                "commandcode",
                "user_command-code",
                "https://api.commandcode.ai/provider/v1/models",
            ),
        )
        for provider, key, expected_url in cases:
            with self.subTest(provider=provider):
                auth.add(key, provider=provider)
                with mock.patch(
                    "urllib.request.urlopen", return_value=FakeResponse()
                ) as urlopen:
                    result = auth.verify(provider)
                self.assertTrue(result.ok)
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, expected_url)
                self.assertEqual(request.get_header("Authorization"), f"Bearer {key}")


if __name__ == "__main__":
    unittest.main()
