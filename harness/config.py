"""Versioned, strict configuration/task/skill loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from . import schema
from .contracts import BudgetSpec, SkillContract, TaskContract, VerificationSpec
from .errors import ConfigurationError
from .llm_light import (
    PRIORITY_KEYS,
    LLMLightConfig,
    RoutingConstraints,
    RoutingPolicy,
    validate_priorities,
)
from .search import SearchConfig, search_config_from_dict

ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")

# Provider kinds accepted on a route.
ROUTE_KINDS = {"replay", "openai_compatible", "anthropic", "ollama", "vllm"}

# Route kinds that speak the OpenAI chat-completions wire format.
OPENAI_SHAPED_KINDS = {"openai_compatible", "ollama", "vllm"}

# Locality is derived from the kind, but an explicit `local:` always wins.
DEFAULT_LOCAL_KINDS = {"replay", "ollama", "vllm"}


@dataclass(frozen=True, slots=True)
class RouteConfig:
    name: str
    kind: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    script: Path | None = None
    timeout_seconds: float = 45.0
    local: bool = True
    # LLM Light decision attributes. Credentials and endpoints are never here.
    cost_per_1m_input: float = 0.0
    cost_per_1m_output: float = 0.0
    latency: str = "medium"
    quality: float = 3.0
    context_window: int = 8192
    tool_support: bool = True


@dataclass(frozen=True, slots=True)
class ModelRouterConfig:
    primary: str
    fallbacks: tuple[str, ...]
    retries_per_route: int
    backoff_seconds: float
    circuit_breaker_failures: int
    routes: dict[str, RouteConfig]


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    approval_mode: str
    allowed_commands: tuple[tuple[str, ...], ...]
    # Checked before the allowlist and never overridden by it. See
    # PolicyEngine._command_denied for why both lists are needed.
    denied_commands: tuple[tuple[str, ...], ...]
    network_enabled: bool
    allowed_domains: tuple[str, ...]
    command_timeout_seconds: float
    browser_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...]
    protocol_version: str = "2025-11-25"
    timeout_seconds: float = 10.0
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    config_path: Path
    runs_dir: Path
    budgets: BudgetSpec
    router: ModelRouterConfig
    policy: PolicyConfig
    mcp_servers: tuple[MCPServerConfig, ...]
    context_recent_observations: int = 6
    context_repo_entries: int = 120
    # The repository's own conventions, read from the run workspace. An empty
    # tuple switches the behaviour off entirely.
    context_instruction_files: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")
    context_max_instruction_chars: int = 4_000
    search: SearchConfig = field(default_factory=SearchConfig)
    llm_light: LLMLightConfig = field(default_factory=LLMLightConfig)
    fault: str = ""
    selected_model: str = ""

    def with_overrides(
        self,
        *,
        model: str | None = None,
        fallback: str | None = None,
        max_turns: int | None = None,
        max_seconds: float | None = None,
        fault: str | None = None,
        profile: str | None = None,
        priorities: tuple[str, ...] = (),
        require_local: bool = False,
        max_cost_per_1m: float | None = None,
    ) -> HarnessConfig:
        budgets = self.budgets
        if max_turns is not None:
            budgets = replace(budgets, max_turns=max_turns)
        if max_seconds is not None:
            budgets = replace(budgets, max_seconds=max_seconds)
        router = self.router
        if model:
            if model not in router.routes:
                raise ConfigurationError(f"unknown model route: {model}")
            router = replace(router, primary=model)
        if fallback:
            if fallback not in router.routes:
                raise ConfigurationError(f"unknown fallback route: {fallback}")
            router = replace(router, fallbacks=(fallback,))
        llm_light = self.llm_light
        if profile:
            # Validated eagerly so a typo fails before a run is created.
            llm_light.policy(profile)
            llm_light = replace(llm_light, default_profile=profile)
        if priorities:
            validate_priorities(tuple(priorities), "priorities")
            llm_light = replace(llm_light, priorities=tuple(priorities), default_profile="")
        if require_local:
            constraints = replace(llm_light.constraints, require_local=True)
            llm_light = replace(llm_light, constraints=constraints)
        if max_cost_per_1m is not None:
            constraints = replace(llm_light.constraints, max_cost_per_1m=max_cost_per_1m)
            llm_light = replace(llm_light, constraints=constraints)
        return replace(
            self,
            budgets=budgets,
            router=router,
            llm_light=llm_light,
            fault=fault if fault is not None else self.fault,
            selected_model=model or self.selected_model,
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {path}: {exc}") from exc
    return schema.mapping(raw, str(path))


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(path: str | Path) -> HarnessConfig:
    config_path = Path(path).expanduser().resolve()
    raw = _load_yaml(config_path)
    runs_dir_value = raw.get("runs_dir", "../.runs")
    # HARNESS_RUNS_DIR overrides the configured runs directory. Intended for
    # tests and CI isolation; production runs use the config value.
    runs_dir = Path(os.environ.get("HARNESS_RUNS_DIR", runs_dir_value)).expanduser()
    raw = {**raw, "runs_dir": runs_dir_value}
    schema.reject_unknown(
        raw,
        {
            "version",
            "runs_dir",
            "budgets",
            "model_router",
            "policy",
            "mcp",
            "context",
            "search",
            "llm_light",
            # Written by the runtime so a resumed run routes identically; not
            # part of the operator-facing hand-authored schema.
            "selected_model",
        },
        "config",
    )
    if schema.integer(schema.require(raw, "version", "config"), "config.version") != 1:
        raise ConfigurationError("config.version must be 1")
    base = config_path.parent
    router_raw = schema.mapping(schema.require(raw, "model_router", "config"), "model_router")
    schema.reject_unknown(
        router_raw,
        {
            "primary",
            "fallbacks",
            "retries_per_route",
            "backoff_seconds",
            "circuit_breaker_failures",
            "routes",
        },
        "model_router",
    )
    routes_raw = schema.mapping(schema.require(router_raw, "routes", "model_router"), "routes")
    routes: dict[str, RouteConfig] = {}
    for name, value in routes_raw.items():
        item = schema.mapping(value, f"model_router.routes.{name}")
        reject_unknown_route_keys(item, f"model_router.routes.{name}")
        kind = schema.string(schema.require(item, "kind", f"routes.{name}"), f"routes.{name}.kind")
        if kind not in ROUTE_KINDS:
            raise ConfigurationError(
                f"routes.{name}.kind must be one of {', '.join(sorted(ROUTE_KINDS))}"
            )
        script_value = item.get("script")
        script = _resolve(base, schema.string(script_value, f"routes.{name}.script")) if script_value else None
        explicit_local = "local" in item
        routes[name] = RouteConfig(
            name=name,
            kind=kind,
            model=schema.string(item.get("model", name), f"routes.{name}.model"),
            base_url=schema.string(item.get("base_url", ""), f"routes.{name}.base_url", allow_empty=True),
            api_key_env=schema.string(
                item.get("api_key_env", ""), f"routes.{name}.api_key_env", allow_empty=True
            ),
            script=script,
            timeout_seconds=schema.number(
                item.get("timeout_seconds", 45), f"routes.{name}.timeout_seconds", minimum=0.1
            ),
            local=(
                schema.boolean(item["local"], f"routes.{name}.local")
                if explicit_local
                else kind in DEFAULT_LOCAL_KINDS
            ),
            **_routing_attributes(item, name),
        )
    primary = schema.string(schema.require(router_raw, "primary", "model_router"), "model_router.primary")
    fallbacks = schema.string_list(router_raw.get("fallbacks", []), "model_router.fallbacks")
    unknown_routes = [route for route in (primary, *fallbacks) if route not in routes]
    if unknown_routes:
        raise ConfigurationError(f"router references unknown routes: {', '.join(unknown_routes)}")
    router = ModelRouterConfig(
        primary=primary,
        fallbacks=fallbacks,
        retries_per_route=schema.integer(
            router_raw.get("retries_per_route", 1), "model_router.retries_per_route", minimum=0
        ),
        backoff_seconds=schema.number(
            router_raw.get("backoff_seconds", 0.2), "model_router.backoff_seconds", minimum=0
        ),
        circuit_breaker_failures=schema.integer(
            router_raw.get("circuit_breaker_failures", 2),
            "model_router.circuit_breaker_failures",
            minimum=1,
        ),
        routes=routes,
    )
    policy_raw = schema.mapping(raw.get("policy", {}), "policy")
    schema.reject_unknown(
        policy_raw,
        {
            "approval_mode",
            "allowed_commands",
            "denied_commands",
            "network_enabled",
            "allowed_domains",
            "command_timeout_seconds",
            "browser_timeout_seconds",
        },
        "policy",
    )
    approval_mode = schema.string(policy_raw.get("approval_mode", "mutations"), "policy.approval_mode")
    if approval_mode not in {"never", "mutations", "always"}:
        raise ConfigurationError("policy.approval_mode must be never, mutations, or always")
    policy = PolicyConfig(
        approval_mode=approval_mode,
        allowed_commands=schema.command_list(policy_raw.get("allowed_commands", []), "policy.allowed_commands"),
        denied_commands=schema.command_list(
            policy_raw.get("denied_commands", []), "policy.denied_commands"
        ),
        network_enabled=schema.boolean(
            policy_raw.get("network_enabled", False), "policy.network_enabled"
        ),
        allowed_domains=schema.string_list(
            policy_raw.get("allowed_domains", []), "policy.allowed_domains"
        ),
        command_timeout_seconds=schema.number(
            policy_raw.get("command_timeout_seconds", 30),
            "policy.command_timeout_seconds",
            minimum=0.1,
        ),
        browser_timeout_seconds=schema.number(
            policy_raw.get("browser_timeout_seconds", 10),
            "policy.browser_timeout_seconds",
            minimum=0.1,
        ),
    )
    mcp_raw = schema.sequence(raw.get("mcp", []), "mcp")
    mcp_servers = []
    for index, value in enumerate(mcp_raw):
        item = schema.mapping(value, f"mcp[{index}]")
        schema.reject_unknown(
            item, {"name", "command", "protocol_version", "timeout_seconds", "enabled"}, f"mcp[{index}]"
        )
        mcp_servers.append(
            MCPServerConfig(
                name=schema.string(schema.require(item, "name", f"mcp[{index}]"), f"mcp[{index}].name"),
                command=schema.string_list(
                    schema.require(item, "command", f"mcp[{index}]"), f"mcp[{index}].command"
                ),
                protocol_version=schema.string(
                    item.get("protocol_version", "2025-11-25"), f"mcp[{index}].protocol_version"
                ),
                timeout_seconds=schema.number(
                    item.get("timeout_seconds", 10), f"mcp[{index}].timeout_seconds", minimum=0.1
                ),
                enabled=schema.boolean(item.get("enabled", True), f"mcp[{index}].enabled"),
            )
        )
    context_raw = schema.mapping(raw.get("context", {}), "context")
    schema.reject_unknown(
        context_raw,
        {
            "recent_observations",
            "repo_entries",
            "instruction_files",
            "max_instruction_chars",
        },
        "context",
    )
    instruction_files = (
        schema.string_list(context_raw["instruction_files"], "context.instruction_files")
        if context_raw.get("instruction_files") is not None
        else ("AGENTS.md", "CLAUDE.md")
    )
    return HarnessConfig(
        config_path=config_path,
        runs_dir=_resolve(base, str(runs_dir)),
        budgets=BudgetSpec.from_dict(schema.mapping(raw.get("budgets", {}), "budgets")),
        router=router,
        policy=policy,
        mcp_servers=tuple(mcp_servers),
        context_recent_observations=schema.integer(
            context_raw.get("recent_observations", 6), "context.recent_observations", minimum=1
        ),
        context_repo_entries=schema.integer(
            context_raw.get("repo_entries", 120), "context.repo_entries", minimum=10
        ),
        search=search_config_from_dict(raw.get("search"), "search"),
        context_instruction_files=instruction_files,
        context_max_instruction_chars=schema.integer(
            context_raw.get("max_instruction_chars", 4_000),
            "context.max_instruction_chars",
            minimum=0,
        ),
        llm_light=_load_llm_light(raw.get("llm_light"), router),
    )


ROUTE_BASE_KEYS = {
    "kind",
    "model",
    "base_url",
    "api_key_env",
    "script",
    "timeout_seconds",
    "local",
}
ROUTE_ROUTING_KEYS = {
    "cost_per_1m_input",
    "cost_per_1m_output",
    "latency",
    "quality",
    "context_window",
    "tool_support",
}


def reject_unknown_route_keys(item: dict, path: str) -> None:
    schema.reject_unknown(item, ROUTE_BASE_KEYS | ROUTE_ROUTING_KEYS, path)


def _routing_attributes(item: dict, name: str) -> dict:
    """Validate the LLM Light decision attributes declared on one route."""
    path = f"routes.{name}"
    latency = schema.string(item.get("latency", "medium"), f"{path}.latency")
    if latency not in {"low", "medium", "high"}:
        raise ConfigurationError(f"{path}.latency must be low, medium, or high")
    return {
        "cost_per_1m_input": schema.number(
            item.get("cost_per_1m_input", 0.0), f"{path}.cost_per_1m_input", minimum=0
        ),
        "cost_per_1m_output": schema.number(
            item.get("cost_per_1m_output", 0.0), f"{path}.cost_per_1m_output", minimum=0
        ),
        "latency": latency,
        "quality": schema.number(item.get("quality", 3.0), f"{path}.quality", minimum=0, maximum=5),
        "context_window": schema.integer(
            item.get("context_window", 8_192), f"{path}.context_window", minimum=1
        ),
        "tool_support": schema.boolean(item.get("tool_support", True), f"{path}.tool_support"),
    }


def _load_constraints(raw: Any, path: str) -> RoutingConstraints:
    allowed = {
        "require_local",
        "require_tools",
        "min_context_window",
        "max_cost_per_1m",
        "allowed",
        "denied",
    }
    item = schema.mapping(raw, path)
    schema.reject_unknown(item, allowed, path)
    ceiling = item.get("max_cost_per_1m")
    return RoutingConstraints(
        require_local=schema.boolean(item.get("require_local", False), f"{path}.require_local"),
        require_tools=schema.boolean(item.get("require_tools", True), f"{path}.require_tools"),
        min_context_window=schema.integer(
            item.get("min_context_window", 0), f"{path}.min_context_window", minimum=0
        ),
        max_cost_per_1m=(
            schema.number(ceiling, f"{path}.max_cost_per_1m", minimum=0) if ceiling is not None else None
        ),
        allowed=schema.string_list(item.get("allowed", []), f"{path}.allowed"),
        denied=schema.string_list(item.get("denied", []), f"{path}.denied"),
    )


def _load_policy(raw: Any, path: str) -> RoutingPolicy:
    item = schema.mapping(raw, path)
    schema.reject_unknown(item, {"mode", "priorities", "weights", "constraints"}, path)
    weights_raw = schema.mapping(item.get("weights", {}), f"{path}.weights")
    weights = {}
    for key, value in weights_raw.items():
        if key not in PRIORITY_KEYS:
            raise ConfigurationError(
                f"{path}.weights contains unknown key {key!r}; "
                f"expected one of {', '.join(PRIORITY_KEYS)}"
            )
        weights[key] = schema.number(value, f"{path}.weights.{key}", minimum=0)
    priorities = schema.string_list(item.get("priorities", []), f"{path}.priorities")
    return RoutingPolicy(
        mode=schema.string(item.get("mode", "lexicographic"), f"{path}.mode"),
        priorities=tuple(priorities),
        weights=weights,
        constraints=(
            _load_constraints(item["constraints"], f"{path}.constraints")
            if "constraints" in item
            else RoutingConstraints()
        ),
    )


def _load_llm_light(raw: Any, router: ModelRouterConfig) -> LLMLightConfig:
    """Load the LLM Light section, defaulting to the router's declared order.

    When the section is absent the harness behaves exactly as it did before: the
    configured primary and fallbacks are used verbatim. LLM Light is additive.
    """
    if raw is None:
        return LLMLightConfig(enabled=False)
    path = "llm_light"
    item = schema.mapping(raw, path)
    schema.reject_unknown(
        item,
        {"enabled", "default_profile", "mode", "priorities", "weights", "constraints", "profiles"},
        path,
    )
    # The section carries `default_profile` and `profiles` on top of the
    # default policy keys, so the default policy is validated against its own
    # key set only.
    default_policy = _load_policy(
        {
            key: value
            for key, value in item.items()
            if key not in {"enabled", "default_profile", "profiles"}
        },
        path,
    )
    profiles_raw = schema.mapping(item.get("profiles", {}), f"{path}.profiles")
    profiles = {
        name: _load_policy(value, f"{path}.profiles.{name}")
        for name, value in profiles_raw.items()
    }
    known = set(router.routes)
    for profile_name, policy in profiles.items():
        for route_name in (*policy.constraints.allowed, *policy.constraints.denied):
            if route_name not in known:
                raise ConfigurationError(
                    f"llm_light.profiles.{profile_name} references unknown route {route_name!r}"
                )
    return LLMLightConfig(
        enabled=schema.boolean(item.get("enabled", True), f"{path}.enabled"),
        default_profile=schema.string(
            item.get("default_profile", ""), f"{path}.default_profile", allow_empty=True
        ),
        mode=default_policy.mode,
        priorities=default_policy.priorities,
        weights=default_policy.weights,
        constraints=default_policy.constraints,
        profiles=profiles,
    )


def load_task(path: str | Path) -> TaskContract:
    task_path = Path(path).expanduser().resolve()
    raw = _load_yaml(task_path)
    schema.reject_unknown(
        raw,
        {
            "version",
            "id",
            "objective",
            "workspace_seed",
            "repository",
            "base_ref",
            "constraints",
            "protected_paths",
            "acceptance",
            "metadata",
        },
        "task",
    )
    if schema.integer(schema.require(raw, "version", "task"), "task.version") != 1:
        raise ConfigurationError("task.version must be 1")
    seed, repository, base_ref = _task_origin(task_path, raw)
    return TaskContract(
        task_id=schema.string(schema.require(raw, "id", "task"), "task.id"),
        objective=schema.string(schema.require(raw, "objective", "task"), "task.objective"),
        workspace_seed=seed,
        repository=repository,
        base_ref=base_ref,
        constraints=schema.string_list(raw.get("constraints", []), "task.constraints"),
        protected_paths=schema.string_list(raw.get("protected_paths", []), "task.protected_paths"),
        acceptance=VerificationSpec.from_dict(
            schema.mapping(schema.require(raw, "acceptance", "task"), "task.acceptance"),
            "task.acceptance",
        ),
        metadata=schema.mapping(raw.get("metadata", {}), "task.metadata"),
    )


def _task_origin(
    task_path: Path, raw: dict[str, Any]
) -> tuple[Path | None, Path | None, str]:
    """Resolve where a task's workspace comes from, and refuse ambiguity.

    A task that names both a seed and a repository has two answers to one
    question, and picking either silently would make the run's provenance a
    guess. Naming neither is the same problem with no answers.
    """
    has_seed = raw.get("workspace_seed") is not None
    has_repository = raw.get("repository") is not None
    if has_seed == has_repository:
        raise ConfigurationError(
            "task must name exactly one of workspace_seed or repository"
        )
    base_ref = (
        schema.string(raw["base_ref"], "task.base_ref")
        if raw.get("base_ref") is not None
        else ""
    )
    if has_seed:
        if base_ref:
            raise ConfigurationError("task.base_ref only applies to a repository task")
        seed = _resolve(
            task_path.parent,
            schema.string(raw["workspace_seed"], "task.workspace_seed"),
        )
        if not seed.is_dir():
            raise ConfigurationError(f"task workspace seed is not a directory: {seed}")
        return seed, None, ""
    repository = _resolve(
        task_path.parent, schema.string(raw["repository"], "task.repository")
    )
    if not repository.is_dir():
        raise ConfigurationError(f"task repository is not a directory: {repository}")
    # Checked here rather than at workspace creation so `harness explain` and
    # `harness doctor` fail on a bad path before a run id exists.
    if not (repository / ".git").exists():
        raise ConfigurationError(f"task repository is not a git repository: {repository}")
    return None, repository, base_ref


def load_skill(path: str | Path) -> SkillContract:
    skill_path = Path(path).expanduser().resolve()
    raw = _load_yaml(skill_path)
    schema.reject_unknown(raw, {"version", "id", "instructions", "allowed_tools"}, "skill")
    if schema.integer(schema.require(raw, "version", "skill"), "skill.version") != 1:
        raise ConfigurationError("skill.version must be 1")
    return SkillContract(
        skill_id=schema.string(schema.require(raw, "id", "skill"), "skill.id"),
        instructions=schema.string(
            schema.require(raw, "instructions", "skill"), "skill.instructions"
        ),
        allowed_tools=schema.string_list(
            schema.require(raw, "allowed_tools", "skill"), "skill.allowed_tools"
        ),
    )

