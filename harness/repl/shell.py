"""The interactive shell around the agent loop.

Owns everything the loop deliberately does not: reading a line, expanding
`@path` and `!command`, slash commands, the banner, and turning Ctrl-C into
an interrupted turn rather than a dead process.
"""

from __future__ import annotations

import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from harness.config import HarnessConfig, RouteConfig, load_config
from harness.core.contracts import ToolSpec
from harness.core.errors import HarnessError
from harness.core.util import is_chat_model, model_version
from harness.execution.process import run_process
from harness.execution.workspace import Workspace
from harness.models import auth
from harness.repl.tools import ReplToolset

from . import prompt as prompt_builder
from .agent import Agent, Events, Interrupted, ModelUnavailable, describe_arguments
from .approvals import Gate, Mode, Request, Verdict
from .conversation import Conversation, ToolCall
from .model import ChatModel, RouteChain
from .render import Console, Renderer, Spinner

HISTORY_LIMIT = 500

HELP = """\
Ask anything, or give an instruction. The agent works in this directory.

  /help              this
  /model [name]      show or switch the model
  /models [filter]   what the current provider actually serves
  /mode [name]       approval mode: suggest, auto-edit, full-auto
  /approvals         what you have allowed for the rest of this session
  /tools             the tools the model can call
  /context           how full the context window is
  /cost              tokens used this session
  /compact           summarise the conversation to free context
  /clear             forget the conversation and start fresh
  /init              write an AGENTS.md describing this repository
  /config            the active config file and route
  /exit              leave

  @path/to/file      include a file's contents with your message
  !command           run a shell command yourself, without the model
  \\ at end of line   continue on the next line
"""


