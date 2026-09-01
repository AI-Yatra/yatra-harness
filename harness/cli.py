"""Operator CLI for task intake, execution, recovery, inspection, and replay."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__, auth
from .artifacts import ArtifactStore
from .config import load_config, load_skill, load_task
from .contracts import RunStatus
from .delivery import DeliveryRequest, deliver
from .doctor import run_doctor
from .errors import HarnessError, InjectedCrash
from .events import EventLog
from .goal import GoalRequest, pursue
from .model_router import build_llm_light, profile_from_route
from .policy import PolicyEngine
from .replay import replay_run
from .runtime import HarnessRuntime
from .state import StateStore
from .tools import build_registry
from .workspace import Workspace

ROUTE_PRIORITY_KEYS = ("privacy", "quality", "cost", "latency", "context")


def _add_routing_arguments(command: Any) -> None:
    """Add the shared LLM Light selection flags to a subcommand."""
    group = command.add_argument_group("LLM Light routing")
    group.add_argument(
        "--profile",
        help="named routing profile from the llm_light.profiles section",
    )
    group.add_argument(
        "--priority",
        action="append",
        default=[],
        metavar="KEY",
        dest="priorities",
        help="rank routes by these keys in order; repeatable. "
        f"one of {', '.join(ROUTE_PRIORITY_KEYS)}",
    )
    group.add_argument(
        "--require-local",
        action="store_true",
        help="exclude any route that sends prompts off this machine",
    )
    group.add_argument(
        "--max-cost",
        type=float,
        default=None,
        metavar="USD",
        help="exclude routes whose blended cost per 1M tokens exceeds this",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="harness", description=__doc__)
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="validate environment and configured adapters")
    doctor.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    doctor.add_argument("--task", type=Path)
    doctor.add_argument("--skill", type=Path)

    explain = commands.add_parser("explain", help="resolve a task without creating a run")
    explain.add_argument("task", type=Path)
    explain.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    explain.add_argument("--skill", type=Path, default=Path("skills/bugfix.yaml"))

    tools = commands.add_parser("tools", help="list normalized native and MCP capabilities")
    tools.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    tools.add_argument("--skill", type=Path, default=Path("skills/bugfix.yaml"))
    tools.add_argument("--source", choices=("all", "native", "mcp"), default="all")

    run = commands.add_parser("run", help="create and execute a task run")
    run.add_argument("task", type=Path)
    run.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    run.add_argument("--skill", type=Path, default=Path("skills/bugfix.yaml"))
    run.add_argument("--model", help="pin one named route as primary, bypassing LLM Light")
    run.add_argument("--fallback", help="replace configured fallback with one named route")
    run.add_argument("--max-turns", type=int)
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--fault", default="")
    run.add_argument(
        "--session",
        default="",
        metavar="ID",
        help="reuse this session's workspace, so the run continues earlier work "
        "instead of starting from the seed again",
    )
    run.add_argument("--yes", action="store_true", help="approve policy-gated actions non-interactively")
    run.add_argument(
        "--scenario",
        choices=("first-patch-fails", "denied-action", "repair-demo"),
        help="teaching scenario shortcut (workshop compatibility)",
    )
    run.add_argument(
        "--approval",
        choices=("prompt", "auto", "never"),
        help="override policy.approval_mode (workshop compatibility)",
    )
    _add_delivery_arguments(run)
    _add_routing_arguments(run)

    routes = commands.add_parser("routes", help="show the LLM Light route plan without running")
    routes.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    _add_routing_arguments(routes)

    resume = commands.add_parser("resume", help="resume a non-terminal run from checkpoint")
    resume.add_argument("run_id")
    resume.add_argument("--runs-dir", type=Path, default=Path(".runs"))
    resume.add_argument("--yes", action="store_true")

    inspect = commands.add_parser("inspect", help="show state and a compact event timeline")
    inspect.add_argument("run_id")
    inspect.add_argument("--runs-dir", type=Path, default=Path(".runs"))
    inspect.add_argument("--events", type=int, default=30)

    replay = commands.add_parser("replay", help="verify and reconstruct an event ledger")
    replay.add_argument("run_id")
    replay.add_argument("--runs-dir", type=Path, default=Path(".runs"))

    auth_parser = commands.add_parser("auth", help="manage provider credentials")
    auth_sub = auth_parser.add_subparsers(dest="auth_action")
    add_cmd = auth_sub.add_parser("add", help="store a key; the provider is inferred")
    add_cmd.add_argument("key", nargs="?", help="the key; omit to be prompted without echo")
    add_cmd.add_argument("--provider", help="override provider detection")
    add_cmd.add_argument("--base-url", help="pin a non-default base URL")
    status_cmd = auth_sub.add_parser("status", help="show configured credentials")
    status_cmd.add_argument("--json", action="store_true")
    verify_cmd = auth_sub.add_parser("verify", help="make a real call to the provider")
    verify_cmd.add_argument("provider", nargs="?", help="default: every configured provider")
    verify_cmd.add_argument("--timeout", type=float, default=20.0)
    remove_cmd = auth_sub.add_parser("remove", help="delete a stored key")
    remove_cmd.add_argument("provider")
    auth_sub.add_parser("providers", help="list known providers")

    goal = commands.add_parser(
        "goal", help="attempt a goal repeatedly until its acceptance command passes"
    )
    goal.add_argument("objective", help="what must become true")
    goal.add_argument(
        "--accept", action="append", default=[], metavar="CMD", required=True,
        help="acceptance command, repeatable. This is the stopping condition",
    )
    goal.add_argument("--repo", type=Path, default=None, help="work on a clone of this repository")
    goal.add_argument("--seed", type=Path, default=None, help="work on a copy of this directory")
    goal.add_argument("--base-ref", default="", help="branch, tag or commit to start from")
    goal.add_argument("--protect", action="append", default=[], metavar="GLOB")
    goal.add_argument("--config", type=Path, default=Path("configs/teaching.yaml"))
    goal.add_argument("--skill", type=Path, default=Path("skills/repo-edit.yaml"))
    goal.add_argument("--max-attempts", type=int, default=3)
    goal.add_argument("--max-seconds", type=float, default=1800.0)
    goal.add_argument("--yes", action="store_true", help="approve policy-gated actions")
    _add_delivery_arguments(goal)
    _add_routing_arguments(goal)

    deliver_cmd = commands.add_parser(
        "deliver", help="commit, push and open a pull request for a completed run"
    )
    deliver_cmd.add_argument("run_id")
    deliver_cmd.add_argument("--runs-dir", type=Path, default=Path(".runs"))
    deliver_cmd.add_argument(
        "--mode", choices=("commit", "branch", "pr"), default="pr",
        help="how far to go: local commit, pushed branch, or pull request",
    )
    deliver_cmd.add_argument("--base", default="", help="pull request target branch")
    deliver_cmd.add_argument(
        "--yes",
        dest="deliver_yes",
        action="store_true",
        help="approve the push and the pull request non-interactively",
    )

    listing = commands.add_parser("list-runs", help="list durable run checkpoints")
    listing.add_argument("--runs-dir", type=Path, default=Path(".runs"))
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    # Load .env before anything resolves a credential, so `harness` and `ay`
    # see the same file. Exported variables still win; a missing or
    # unreadable .env is ignored rather than fatal.
    auth.load_env_file()
    try:
        if arguments.command == "auth":
            return _auth(arguments)
        if arguments.command == "doctor":
            return _doctor(arguments)
        if arguments.command == "explain":
            return _explain(arguments)
        if arguments.command == "tools":
            return _tools(arguments)
        if arguments.command == "routes":
            return _routes(arguments)
        if arguments.command == "run":
            model, fallback = _resolve_model_alias(arguments.model, arguments.fallback)
            scenario_route = _resolve_scenario(arguments.scenario)
            if scenario_route and not model:
                model = scenario_route
            approval = _resolve_approval(arguments)
            result = HarnessRuntime.start(
                config_path=arguments.config,
                task_path=arguments.task,
                skill_path=arguments.skill,
                model=model,
                fallback=fallback,
                max_turns=arguments.max_turns,
                max_seconds=arguments.max_seconds,
                fault=arguments.fault,
                profile=arguments.profile,
                priorities=tuple(arguments.priorities),
                require_local=arguments.require_local,
                max_cost_per_1m=arguments.max_cost,
                session_id=arguments.session,
                approval_callback=approval,
            )
            code = _print_result(result)
            if arguments.deliver != "none" and code == 0:
                code = _deliver(
                    result.run_dir,
                    mode=arguments.deliver,
                    base=arguments.base,
                    yes=arguments.deliver_yes,
                )
            return code
        if arguments.command == "resume":
            result = HarnessRuntime.resume(
                arguments.run_id,
                runs_dir=arguments.runs_dir,
                approval_callback=_approval(arguments.yes),
            )
            return _print_result(result)
        if arguments.command == "inspect":
            return _inspect(arguments)
        if arguments.command == "replay":
            return _replay(arguments)
        if arguments.command == "goal":
            return _goal(arguments)
        if arguments.command == "deliver":
            run_dir = arguments.runs_dir.expanduser().resolve() / arguments.run_id
            return _deliver(
                run_dir, mode=arguments.mode, base=arguments.base, yes=arguments.deliver_yes
            )
        if arguments.command == "list-runs":
            return _list_runs(arguments)
    except InjectedCrash as exc:
        print(f"INTERRUPTED: {exc}", file=sys.stderr)
        return 75
    except (HarnessError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _doctor(arguments: Any) -> int:
    checks = run_doctor(arguments.config, task_path=arguments.task, skill_path=arguments.skill)
    for check in checks:
        marker = "PASS" if check.ok else ("FAIL" if check.required else "WARN")
        print(f"{marker:4}  {check.name:24} {check.detail}")
    return 0 if all(check.ok or not check.required for check in checks) else 2


def _explain(arguments: Any) -> int:
    config = load_config(arguments.config)
    task = load_task(arguments.task)
    skill = load_skill(arguments.skill)
    value = {
        "task": {
            "id": task.task_id,
            "objective": task.objective,
            "workspace_mode": "repository" if task.repository else "seed",
            "workspace_origin": str(task.origin),
            "base_ref": task.base_ref,
            "constraints": list(task.constraints),
            "protected_paths": list(task.protected_paths),
            "acceptance": [list(command) for command in task.acceptance.commands],
        },
        "skill": {"id": skill.skill_id, "allowed_tools": list(skill.allowed_tools)},
        "model_router": {
            "primary": config.router.primary,
            "fallbacks": list(config.router.fallbacks),
            "routes": sorted(config.router.routes),
            "llm_light": _routing_view(config),
        },
        "budgets": {
            "turns": config.budgets.max_turns,
            "tool_calls": config.budgets.max_tool_calls,
            "seconds": config.budgets.max_seconds,
            "context_chars": config.budgets.max_context_chars,
            "output_chars": config.budgets.max_output_chars,
            "verification_attempts": config.budgets.max_verification_attempts,
        },
        "policy": {
            "approval_mode": config.policy.approval_mode,
            "network_enabled": config.policy.network_enabled,
            "allowed_commands": [list(c) for c in config.policy.allowed_commands],
            "allowed_domains": list(config.policy.allowed_domains),
            "command_timeout_seconds": config.policy.command_timeout_seconds,
            "browser_timeout_seconds": config.policy.browser_timeout_seconds,
        },
    }
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def _tools(arguments: Any) -> int:
    config = load_config(arguments.config)
    skill = load_skill(arguments.skill)
    with tempfile.TemporaryDirectory(prefix="harness-tools-") as temporary:
        root = Path(temporary)
        workspace = Workspace(root / "workspace", ())
        workspace.root.mkdir()
        artifacts = ArtifactStore(root / "run")
        policy = PolicyEngine(config.policy, skill.allowed_tools, lambda *_args: True)
        registry = build_registry(config, skill, workspace, artifacts, policy)
        for spec in registry.specs():
            is_mcp = spec.source.startswith("mcp:")
            if arguments.source == "native" and is_mcp:
                continue
            if arguments.source == "mcp" and not is_mcp:
                continue
            print(f"{spec.name:18} {spec.risk.value:8} {spec.source:20} {spec.description}")
    return 0


ROUTES_COLUMNS = (
    ("rank", 4, ""),
    ("route", 22, "<"),
    ("local", 6, "<"),
    ("cost", 9, ">"),
    ("latency", 8, "<"),
    ("quality", 8, ">"),
    ("context", 10, ">"),
)


def _routing_view(config: Any) -> dict[str, Any]:
    """Render the LLM Light plan for inspection. Never includes credentials."""
    light = build_llm_light(config.llm_light)
    if light is None:
        return {
            "enabled": False,
            "resolved_order": [
                config.router.primary,
                *config.router.fallbacks,
            ],
        }
    plan = light.plan(
        {name: profile_from_route(route) for name, route in config.router.routes.items()}
    )
    return {
        "enabled": True,
        "profile": plan.profile,
        "mode": plan.mode,
        "priorities": list(plan.priorities),
        "resolved_order": list(plan.routes),
        "candidates": [item.as_dict() for item in plan.decisions if item.selected],
        "excluded": [item.as_dict() for item in plan.decisions if not item.selected],
        "available_profiles": sorted(config.llm_light.profiles),
    }


def _routes(arguments: Any) -> int:
    """Render the LLM Light plan. No network, no credentials, no side effects."""
    config = load_config(arguments.config).with_overrides(
        profile=arguments.profile,
        priorities=tuple(arguments.priorities),
        require_local=arguments.require_local,
        max_cost_per_1m=arguments.max_cost,
    )
    light = build_llm_light(config.llm_light)
    if light is None:
        print("LLM Light is disabled for this config; using the declared route order.")
        print(f"primary:   {config.router.primary}")
        print(f"fallbacks: {', '.join(config.router.fallbacks) or '(none)'}")
        return 0
    plan = light.plan(
        {name: profile_from_route(route) for name, route in config.router.routes.items()}
    )
    print(f"profile:     {plan.profile}")
    print(f"mode:        {plan.mode}")
    print(f"priorities:  {' > '.join(plan.priorities) if plan.priorities else '(declared order)'}")
    print()
    header = "  ".join(
        f"{title:{align}{width}}" for title, width, align in ROUTES_COLUMNS
    )
    print(header)
    print("-" * len(header))
    for decision in plan.decisions:
        metrics = decision.metrics
        row = (
            decision.rank if decision.selected else "-",
            decision.name,
            "local" if metrics.get("local") else "remote",
            f"{metrics.get('blended_cost_usd_per_1m', 0):.4f}",
            metrics.get("latency", ""),
            f"{metrics.get('quality', 0):.1f}",
            metrics.get("context_window", 0),
        )
        print(
            "  ".join(
                f"{str(value):{align}{width}}"
                for value, (_, width, align) in zip(row, ROUTES_COLUMNS, strict=True)
            )
        )
    print()
    print(f"order: {plan.primary} -> " + " -> ".join(plan.fallbacks or ("(no fallback)",)))
    excluded = [item for item in plan.decisions if not item.selected]
    if excluded:
        print("\nEXCLUDED")
        for item in excluded:
            print(f"  {item.name:22} {item.reason}")
    return 0


SCENARIO_ROUTES = {
    "repair-demo": "teaching",
    "first-patch-fails": "teaching",
    "denied-action": "teaching",
}

# Workshop-plan shorthand for route selection.
MODEL_ALIASES = {
    "replay:repair-demo": "teaching",
    "replay:provider-failure": "broken",
}


def _resolve_model_alias(model: str | None, fallback: str | None) -> tuple[str | None, str | None]:
    """Translate workshop shorthand route names to configured route names."""
    return (
        MODEL_ALIASES.get(model, model) if model else None,
        MODEL_ALIASES.get(fallback, fallback) if fallback else None,
    )


def _resolve_scenario(scenario: str | None) -> str | None:
    """Map a workshop scenario name onto a route. None when unused."""
    if scenario is None:
        return None
    route = SCENARIO_ROUTES[scenario]
    print(f"scenario {scenario!r}: using route {route!r}")
    return route


def _resolve_approval(arguments: Any):
    """Apply --approval / --yes to the approval callback selection.

    ``--approval auto`` (and the compatibility ``--yes``) auto-approve every
    policy-gated action. ``--approval never`` denies anything that requires
    approval -- the strictest setting. ``--approval prompt`` asks interactively
    when stdin is a terminal and denies otherwise.
    """
    choice = getattr(arguments, "approval", None)
    if choice == "auto" or (choice is None and arguments.yes):
        return _approval(True)
    if choice == "never":
        return _approval(False)
    return _approval(False)  # prompt: interactive when tty, deny otherwise


def _approval(yes: bool):
    def decide(spec: Any, arguments: dict[str, Any], reason: str) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            return False
        print(f"Approval required for {spec.name} ({spec.risk.value}): {reason}")
        print(json.dumps(arguments, indent=2, sort_keys=True))
        try:
            answer = input("Approve this one action? [y/N] ").strip().lower()
        except EOFError:
            # isatty() is not a reliable non-interactive test: on Windows it
            # reports True for the NUL device, so a redirected stdin reaches
            # input() anyway. Denying here keeps an unattended run from dying
            # on an unhandled exception and leaving the bundle unfinalized.
            print("no interactive approver available; denying")
            return False
        return answer in {"y", "yes"}

    return decide


def _auth(arguments: Any) -> int:
    action = getattr(arguments, "auth_action", None)
    if action == "add":
        key = arguments.key
        if not key:
            # Prompting keeps the secret out of shell history.
            key = getpass.getpass("Paste the API key (input hidden): ").strip()
        record = auth.add(key, provider=arguments.provider, base_url=arguments.base_url or "")
        print(f"stored {record['provider']} key {record['key']}")
        print(f"  file:  {record['path']}")
        print(f"  routes naming {record['env']} now resolve without exporting it")
        return 0
    if action == "status":
        rows = auth.status()
        if arguments.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        print(f"{'provider':<12} {'source':<12} {'key':<30} note")
        print("-" * 80)
        for row in rows:
            if not row["ready"]:
                continue
            print(f"{row['provider']:<12} {row['source']:<12} {row['key']:<30} {row['note']}")
        missing = [r["provider"] for r in rows if not r["ready"]]
        if missing:
            print()
            print("no credential: " + ", ".join(missing))
        print()
        print(f"store: {auth.store_path()}")
        return 0
    if action == "verify":
        if arguments.provider:
            names = [arguments.provider]
        else:
            names = [r["provider"] for r in auth.status()
                     if r["source"] != auth.SOURCE_NONE]
        if not names:
            print("no credentials configured; nothing to verify")
            return 1
        failures = 0
        for name in names:
            result = auth.verify(name, timeout=arguments.timeout)
            print(f"{'PASS' if result.ok else 'FAIL'}  {result.provider:<12} "
                  f"[{result.source}] {result.detail}")
            failures += 0 if result.ok else 1
        return 1 if failures else 0
    if action == "remove":
        if auth.remove_key(arguments.provider):
            print(f"removed the stored {arguments.provider} key")
            return 0
        print(f"no stored key for {arguments.provider}")
        return 1
    if action == "providers":
        print(f"{'provider':<12} {'api':<18} {'env var':<22} aliases")
        print("-" * 80)
        for provider in auth.PROVIDERS:
            env = provider.env[0] if provider.env else "-"
            print(f"{provider.name:<12} {provider.api:<18} {env:<22} "
                  f"{', '.join(provider.aliases)}")
        return 0
    print("usage: harness auth {add,status,verify,remove,providers}", file=sys.stderr)
    return 2


def _goal(arguments: Any) -> int:
    """Pursue a goal, then deliver it if it was reached and delivery was asked for."""
    if (arguments.repo is None) == (arguments.seed is None):
        print("error: goal needs exactly one of --repo or --seed", file=sys.stderr)
        return 2
    config = load_config(arguments.config)
    approval = _resolve_approval(arguments)

    def runner(task_path: Path, attempt: int) -> Any:
        print(f"\n== attempt {attempt} ==")
        result = HarnessRuntime.start(
            config_path=arguments.config,
            task_path=task_path,
            skill_path=arguments.skill,
            profile=arguments.profile,
            priorities=tuple(arguments.priorities),
            require_local=arguments.require_local,
            max_cost_per_1m=arguments.max_cost,
            approval_callback=approval,
        )
        print(f"   {result.status.value}: {result.terminal_reason}")
        return result

    result = pursue(
        GoalRequest(
            objective=arguments.objective,
            acceptance=tuple(arguments.accept),
            config_path=arguments.config,
            skill_path=arguments.skill,
            runs_dir=config.runs_dir,
            seed=arguments.seed,
            repository=arguments.repo,
            base_ref=arguments.base_ref,
            protect=tuple(arguments.protect),
            max_attempts=arguments.max_attempts,
            max_seconds=arguments.max_seconds,
        ),
        runner=runner,
    )
    print()
    print(f"goal: {'ACHIEVED' if result.achieved else 'NOT ACHIEVED'}")
    print(f"reason: {result.reason}")
    print(f"attempts: {len(result.attempts)}")
    print(f"record: {result.record_path}")
    if not result.achieved:
        return 2
    if arguments.deliver != "none":
        return _deliver(
            config.runs_dir / result.last_run_id,
            mode=arguments.deliver,
            base=arguments.base,
            yes=arguments.deliver_yes,
        )
    return 0


def _add_delivery_arguments(command: Any) -> None:
    group = command.add_argument_group("delivery")
    group.add_argument(
        "--deliver",
        choices=("none", "commit", "branch", "pr"),
        default="none",
        help="what to do with a run that passes verification (default: nothing)",
    )
    group.add_argument("--base", default="", help="pull request target branch")
    group.add_argument(
        "--deliver-yes",
        action="store_true",
        help="approve pushing and opening the pull request without prompting. "
        "Deliberately separate from --yes: that approves what the model may do "
        "inside the workspace, this approves publishing the result",
    )


def _deliver(run_dir: Path, *, mode: str, base: str, yes: bool) -> int:
    """Deliver a finished run from its bundle.

    The bundle is the only input, so `harness run --deliver` and
    `harness deliver <run-id>` cannot drift apart: both read the run's own
    frozen task and durable state rather than whatever the caller has to hand.
    """
    state = StateStore(run_dir / "state.json").load()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    task = load_task(run_dir / manifest["inputs"]["task"])
    request = DeliveryRequest(
        mode=mode,
        run_id=state.run_id,
        run_dir=run_dir,
        workspace=Path(state.workspace),
        objective=task.objective,
        status=state.status,
        summary=state.finish_summary,
        base=base,
    )
    result = deliver(request, approve=_delivery_approval(yes))
    print(f"delivered: {result.mode}")
    print(f"branch: {result.branch}")
    print(f"commit: {result.commit}")
    if result.pushed:
        print(f"pushed: {result.branch} -> origin")
    if result.pull_request_url:
        print(f"pull request: {result.pull_request_url}")
    return 0


def _delivery_approval(yes: bool):
    """Confirm an outward-facing delivery step.

    Deliberately separate from the policy approver: that one authorises what
    the model asked to do inside the workspace, this one authorises what the
    operator is about to publish. Without a terminal and without --yes the
    answer is no, so an unattended run never pushes by accident.
    """
    def decide(description: str) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            return False
        try:
            answer = input(f"{description}? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    return decide


def _print_result(result: Any) -> int:
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status.value}")
    print(f"reason: {result.terminal_reason}")
    print(f"run_dir: {result.run_dir}")
    print(f"summary: {result.summary_path}")
    return 0 if result.status is RunStatus.COMPLETED else 2


def _inspect(arguments: Any) -> int:
    run_dir = arguments.runs_dir.expanduser().resolve() / arguments.run_id
    state = StateStore(run_dir / "state.json").load()
    events = list(EventLog(run_dir / "events.jsonl", arguments.run_id).read())
    print(
        json.dumps(
            {
                "run_id": state.run_id,
                "status": state.status.value,
                "turn": state.turn,
                "tool_calls": state.tool_calls,
                "verification_attempts": state.verification_attempts,
                "retries": state.retries,
                "terminal_reason": state.terminal_reason,
            },
            indent=2,
        )
    )
    print("\nEVENTS")
    for event in events[-arguments.events :]:
        print(f"{event.sequence:04d}  {event.timestamp}  {event.event_type}")
    return 0


def _replay(arguments: Any) -> int:
    summary = replay_run(arguments.runs_dir.expanduser().resolve() / arguments.run_id)
    print(json.dumps({
        "run_id": summary.run_id,
        "events": summary.events,
        "model_calls": summary.model_calls,
        "tool_calls": summary.tool_calls,
        "verification_attempts": summary.verification_attempts,
        "terminal_event": summary.terminal_event,
        "ledger_hash": summary.ledger_hash,
    }, indent=2, sort_keys=True))
    return 0


def _list_runs(arguments: Any) -> int:
    runs_dir = arguments.runs_dir.expanduser().resolve()
    if not runs_dir.exists():
        return 0
    for path in sorted(runs_dir.iterdir(), reverse=True):
        if not path.is_dir() or not (path / "state.json").is_file():
            continue
        state = StateStore(path / "state.json").load()
        print(f"{state.run_id:72} {state.status.value:18} {state.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

