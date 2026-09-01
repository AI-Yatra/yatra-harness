"""Preflight diagnostics shared by operators and the CLI."""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import auth
from .config import HarnessConfig, load_config, load_skill, load_task
from .errors import HarnessError
from .mcp import MCPStdioClient
from .model_router import build_llm_light, profile_from_route


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_doctor(
    config_path: Path,
    *,
    task_path: Path | None = None,
    skill_path: Path | None = None,
) -> tuple[Check, ...]:
    checks: list[Check] = []
    try:
        config = load_config(config_path)
        checks.append(Check("configuration", True, str(config.config_path)))
    except Exception as exc:
        return (Check("configuration", False, str(exc)),)
    checks.append(
        Check(
            "python",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    git = shutil.which("git")
    checks.append(Check("git", git is not None, git or "not found on PATH"))
    checks.append(_route_check(config, config.router.primary, required=True))
    for route in config.router.fallbacks:
        checks.append(_route_check(config, route, required=False))
    if task_path:
        try:
            task = load_task(task_path)
            checks.append(Check("task", True, f"{task.task_id}: {task.origin}"))
        except Exception as exc:
            checks.append(Check("task", False, str(exc)))
    if skill_path:
        try:
            skill = load_skill(skill_path)
            checks.append(Check("skill", True, f"{skill.skill_id}: {len(skill.allowed_tools)} tools"))
        except Exception as exc:
            checks.append(Check("skill", False, str(exc)))
    checks.append(_routing_check(config))
    with tempfile.TemporaryDirectory(prefix="harness-doctor-") as temporary:
        cwd = Path(temporary)
        for server in config.mcp_servers:
            if not server.enabled:
                continue
            command = tuple(sys.executable if index == 0 and part in {"{python}", "python", "python3"} else part for index, part in enumerate(server.command))
            try:
                with MCPStdioClient(
                    command,
                    cwd=cwd,
                    protocol_version=server.protocol_version,
                    timeout_seconds=server.timeout_seconds,
                ) as client:
                    tools = client.list_tools()
                checks.append(Check(f"mcp:{server.name}", True, f"{len(tools)} tool(s)"))
            except Exception as exc:
                checks.append(Check(f"mcp:{server.name}", False, str(exc)))
    return tuple(checks)


def _routing_check(config: HarnessConfig) -> Check:
    """Verify the LLM Light plan can actually be computed for this config.

    A routing decision that cannot be made is a configuration error, not a
    runtime surprise, so it is caught here before any run is created.
    """
    light = build_llm_light(config.llm_light)
    if light is None:
        return Check("llm_light", True, "disabled; using the declared route order")
    try:
        plan = light.plan(
            {name: profile_from_route(route) for name, route in config.router.routes.items()}
        )
    except HarnessError as exc:
        return Check("llm_light", False, str(exc))
    detail = f"{plan.profile}: {' -> '.join(plan.routes)}"
    if config.llm_light.profiles:
        detail = f"{len(config.llm_light.profiles)} profile(s); {detail}"
    return Check("llm_light", True, detail)


def _route_check(config: HarnessConfig, name: str, *, required: bool) -> Check:
    route = config.router.routes[name]
    detail = route.base_url
    if route.kind == "replay":
        ok = route.script is not None and route.script.is_file()
        return Check(f"model:{name}", ok, str(route.script), required)
    # A route whose credential is absent cannot start a run. Preflight has to
    # fail here, otherwise doctor and the runner disagree about readiness and
    # the operator only finds out after paying for a workspace and a run id.
    if route.api_key_env:
        credential = auth.resolve_route(route.api_key_env, route.base_url)
        if not credential.available:
            return Check(
                f"model:{name}",
                False,
                f"{route.base_url} (no credential for {route.api_key_env}; "
                f"export it or run `harness auth add <key>`)",
                required,
            )
        detail = f"{route.base_url} (credential from {credential.source})"

    parsed = urlparse(route.base_url)
    if route.local and parsed.hostname:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname, port), timeout=0.5):
                pass
            return Check(f"model:{name}", True, f"reachable at {parsed.hostname}:{port}", required)
        except OSError as exc:
            return Check(
                f"model:{name}",
                False,
                f"configured but unreachable at {parsed.hostname}:{port}: {exc}",
                required,
            )
    return Check(f"model:{name}", bool(route.base_url), detail or "missing base_url", required)

