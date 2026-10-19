"""The Assistant's settings: per computer, never in the memory folder (product-design.md §17.2).

``assistant.json`` (mode 0600) sits next to MyMemo Cloud's ``cloud.json`` in ``cloud.cloud_dir()``, so
``OPENMYMEMO_CLOUD_DIR`` moves both. On macOS the API key (and the MyMemo token) go in the login Keychain when the
``security`` tool is there and the key is plain ASCII; otherwise they stay in the 0600 file. MyMemo's hosted model
(§17.8) keeps its access and refresh tokens the same way, and the rest of its sign-in (service, expiry, account, any
pause the service asked for) under ``mymemo`` in the file; ``mymemo.py`` manages them. ``OPENMYMEMO_ASSISTANT_*``
environment variables override the file for scripts, and the panel shows those fields as set from outside.

Keys this version doesn't know are kept when the file is rewritten, so a newer OpenMyMemo's settings survive an
older one changing a switch.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("openmymemo.assistant")

FEATURES = ("import", "sort", "recall", "check", "session", "tidy")
KINDS = ("local", "key", "mymemo")
PROTOCOLS = ("openai", "anthropic")
# Imports and sorting are what step 1 builds; recall and checking writes are cheap and on once the Assistant is on.
# Session-end extraction and tidy-up read much more, so they wait until the person turns them on (§17.6, §17.7).
DEFAULT_FEATURES = {"import": True, "sort": True, "recall": True, "check": True, "session": False, "tidy": False}
DEFAULT_CAPS = {"calls": 1000, "tokens": 2_000_000}
DEFAULT_TIMEOUT = 60.0
DEFAULT_CHECK_BUDGET = 2.0  # seconds a proposal may wait for the Assistant's check before today's rules file it
CHECK_BUDGET_RANGE = (0.5, 30.0)
# Session-end extraction asks per app (§17.7): the apps whose hooks can call it, and what the person said.
SESSION_APPS = ("claude-code", "codex")
CONSENT = ("allowed", "declined", "asked")
DEFAULT_URLS = {
    ("local", "openai"): "http://127.0.0.1:11434/v1",  # Ollama; LM Studio is :1234/v1, llama.cpp :8080/v1
    ("key", "openai"): "https://api.openai.com/v1",
    ("key", "anthropic"): "https://api.anthropic.com",
}
MYMEMO_PATH = "/openmymemo/assistant/v1"
KEYCHAIN_SERVICE = "OpenMyMemo Assistant"
SECRETS = {"api_key": "api-key", "mymemo_token": "mymemo-token", "mymemo_refresh": "mymemo-refresh"}  # → Keychain
_SAFE_SECRET = re.compile(r"[A-Za-z0-9._~+/=:-]{1,512}")
ENV = "OPENMYMEMO_ASSISTANT_"


class SettingsError(Exception):
    """A setting that can't be used; the message is for the person."""


def _cloud_dir() -> Path:
    from ..cloud import cloud_dir

    return cloud_dir()


def settings_path() -> Path:
    return _cloud_dir() / "assistant.json"


