#!/usr/bin/env python3
"""ay: a Claude Code-style REPL on top of the Yatra Harness.

Turns a natural-language message into a task.yaml, runs it through the
harness with live event streaming, and lets you iterate in the same session.

Commands:
  <your message>          run the agent on the message (task intake)
  /runs                   list past runs
  /inspect <run_id>       show a run's terminal state + recent events
  /resume <run_id>        resume a non-terminal run from its checkpoint
  /config                 show the active config path + model
  /model <name>           switch the configured model (qwen-plus, qwen-max, ...)
  /pr [run_id]            open a pull request for a completed run (--repo only)
  /help                   this help
  /exit, /quit            leave

Requires:
  - yatra-harness synced with `uv sync` (openpyxl installed)
  - a credential for the variable the active config's primary route names
    (DASHSCOPE_API_KEY by default). Supply it with `ay auth add <key>`, or
    export it, or put it in .env -- all three are resolved by harness.auth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "configs" / "palimpsest-config.yaml"
DEFAULT_SKILL = ROOT / "skills" / "palimpsest-skill.yaml"
TASKS_DIR = ROOT / "tasks" / "chat"
CHAT_SEED = ROOT / "fixtures" / "chat_seed"
RUNS_DIR = ROOT / ".runs"

HELP_TEXT = """\
Commands:
  <your message>          run the agent on the message (task intake)
  /runs                   list past runs
  /inspect <run_id>       show a run's terminal state + recent events
  /resume <run_id>        resume a non-terminal run from its checkpoint
  /config                 show the active config path + model
  /model <name>           switch the configured model (qwen-plus, qwen-max, ...)
  /pr [run_id]            open a pull request for a completed run (--repo only)
  /help                   this help
  /exit, /quit            leave
