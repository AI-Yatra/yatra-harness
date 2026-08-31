"""Small strict-validation helpers for versioned YAML/JSON contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .errors import ConfigurationError


def mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{path} must be a mapping")
    return dict(value)


def sequence(value: Any, path: str) -> list[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ConfigurationError(f"{path} must be a list")
    return list(value)


def string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigurationError(f"{path} must be a non-empty string")
    return value


def integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{path} must be >= {minimum}")
    return value


def number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError(f"{path} must be a number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ConfigurationError(f"{path} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigurationError(f"{path} must be <= {maximum}")
    return result


def boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{path} must be true or false")
    return value


def optional(data: Mapping[str, Any], key: str, default: Any) -> Any:
    return data[key] if key in data else default


def require(data: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise ConfigurationError(f"{path}.{key} is required")
    return data[key]


def reject_unknown(data: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigurationError(f"{path} contains unknown keys: {', '.join(unknown)}")


def string_list(value: Any, path: str) -> tuple[str, ...]:
    return tuple(string(item, f"{path}[{index}]") for index, item in enumerate(sequence(value, path)))


def command_list(value: Any, path: str) -> tuple[tuple[str, ...], ...]:
    commands = []
    for index, item in enumerate(sequence(value, path)):
        commands.append(string_list(item, f"{path}[{index}]"))
    return tuple(commands)