@dataclass
class Settings:
    enabled: bool = False
    kind: str = "local"
    protocol: str = "openai"
    base_url: str = ""  # empty: the kind's default (DEFAULT_URLS, or MyMemo's service)
    model: str = ""
    # An embeddings model at the same endpoint (OpenAI-compatible ``/embeddings``), for search by meaning (§17.4);
    # empty: none. Not for Anthropic's API or MyMemo's hosted model, which have no embeddings.
    embeddings_model: str = ""
    api_key: str = field(default="", repr=False)
    mymemo_token: str = field(default="", repr=False)  # MyMemo's access token
    mymemo_refresh: str = field(default="", repr=False)
    mymemo: dict[str, Any] = field(default_factory=dict)  # MyMemo's sign-in: service, expires_at, via, account, hold
    features: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_FEATURES))
    private_topics: list[str] = field(default_factory=list)  # opted in, for a model on this computer only
    caps: dict[str, int | None] = field(default_factory=lambda: dict(DEFAULT_CAPS))
    usage: dict[str, Any] = field(default_factory=dict)  # {"month": "2026-10", "calls": n, "tokens": n}
    timeout: float = DEFAULT_TIMEOUT
    check_budget: float = DEFAULT_CHECK_BUDGET
    session_apps: dict[str, str] = field(default_factory=dict)  # app id → allowed | declined | asked
    from_env: list[str] = field(default_factory=list)  # fields an OPENMYMEMO_ASSISTANT_* variable sets
    key_store: str | None = None  # where the secret in use came from: keychain | file | env | None

    @property
    def secret(self) -> str:
        """The credential for the chosen kind: the API key, or MyMemo's token."""
        return self.mymemo_token if self.kind == "mymemo" else self.api_key

    @property
    def endpoint(self) -> str:
        """The base URL calls go to."""
        if self.kind == "mymemo":  # the service signed in to, else MyMemo Cloud's choice (§16.11)
            from ..cloud import service_url

            if self.base_url:
                return self.base_url.rstrip("/")
            service = self.mymemo.get("service") if isinstance(self.mymemo.get("service"), str) else ""
            return (service or service_url()).rstrip("/") + MYMEMO_PATH
        return (self.base_url or DEFAULT_URLS.get((self.kind, self.protocol), "")).rstrip("/")

    @property
    def host(self) -> str:
        return urlsplit(self.endpoint).hostname or ""

    @property
    def local(self) -> bool:
        """A model on this computer: kind ``local`` and a loopback address (checked again before every call)."""
        return self.kind == "local" and is_loopback(self.endpoint)

    @property
    def embeddings_available(self) -> bool:
        """Whether this endpoint can embed: an OpenAI-compatible endpoint (on this computer or with a key) and an
        embeddings model chosen. Anthropic's API and MyMemo's hosted model have none (§17.8)."""
        return bool(self.embeddings_model) and self.kind in ("local", "key") and self.protocol == "openai"

    def month_usage(self, now: datetime | None = None) -> dict[str, Any]:
        month = (now or datetime.now(UTC)).strftime("%Y-%m")
        u = self.usage if self.usage.get("month") == month else {}
        return {"month": month, "calls": int(u.get("calls") or 0), "tokens": int(u.get("tokens") or 0)}

    def problem(self) -> str | None:
        """Why calls can't be made with these settings, for the person; None when they can."""
        if self.kind == "mymemo":
            if not self.mymemo_token:
                return "Sign in to MyMemo to use its hosted model."
        elif not self.model:
            return "Choose a model."
        bad = url_problem(self.endpoint, self.kind)
        if bad:
            return bad
        if self.kind == "key" and not self.api_key:
            return "Enter your API key."
        return None


# ---- addresses ---------------------------------------------------------------------------------------------


_URL_CHARS = re.compile(r"[A-Za-z0-9._~:/%\[\]@!$&'()*+,;=?#-]*")


def _host(url: str) -> str | None:
    """The host a request to ``url`` really goes to, or None when it is unclear: the standard library and the HTTP
    client must agree, and the address must be plain ASCII without backslashes (which parsers read differently)."""
    if not _URL_CHARS.fullmatch(url):
        return None
    try:
        import httpx2

        ours = (urlsplit(url).hostname or "").lower()
        theirs = httpx2.URL(url).host.lower()
    except Exception:  # noqa: BLE001 - httpx2 raises its own InvalidURL
        return None
    return ours if ours and ours == theirs.strip("[]") else None


def is_loopback(url: str) -> bool:
    host = _host(url)
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_problem(url: str, kind: str) -> str | None:
    """Why a base URL can't be used, for the person; None when it can. ``local`` must be loopback; anything else
    must be https, or http on loopback (a proxy on this computer). No user, query or fragment."""
    url = url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "That isn't a valid web address."
    if not parts.hostname or len(url) > 300 or _host(url) is None:
        return "That isn't a valid web address."
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment or "#" in url:
        return "Enter the address only, without a user name, ? or #."
    if parts.scheme not in ("http", "https"):
        return "The address must start with http:// or https://."
    if kind == "local" and not is_loopback(url):
        return "A model on this computer must have a local address (127.0.0.1 or localhost)."
    if parts.scheme == "http" and not is_loopback(url):
        return "The address must start with https:// (http:// only for a server on this computer)."
    return None


def check_url(url: str, kind: str) -> str:
    """A base URL as stored (no trailing slash); raises SettingsError with ``url_problem``'s reason."""
    bad = url_problem(url, kind)
    if bad:
        raise SettingsError(bad)
    return url.strip().rstrip("/")