"""


BANNER = (
    "   ███████ ███████     ███ ███ ███████ ███████ ███████ ███████\n"
    "   ███ ███   ███       ███ ███ ███ ███   ███   ███ ███ ███ ███\n"
    "   ███████   ███       ███████ ███████   ███   ███████ ███████\n"
    "   ███ ███   ███         ███   ███ ███   ███   ███ ██  ███ ███\n"
    "   ███ ███ ███████       ███   ███ ███   ███   ███  ██ ███ ███\n"
)


ASCII_BANNER = (
    "   #####  ##     ##  ##  ####  ###### ####   ####\n"
    "   ##  ##  ##      ####  ##  ##   ##   ##  ## ##  ##\n"
    "   #####   ##       ##   ######   ##   ####   ######\n"
    "   ##  ##  ##       ##   ##  ##   ##   ## ##  ##  ##\n"
    "   ##  ## ####      ##   ##  ##   ##   ##  ## ##  ##\n"
)


def _tint(text: str, code: str) -> str:
    """ANSI colour, skipped when output is piped or NO_COLOR is set."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return "\033[" + code + "m" + text + "\033[0m"


def _short_path(path: Path) -> str:
    """Render a path with the home directory collapsed to ~."""
    try:
        return "~" + os.sep + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class ChatApp:
    def __init__(
        self,
        config_path: Path,
        skill_path: Path,
        verbose: bool,
        seed: Path | None = None,
        accept: list[str] | None = None,
        protect: list[str] | None = None,
        repository: Path | None = None,
        base_ref: str = "",
        deliver: str = "none",
        base: str = "",
        deliver_yes: bool = False,
        session: str = "",
        stateless: bool = False,
    ) -> None:
        self.config_path = config_path
        self.skill_path = skill_path
        self.verbose = verbose
        # A chat message normally runs against an empty scratch workspace.
        # --seed points it at a real fixture instead, which is what makes a
        # seeded task such as the palimpsest contact cards reachable here.
        self.seed = Path(seed).resolve() if seed else CHAT_SEED
        # --accept replaces the trivially-passing default acceptance command.
        # Without it a chat run cannot fail, so its verdict is not evidence.
        self.accept = list(accept or [])
        self.protect = list(protect or [])
        # --repo replaces the seed entirely: the run works on a clone of a
        # real repository, which is what makes a pull request possible.
        self.repository = Path(repository).resolve() if repository else None
        self.base_ref = base_ref or ""
        self.deliver = deliver or "none"
        self.base = base or ""
        # The REPL always passes --yes so the model's tool calls are not
        # gated mid-conversation. Publishing is a different kind of consent,
        # so it needs its own flag and asks at the terminal by default.
        self.deliver_yes = bool(deliver_yes)
        # A conversation gets one workspace and one memory. Without that,
        # turn two cannot build on turn one and does not know it happened.
        # Generated rather than fixed, so two REPLs never collide.
        self.session_id = session or f"ay-{uuid.uuid4().hex[:10]}"
        self.stateless = bool(stateless)
        self.last_run_id: str | None = None
        self.model = self._detect_model()

    # ---------------------------------------------------------------- setup

    def _load_env(self) -> None:
        """Load .env through harness.auth so both entry points agree.

        The nearest .env above the working directory wins; the install
        directory is the fallback, which is what `ay` used to read and keeps
        a repo-local .env working when run from elsewhere.
        """
        from harness import auth  # noqa: PLC0415

        if auth.load_env_file() is None:
            auth.load_env_file(ROOT)

    def _primary_route(self) -> Any | None:
        """The config's primary route, or None if the config cannot be read.

        Everything that needs to know about the model, the endpoint or the
        credential goes through here, so the banner, /model and the startup
        gate can never disagree about which route is in play.

        The except is narrow deliberately: a blanket one turns a mistake in
        this method into a silent wrong answer. load_config funnels every
        bad-config failure into ConfigurationError, so HarnessError is the
        whole expected set.
        """
        try:
            from harness.config import load_config  # noqa: PLC0415
            from harness.errors import HarnessError  # noqa: PLC0415
        except ImportError:
            return None
        try:
            router = load_config(self.config_path).router
            return router.routes[router.primary]
        except (HarnessError, OSError, KeyError):
            return None

    def _detect_model(self) -> str:
        """The primary route's model.

        Resolved through the config loader rather than by matching the first
        `model:` line, because the routes mapping is not ordered
        primary-first -- model_router.primary names it.
        """
        route = self._primary_route()
        return route.model if route is not None and route.model else "?"

    @staticmethod
    def _model_line(text: str, route_name: str) -> int | None:
        """Index of the `model:` line belonging to `route_name`.

        Scoped to the routes mapping and then to that route's own block. A
        first-match rewrite retargets a different route the moment the
        primary is not listed first, and it writes, so it corrupts rather
        than merely misreports.
        """
        lines = text.splitlines()
        header = re.compile(r"^(\s*)" + re.escape(route_name) + r":\s*(#.*)?$")
        model = re.compile(r"^\s*model:\s*\S+\s*$")
        routes_indent: int | None = None
        for index, line in enumerate(lines):
            if routes_indent is None:
                found = re.match(r"^(\s*)routes:\s*$", line)
                if found:
                    routes_indent = len(found.group(1))
                continue
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if len(line) - len(line.lstrip()) <= routes_indent:
                    return None  # walked out of the routes mapping
            found = header.match(line)
            if not found:
                continue
            block_indent = len(found.group(1))
            for offset in range(index + 1, len(lines)):
                current = lines[offset]
                if not current.strip() or current.lstrip().startswith("#"):
                    continue
                if len(current) - len(current.lstrip()) <= block_indent:
                    break  # walked out of this route's block
                if model.match(current):
                    return offset
            return None
        return None

    def _check_key(self) -> bool:
        """Report a missing credential up front rather than mid-run.

        Resolution goes through ``harness.auth``, so a key held by
        ``ay auth add`` counts here exactly as an exported variable does.
        Reading the environment alone made a stored key invisible and left
        the REPL refusing to start on a credential it already had.

        A route with no api_key_env, and a config that cannot be read, both
        pass: the first needs no credential (a local server or a replay
        script) and the second has a real error of its own that the harness
        will report far better than a guess from here.
        """
        self._load_env()
        from harness import auth  # noqa: PLC0415

        route = self._primary_route()
        if route is None or not route.api_key_env:
            return True
        env_var = route.api_key_env
        if auth.resolve_route(env_var, route.base_url).available:
            return True
        # Labels are left-aligned because the variable name makes the
        # export line an unpredictable width.
        print(f"No credential for {env_var} (needed by {self.config_path.name}).")
        print("  store one:  ay auth add <key>   (the provider is inferred)")
        print(f"  or export:  {env_var}=...   (environment or .env)")
        print("  check:      ay auth status")
        return False

    # ------------------------------------------------------------- task gen

    def _slug(self, message: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", message).strip("-").lower()
        return (slug or "task")[:40]

    def _write_task(self, message: str) -> Path:
        """Write a chat task YAML for the message and return its path."""
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        if self.seed == CHAT_SEED:
            CHAT_SEED.mkdir(parents=True, exist_ok=True)
        slug = self._slug(message)
        task_id = f"chat-{slug}-{uuid.uuid4().hex[:6]}"
        path = TASKS_DIR / f"{task_id}.yaml"
        # Objective is the user message. The seed defaults to an empty
        # scratch dir and the acceptance command to one that always passes,
        # so an unconfigured chat run is checked by the operator's judgment
        # rather than by the harness. Supplying --accept (and usually --seed
        # and --protect) turns the run into a falsifiable one.
        if self.accept:
            commands = [shlex.split(command) for command in self.accept]
            require_diff = True
        elif self.repository is not None:
            # Against a real repository "the agent did nothing" must not read
            # as success. There is still no acceptance command to run -- the
            # operator has to supply one with --accept -- but an empty diff
            # now fails, which is the weakest honest gate available here.
            commands = [["python", "-c", "print('repo acceptance ok')"]]
            require_diff = True
        else:
            commands = [["python", "-c", "print('chat acceptance ok')"]]
            require_diff = False
        # Prefer a path relative to the task file so run bundles stay
        # relocatable; fall back to absolute (e.g. a seed on another drive).
        constraints = [
            "  - Work in the workspace; create files there if the task needs artifacts.",
            "  - Keep responses concise and factual.",
        ]
        for line in self._session_notes().splitlines():
            if line.strip():
                constraints.append(f"  - {json.dumps(line.strip())}")
        constraints = "\n".join(constraints)
        if self.repository is not None:
            origin = f"repository: {json.dumps(self.repository.as_posix())}\n"
            if self.base_ref:
                origin += f"base_ref: {json.dumps(self.base_ref)}\n"
        else:
            try:
                seed_value = os.path.relpath(self.seed, TASKS_DIR).replace(os.sep, "/")
            except ValueError:
                seed_value = self.seed.as_posix()
            origin = f"workspace_seed: {json.dumps(seed_value)}\n"
        content = f"""\
version: 1
id: {task_id}
objective: >-
  {message}
{origin}\
constraints:
{constraints}
protected_paths: {json.dumps(self.protect)}
acceptance:
  commands: {json.dumps(commands)}
  require_non_empty_diff: {json.dumps(require_diff)}
  timeout_seconds: 30
metadata:
  workshop_module: ay
  difficulty: open-ended
  notes: |
    Generated by ay REPL from a user message.
"""
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------- exec

    def _harness_command(self, task_path: Path) -> list[str]:
        """The exact `harness run` invocation for a task.

        Split out from `_run_harness` so the flags can be asserted without
        starting a subprocess and a model call.
        """
        command = [
            sys.executable,
            "-m",
            "harness",
            "run",
            str(task_path),
            "--config",
            str(self.config_path),
            "--skill",
            str(self.skill_path),
            "--yes",
        ]
        if not self.stateless:
            command += ["--session", self.session_id]
        if self.deliver != "none":
            command += ["--deliver", self.deliver]
            if self.base:
                command += ["--base", self.base]
            if self.deliver_yes:
                command += ["--deliver-yes"]
        return command

    def _note_run_id(self, line: str) -> None:
        """Remember the run id the harness printed, so /pr needs no argument."""
        if line.startswith("run_id: "):
            candidate = line[len("run_id: "):].strip()
            if candidate:
                self.last_run_id = candidate

    def _session_notes(self) -> str:
        """What earlier turns in this conversation did, for the next task.

        Read from disk on every message rather than held in memory, so the
        notes stay correct if a run is inspected or resumed out of band.
        """
        if self.stateless:
            return ""
        try:
            from harness.session import SessionStore  # noqa: PLC0415

            store = SessionStore(RUNS_DIR)
            return store.notes(store.open(self.session_id))
        except (OSError, ValueError, ImportError):
            # Memory is a convenience; losing it must not end the conversation.
            return ""

    def _record_turn(self, message: str) -> None:
        """Write what this turn did into the session's memory.

        Read back from the run's own state rather than inferred from the exit
        code, so the memory says what actually happened -- a run can end
        BLOCKED or BUDGET_EXHAUSTED, and "it failed" is not enough for the
        next turn to avoid repeating it.
        """
        if self.stateless or not self.last_run_id:
            return
        try:
            from harness.contracts import RunStatus  # noqa: PLC0415
            from harness.session import SessionStore  # noqa: PLC0415

            state_path = RUNS_DIR / self.last_run_id / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            store = SessionStore(RUNS_DIR)
            session = store.open(self.session_id)
            store.record(
                session,
                run_id=self.last_run_id,
                message=message,
                status=RunStatus(state.get("status", "FAILED")),
                reason=str(state.get("terminal_reason", "")),
                changed=tuple(self._changed_paths(RUNS_DIR / self.last_run_id)),
            )
        except (OSError, ValueError, KeyError, ImportError, json.JSONDecodeError):
            # Memory is a convenience; failing to write it must not end the
            # conversation or discard the work the run already did.
            pass

    @staticmethod
    def _changed_paths(run_dir: Path) -> list[str]:
        attempts = sorted((run_dir / "artifacts" / "verification").glob("attempt-*.json"))
        if not attempts:
            return []
        try:
            return list(json.loads(attempts[-1].read_text(encoding="utf-8")).get("changed_paths") or [])
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _run_harness(self, task_path: Path) -> int:
        """Run the harness as a subprocess and stream events live."""
        self._load_env()
        cmd = self._harness_command(task_path)
        print(f"\n==> {self.model} | {task_path.stem}\n")
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # stdout is non-None because we passed subprocess.PIPE; the assert
        # helps the type-checker and documents the invariant.
        assert process.stdout is not None
        # Stream lines live (flush per line so the user sees progress).
        for line in process.stdout:
            self._note_run_id(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        process.wait()
        return process.returncode

    # ------------------------------------------------------------- repl

    def _handle_command(self, line: str) -> bool:
        """Handle a /command. Return False if the session should end."""
        parts = line.split()
        command = parts[0].lower()
        if command in {"/exit", "/quit", "/q"}:
            print("bye")
            return False
        if command == "/help" or command == "/?":
            print(HELP_TEXT)
            return True
        if command == "/pr":
            self._pull_request(parts[1] if len(parts) > 1 else None)
            return True
        if command == "/runs":
            self._list_runs()
            return True
        if command == "/inspect":
            if len(parts) < 2:
                print("usage: /inspect <run_id>")
                return True
            self._inspect(parts[1])
            return True
        if command == "/resume":
            if len(parts) < 2:
                print("usage: /resume <run_id>")
                return True
            self._resume(parts[1])
            return True
        if command == "/config":
            print(f"config: {self.config_path}")
            print(f"model:  {self.model}")
            print(f"skill:  {self.skill_path}")
            print(f"seed:   {self.seed}")
            if self.accept:
                print(f"accept: {self.accept}  (real verification, diff required)")
            else:
                print("accept: (default) always passes -- this run cannot fail")
            print(f"protect: {self.protect or '[]'}")
            return True
        if command == "/model":
            if len(parts) < 2:
                print(f"current model: {self.model}")
                return True
            self._set_model(parts[1])
            return True
        print(f"unknown command: {command}  (try /help)")
        return True

    def _list_runs(self) -> None:
        if not RUNS_DIR.exists():
            print("no runs yet")
            return
        runs = sorted(
            (p for p in RUNS_DIR.iterdir() if p.is_dir() and (p / "state.json").is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            print("no runs yet")
            return
        for run in runs[:15]:
            state = (run / "state.json").read_text(encoding="utf-8")
            status = re.search(r'"status"\s*:\s*"([^"]+)"', state)
            task = re.search(r'"task_id"\s*:\s*"([^"]+)"', state)
            print(f"{run.name:72} {status.group(1) if status else '?':18} {task.group(1) if task else '?'}")

    def _inspect(self, run_id: str) -> None:
        self._load_env()
        cmd = [sys.executable, "-m", "harness", "inspect", run_id]
        subprocess.run(cmd, cwd=ROOT, env=os.environ.copy())

    def _resume(self, run_id: str) -> None:
        self._load_env()
        cmd = [sys.executable, "-m", "harness", "resume", run_id, "--yes"]
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            self._note_run_id(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        process.wait()

    def _set_model(self, model: str) -> None:
        """Rewrite the primary route's model in place.

        Edited as text rather than round-tripped through YAML so the
        comments in the config -- which explain why particular models were
        chosen -- survive a /model switch.
        """
        route = self._primary_route()
        if route is None:
            print(f"could not read the config: {self.config_path}")
            return
        try:
            text = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not read the config: {exc}")
            return
        index = self._model_line(text, route.name)
        if index is None:
            print(f"no model line for route {route.name!r} in {self.config_path}")
            return
        # keepends so the file's existing line endings are preserved.
        lines = text.splitlines(keepends=True)
        lines[index] = re.sub(
            r"^(\s*model:\s*)\S+(\s*)$", r"\g<1>" + model + r"\g<2>", lines[index]
        )
        self.config_path.write_text("".join(lines), encoding="utf-8")
        self.model = model
        print(f"model -> {model}  (route {route.name})")

    # ------------------------------------------------------------- main

    def _pull_request(self, run_id: str | None) -> None:
        """Deliver a completed run as a pull request.

        Delegates to `harness deliver` rather than importing the delivery
        module, for the same reason `ay auth` delegates: one code path for
        both entry points, so they cannot disagree about what delivery means.
        """
        target = run_id or self.last_run_id
        if not target:
            print("no run to deliver yet; send a message first, or /pr <run_id>")
            return
        if self.repository is None and run_id is None:
            print("this session is not attached to a repository; start ay with --repo <path>")
            return
        argv = ["deliver", target, "--runs-dir", str(RUNS_DIR), "--mode", "pr"]
        if self.base:
            argv += ["--base", self.base]
        _delegate_to_cli(argv)

    def _print_banner(self) -> None:
        try:
            from harness import __version__ as version
        except Exception:  # noqa: BLE001 - the banner must never block startup
            version = "?"
        if self.accept:
            contract = "verified (" + "; ".join(self.accept) + ")"
        elif self.repository is not None:
            contract = "diff-only (no acceptance command; pass --accept)"
        else:
            contract = "unverified (acceptance always passes)"
        print()
        try:
            print(_tint(BANNER, "36"))
        except UnicodeEncodeError:
            # Some Windows consoles are cp1252; the block glyphs cannot be
            # encoded there. A banner is decoration and must never be fatal.
            print(_tint(ASCII_BANNER, "36"))
        print(_tint(f"# Yatra Harness v{version} · ay REPL", "1"))
        print(_tint(f"# model: {self.model} · config: {self.config_path.name}", "2"))
        if self.repository is not None:
            origin = f"repo: {self.repository.name} ({self.base_ref or 'HEAD'})"
        else:
            origin = f"seed: {self.seed.name}"
        print(_tint(f"# {origin} · contract: {contract}", "2"))
        if not self.stateless:
            print(_tint(f"# session: {self.session_id} (workspace and memory persist)", "2"))
        if self.deliver != "none":
            print(_tint(f"# deliver: {self.deliver} · base: {self.base or 'default'}", "2"))
        print(_tint(f"# {_short_path(ROOT)}", "2"))
        print()
        print("Type a message to run the agent, or /help for commands." + chr(10))

    def run(self) -> int:
        self._print_banner()
        if not self._check_key():
            return 2
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                return 0
            if not line:
                continue
            if line.startswith("/"):
                if not self._handle_command(line):
                    return 0
                continue
            task_path = self._write_task(line)
            try:
                code = self._run_harness(task_path)
            except KeyboardInterrupt:
                print("\n(interrupted)")
                code = 130
            self._record_turn(line)
            if code == 0:
                print("\n[done] acceptance passed")
            else:
                print(f"\n[harness exited {code}] inspect with /inspect or /runs")


def _delegate_to_cli(argv: list[str]) -> int:
    """Hand a subcommand to the harness CLI so `ay` and `harness` agree.

    `ay auth add <key>` and `harness auth add <key>` must be the same code
    path; two credential systems is the failure this module exists to avoid.
    """
    from harness.cli import main as cli_main

    return cli_main(argv)


def main() -> int:
    # Model output and the banner are arbitrary Unicode; a cp1252 console
    # would otherwise raise UnicodeEncodeError mid-stream and kill the REPL.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return main_with_argv(sys.argv[1:])


def main_with_argv(argv: list[str]) -> int:
    """`main` without the console setup, so the argument rules are testable."""
    if argv and argv[0] == "auth":
        return _delegate_to_cli(argv)

    parser = argparse.ArgumentParser(
        prog="ay",
        description="Claude Code-style REPL for the yatra-harness")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config YAML (default palimpsest-config.yaml)")
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL, help="skill YAML (default palimpsest-skill.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    parser.add_argument("--seed", type=Path, default=None,
                        help="workspace seed directory (default fixtures/chat_seed)")
    parser.add_argument("--accept", action="append", default=[], metavar="CMD",
                        help="acceptance command, repeatable; enables real "
                             "verification and requires a non-empty diff")
    parser.add_argument("--protect", action="append", default=[], metavar="GLOB",
                        help="protected path glob, repeatable")
    parser.add_argument("--repo", type=Path, default=None, metavar="PATH",
                        help="work on a clone of this git repository instead of a seed; "
                             "required to open a pull request")
    parser.add_argument("--base-ref", default="", metavar="REF",
                        help="branch, tag or commit the work starts from (--repo only)")
    parser.add_argument("--deliver", choices=("none", "commit", "branch", "pr"),
                        default="none",
                        help="what to do with a run that passes verification")
    parser.add_argument("--base", default="", metavar="BRANCH",
                        help="pull request target branch")
    parser.add_argument("--deliver-yes", action="store_true",
                        help="push and open without prompting")
    parser.add_argument("--session", default="", metavar="ID",
                        help="resume a named session's workspace and memory")
    parser.add_argument("--stateless", action="store_true",
                        help="give every message a fresh workspace, as before")
    arguments = parser.parse_args(argv)
    if arguments.repo is not None and arguments.seed is not None:
        parser.error("--repo and --seed name two different workspaces; choose one")
    if arguments.seed is not None and not arguments.seed.is_dir():
        print(f"error: --seed is not a directory: {arguments.seed}")
        return 2
    if arguments.repo is not None:
        if not arguments.repo.is_dir():
            print(f"error: --repo is not a directory: {arguments.repo}")
            return 2
        if not (arguments.repo / ".git").exists():
            print(f"error: --repo is not a git repository: {arguments.repo}")
            return 2
    if arguments.deliver != "none" and arguments.repo is None:
        print("error: --deliver needs --repo; a seed workspace has no remote to push to")
        return 2
    app = ChatApp(arguments.config, arguments.skill, arguments.verbose,
                  seed=arguments.seed, accept=arguments.accept,
                  protect=arguments.protect, repository=arguments.repo,
                  base_ref=arguments.base_ref, deliver=arguments.deliver,
                  base=arguments.base, deliver_yes=arguments.deliver_yes,
                  session=arguments.session, stateless=arguments.stateless)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
