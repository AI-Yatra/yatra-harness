"""Credential storage and resolution for model providers.

Design rules, in priority order:

1. **Never leak.** ``redact`` is the only printable form of a key. This module
   is the only one that holds a raw secret, and it hands it to the provider
   adapter and nowhere else. Keys never reach an event, artifact or summary.
2. **Take any key.** ``harness auth add <key>`` infers the provider from the
   key's prefix. Prefixes match longest-first, so ``sk-ant-api03-`` is not
   swallowed by ``sk-``.
3. **One precedence order, stated.** Environment variable first, then the
   stored file. ``harness auth status`` prints which source won, so a stale
   environment variable shadowing a stored key is visible rather than
   mysterious.
4. **Local servers need no key.** Ollama and vLLM resolve as ready without one.
5. **Verification is a real call.** ``harness auth verify`` asks the provider to
   list its models. A variable being set is not evidence that the key works.

The store lives outside the repository so it cannot be committed by accident.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from harness.core.errors import HarnessError
from harness.core.util import (
    atomic_write_json,
    is_chat_model,
    model_version,
    provider_error_message,
    utc_now,
)

STORE_VERSION = 1

API_OPENAI = "openai-compatible"
API_ANTHROPIC = "anthropic"
API_LOCAL = "local"

#: How `verify` proves a key works.
#:
#: `completion` is the default and lists the models *and* sends a one-token
#: request. Listing alone is not evidence, in two separate ways that both bit
#: us in practice. OpenCode Zen and Command Code serve `/models` without any
#: auth at all, so listing there returns 200 for a key that is nonsense. And
#: Cerebras gates listing on the key but gates *completions* on quota, so a
#: valid key with nothing left in it lists two models happily and then answers
#: every request with 402. Being told a key works and then watching it fail is
#: worse than not offering verification.
#:
#: `models` remains for providers whose completion shape is not the
#: OpenAI one, where the probe cannot be built generically.
VERIFY_MODELS = "models"
VERIFY_COMPLETION = "completion"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    api: str
    env: tuple[str, ...]
    base_url: str = ""
    prefixes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    note: str = ""
    verify_via: str = VERIFY_COMPLETION
    #: Preferred models for a `completion` probe, cheapest first. Each is
    #: checked against the live model list before use, so a retired id costs
    #: a fallback rather than a confusing failure.
    probe_models: tuple[str, ...] = ()

    @property
    def needs_key(self) -> bool:
        return self.api != API_LOCAL


P = Provider
PROVIDERS: tuple[Provider, ...] = (
    P("dashscope", API_OPENAI, ("DASHSCOPE_API_KEY",),
      "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
      ("sk-ws-",), aliases=("qwen", "alibaba"),
      note="Qwen Cloud; the international endpoint is the default"),
    P("openai", API_OPENAI, ("OPENAI_API_KEY",), "https://api.openai.com/v1",
      ("sk-proj-", "sk-svcacct-", "sk-admin-", "sk-"), aliases=("gpt",)),
    P("anthropic", API_ANTHROPIC, ("ANTHROPIC_API_KEY",),
      "https://api.anthropic.com", ("sk-ant-api03-", "sk-ant-"),
      aliases=("claude",), verify_via=VERIFY_MODELS),
    P("nvidia", API_OPENAI, ("NVIDIA_API_KEY",),
      "https://integrate.api.nvidia.com/v1", ("nvapi-",), aliases=("nim",)),
    P("openrouter", API_OPENAI, ("OPENROUTER_API_KEY",),
      "https://openrouter.ai/api/v1", ("sk-or-v1-", "sk-or-")),
    P("groq", API_OPENAI, ("GROQ_API_KEY",), "https://api.groq.com/openai/v1",
      ("gsk_",)),
    P("deepseek", API_OPENAI, ("DEEPSEEK_API_KEY",),
      "https://api.deepseek.com/v1", ("sk-",)),
    P("moonshot", API_OPENAI, ("MOONSHOT_API_KEY",),
      "https://api.moonshot.cn/v1", ("sk-",), aliases=("kimi",)),
    P("together", API_OPENAI, ("TOGETHER_API_KEY",),
      "https://api.together.xyz/v1"),
    P("fireworks", API_OPENAI, ("FIREWORKS_API_KEY",),
      "https://api.fireworks.ai/inference/v1", ("fw_",)),
    # Google AI Studio. The OpenAI-compatible surface lives under
    # /v1beta/openai; the bare /v1beta is the native generateContent API,
    # which does not speak chat/completions and rejects a Bearer token.
    # Pointing here previously meant every request went to the wrong path.
    # Both key formats are current. Google is partway through replacing the
    # long-standing `AIza` keys with `AQ.` ones, and a newly created key comes
    # out in the new format. The `AQ.` keys are rejected on some native paths
    # that take `?key=`, but work over `Authorization: Bearer` against the
    # OpenAI-compatible surface, which is the one this route uses.
    P("google", API_OPENAI, ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
      "https://generativelanguage.googleapis.com/v1beta/openai", ("AIza", "AQ."),
      aliases=("gemini", "aistudio", "google-ai-studio"),
      note="Google AI Studio; keys from aistudio.google.com/apikey"),
    # OpenCode Zen. One key, many vendors' models, OpenAI-shaped.
    P("opencode", API_OPENAI, ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"),
      "https://opencode.ai/zen/v1",
      aliases=("zen", "opencode-zen"),
      prefixes=("sk-",),
      note="OpenCode Zen gateway; keys from opencode.ai/auth",
      verify_via=VERIFY_COMPLETION,
      probe_models=("deepseek-v4-flash-free", "mimo-v2.5-free", "claude-haiku-4-5")),
    # Command Code. Also serves an Anthropic-shaped endpoint at
    # /provider/v1/messages, which a `kind: anthropic` route reaches by
    # naming this same base URL.
    P("commandcode", API_OPENAI,
      ("COMMAND_CODE_API_KEY", "CMD_API_KEY"),
      "https://api.commandcode.ai/provider/v1",
      aliases=("command-code", "cmd", "command"),
      note="Command Code gateway; keys from Studio, Pro plan or higher",
      verify_via=VERIFY_COMPLETION,
      probe_models=("poolside/laguna-s-2.1-free", "deepseek/deepseek-v4-flash")),
    # ── free-tier friendly, all OpenAI-shaped ──
    # Each was confirmed reachable at the endpoint below; the notes record the
    # free allowance that was documented at the time and the constraint that
    # actually bites an agent loop, which is rarely the headline number.
    P("cerebras", API_OPENAI, ("CEREBRAS_API_KEY",), "https://api.cerebras.ai/v1",
      ("csk-",),
      note="free tier is account-dependent and context-capped; verify before relying on it"),
    P("mistral", API_OPENAI, ("MISTRAL_API_KEY",), "https://api.mistral.ai/v1",
      aliases=("codestral",),
      note="large monthly token allowance, but only a couple of requests/minute"),
    P("chutes", API_OPENAI, ("CHUTES_API_KEY",), "https://llm.chutes.ai/v1",
      ("cpk_",), note="open-weight models, free tier"),
    P("sambanova", API_OPENAI, ("SAMBANOVA_API_KEY",), "https://api.sambanova.ai/v1",
      note="free tier; model list is public"),
    P("nebius", API_OPENAI, ("NEBIUS_API_KEY",), "https://api.studio.nebius.com/v1",
      note="free credits on signup"),
    P("hyperbolic", API_OPENAI, ("HYPERBOLIC_API_KEY",), "https://api.hyperbolic.xyz/v1",
      note="free credits on signup"),
    P("deepinfra", API_OPENAI, ("DEEPINFRA_API_KEY",),
      "https://api.deepinfra.com/v1/openai",
      note="free credits on signup; model list is public"),
    P("ollama", API_LOCAL, ("OLLAMA_API_KEY",), "http://127.0.0.1:11434/v1",
      verify_via=VERIFY_MODELS, note="local server, no key required"),
    P("vllm", API_LOCAL, ("VLLM_API_KEY",), "http://127.0.0.1:8000/v1",
      verify_via=VERIFY_MODELS, note="local server, no key required"),
)

BY_NAME: dict[str, Provider] = {}
for _p in PROVIDERS:
    BY_NAME[_p.name] = _p
    for _alias in _p.aliases:
        BY_NAME[_alias] = _p

BY_ENV: dict[str, Provider] = {name: _p for _p in PROVIDERS for name in _p.env}


class AuthError(HarnessError):
    """Raised for an unknown provider or an unusable credential store.

    Inherits HarnessError so the CLI reports it as a one-line error rather
    than a traceback.
    """


def get_provider(name: str) -> Provider:
    key = (name or "").strip().lower()
    if key in BY_NAME:
        return BY_NAME[key]
    known = ", ".join(p.name for p in PROVIDERS)
    raise AuthError(f"unknown provider {name!r}. known providers: {known}")


def redact(secret: str | None) -> str:
    """The only representation of a key that may be printed or logged."""
    if not secret:
        return "<unset>"
    value = str(secret)
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:7]}...{value[-4:]} ({len(value)} chars)"


@dataclass(frozen=True, slots=True)
class Detection:
    """What a key's prefix can and cannot tell us."""

    #: The single provider the prefix identifies, when it identifies one.
    provider: Provider | None
    #: Every provider whose longest matching prefix tied. More than one means
    #: the prefix is not evidence and something else has to decide.
    candidates: tuple[Provider, ...] = ()

    @property
    def certain(self) -> bool:
        return self.provider is not None