# ---- the Keychain (macOS) ----------------------------------------------------------------------------------

_run = subprocess.run  # replaced in tests


def keychain_available() -> bool:
    return (sys.platform == "darwin" and os.environ.get(f"{ENV}KEYCHAIN", "1") != "0"
            and shutil.which("security") is not None)


def keychain_get(account: str) -> str | None:
    try:
        r = _run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(account: str, secret: str) -> bool:
    """Store a secret; False when it can't be (no Keychain, or characters `security -i` can't take quoted). The
    secret goes in on stdin, never on the command line where other processes could read it."""
    if not keychain_available() or not _SAFE_SECRET.fullmatch(secret):
        return False
    command = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -w "{secret}"\n'
    try:
        r = _run(["security", "-i"], input=command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and keychain_get(account) == secret


def keychain_delete(account: str) -> None:
    if not keychain_available():
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        _run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
             capture_output=True, text=True, timeout=10)


# ---- reading and writing assistant.json ----------------------------------------------------------------------


def _read_raw() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Assistant: ignoring an unreadable %s (%s)", settings_path(), type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _cap(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _budget(value: float) -> float:
    return min(max(float(value), CHECK_BUDGET_RANGE[0]), CHECK_BUDGET_RANGE[1])


def _truthy(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "yes", "on")


def _from_raw(data: Mapping[str, Any]) -> Settings:
    s = Settings()
    s.enabled = data.get("enabled") is True
    if data.get("kind") in KINDS:
        s.kind = data["kind"]
    if data.get("protocol") in PROTOCOLS:
        s.protocol = data["protocol"]
    for name in ("base_url", "model", "embeddings_model"):
        if isinstance(data.get(name), str):
            setattr(s, name, data[name].strip())
    features = data.get("features")
    if isinstance(features, dict):
        s.features.update({k: v for k, v in features.items() if k in FEATURES and isinstance(v, bool)})
    topics = data.get("private_topics")
    if isinstance(topics, list):
        s.private_topics = sorted({t for t in topics if isinstance(t, str) and t})
    caps = data.get("caps")
    if isinstance(caps, dict):
        s.caps = {k: _cap(caps.get(k, DEFAULT_CAPS[k])) for k in DEFAULT_CAPS}
    if isinstance(data.get("usage"), dict):
        s.usage = dict(data["usage"])
    if isinstance(data.get("mymemo"), dict):
        s.mymemo = dict(data["mymemo"])
    if isinstance(data.get("timeout"), int | float) and not isinstance(data.get("timeout"), bool):
        s.timeout = min(max(float(data["timeout"]), 5.0), 600.0)
    if isinstance(data.get("check_budget"), int | float) and not isinstance(data.get("check_budget"), bool):
        s.check_budget = _budget(data["check_budget"])
    apps = data.get("session_apps")
    if isinstance(apps, dict):
        s.session_apps = {k: v for k, v in apps.items() if k in SESSION_APPS and v in CONSENT}
    for name, account in SECRETS.items():
        where = data.get(f"{name}_in")
        if where == "keychain":
            value = keychain_get(account) or ""
        else:
            value = data.get(name) if isinstance(data.get(name), str) else ""
        setattr(s, name, value)
        if name == ("mymemo_token" if s.kind == "mymemo" else "api_key") and value:  # (not the refresh token)
            s.key_store = "keychain" if where == "keychain" else "file"
    return s