class UnknownRoute(HarnessError):
    """`--model route:model` named a route the config does not define."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        super().__init__(f"no route named {name!r}. configured routes: {', '.join(known)}")


@dataclass
class Options:
    """Everything the shell was started with."""

    config_path: Path
    root: Path
    mode: Mode = Mode.SUGGEST
    session_id: str = ""
    resume: bool = False
    model_override: str = ""
    initial_message: str = ""
    print_once: bool = False
    sessions_dir: Path = field(default_factory=lambda: Path(".ay"))


class Shell:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.console = Console()
        self.render = Renderer(self.console)
        self.config: HarnessConfig = load_config(options.config_path)
        self.root = options.root.resolve()
        self.mode = options.mode
        self.session_id = options.session_id or self._latest_session() or f"ay-{uuid.uuid4().hex[:8]}"
        self.workspace = Workspace(self.root, ())
        self.toolset = ReplToolset(self.workspace, self.config)
        self.gate = Gate(self.config.policy, mode=self.mode, prompt=self._ask)
        self.guessed_route = ""
        self.route = self._route(options.model_override)
        self.model = self._chat_model()
        self.conversation = self._open_conversation()
        self.agent = Agent(
            model=self.model,
            conversation=self.conversation,
            toolset=self.toolset,
            gate=self.gate,
            config=self.config,
            events=self._events(),
        )
        self.total_in = 0
        self.total_out = 0
        self._spinner: Spinner | None = None
        self._streamed = False

    # ------------------------------------------------------------------ setup

    def _route(self, override: str) -> RouteConfig:
        """Resolve `--model` / `/model` to a route, and say when it guessed.

        An override is one of three things: a route name, a model id some
        route already declares, or a bare model id we have to attach to some
        endpoint. Only the third is ambiguous, and getting it wrong is
        invisible until the request fails -- a bare `gemini-flash-lite-latest`
        used to land on the primary route's endpoint and come back as a 404
        from the wrong provider. `route:model` says it explicitly.
        """
        from dataclasses import replace  # noqa: PLC0415

        router = self.config.router
        # Base the fallback on whatever route is live, so `/model <id>` on the
        # gemini route swaps the model and stays on gemini's endpoint and key.
        current = getattr(self, "route", None) or router.routes[router.primary]
        if not override:
            return current

        if ":" in override:
            name, _, model = override.partition(":")
            if name in router.routes:
                return replace(router.routes[name], model=model)
            raise UnknownRoute(name, tuple(sorted(router.routes)))

        if override in router.routes:
            return router.routes[override]
        by_model = {r.model: r for r in router.routes.values()}
        if override in by_model:
            return by_model[override]
        self.guessed_route = current.name
        return replace(current, model=override)

    def _chat_model(self) -> RouteChain:
        """The chosen route, then every other one that has a credential.

        Ordering the rest by the config's declared fallbacks first keeps the
        operator's preference; anything else usable is appended so a free tier
        running dry mid-conversation costs a notice rather than the session.
        """
        router = self.config.router
        ordered = [self.route]
        preferred = [n for n in router.fallbacks if n in router.routes]
        rest = [n for n in sorted(router.routes) if n not in preferred]
        candidates = [
            router.routes[n]
            for n in preferred + rest
            if n != self.route.name and self._has_credential(router.routes[n])
        ]
        # A local server that is not running looks identical to one that is
        # until a request is made, so it is tried only after the remotes.
        ordered += [r for r in candidates if not r.local]
        ordered += [r for r in candidates if r.local]
        return RouteChain(
            [
                ChatModel(
                    route,
                    retries=router.retries_per_route,
                    backoff_seconds=router.backoff_seconds,
                )
                for route in ordered
            ],
            on_switch=self._announce_switch,
        )

    def _announce_switch(self, dead: str, live: str, reason: str) -> None:
        self._stop_spinner()
        self.render.notice(f"{dead} is unavailable ({reason.removeprefix('provider ')})")
        self.render.notice(f"switched to {live}; continuing")
        self.route = self.config.router.routes.get(live, self.route)

    def _session_path(self) -> Path:
        return self.options.sessions_dir / f"{self.session_id}.json"

    def _latest_session(self) -> str:
        """The most recently saved session here, for `--resume` with no name."""
        if not self.options.resume:
            return ""
        try:
            saved = sorted(
                self.options.sessions_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return ""
        return saved[0].stem if saved else ""

    def _window(self) -> int:
        return getattr(self.route, "context_window", 0) or 128_000

    def _open_conversation(self) -> Conversation:
        system = prompt_builder.build(self.config, self.root, mode=self.mode)
        window = self._window()
        if self.options.resume and self._session_path().exists():
            return Conversation.load(self._session_path(), system=system, max_tokens=window)
        return Conversation(system, max_tokens=window)

    def _events(self) -> Events:
        return Events(
            on_text=self.render.assistant_text,
            on_delta=self._on_delta if self._can_stream() else None,
            on_tool_start=self._on_tool_start,
            on_tool_end=self._on_tool_end,
            on_tool_denied=self._on_tool_denied,
            on_notice=self.render.notice,
            on_thinking=self._on_thinking,
        )

    def _can_stream(self) -> bool:
        return bool(getattr(self.route, "stream", False)) and self.console.stream.isatty()

    # ------------------------------------------------------------- loop events

    def _on_thinking(self, busy: bool) -> None:
        if busy:
            self._streamed = False
            self._spinner = Spinner(self.console).__enter__()
        elif self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
            if self._streamed:
                self.console.line()
                self.console.line()

    def _on_delta(self, text: str) -> None:
        """Model prose as it arrives.

        The spinner is stopped on the first token rather than at the end of
        the request, because a spinner still turning under text that is
        already streaming reads as two things happening at once.
        """
        if not text:
            return
        if not self._streamed:
            if self._spinner is not None:
                self._spinner.stop()
                self._spinner = None
            self.console.line()
            self._streamed = True
        self.console.write(text)

    def _on_tool_start(self, call: ToolCall, spec: ToolSpec) -> None:
        del spec
        self.render.tool_start(call.name, describe_arguments(call))

    def _on_tool_end(self, call: ToolCall, detail: str, ok: bool) -> None:
        del call
        self.render.tool_result(detail, ok=ok)

    def _on_tool_denied(self, call: ToolCall, reason: str) -> None:
        del call
        self.render.tool_denied(reason)

    def _ask(self, request: Request) -> Verdict:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        return self.render.ask(request)

    # ------------------------------------------------------------------ input

    def _setup_readline(self) -> None:
        """Line editing and history, when the platform has it.

        Absent on a bare Windows console without pyreadline; the REPL still
        works there, it just loses arrow-key recall.
        """
        try:
            import readline  # noqa: PLC0415
        except ImportError:
            return
        history = self.options.sessions_dir / "history"
        try:
            history.parent.mkdir(parents=True, exist_ok=True)
            if history.exists():
                readline.read_history_file(str(history))
        except (OSError, ValueError):
            return
        readline.set_history_length(HISTORY_LIMIT)
        self._history_file = history

    def _save_history(self) -> None:
        path = getattr(self, "_history_file", None)
        if path is None:
            return
        try:
            import readline  # noqa: PLC0415

            readline.write_history_file(str(path))
        except (ImportError, OSError, ValueError):
            pass

    def _read(self) -> str | None:
        """One logical input, which may span lines. None means end of input."""
        parts: list[str] = []
        while True:
            marker = "  " if parts else self.console.accent("> ")
            try:
                self.console.write("\n" + marker if not parts else marker)
                line = input()
            except EOFError:
                return None
            except KeyboardInterrupt:
                self.console.line()
                if not parts:
                    self.console.line(self.console.dim("  (ctrl-c again or /exit to leave)"))
                    return ""
                return ""
            if line.endswith("\\"):
                parts.append(line[:-1])
                continue
            parts.append(line)
            return "\n".join(parts).strip()

    def _expand(self, text: str) -> str:
        """Inline the contents of any `@path` the operator referenced."""
        found = re.findall(r"(?<![\w@])@([\w./\\-]+)", text)
        if not found:
            return text
        blocks: list[str] = []
        for reference in dict.fromkeys(found):
            try:
                outcome = self.toolset.read_file({"path": reference})
            except HarnessError as exc:
                self.render.notice(f"@{reference}: {exc}")
                continue
            if not outcome.ok:
                self.render.notice(f"@{reference}: {outcome.content}")
                continue
            self.render.notice(f"included @{reference} ({outcome.detail})")
            blocks.append(f"--- {reference} ---\n{outcome.content}")
        if not blocks:
            return text
        return text + "\n\n" + "\n\n".join(blocks)

    # --------------------------------------------------------------- commands

    def _command(self, line: str) -> bool:
        """Run a slash command. Returns False when the session should end."""
        parts = shlex.split(line) if " " in line else [line]
        name = parts[0].lower().lstrip("/")
        argument = " ".join(parts[1:]).strip()

        if name in {"exit", "quit", "q"}:
            return False
        if name in {"help", "?"}:
            self.console.line()
            for row in HELP.splitlines():
                self.console.line("  " + (self.console.dim(row) if row.startswith("  ") else row))
            return True
        if name == "model":
            self._switch_model(argument)
            return True
        if name == "models":
            self._list_provider_models(argument)
            return True
        if name == "mode":
            self._switch_mode(argument)
            return True
        if name == "approvals":
            standing = self.gate.standing_approvals
            if not standing:
                self.render.notice("Nothing is blanket-approved; every side effect still asks.")
            else:
                for key in standing:
                    self.render.notice(f"always allowed: {key}")
            return True
        if name == "tools":
            for spec in self.toolset.specs():
                self.console.line(
                    f"  {self.console.bold(spec.name.ljust(14))}"
                    f"{self.console.dim(spec.risk.value.ljust(9))}"
                    f"{spec.description.splitlines()[0][: self.console.width - 30]}"
                )
            return True
        if name == "context":
            self._show_context()
            return True
        if name == "cost":
            self.render.notice(
                f"{self.total_in:,} tokens in, {self.total_out:,} out, "
                f"{self.conversation.compactions} compaction(s) this session."
            )
            return True
        if name == "compact":
            self._compact()
            return True
        if name == "clear":
            self.conversation.messages.clear()
            self.conversation.compactions = 0
            self.render.notice("Conversation cleared. The working directory is untouched.")
            return True
        if name == "init":
            self._run_turn(
                "Write an AGENTS.md at the root of this repository describing what it is, "
                "how to build, test and lint it, and the conventions a contributor must "
                "follow. Read enough of the repository first to be accurate. If one "
                "already exists, improve it rather than replacing it wholesale."
            )
            return True
        if name == "config":
            self.render.notice(f"config: {self.options.config_path}")
            self.render.notice(f"route:  {self.route.name} ({self.route.kind})")
            self.render.notice(f"model:  {self.route.model}")
            self.render.notice(f"cwd:    {self.root}")
            self.render.notice(f"session: {self.session_id}")
            return True

        self.render.error(f"unknown command: /{name}   (try /help)")
        return True

    def _switch_model(self, argument: str) -> None:
        if not argument:
            self._list_routes()
            return
        self.guessed_route = ""
        try:
            self.route = self._route(argument)
        except UnknownRoute as exc:
            self.render.error(str(exc))
            self._list_routes()
            return
        if self.guessed_route:
            self.render.notice(
                f"{argument!r} is not a configured route or model; using it as a model id "
                f"on route {self.guessed_route!r} ({self.route.base_url})"
            )
        self.model = self._chat_model()
        self.agent.model = self.model
        self.agent.events = self._events()
        self.conversation.max_tokens = self._window()
        self.render.notice(f"model -> {self.route.model} (route {self.route.name})")
        if self.route.api_key_env and not self._has_credential(self.route):
            self.render.error(
                f"{self.route.name} has no credential for {self.route.api_key_env}. "
                f"Run: ay auth add --provider <name> <key>"
            )

    def _has_credential(self, route: RouteConfig) -> bool:
        if not route.api_key_env:
            return True
        return auth.resolve_route(route.api_key_env, route.base_url).available

    def _list_provider_models(self, needle: str = "") -> None:
        """Ask the current provider what it serves, so a stale id is fixable.

        Model ids churn constantly and a config pinned to a retired one fails
        as a 404 that reads like a broken endpoint. Asking is cheap and the
        answer is authoritative.
        """
        provider = auth.provider_for_base_url(self.route.base_url)
        if provider is None:
            self.render.error(
                f"route {self.route.name!r} points at {self.route.base_url}, which is not a "
                "known provider, so its model list cannot be fetched"
            )
            return
        credential = auth.resolve_route(self.route.api_key_env, self.route.base_url)
        with Spinner(self.console, f"asking {provider.name}"):
            try:
                names = auth.list_models(provider, credential.key, timeout=20)
            except HarnessError as exc:
                self.render.error(str(exc))
                return
        names = [n.removeprefix("models/") for n in names]
        if needle:
            names = [n for n in names if needle.lower() in n.lower()]
        if not names:
            self.render.notice(f"{provider.name} listed no models matching {needle!r}")
            return
        self.render.notice(f"{provider.name}: {len(names)} models")
        self.console.line()
        for chunk in range(0, min(len(names), 60), 2):
            row = names[chunk : chunk + 2]
            self.console.line("  " + "".join(n.ljust(44) for n in row).rstrip())
        if len(names) > 60:
            self.console.line(self.console.dim(f"  ... {len(names) - 60} more; /models <filter>"))
        self.console.line()
        self.render.notice(f"use one with /model {self.route.name}:<id>")

    def _list_routes(self) -> None:
        """Every route, which model it uses, and whether it can be reached.

        Listing bare model ids was not enough: `/model` is switched by route
        name, and a route with no key looks identical to one that works until
        you send a message to it.
        """
        console = self.console
        self.render.notice(f"in use: {self.route.name} ({self.route.model})")
        console.line()
        for name, route in sorted(self.config.router.routes.items()):
            ready = self._has_credential(route)
            mark = console.accent("*") if name == self.route.name else " "
            state = console.dim("ready") if ready else console.bad("no key")
            console.line(
                f"  {mark} {console.bold(name.ljust(14))}"
                f"{console.dim(route.model.ljust(24))}{state}"
            )
        console.line()
        self.render.notice("switch with /model <name>, for example /model gemini")

    def _switch_mode(self, argument: str) -> None:
        if not argument:
            self.render.notice(f"mode: {self.mode.value} - {self.mode.label}")
            return
        try:
            self.mode = Mode(argument.strip().lower())
        except ValueError:
            self.render.error(f"modes are: {', '.join(m.value for m in Mode)}")
            return
        self.gate.mode = self.mode
        self.render.notice(f"mode -> {self.mode.value} ({self.mode.label})")

    def _show_context(self) -> None:
        used = self.conversation.token_estimate()
        window = self.conversation.max_tokens
        share = min(1.0, used / window) if window else 0.0
        filled = int(share * 24)
        bar = "█" * filled + "░" * (24 - filled)
        self.render.notice(
            f"{bar}  ~{used:,} of {window:,} tokens ({share * 100:.0f}%), "
            f"{len(self.conversation.messages)} messages"
        )

    def _compact(self) -> None:
        with Spinner(self.console, "compacting"):
            try:
                freed = self.agent.compact()
            except (HarnessError, ModelUnavailable) as exc:
                self.render.error(f"could not compact: {exc}")
                return
        self.render.notice(f"Compacted, freeing roughly {freed:,} tokens.")

    def _shell_escape(self, line: str) -> None:
        """`!command` runs as the operator, not the model, and is not recorded."""
        command = line[1:].strip()
        if not command:
            return
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            self.render.error(f"could not parse the command: {exc}")
            return
        if not parts:
            return
        try:
            result = run_process(
                parts,
                cwd=self.root,
                timeout=self.config.policy.command_timeout_seconds,
                max_output_chars=self.config.budgets.max_output_chars,
                environment=dict(os.environ),
            )
        except (OSError, ValueError) as exc:
            self.render.error(str(exc))
            return
        self.console.line()
        for row in result.output.splitlines()[:200]:
            self.console.line("  " + row)
        if result.returncode:
            self.render.notice(f"exit {result.returncode}")

    # ------------------------------------------------------------------- turns

    def _run_turn(self, message: str) -> bool:
        """Run one message. False when the turn did not complete."""
        started = time.monotonic()
        try:
            stats = self.agent.send(message)
        except KeyboardInterrupt:
            self._abandon_turn()
            return False
        except Interrupted:
            self._abandon_turn()
            return False
        except ModelUnavailable as exc:
            self._stop_spinner()
            self.render.error(str(exc))
            _answer_dangling_calls(self.conversation, "The model became unavailable before this ran.")
            self._suggest_alternatives(str(exc))
            return False
        except HarnessError as exc:
            self._stop_spinner()
            self.render.error(str(exc))
            return False
        self.total_in += stats.input_tokens
        self.total_out += stats.output_tokens
        elapsed = time.monotonic() - started
        sep = self.console.glyphs.sep
        total = stats.input_tokens + stats.output_tokens
        if stats.tool_calls or elapsed > 8:
            self.render.notice(
                f"{stats.tool_calls} tool call{'s' if stats.tool_calls != 1 else ''}"
                f" {sep} {elapsed:.0f}s"
                + (f" {sep} {total:,} tokens" if stats.input_tokens else "")
            )
        self._persist()
        return True

    def _suggest_alternatives(self, reason: str = "") -> None:
        """Point at something that would actually work, rather than at nothing.

        A quota error is the common way a session dies, and "try /model" is
        useless without knowing what to switch to. Quotas are usually per
        model rather than per key, so a sibling model on the same route is
        the first thing to offer, ahead of a different provider entirely.
        """
        self.render.notice("The conversation is intact.")
        if "quota" in reason.lower() or "rate limit" in reason.lower():
            siblings = self._sibling_models()
            if siblings:
                self.render.notice(
                    "This quota is per model. Same provider, different model: "
                    + ", ".join(f"/model {name}" for name in siblings)
                )
        others = [
            name
            for name, route in sorted(self.config.router.routes.items())
            if name != self.route.name and self._has_credential(route)
        ]
        if others:
            self.render.notice(
                "Other routes with a credential: "
                + ", ".join(f"/model {name}" for name in others)
            )

    def _sibling_models(self) -> list[str]:
        """Other models the current route's provider offers, cheapest first.

        Asked of the provider rather than hardcoded, so this keeps working
        when a vendor retires a model. A failure here is not worth reporting:
        it is a suggestion, and the real error has already been shown.
        """
        try:
            provider = auth.provider_for_base_url(self.route.base_url)
            if provider is None:
                return []
            credential = auth.resolve_route(self.route.api_key_env, self.route.base_url)
            available = auth.list_models(provider, credential.key, timeout=10)
        except (HarnessError, OSError, ValueError):
            return []
        current = self.route.model
        family = current.split("-")[0]
        names = [name.removeprefix("models/") for name in available]
        candidates = [
            name
            for name in names
            if name != current and name.startswith(family) and is_chat_model(name)
        ]
        # Newest first, and the smaller variants within a version first: a
        # blown quota is usually on the largest model, and an older listed
        # model is often retired on this endpoint even though it is still
        # advertised.
        candidates.sort(key=lambda name: (-model_version(name), 0 if "lite" in name else 1, name))
        return candidates[:3]

    def _abandon_turn(self) -> None:
        """Leave the thread in a state the next turn can build on.

        A cut turn leaves an assistant message whose tool calls have no
        results. Providers reject that on the next request, so the loop is
        told what happened and the dangling calls are answered.
        """
        self._stop_spinner()
        self.console.line()
        self.render.notice("Interrupted.")
        _answer_dangling_calls(self.conversation, "Interrupted by the operator before this ran.")
        self.conversation.add_system_note(
            "The operator interrupted you. Stop what you were doing and wait for their "
            "next instruction."
        )
        self._persist()

    def _stop_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None

    def _persist(self) -> None:
        try:
            self.agent.save(self._session_path())
        except OSError:
            pass  # a session that cannot be saved is still a usable session

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        if not self._credential_ready():
            return 2
        self._setup_readline()
        self._banner()

        if self.options.initial_message:
            completed = self._run_turn(self._expand(self.options.initial_message))
            if self.options.print_once:
                # A script that pipes `ay -p` has nothing else to go on.
                return 0 if completed else 1

        interrupts = 0
        while True:
            line = self._read()
            if line is None:
                break
            if not line:
                interrupts += 1
                if interrupts >= 2:
                    break
                continue
            interrupts = 0
            if line.startswith("!"):
                self._shell_escape(line)
                continue
            if line.startswith("/"):
                if not self._command(line):
                    break
                continue
            self._run_turn(self._expand(line))

        self._save_history()
        self.console.line(self.console.dim("\n  bye"))
        return 0

    def _credential_ready(self) -> bool:
        auth.load_env_file() or auth.load_env_file(Path(__file__).resolve().parents[2])
        variable = self.route.api_key_env
        if not variable:
            return True
        if auth.resolve_route(variable, self.route.base_url).available:
            return True
        self.console.line()
        self.render.error(f"No credential for {variable}, which route {self.route.name!r} needs.")
        self.render.notice("store one:  ay auth add <key>")
        self.render.notice(f"or export:  {variable}=...   (environment or .env)")
        self.render.notice("check:      ay auth status")
        return False

    def _banner(self) -> None:
        console = self.console
        try:
            from .. import __version__ as version  # noqa: PLC0415
        except ImportError:
            version = "?"
        console.line()
        console.line("  " + console.accent(console.bold("ay")) + console.dim(f"  yatra-harness {version}"))
        sep = console.glyphs.sep
        console.line(
            "  " + console.dim(f"{self.route.model} {sep} {self.mode.value} {sep} {_short(self.root)}")
        )
        console.line("  " + console.dim("/help for commands, @file to include a file, !cmd to run one"))
        if self.guessed_route:
            console.line(
                "  "
                + console.bad(
                    f"{self.route.model!r} is not a configured route or model; "
                    f"sending it to route {self.guessed_route!r} "
                    f"({self.route.base_url}). Use --model <route> or "
                    f"--model <route>:<model> to be explicit."
                )
            )
        if self.mode is Mode.FULL_AUTO:
            console.line("  " + console.bad("full-auto: edits and commands run without asking"))


def _answer_dangling_calls(conversation: Conversation, reason: str) -> None:
    """Give every unanswered tool call a result, so the thread stays valid."""
    answered = {
        message.get("tool_call_id")
        for message in conversation.messages
        if message.get("role") == "tool"
    }
    pending: list[tuple[str, str]] = []
    for message in conversation.messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            identifier = str(call.get("id") or "")
            if identifier and identifier not in answered:
                pending.append((identifier, str((call.get("function") or {}).get("name") or "tool")))
    for identifier, name in pending:
        conversation.add_tool_result(identifier, name, reason)


def _short(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
