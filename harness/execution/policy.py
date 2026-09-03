"""Capability authorization separate from technical tool availability."""

from __future__ import annotations

import fnmatch
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harness.core.contracts import RiskLevel, ToolSpec

# Guarded because `config` is the composition root: it imports every
# module it configures, so importing it back at runtime would close a
# cycle. These names appear only in annotations, which
# `from __future__ import annotations` leaves as strings.
if TYPE_CHECKING:
    from harness.config import PolicyConfig


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


ApprovalCallback = Callable[[ToolSpec, dict[str, Any], str], bool]


#: Shells that take a command string after a flag. `bash -lc` and friends
#: combine letters, so these match on any flag containing a `c` rather than on
#: an exact spelling.
SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "ash"})

#: Interpreters that take code after an exact flag.
CODE_INTERPRETERS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "perl": frozenset({"-e", "-E"}),
    "ruby": frozenset({"-e"}),
    "php": frozenset({"-r"}),
    "cmd": frozenset({"/c", "/k"}),
    "powershell": frozenset({"-c", "-command", "-encodedcommand", "-ec"}),
    "pwsh": frozenset({"-c", "-command", "-encodedcommand", "-ec"}),
}

#: Commands whose arguments are simply another command. Stripping them exposes
#: the real command underneath.
COMMAND_PREFIXES = frozenset(
    {
        "sudo",
        "doas",
        "nohup",
        "nice",
        "ionice",
        "stdbuf",
        "time",
        "timeout",
        "env",
        "setsid",
        "eatmydata",
        "xargs",
        "watch",
        "script",
    }
)

#: How deep to keep unwrapping. `bash -c "bash -c '...'"` is worth following;
#: past a few levels the input is pathological rather than plausible.
MAX_UNWRAP_DEPTH = 4

#: An allowlist entry matching every command, since every command starts with
#: the empty prefix.
#:
#: The two loops need different answers here and both are right. An unattended
#: run has to say in advance what may execute, because nobody is there to
#: judge it, so it lists prefixes. A conversation has the operator present and
#: asks them per command, so a prefix list would only mean enumerating
#: beforehand everything they might later agree to, and the practical result
#: of that is the list gets set to something permissive and stops meaning
#: anything.
#:
#: This is the allowlist stepping aside, not the policy doing so. The
#: deny-list, the network rule and the approval gate all still apply.
ANY_COMMAND: tuple[str, ...] = ()

#: Shell operators that separate one command from the next.
_OPERATORS = frozenset({";", "&&", "||", "|", "&"})
_OPERATOR_SPLIT = re.compile(r"[;&|]+")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def normalize_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """The spelling both the policy and the executor judge a command by.

    The head is reduced to a bare program name, so a rule written for `rm` also
    binds `/bin/rm`, `C:\\Windows\\System32\\rm.exe` and `RM.EXE`. Version
    suffixes fold too: `python3` and `python` run the same interpreter, so a
    rule written for one has to bind the other.

    Surrounding whitespace and trailing dots come off first. Windows resolves
    `"rm "` and `"rm."` to the same program it resolves `rm` to, so a model
    that puts one space after the program name would otherwise walk straight
    through a deny rule that says it cannot run with or without approval.
    Nothing legitimate spells a program that way.
    """
    if not command:
        return command
    head = command[0].strip().replace("\\", "/").rsplit("/", 1)[-1].lower().rstrip(". ")
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
            break
    if head in {"python2", "python3"} or head.startswith("python3."):
        head = "python"
    elif head == "nodejs":
        head = "node"
    return (head, *command[1:])


def _inner_code(command: tuple[str, ...]) -> str:
    """The code a wrapper is carrying, or "" when it carries none."""
    if len(command) < 2:
        return ""
    head = command[0]
    if head in SHELL_INTERPRETERS:
        for index, argument in enumerate(command[1:], start=1):
            # -c, -lc, -ic and -xc all mean the next argument is a command.
            if argument.startswith("-") and "c" in argument[1:].lower():
                return command[index + 1] if index + 1 < len(command) else ""
        return ""
    flags = CODE_INTERPRETERS.get(head)
    if flags:
        for index, argument in enumerate(command[1:], start=1):
            if argument.lower() in flags:
                return command[index + 1] if index + 1 < len(command) else ""
    return ""