def _apply_env(s: Settings, env: Mapping[str, str]) -> None:
    def get(name: str) -> str | None:
        value = env.get(ENV + name)
        return value.strip() if value is not None and value.strip() else None

    if (v := get("ENABLED")) is not None:
        s.enabled = _truthy(v)
        s.from_env.append("enabled")
    if (v := get("KIND")) in KINDS:
        s.kind = v
        s.from_env.append("kind")
    if (v := get("PROTOCOL")) in PROTOCOLS:
        s.protocol = v
        s.from_env.append("protocol")
    if (v := get("BASE_URL")) is not None:
        s.base_url = v.rstrip("/")
        s.from_env.append("base_url")
    if (v := get("MODEL")) is not None:
        s.model = v
        s.from_env.append("model")
    if (v := get("EMBEDDINGS_MODEL")) is not None:
        s.embeddings_model = v
        s.from_env.append("embeddings_model")
    if (v := get("API_KEY")) is not None:
        if s.kind == "mymemo":
            s.mymemo_token = v
        else:
            s.api_key = v
        s.key_store = "env"
        s.from_env.append("api_key")
    if (v := get("FEATURES")) is not None:  # the features that are on, comma-separated; the rest are off
        on = {x.strip() for x in v.split(",")}
        s.features = {k: k in on for k in FEATURES}
        s.from_env.append("features")
    for name in ("calls", "tokens"):
        if (v := get(f"CAP_{name.upper()}")) is not None:
            s.caps[name] = _cap(v)
            s.from_env.append(f"caps.{name}")
    if (v := get("TIMEOUT")) is not None:
        with contextlib.suppress(ValueError):
            s.timeout = min(max(float(v), 5.0), 600.0)
            s.from_env.append("timeout")
    if (v := get("CHECK_BUDGET")) is not None:
        with contextlib.suppress(ValueError):
            s.check_budget = _budget(float(v))
            s.from_env.append("check_budget")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """This computer's Assistant settings: the file, the Keychain, then ``OPENMYMEMO_ASSISTANT_*``."""
    s = _from_raw(_read_raw())
    _apply_env(s, os.environ if environ is None else environ)
    return s


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """One change to ``assistant.json`` at a time, across the panel and the CLI (usage counters included)."""
    from ..vault import _flock

    folder = _cloud_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(folder / ".assistant.lock", "a+") as fh:
        _flock(fh, True)
        try:
            yield
        finally:
            _flock(fh, False)


def _write(data: dict[str, Any]) -> None:
    from ..cloud import _write_private

    _write_private(settings_path(), json.dumps({"format": 1, **{k: v for k, v in data.items() if k != "format"}},
                                              ensure_ascii=False, indent=1) + "\n")


UNSET: Any = object()


def save_settings(*, enabled: bool | None = None, kind: str | None = None, protocol: str | None = None,
                  base_url: str | None = None, model: str | None = None, embeddings_model: str | None = None,
                  api_key: str | None = None,
                  mymemo_token: str | None = None, features: Mapping[str, bool] | None = None,
                  private_topics: list[str] | None = None, caps: Mapping[str, Any] | None = None,
                  timeout: float | None = None, check_budget: float | None = None,
                  session_apps: Mapping[str, str] | None = None) -> Settings:
    """Change the stored settings (None leaves a field alone; ``""`` clears a URL, model or secret). Validates
    before writing anything; raises SettingsError. Returns the settings now in force (environment included)."""
    with _lock():
        raw = _read_raw()
        current = _from_raw(raw)
        new_kind = kind if kind is not None else current.kind
        if new_kind not in KINDS:
            raise SettingsError(f"kind must be one of {', '.join(KINDS)}")
        if protocol is not None and protocol not in PROTOCOLS:
            raise SettingsError(f"protocol must be one of {', '.join(PROTOCOLS)}")
        if kind is not None:
            raw["kind"] = kind
            if kind == "mymemo":
                raw["protocol"] = "openai"  # MyMemo's endpoint is OpenAI-compatible (§17.8)
            elif kind == "local" and protocol is None:
                raw["protocol"] = "openai"  # local servers speak the OpenAI shape
            if kind != current.kind and base_url is None:
                raw["base_url"] = ""  # another kind's address would be wrong for this one
        if protocol is not None:
            raw["protocol"] = "openai" if new_kind != "key" else protocol
            if protocol != current.protocol and base_url is None and new_kind == "key":
                raw["base_url"] = ""
        if base_url is not None:
            raw["base_url"] = check_url(base_url, new_kind) if base_url.strip() else ""
        elif raw.get("base_url"):
            if url_problem(raw["base_url"], new_kind):
                raw["base_url"] = ""
        if model is not None:
            if len(model) > 200 or any(c.isspace() for c in model.strip()):
                raise SettingsError("That isn't a model name.")
            raw["model"] = model.strip()
        if embeddings_model is not None:
            if len(embeddings_model) > 200 or any(c.isspace() for c in embeddings_model.strip()):
                raise SettingsError("That isn't a model name.")
            raw["embeddings_model"] = embeddings_model.strip()
        if features is not None:
            unknown = set(features) - set(FEATURES)
            if unknown:
                raise SettingsError(f"Unknown feature: {', '.join(sorted(unknown))}")
            merged = {**DEFAULT_FEATURES, **(raw.get("features") if isinstance(raw.get("features"), dict) else {})}
            merged.update({k: bool(v) for k, v in features.items()})
            raw["features"] = {k: merged[k] for k in FEATURES} | {k: v for k, v in merged.items() if k not in FEATURES}
        if private_topics is not None:
            raw["private_topics"] = sorted({t.strip() for t in private_topics if isinstance(t, str) and t.strip()})
        if caps is not None:
            unknown = set(caps) - set(DEFAULT_CAPS)
            if unknown:
                raise SettingsError(f"Unknown limit: {', '.join(sorted(unknown))}")
            stored = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
            raw["caps"] = {**DEFAULT_CAPS, **stored, **{k: _cap(v) for k, v in caps.items()}}
        if timeout is not None:
            raw["timeout"] = min(max(float(timeout), 5.0), 600.0)
        if check_budget is not None:
            raw["check_budget"] = _budget(check_budget)
        if session_apps is not None:
            bad = [k for k, v in session_apps.items() if k not in SESSION_APPS or v not in CONSENT]
            if bad:
                raise SettingsError(f"Unknown app or answer: {', '.join(sorted(bad))}")
            stored = raw.get("session_apps") if isinstance(raw.get("session_apps"), dict) else {}
            raw["session_apps"] = {**stored, **session_apps}
        for name, value in (("api_key", api_key), ("mymemo_token", mymemo_token)):
            if value is not None:
                set_secret(raw, name, value)
        if enabled is not None:
            raw["enabled"] = bool(enabled)
        _write(raw)
    return load_settings()


