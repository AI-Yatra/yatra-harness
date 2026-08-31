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

from .errors import HarnessError
from .util import atomic_write_json, utc_now

STORE_VERSION = 1

API_OPENAI = "openai-compatible"
API_ANTHROPIC = "anthropic"
API_LOCAL = "local"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    api: str
    env: tuple[str, ...]
    base_url: str = ""
    prefixes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    note: str = ""

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
      ("sk-proj-", "sk-svcacct-", "sk-admin-"), aliases=("gpt",)),
    P("anthropic", API_ANTHROPIC, ("ANTHROPIC_API_KEY",),
      "https://api.anthropic.com", ("sk-ant-api03-", "sk-ant-"),
      aliases=("claude",)),
    P("nvidia", API_OPENAI, ("NVIDIA_API_KEY",),
      "https://integrate.api.nvidia.com/v1", ("nvapi-",), aliases=("nim",)),
    P("openrouter", API_OPENAI, ("OPENROUTER_API_KEY",),
      "https://openrouter.ai/api/v1", ("sk-or-v1-", "sk-or-")),
    P("groq", API_OPENAI, ("GROQ_API_KEY",), "https://api.groq.com/openai/v1",
      ("gsk_",)),
    P("deepseek", API_OPENAI, ("DEEPSEEK_API_KEY",),
      "https://api.deepseek.com/v1"),
    P("moonshot", API_OPENAI, ("MOONSHOT_API_KEY",),
      "https://api.moonshot.cn/v1", aliases=("kimi",)),
    P("together", API_OPENAI, ("TOGETHER_API_KEY",),
      "https://api.together.xyz/v1"),
    P("fireworks", API_OPENAI, ("FIREWORKS_API_KEY",),
      "https://api.fireworks.ai/inference/v1", ("fw_",)),
    P("google", API_OPENAI, ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
      "https://generativelanguage.googleapis.com/v1beta", ("AIza",),
      aliases=("gemini",)),
    P("ollama", API_LOCAL, ("OLLAMA_API_KEY",), "http://127.0.0.1:11434/v1",
      note="local server, no key required"),
    P("vllm", API_LOCAL, ("VLLM_API_KEY",), "http://127.0.0.1:8000/v1",
      note="local server, no key required"),
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


def detect(key: str) -> Provider | None:
    """Infer a provider from a bare key. Longest matching prefix wins."""
    candidate = (key or "").strip()
    if not candidate:
        return None
    best: tuple[int, Provider] | None = None
    for provider in PROVIDERS:
        for prefix in provider.prefixes:
            if candidate.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), provider)
    if best:
        return best[1]
    if candidate.startswith("sk-"):
        # A bare sk- key with no more specific prefix is a legacy OpenAI key.
        return BY_NAME["openai"]
    return None


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


def verify(provider: str, timeout: float = 20.0) -> VerifyResult:
    """Ask the provider to list its models. A real call, not a variable check."""
    resolved = get_provider(provider)
    credential = resolve(resolved.name)
    if not credential.available and resolved.needs_key:
        return VerifyResult(
            resolved.name, False,
            f"no credential; set {resolved.env[0]} or run `harness auth add`",
            credential.source,
        )
    if not resolved.base_url:
        return VerifyResult(resolved.name, False, "no base URL configured", credential.source)
    if resolved.api == API_ANTHROPIC:
        url = f"{resolved.base_url.rstrip('/')}/v1/models"
        headers = {"x-api-key": credential.key, "anthropic-version": "2023-06-01"}
    else:
        url = f"{resolved.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {credential.key}"} if credential.key else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
        models = payload.get("data") or payload.get("models") or []
        return VerifyResult(
            resolved.name, True, f"{len(models)} models reachable", credential.source
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)["error"]["message"]
        except (json.JSONDecodeError, KeyError, TypeError):
            body = body[:160]
        return VerifyResult(
            resolved.name, False, f"HTTP {exc.code}: {body}", credential.source
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return VerifyResult(
            resolved.name, False, f"{type(exc).__name__}: {exc}", credential.source
        )


def add(key: str, provider: str | None = None, base_url: str = "") -> dict:
    """Store a key, inferring the provider from its prefix when not given."""
    secret = (key or "").strip()
    if not secret:
        raise AuthError("refusing to store an empty key")
    if provider:
        chosen = get_provider(provider)
    else:
        detected = detect(secret)
        if detected is None:
            known = ", ".join(p.name for p in PROVIDERS)
            raise AuthError(
                "could not infer the provider from this key. "
                f"pass --provider with one of: {known}"
            )
        chosen = detected
    return put_key(chosen.name, secret, base_url)