def identify(key: str) -> Detection:
    """Which provider issued this key, as far as its prefix can say.

    Longest prefix wins, so `sk-ant-api03-` is not swallowed by `sk-`. When
    several providers declare the *same* longest prefix the answer is
    genuinely unknown: a bare `sk-` is issued by OpenAI, DeepSeek, Moonshot
    and OpenCode alike. Guessing one of them silently files the key under the
    wrong provider, where it fails later as a puzzling 401 somewhere else.
    """
    candidate = (key or "").strip()
    if not candidate:
        return Detection(None)
    best = 0
    matched: list[Provider] = []
    for provider in PROVIDERS:
        for prefix in provider.prefixes:
            if not candidate.startswith(prefix):
                continue
            if len(prefix) > best:
                best, matched = len(prefix), [provider]
            elif len(prefix) == best and provider not in matched:
                matched.append(provider)
    if not matched:
        return Detection(None)
    if len(matched) == 1:
        return Detection(matched[0], tuple(matched))
    return Detection(None, tuple(matched))


def detect(key: str) -> Provider | None:
    """The provider a key's prefix identifies, or None if it cannot say."""
    return identify(key).provider


def store_path() -> Path:
    """User level, deliberately outside the repository."""
    override = os.environ.get("YATRA_HARNESS_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yatra-harness" / "auth.json"


def load_store() -> dict:
    path = store_path()
    if not path.is_file():
        return {"version": STORE_VERSION, "providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"credential store is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or "providers" not in data:
        raise AuthError(f"credential store is malformed: {path}")
    return data


def save_store(data: dict) -> Path:
    path = store_path()
    # Plain mkdir: a restrictive mode here produces a Windows ACL without an
    # entry for the creating user, which makes the directory unmanageable.
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, mode=0o600)
    return path


def put_key(provider: str, key: str, base_url: str = "") -> dict:
    resolved = get_provider(provider)
    secret = (key or "").strip()
    if not secret:
        raise AuthError("refusing to store an empty key")
    data = load_store()
    entry: dict[str, str] = {"key": secret, "added_at": utc_now()}
    if base_url:
        entry["base_url"] = base_url
    data.setdefault("providers", {})[resolved.name] = entry
    save_store(data)
    return {
        "provider": resolved.name,
        "key": redact(secret),
        "env": resolved.env[0],
        "path": str(store_path()),
    }


def remove_key(provider: str) -> bool:
    resolved = get_provider(provider)
    data = load_store()
    existed = data.get("providers", {}).pop(resolved.name, None) is not None
    if existed:
        save_store(data)
    return existed


SOURCE_ENV = "environment"
SOURCE_STORED = "stored"
SOURCE_NONE = "none"


@dataclass(frozen=True, slots=True)
class Credential:
    provider: str
    key: str
    source: str
    env_var: str

    @property
    def available(self) -> bool:
        return bool(self.key)


def _stored_key(provider_name: str) -> str:
    try:
        data = load_store()
    except AuthError:
        return ""
    entry = data.get("providers", {}).get(provider_name) or {}
    return str(entry.get("key", ""))


def resolve(provider: str) -> Credential:
    """Resolve a provider's credential: environment first, then the store."""
    resolved = get_provider(provider)
    for name in resolved.env:
        value = os.environ.get(name, "")
        if value:
            return Credential(resolved.name, value, SOURCE_ENV, name)
    stored = _stored_key(resolved.name)
    primary = resolved.env[0] if resolved.env else ""
    if stored:
        return Credential(resolved.name, stored, SOURCE_STORED, primary)
    return Credential(resolved.name, "", SOURCE_NONE, primary)


def resolve_env(env_var: str) -> Credential:
    """Resolve the credential a route asks for by environment variable name.

    Routes name a variable rather than a provider, so this is the entry point
    the provider adapters and the doctor both use. Checking the environment
    before the store keeps a deliberately exported variable authoritative.
    """
    if not env_var:
        return Credential("", "", SOURCE_NONE, "")
    value = os.environ.get(env_var, "")
    provider = BY_ENV.get(env_var)
    name = provider.name if provider else ""
    if value:
        return Credential(name, value, SOURCE_ENV, env_var)
    if provider:
        stored = _stored_key(provider.name)
        if stored:
            return Credential(name, stored, SOURCE_STORED, env_var)
    return Credential(name, "", SOURCE_NONE, env_var)


def _normalize_url(url: str) -> str:
    """Compare endpoints without tripping over case or a trailing slash."""
    return (url or "").strip().rstrip("/").lower()


def provider_for_base_url(base_url: str) -> Provider | None:
    """Map a route's endpoint back to a provider.

    A stored ``base_url`` recorded by ``auth add --base-url`` wins over the
    built-in endpoint, because that entry was created for exactly this
    gateway and a shared default endpoint would otherwise shadow it.
    """
    target = _normalize_url(base_url)
    if not target:
        return None
    try:
        data = load_store()
    except AuthError:
        data = {}
    for name, entry in (data.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        if _normalize_url(str(entry.get("base_url", ""))) == target:
            provider = BY_NAME.get(name)
            if provider is not None:
                return provider
    for provider in PROVIDERS:
        if _normalize_url(provider.base_url) == target:
            return provider
    return None


def resolve_route(env_var: str, base_url: str = "") -> Credential:
    """Resolve the credential for a configured route.

    A route names a variable and an endpoint, and the variable need not be
    one this module knows: ``api_key_env: HARNESS_REMOTE_API_KEY`` is a
    perfectly ordinary thing to write, and teaching.yaml ships exactly that.
    Resolving on the name alone left such a route unable to see a stored key
    while the error it raised told the operator to store one -- advice that
    could not work. The endpoint is the second way in.

    Precedence is unchanged and deliberate: an exported variable wins, then
    the store by variable name, then the store by endpoint.
    """
    credential = resolve_env(env_var)
    if credential.available or not base_url:
        return credential
    provider = provider_for_base_url(base_url)
    if provider is None:
        return credential
    stored = _stored_key(provider.name)
    if not stored:
        return credential
    return Credential(provider.name, stored, SOURCE_STORED, env_var)


ENV_FILE_NAME = ".env"


def env_file_path(start: Path | None = None) -> Path | None:
    """Locate the .env to load: the override, else the nearest one above.

    Walking up from the working directory means a run started from a
    subdirectory of a project finds the same file as one started at its
    root, which is the behaviour every other dotenv-reading tool has.
    """
    override = os.environ.get("YATRA_HARNESS_ENV_FILE")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    try:
        base = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    except OSError:
        return None
    for directory in (base, *base.parents):
        candidate = directory / ENV_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def parse_env_file(text: str) -> dict[str, str]:
    """Parse the shapes people actually write in a .env.

    Accepts a leading ``export`` because that is what gets pasted out of a
    shell profile, and strips one matching pair of surrounding quotes
    because a quoted key would otherwise be sent to the provider with the
    quotes attached and fail as a 401 with nothing to point at.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_env_file(start: Path | None = None) -> Path | None:
    """Load .env into the environment. Exported variables always win.

    Both entry points call this, so `ay` and `harness` agree about which
    credentials exist. Previously only the REPL read .env, and a key that
    made `ay` work left `harness run` reporting none.

    A missing or unreadable file is not an error: .env is a convenience,
    and failing to start over a malformed one would be worse than ignoring
    it, since the credential may well be exported already.
    """
    path = env_file_path(start)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for name, value in parse_env_file(text).items():
        os.environ.setdefault(name, value)
    return path


def status() -> list[dict]:
    """One row per provider: where its credential comes from, if anywhere."""
    rows = []
    for provider in PROVIDERS:
        credential = resolve(provider.name)
        rows.append(
            {
                "provider": provider.name,
                "api": provider.api,
                "env": provider.env[0] if provider.env else "",
                "source": credential.source,
                "key": redact(credential.key),
                "ready": credential.available or not provider.needs_key,
                "note": provider.note,
            }
        )
    return rows


@dataclass(frozen=True, slots=True)
class VerifyResult:
    provider: str
    ok: bool
    detail: str
    source: str


class _HTTPFailure(Exception):
    """A request that did not come back as usable JSON, already worded."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    #: The statuses that are actually about the credential or its budget.
    CREDENTIAL_STATUSES = (401, 402, 403, 429)

    @property
    def about_the_credential(self) -> bool:
        """True when the failure is about the key or its quota.

        Listed rather than excluded, because everything else a probe can hit
        is about the *model* it chose: a 400 for the wrong input shape, a 404
        for a retired id, a 500 from an endpoint that does not take chat at
        all. Reporting any of those as a bad key sends the operator off to
        regenerate a credential that was fine.
        """
        return self.status in self.CREDENTIAL_STATUSES


def _error_message(body: str) -> str:
    """The provider's own message. Shared so every entry point words it alike."""
    return provider_error_message(body, limit=160)


#: Sent on every request this module makes. urllib's default
#: `Python-urllib/3.x` is blocked outright by the Cloudflare rules in front of
#: several gateways, which surfaces as a 403 "error code: 1010" that looks
#: exactly like a rejected key and is not one.
USER_AGENT = "yatra-harness/1.0"


def _request_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    body: dict | None = None,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {**headers, "User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # HTTPError is itself a response object and holds the socket open
        # until it is closed, which a bare read() does not do.
        with exc:
            detail = _error_message(exc.read().decode("utf-8", "replace"))
        raise _HTTPFailure(f"HTTP {exc.code}: {detail}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise _HTTPFailure(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise _HTTPFailure("provider did not return a JSON object")
    return payload


def _auth_headers(provider: Provider, key: str) -> dict[str, str]:
    if provider.api == API_ANTHROPIC:
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _models_url(provider: Provider) -> str:
    base = provider.base_url.rstrip("/")
    # Anthropic's model list sits at /v1/models under the bare api host,
    # where every OpenAI-shaped provider already has /v1 in its base URL.
    return f"{base}/v1/models" if provider.api == API_ANTHROPIC else f"{base}/models"


def list_models(provider: Provider, key: str, timeout: float = 20.0) -> list[str]:
    """The model ids this provider currently serves."""
    payload = _request_json(_models_url(provider), _auth_headers(provider, key), timeout)
    entries = payload.get("data") or payload.get("models") or []
    if not isinstance(entries, list):
        return []
    ids = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            ids.append(str(entry["id"]))
        elif isinstance(entry, str):
            ids.append(entry)
    return ids


def _probe_model(provider: Provider, available: list[str]) -> str:
    """The model to send a one-token request to.

    Preferred ids are checked against what the provider actually serves, so a
    retired id costs a fallback rather than a 404 the operator has to decode.
    A free model is chosen over a paid one where the provider marks them,
    because verifying a key should not bill for it.

    Failing both, the newest chat model wins. Taking the first one listed is
    not good enough: Google's catalogue opens with models that are retired
    for new projects and answers them with a 404, which then reads as a bad
    key rather than a bad choice of probe.
    """
    names = [name.removeprefix("models/") for name in available]
    offered = set(names)
    for candidate in provider.probe_models:
        if candidate in offered:
            return candidate
    free = [model for model in names if model.endswith("-free")]
    if free:
        return free[0]
    usable = [name for name in names if is_chat_model(name)]
    usable.sort(key=lambda name: (-model_version(name), name))
    if usable:
        return usable[0]
    return names[0] if names else ""


def check_credential(
    provider: Provider, key: str, timeout: float = 20.0
) -> tuple[bool, str]:
    """Does *key* actually work for *provider*? Returns (ok, detail).

    Split out from `verify` so a key that is not stored yet can be tested,
    which is what makes it possible to resolve an ambiguous prefix by asking
    rather than by guessing.
    """
    if not provider.base_url:
        return False, "no base URL configured"
    try:
        if provider.verify_via == VERIFY_COMPLETION:
            return True, _verify_completion(provider, key, timeout)
        return True, f"{len(list_models(provider, key, timeout))} models reachable"
    except _HTTPFailure as exc:
        return False, str(exc)


def verify(provider: str, timeout: float = 20.0) -> VerifyResult:
    """Prove the key works with a real call, not by checking a variable is set."""
    resolved = get_provider(provider)
    credential = resolve(resolved.name)
    if not credential.available and resolved.needs_key:
        return VerifyResult(
            resolved.name, False,
            f"no credential; set {resolved.env[0]} or run `harness auth add`",
            credential.source,
        )
    ok, detail = check_credential(resolved, credential.key, timeout)
    return VerifyResult(resolved.name, ok, detail, credential.source)


PROBE_ATTEMPTS = 4


def _verify_completion(provider: Provider, key: str, timeout: float) -> str:
    """Send the smallest request the key and its quota have to be good for.

    Listing models is not evidence twice over: some gateways serve the list
    with no auth at all, and some providers gate the list on the key but gate
    completions on quota, so an empty account lists happily and then answers
    every request with 402.

    Several models are tried because a catalogue contains things that are not
    chat models -- speech, embedding, retired ids -- and picking one of those
    fails with a 400 that says nothing about the key. Only a failure that is
    actually about the credential stops the search.
    """
    available = list_models(provider, key, timeout)
    candidates = _probe_candidates(provider, available)
    if not candidates:
        raise _HTTPFailure("provider listed no models to probe")
    last: _HTTPFailure | None = None
    for model in candidates:
        try:
            _request_json(
                f"{provider.base_url.rstrip('/')}/chat/completions",
                _auth_headers(provider, key),
                timeout,
                body={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
            )
        except _HTTPFailure as exc:
            if exc.about_the_credential:
                raise
            last = exc
            continue
        return f"key accepted by {model}; {len(available)} models offered"
    raise last or _HTTPFailure("no model accepted a probe request")


def _probe_candidates(provider: Provider, available: list[str]) -> list[str]:
    """Models to try, best first, so one bad guess is not the whole answer."""
    names = [name.removeprefix("models/") for name in available]
    offered = set(names)
    ordered = [m for m in provider.probe_models if m in offered]
    ordered += [m for m in names if m.endswith("-free") and m not in ordered]
    usable = [n for n in names if is_chat_model(n) and n not in ordered]
    usable.sort(key=lambda name: (-model_version(name), name))
    ordered += usable
    return ordered[:PROBE_ATTEMPTS]


def add(
    key: str,
    provider: str | None = None,
    base_url: str = "",
    *,
    probe: bool = True,
    timeout: float = 15.0,
) -> dict:
    """Store a key under the provider it actually belongs to.

    A distinctive prefix settles it. A shared one -- a bare `sk-`, which
    OpenAI, DeepSeek, Moonshot and OpenCode all issue -- settles nothing, so
    the candidates are asked which of them the key authenticates against.
    Filing it under a guess is worse than asking: the key then fails as a 401
    on a provider the operator never chose, and the mistake is invisible in
    `auth status`, which shows a key present and looking fine.
    """
    secret = (key or "").strip()
    if not secret:
        raise AuthError("refusing to store an empty key")
    if provider:
        record = put_key(get_provider(provider).name, secret, base_url)
        record["how"] = "named by the operator"
        return record

    found = identify(secret)
    if found.certain:
        record = put_key(found.provider.name, secret, base_url)
        record["how"] = "inferred from the key's prefix"
        return record

    if found.candidates and probe:
        for candidate in found.candidates:
            ok, _detail = check_credential(candidate, secret, timeout)
            if ok:
                record = put_key(candidate.name, secret, base_url)
                record["how"] = f"confirmed by {candidate.name} accepting the key"
                return record
        names = ", ".join(p.name for p in found.candidates)
        raise AuthError(
            f"this key was not accepted by any provider that issues keys of this shape "
            f"({names}). If it belongs to a different provider, name it: "
            f"harness auth add --provider <name> <key>"
        )

    known = ", ".join(p.name for p in PROVIDERS)
    if found.candidates:
        names = ", ".join(p.name for p in found.candidates)
        raise AuthError(
            f"this key's prefix is shared by {names}, so the provider cannot be "
            "inferred. Name it: harness auth add --provider <name> <key>"
        )
    raise AuthError(
        "could not infer the provider from this key. "
        f"pass --provider with one of: {known}"
    )
