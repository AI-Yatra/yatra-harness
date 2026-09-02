"""Best-effort secret redaction before persistence or display."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
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

