"""Settings that follow the project rather than the install.

`ay` read one config, from its own install directory, and nothing from the
directory you started it in. So a rule could not be scoped to one repository:
either every session got it or none did. Writing a demo that needed one deny
rule in one folder is what made the gap concrete -- the constraint had to
become a sentence in AGENTS.md that the model chooses to obey, instead of a
gate that stops it.

The precedence chain is the one every comparable tool settled on, and the
order is the point rather than the file names:

    explicit --config          what the operator typed wins outright
    .yatra/settings.local.yaml machine-local, gitignored, not shared
    .yatra/settings.yaml       the project's, committed, shared with the team
    ~/.yatra-harness/settings.yaml   personal defaults for every project
    configs/ay.yaml            what ships

**Permission rules do not follow that order.** They merge across every layer,
and a `deny` written at any layer survives all of them. Overriding is right
for a model choice and wrong for a refusal: if a project bans `git push` a
personal file must not quietly re-enable it, and the operator who wrote the
narrower rule is usually the one who knows why. Everything else is last
writer wins.

Discovery walks up from the working directory, so a session started three
folders inside a repository still finds the repository's settings. It stops at
a `.git` directory or the filesystem root, because past that point the files
belong to somebody else's project.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from harness.core.errors import ConfigurationError

#: The directory a project keeps its settings in.
PROJECT_DIR = ".yatra"

#: Committed, then machine-local. Ordered lowest precedence first.
PROJECT_FILES = ("settings.yaml", "settings.local.yaml")

#: Personal defaults, beside the credential store for the same reason: it is
#: user state and it does not belong in a repository.
USER_FILE = Path.home() / ".yatra-harness" / "settings.yaml"

#: How far up the tree to look. A repository deeper than this is not a
#: repository, and an unbounded walk on a broken symlink is a hang.
MAX_DEPTH = 40

#: Keys whose lists merge instead of replacing, because dropping one would
#: quietly remove a refusal somebody wrote on purpose.
MERGED_DENY_PATHS = (
    ("policy", "denied_commands"),
    ("policy", "rules", "deny"),
    ("policy", "rules", "ask"),
)


@dataclass(frozen=True, slots=True)
class Layer:
    """One settings file that was found, and what it said."""

    path: Path
    values: dict[str, Any]
    #: `project`, `local`, `user` or `explicit`, for `ay /config` to print.
    scope: str


def find_project_root(start: Path) -> Path | None:
    """The nearest directory above *start* holding a `.yatra` or a `.git`.

    `.git` counts because that is what an operator means by "this project",
    and it is where they will expect to put `.yatra/` next.
    """
    current = Path(start).expanduser().resolve()
    for _ in range(MAX_DEPTH):
        if (current / PROJECT_DIR).is_dir() or (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def discover(root: Path | None = None) -> list[Layer]:
    """Every settings layer that applies, lowest precedence first."""
    layers: list[Layer] = []
    if USER_FILE.is_file():
        layers.append(Layer(USER_FILE, _read(USER_FILE), "user"))
    project = find_project_root(root or Path.cwd())
    if project is not None:
        for index, name in enumerate(PROJECT_FILES):
            path = project / PROJECT_DIR / name
            if path.is_file():
                layers.append(Layer(path, _read(path), "local" if index else "project"))
    return layers


def _read(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must be a mapping at the top level")
    return loaded


def apply(base: dict[str, Any], layers: list[Layer]) -> dict[str, Any]:
    """Fold every layer onto *base*, lowest precedence first."""
    merged = base
    for layer in layers:
        merged = merge(merged, layer.values)
    return merged


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *overlay* onto *base*, without mutating either.

    Mappings merge key by key. Everything else is replaced, so a project that
    sets `model_router.primary` gets the route it asked for rather than some
    combination of two. The exception is the deny lists, which are unioned by
    `_keep_refusals` afterwards.
    """
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = merge(current, value)
        else:
            result[key] = value
    return _keep_refusals(result, base, overlay)


def _keep_refusals(
    result: dict[str, Any], base: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    """Union the deny and ask lists rather than letting the overlay win.

    A layer may add a refusal and may not remove one. The operator who wrote
    the narrower rule is usually the one who knew why it was there, and a
    personal file silently re-enabling something a project banned is the
    failure this exists to prevent.
    """
    for path in MERGED_DENY_PATHS:
        first, rest = _dig(base, path), _dig(overlay, path)
        if not isinstance(first, list) or not isinstance(rest, list):
            continue
        combined = list(first)
        for entry in rest:
            if entry not in combined:
                combined.append(entry)
        _plant(result, path, combined)
    return result


def _dig(values: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(values, dict) or key not in values:
            return None
        values = values[key]
    return values


def _plant(values: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        values = values.setdefault(key, {})
        if not isinstance(values, dict):
            return
    values[path[-1]] = value


def describe(layers: list[Layer]) -> Iterator[str]:
    """One line per layer, for `/config` to print."""
    for layer in layers:
        yield f"{layer.scope:<9}{_short(layer.path)}"


def _short(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return str(path)


def project_settings_path(root: Path, *, local: bool = False) -> Path:
    """Where a project's settings belong, for a writer to create."""
    return Path(root) / PROJECT_DIR / PROJECT_FILES[1 if local else 0]


def ignore_local_settings(root: Path) -> None:
    """Keep the machine-local file out of the repository.

    Written to `.yatra/.gitignore` rather than the project's own, so creating
    it never touches a file the operator maintains.
    """
    directory = Path(root) / PROJECT_DIR
    marker = directory / ".gitignore"
    if marker.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{PROJECT_FILES[1]}\n", encoding="utf-8")


def env_root() -> Path | None:
    """`YATRA_PROJECT_DIR`, for a test or a wrapper that knows better."""
    value = os.environ.get("YATRA_PROJECT_DIR")
    return Path(value).expanduser() if value else None