def set_secret(raw: dict[str, Any], name: str, value: str) -> None:
    """Put one secret (a ``SECRETS`` field) into the raw settings: the Keychain when it can, else the file; ``""``
    removes it from both. Raises SettingsError for something that isn't a key. Call it under ``_lock``."""
    value = value.strip()
    if len(value) > 4096 or any(c.isspace() for c in value):
        raise SettingsError("That doesn't look like a key.")
    account = SECRETS[name]
    if raw.get(f"{name}_in") == "keychain":
        keychain_delete(account)
    raw.pop(name, None)
    raw.pop(f"{name}_in", None)
    if value and keychain_set(account, value):
        raw[f"{name}_in"] = "keychain"
    elif value:
        raw[name] = value
        raw[f"{name}_in"] = "file"


def _usage_of(raw: Mapping[str, Any]) -> dict[str, Any]:
    """This month's counters from the raw file, without touching the Keychain."""
    s = Settings()
    s.usage = dict(raw["usage"]) if isinstance(raw.get("usage"), dict) else {}
    return s.month_usage()


def record_usage(calls: int = 0, tokens: int = 0) -> dict[str, Any]:
    """Add to this month's counters (a new month starts from zero); returns them."""
    with _lock():
        raw = _read_raw()
        usage = _usage_of(raw)
        usage["calls"] += calls
        usage["tokens"] += tokens
        raw["usage"] = usage
        _write(raw)
        return usage


def reserve_call(caps: Mapping[str, int | None]) -> str | None:
    """Count one call before it is made, unless a monthly limit is reached: returns which (``calls`` or ``tokens``),
    or None when the call was counted. Checked and counted under one lock, so two processes can't both pass the
    last call."""
    with _lock():
        raw = _read_raw()
        usage = _usage_of(raw)
        if caps.get("calls") is not None and usage["calls"] >= caps["calls"]:
            return "calls"
        if caps.get("tokens") is not None and usage["tokens"] >= caps["tokens"]:
            return "tokens"
        usage["calls"] += 1
        raw["usage"] = usage
        _write(raw)
        return None


def ask_session_consent(app: str) -> str | None:
    """What the person said about reading ``app``'s sessions; the first time, records the question as ``asked`` so
    the panel shows it (product-design.md §17.7: asked per app on first use). Returns the answer, or ``asked``."""
    if app not in SESSION_APPS:
        return None
    with _lock():
        raw = _read_raw()
        apps = raw.get("session_apps") if isinstance(raw.get("session_apps"), dict) else {}
        answer = apps.get(app)
        if answer in CONSENT:
            return answer
        raw["session_apps"] = {**apps, app: "asked"}
        _write(raw)
        return "asked"