def _split_shell(code: str) -> list[tuple[str, ...]]:
    """The separate commands a shell string runs.

    Operators separate commands, so each side is judged on its own: `ls && rm
    -rf /` is a call to rm however harmless its first half looks. Unparseable
    input degrades to a whitespace split rather than being skipped, because
    skipping is precisely how a check gets bypassed.
    """
    commands: list[tuple[str, ...]] = []

    def tokenize(text: str) -> list[str]:
        try:
            return shlex.split(text, posix=True)
        except ValueError:
            return text.replace('"', " ").replace("'", " ").split()

    current: list[str] = []
    for token in tokenize(code):
        if token in _OPERATORS:
            if current:
                commands.append(tuple(current))
                current = []
            continue
        current.append(token)
    if current:
        commands.append(tuple(current))

    # Operators are often glued to a word (`ls&&rm -rf /`), which shlex keeps
    # as a single token, so split textually as a second opinion.
    pieces = _OPERATOR_SPLIT.split(code)
    if len(pieces) > 1:
        for piece in pieces:
            parts = tokenize(piece)
            if parts:
                commands.append(tuple(parts))
    return commands


def expand_command(command: tuple[str, ...], _depth: int = 0) -> list[tuple[str, ...]]:
    """Every command that running *command* would actually execute.

    `bash -c "rm -rf /"` is not a call to bash as far as danger goes, it is a
    call to rm, and a check that reads only argv sees the wrong one. The
    original is returned alongside anything it wraps, so a deny-list written in
    the obvious way still binds the wrapped form.
    """
    normalized = normalize_command(tuple(command))
    if not normalized:
        return []
    found = [normalized]
    if _depth >= MAX_UNWRAP_DEPTH:
        return found

    # `sudo rm -rf /` and `env FOO=1 rm -rf /` are calls to rm.
    if normalized[0] in COMMAND_PREFIXES:
        rest = normalized[1:]
        while rest and (rest[0].startswith("-") or _ASSIGNMENT.match(rest[0])):
            rest = rest[1:]
        if rest:
            found.extend(expand_command(rest, _depth + 1))

    code = _inner_code(normalized)
    if code.strip():
        for piece in _split_shell(code):
            found.extend(expand_command(piece, _depth + 1))
    return found


def carried_code(command: tuple[str, ...], _depth: int = 0) -> list[str]:
    """The raw code strings a command hands to an interpreter.

    Kept separately from `expand_command` because argv matching cannot reach
    inside a language expression. `python -c 'os.system("rm -rf /")'` tokenises
    into `os.system(rm`, which is not a call to rm by any structural reading,
    so the text itself is the only thing left to look at.
    """
    normalized = normalize_command(tuple(command))
    if not normalized or _depth >= MAX_UNWRAP_DEPTH:
        return []
    found: list[str] = []
    code = _inner_code(normalized)
    if code.strip():
        found.append(code)
        for piece in _split_shell(code):
            found.extend(carried_code(piece, _depth + 1))
    if normalized[0] in COMMAND_PREFIXES:
        rest = normalized[1:]
        while rest and (rest[0].startswith("-") or _ASSIGNMENT.match(rest[0])):
            rest = rest[1:]
        if rest:
            found.extend(carried_code(rest, _depth + 1))
    return found


def _mentions(pattern: tuple[str, ...], code: str) -> bool:
    """Whether *code* contains the pattern as literal text.

    Crude on purpose. It catches `os.system("rm -rf /")`, which structural
    matching cannot, and it misses `shutil.rmtree("/")`, which no pattern list
    can catch. That second case is why the sandbox rather than the deny-list is
    the real boundary for untrusted code.
    """
    needle = " ".join(pattern)
    return needle in " ".join(code.split())


def _matches(pattern: tuple[str, ...], command: tuple[str, ...]) -> bool:
    """Whether *command* is an instance of *pattern*.

    The pattern may start at any position, because the dangerous form is
    reachable as an argument to something allowed: `python -m pip install` has
    to match a rule written as `pip install`. From wherever it starts, the
    remaining words must appear in order but need not be adjacent, because
    `git -C . push --force` is the same dangerous operation as
    `git push --force` and an adjacency test is defeated by one inserted flag.

    The price of ordered matching is the occasional false refusal, such as a
    file literally named `push`. That is the right way round: a refusal is
    recoverable by rephrasing, and a false allow is not.
    """
    if not pattern or not command:
        return False
    for start, token in enumerate(command):
        if token != pattern[0]:
            continue
        remaining = list(pattern[1:])
        for later in command[start + 1 :]:
            if remaining and later == remaining[0]:
                remaining.pop(0)
        if not remaining:
            return True
    return False


