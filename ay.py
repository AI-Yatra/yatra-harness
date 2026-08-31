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
  /help                   this help
  /exit, /quit            leave

Requires:
  - yatra-harness synced with `uv sync` (openpyxl installed)
  - DASHSCOPE_API_KEY in .env or the environment (Qwen Cloud / DashScope)
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

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
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
        self.model = self._detect_model()

    # ---------------------------------------------------------------- setup

    def _load_env(self) -> None:
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    def _detect_model(self) -> str:
        """Read the primary route's model from the active config."""
        text = self.config_path.read_text(encoding="utf-8")
        match = re.search(r"^\s*model:\s*(\S+)\s*$", text, re.MULTILINE)
        return match.group(1) if match else "?"

    def _check_key(self) -> bool:
        self._load_env()
        key = os.environ.get("DASHSCOPE_API_KEY", "")
        if key:
            return True
        print("No DASHSCOPE_API_KEY found. Set it in .env or the environment.")
        print("  notepad .env   ->   DASHSCOPE_API_KEY=sk-ws-...")
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
        else:
            commands = [["python", "-c", "print('chat acceptance ok')"]]
            require_diff = False
        # Prefer a path relative to the task file so run bundles stay
        # relocatable; fall back to absolute (e.g. a seed on another drive).
        try:
            seed_value = os.path.relpath(self.seed, TASKS_DIR).replace(os.sep, "/")
        except ValueError:
            seed_value = self.seed.as_posix()
        content = f"""\
version: 1
id: {task_id}
objective: >-
  {message}
workspace_seed: {json.dumps(seed_value)}
constraints:
  - Work in the workspace; create files there if the task needs artifacts.
  - Keep responses concise and factual.
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

    def _run_harness(self, task_path: Path) -> int:
        """Run the harness as a subprocess and stream events live."""
        self._load_env()
        cmd = [
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
            sys.stdout.write(line)
            sys.stdout.flush()
        process.wait()

    def _set_model(self, model: str) -> None:
        text = self.config_path.read_text(encoding="utf-8")
        updated, count = re.subn(r"^(\s*model:\s*)\S+\s*$", r"\g<1>" + model, text, count=1, flags=re.MULTILINE)
        if count == 0:
            print(f"could not find a model line in {self.config_path}")
            return
        self.config_path.write_text(updated, encoding="utf-8")
        self.model = model
        print(f"model -> {model}")

    # ------------------------------------------------------------- main

    def _print_banner(self) -> None:
        try:
            from harness import __version__ as version
        except Exception:  # noqa: BLE001 - the banner must never block startup
            version = "?"
        if self.accept:
            contract = "verified (" + "; ".join(self.accept) + ")"
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
        print(_tint(f"# seed: {self.seed.name} · contract: {contract}", "2"))
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
    argv = sys.argv[1:]
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
    arguments = parser.parse_args()
    if arguments.seed is not None and not arguments.seed.is_dir():
        print(f"error: --seed is not a directory: {arguments.seed}")
        return 2
    app = ChatApp(arguments.config, arguments.skill, arguments.verbose,
                  seed=arguments.seed, accept=arguments.accept,
                  protect=arguments.protect)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
