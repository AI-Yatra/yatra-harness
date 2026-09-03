#!/usr/bin/env python3
"""ay: a coding agent in your terminal, in your working directory.

    $ cd ~/my-project
    $ ay
    > why does the auth test fail on windows?

One conversation, one working directory, many turns. The model reads,
searches, edits and runs things where you are standing; writes and commands
ask before they happen.

Sub-commands:
    ay                      start a session in the current directory
    ay "message"            run one message and stay
    ay -p "message"         run one message, print, and exit
    ay auth ...             manage provider credentials
    ay run ...              anything else is handed to the harness CLI

The batch harness is still there and unchanged: `ay run task.yaml` executes a
task contract against a copied workspace and verifies it. This REPL is the
other shape, for the work that is a conversation rather than a contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "configs" / "ay.yaml"

#: Handed straight to `harness.cli`, so `ay` and `harness` cannot disagree
#: about what these mean. Anything not listed starts a conversation.
DELEGATED = {
    "auth", "run", "resume", "doctor", "inspect", "replay", "explain",
    "tools", "routes", "list-runs", "deliver", "review", "eval", "goal", "loop",
}


def main() -> int:
    # Model output is arbitrary Unicode. A cp1252 console would otherwise raise
    # UnicodeEncodeError mid-stream and take the session with it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return main_with_argv(sys.argv[1:])


def main_with_argv(argv: list[str]) -> int:
    """`main` without the console setup, so the argument rules are testable."""
    if argv and argv[0] in DELEGATED:
        from harness.cli import main as cli_main  # noqa: PLC0415

        return cli_main(argv)

    parser = build_parser()
    arguments = parser.parse_args(argv)

    root = (arguments.cwd or Path.cwd()).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    config_path = arguments.config
    if not config_path.is_file():
        print(f"error: no such config: {config_path}", file=sys.stderr)
        return 2

    from harness.core.errors import HarnessError  # noqa: PLC0415
    from harness.repl.approvals import Mode  # noqa: PLC0415
    from harness.repl.shell import Options, Shell  # noqa: PLC0415

    message = " ".join(arguments.message).strip()
    if arguments.print_only and not message:
        parser.error("-p needs a message to run")

    options = Options(
        config_path=config_path,
        root=root,
        mode=Mode(arguments.mode),
        session_id=arguments.session,
        resume=bool(arguments.resume or arguments.session),
        model_override=arguments.model,
        prompt_profile=arguments.prompt_profile,
        initial_message=message,
        print_once=bool(arguments.print_only),
        sessions_dir=root / ".ay",
    )
    try:
        return Shell(options).run()
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ay",
        description="A coding agent in your terminal.",
        epilog="ay auth, ay run and the other harness verbs are passed through to the harness CLI.",
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="an opening message; without one, ay starts an empty session",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="print_only",
        action="store_true",
        help="run the message, print the answer, and exit without a prompt",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        type=Path,
        default=None,
        metavar="DIR",
        help="work in this directory instead of the current one",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        metavar="FILE",
        help=f"harness config YAML (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--prompt-profile",
        default="",
        metavar="NAME",
        help=(
            "prompting dials for every route this session: bare, lean, "
            "standard, deep or xml (default: each route decides)"
        ),
    )
    parser.add_argument(
        "--model",
        default="",
        metavar="NAME",
        help="route name or model id to use instead of the config's primary",
    )
    parser.add_argument(
        "--mode",
        default="suggest",
        choices=("plan", "suggest", "auto-edit", "full-auto"),
        help="how much to ask before acting (default: suggest)",
    )
    parser.add_argument(
        "--session",
        default="",
        metavar="ID",
        help="name this session, and reopen it if it already exists",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reopen the most recent session in this directory",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