def denied_pattern(
    command: tuple[str, ...], patterns: tuple[tuple[str, ...], ...]
) -> str | None:
    """The deny pattern *command* matches, or None.

    Every spelling the command would actually execute is checked, not only the
    argv handed in, because the dangerous forms are reachable through a shell
    or an interpreter: `python` has to be runnable for the tests, and
    `python -c "..."` is arbitrary code.

    Module-level so the conversational REPL and `harness run` cannot drift on
    what counts as dangerous.
    """
    candidates = expand_command(tuple(command))
    code_blocks = carried_code(tuple(command))
    for pattern in patterns:
        if not pattern:
            continue
        normalized_pattern = normalize_command(tuple(pattern))
        for candidate in candidates:
            if _matches(normalized_pattern, candidate):
                return " ".join(pattern)
        for code in code_blocks:
            if _mentions(tuple(pattern), code):
                return " ".join(pattern)
    return None


#: What a rule does when it matches, strongest first. Order is the precedence:
#: a deny anywhere in the list beats an ask, which beats an allow, so adding a
#: permissive rule can never quietly widen something already restricted.
EFFECTS = ("deny", "ask", "allow")

_RULE = re.compile(r"^\s*(?P<tool>[A-Za-z_][A-Za-z0-9_]*|\*)\s*(?:\(\s*(?P<pattern>.*?)\s*\))?\s*$")


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One operator rule: which tool, which arguments, and what to do.

    Written as `run_command(git push *)` or `write_file(*.env)` or bare
    `web_search`, which is the shape people already know from other harnesses
    and reads the same as the call it governs.
    """

    effect: str
    tool: str
    pattern: str = ""
    source: str = ""

    @property
    def text(self) -> str:
        return f"{self.tool}({self.pattern})" if self.pattern else self.tool


def parse_rule(text: str, effect: str) -> PolicyRule:
    """Parse one rule, or say why it is not one."""
    if effect not in EFFECTS:
        raise ValueError(f"rule effect must be one of {', '.join(EFFECTS)}, got {effect!r}")
    match = _RULE.match(text)
    if not match:
        raise ValueError(
            f"{text!r} is not a rule. Write it as tool(pattern), for example "
            "run_command(git push *), or as a bare tool name."
        )
    return PolicyRule(effect, match.group("tool"), match.group("pattern") or "", text)


def _glob_tokens(
    pattern: tuple[str, ...], tokens: tuple[str, ...], *, open_ended: bool = False
) -> bool:
    """Match a token pattern where `*` stands for any run of tokens.

    Tokens rather than characters, because a command is a list and matching it
    as a string would let `git pushed` satisfy a rule written for `git push`.

    `open_ended` lets the subject carry extra trailing tokens the pattern did
    not mention. Without it a rule had to name every argument: `run_command(rm*)`
    matched a bare `rm` and not `rm -rf data`, so the most obvious deny rule
    anyone would write silently caught nothing. It is passed only for `deny`
    and `ask`, never for `allow` -- widening a refusal is safe, and widening a
    permission grants something the operator did not write.
    """
    if not pattern:
        return open_ended or not tokens
    head, rest = pattern[0], pattern[1:]
    if head == "*":
        if not rest:
            return True
        return any(
            _glob_tokens(rest, tokens[index:], open_ended=open_ended)
            for index in range(len(tokens) + 1)
        )
    if not tokens or (tokens[0] != head and not fnmatch.fnmatch(tokens[0], head)):
        return False
    return _glob_tokens(rest, tokens[1:], open_ended=open_ended)


def _subjects(tool: str, arguments: dict[str, Any]) -> list[tuple[str, ...]]:
    """What a rule's pattern is matched against for this call.

    A command is matched against every spelling it would actually run, so a
    rule written for `git push` also covers `bash -c "git push"`, exactly as
    the deny-list does. Everything else is matched on its path, which is the
    argument an operator means when they write `write_file(*.env)`.
    """
    if tool == "run_command":
        command = arguments.get("command")
        if isinstance(command, str):
            command = command.split()
        if not isinstance(command, list):
            return []
        return expand_command(tuple(str(part) for part in command))
    path = arguments.get("path") or arguments.get("url") or arguments.get("query")
    return [(str(path),)] if path else [()]


def rule_for(
    tool: str, arguments: dict[str, Any], rules: Sequence[PolicyRule]
) -> PolicyRule | None:
    """The rule governing this call, or None when the operator wrote none.

    Deny is searched before ask and ask before allow, so precedence does not
    depend on the order rules happen to appear in the file. Within one effect
    the first match wins, which makes a list readable top to bottom.
    """
    subjects = _subjects(tool, arguments)
    for effect in EFFECTS:
        for rule in rules:
            if rule.effect != effect:
                continue
            if rule.tool not in ("*", tool):
                continue
            if not rule.pattern:
                return rule
            # A command pattern and a path pattern are matched differently, and
            # conflating them was a hole rather than a nicety. `normalize_command`
            # strips the directory off an executable so a rule for `rm` also
            # covers `/bin/rm`. Applied to a path pattern it strips the
            # directory there too, so `edit_file(data/**)` normalized to `**`
            # and denied every edit in the repository -- the operator writes
            # the most natural rule there is and locks the agent out entirely.
            if tool == "run_command":
                wanted = tuple(shlex.split(rule.pattern)) if rule.pattern.strip() else ()
                # A pattern ending in a glob means "and whatever follows",
                # which is what an operator writing `rm*` intends.
                open_ended = bool(wanted) and wanted[-1].endswith("*") and effect != "allow"
                if any(
                    _glob_tokens(normalize_command(wanted), subject, open_ended=open_ended)
                    for subject in subjects
                ):
                    return rule
                continue
            if any(
                len(subject) == 1 and fnmatch.fnmatch(subject[0], rule.pattern)
                for subject in subjects
            ):
                return rule
    return None


class PolicyEngine:
    def __init__(
        self,
        config: PolicyConfig,
        allowed_tools: tuple[str, ...],
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.config = config
        self.allowed_tools = frozenset(allowed_tools)
        self.approval_callback = approval_callback

    def evaluate(self, tool: ToolSpec, arguments: dict[str, Any]) -> PolicyDecision:
        if tool.name not in self.allowed_tools:
            return PolicyDecision(False, False, f"tool {tool.name!r} is not enabled by the skill")
        if tool.risk is RiskLevel.NETWORK and not self.config.network_enabled:
            return PolicyDecision(False, False, "network tools are disabled by policy")
        if tool.name == "run_command":
            command = arguments.get("command")
            if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
                return PolicyDecision(False, False, "run_command requires a string array")
            # The deny-list is checked first and cannot be overridden, so a
            # refusal here never reaches an approver: a human clicking yes on
            # a prompt is exactly the mistake it exists to prevent.
            denied = self._command_denied(tuple(command))
            if denied is not None:
                return PolicyDecision(
                    False, False, f"command matches the deny-list pattern {denied!r}"
                )
            if not self._command_allowed(tuple(command)):
                return PolicyDecision(False, False, "command is not on the configured allowlist")
        requires_approval = self._requires_approval(tool.risk)
        if not requires_approval:
            return PolicyDecision(True, False, "allowed by policy")
        if self.approval_callback is None:
            # Deliberately different wording from a refusal. "Denied" reads as
            # a decision that might go the other way, and a model that
            # believes that asks again every turn -- one live run spent its
            # whole budget on twelve denied patches. Nobody is there to ask,
            # so say that, and say that asking again will not help.
            return PolicyDecision(
                False,
                True,
                f"{tool.name} needs approval and no approver is available in this run, so it "
                "cannot be used; asking again will not succeed. Use a different registered "
                "tool, or finish and report what is blocked.",
            )
        if self.approval_callback(tool, arguments, f"authorize {tool.risk.value} capability"):
            return PolicyDecision(True, True, "approved by operator")
        return PolicyDecision(False, True, "operator denied approval")

    def _command_denied(self, command: tuple[str, ...]) -> str | None:
        return denied_pattern(command, self.config.denied_commands)

    def _command_allowed(self, command: tuple[str, ...]) -> bool:
        # The allowlist answers "may a command of this shape run at all",
        # which it can only do by prefix; the deny-list above is what catches
        # the dangerous forms reachable as arguments to an allowed command.
        # Every wrapped spelling has to clear it too, or `bash -c "curl ..."`
        # would pass an allowlist that never mentions curl.
        for candidate in expand_command(command):
            if not any(
                candidate[: len(prefix)] == normalize_command(prefix)
                for prefix in self.config.allowed_commands
            ):
                return False
        return True

    def _requires_approval(self, risk: RiskLevel) -> bool:
        if self.config.approval_mode == "never":
            return False
        if self.config.approval_mode == "always":
            return True
        return risk in {RiskLevel.WRITE, RiskLevel.EXECUTE, RiskLevel.NETWORK}
