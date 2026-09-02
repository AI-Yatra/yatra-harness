"""Best-effort secret redaction before persistence or display."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
#: How much key-shaped text has to follow a known prefix before it is treated
#: as a key. Long enough that prose like "the sk- prefix" survives, short
#: enough that every real key in the catalogue is covered.
_MIN_KEY_BODY = 10


def _provider_patterns() -> tuple[re.Pattern[str], ...]:
    """A pattern per credential prefix the provider catalogue knows about.

    Derived rather than hand-maintained, because the hand-maintained version
    fell behind: the catalogue grew to seventeen prefixes while this list
    matched one, so a groq, cerebras, google, nvidia or inception key reached
    the ledger in the clear. Adding a provider must not be able to add a leak,
    and now the only way to do that is to add a provider with no prefix.

    A lookbehind rather than `\\b` because prefixes are not all word-shaped:
    `AQ.` ends in a dot, which is not a word character.
    """
    from harness.models.auth import PROVIDERS  # noqa: PLC0415 - avoids a cycle

    prefixes = sorted(
        {prefix for provider in PROVIDERS for prefix in provider.prefixes},
        key=len,
        reverse=True,
    )
    return tuple(
        re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(prefix) + r"[A-Za-z0-9._\-]{" + str(_MIN_KEY_BODY) + r",}"
        )
        for prefix in prefixes
    )


TOKEN_PATTERNS = (
    *_provider_patterns(),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    # Not in the catalogue because it is not a model provider, but it travels
    # in the same environments and the same error bodies.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class Redactor:
    def __init__(self, explicit_values: Sequence[str] = ()) -> None:
        self._explicit = tuple(value for value in explicit_values if len(value) >= 4)

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._explicit:
            redacted = redacted.replace(secret, "<redacted>")
        for pattern in TOKEN_PATTERNS:
            redacted = pattern.sub("<redacted>", redacted)
        return redacted

    def value(self, value: Any, *, key: str = "") -> Any:
        if key and SENSITIVE_KEYS.search(key):
            return "<redacted>"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(item_key): self.value(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [self.value(item) for item in value]
        return value

