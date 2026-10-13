"""MyMemo Cloud: the optional connector that lets ChatGPT and claude.ai on the web use this folder
(product-design.md §16).

Nothing here runs until the person links this computer to their MyMemo account. From then on, the process that
serves the panel for the linked folder keeps one outbound WebSocket to the service (the bridge) and answers the
tool calls the service relays from web apps, by running the same tool code as the local MCP server, as connector
``cloud-<slug>`` ("ChatGPT (web)", "Claude (web)", …). Every write is a proposal, as for any app.

* **The link** is per computer, never in the folder (the folder may be synced or kept in git): ``cloud.json``,
  mode 0600, in ``OPENMYMEMO_CLOUD_DIR`` or the per-user application folder (``cloud_dir``). It holds the service
  URL, the device id and token, the X25519 private key, its key id, the folder it serves, the account's display
  name and the plan.
* **Linking** is OAuth with PKCE and a loopback redirect (RFC 8252): the browser comes back to the panel's
  ``/cloud/callback`` (or to a short-lived listener of the CLI's own), whose ``state`` must match a link started
  in this process within ten minutes, once. The code is exchanged at ``POST /openmymemo/device/link``, which also
  registers the public key.
* **The service** is ``OPENMYMEMO_CLOUD_URL`` (default https://api.mymemo.ai); the bridge is ``/openmymemo/bridge``
  on the same host. The wire format is pinned by ``tests/fixtures/cloud/`` (shared with mymemo-service).
* **The bridge** runs on a thread of its own, starts once the panel's port is bound (``web.PanelServer``) and
  closes with 1000 when the panel stops. It reconnects with backoff and follows the close codes in
  ``tests/fixtures/cloud/bridge/close_codes.json``.

The service never sees anything at rest in readable form, and nothing here logs tool arguments or results.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import hashlib
import html
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from . import __version__
from .connectors import Connector, Connectors, Revoked
from .vault import NotFound, Vault, VaultError, now_iso

log = logging.getLogger("openmymemo.cloud")

DEFAULT_SERVICE = "https://api.mymemo.ai"
CLIENT_ID = "openmymemo-desktop"
SUBPROTOCOL = "openmymemo-bridge.v1"
LINK_TTL = 600.0  # seconds a started link waits for its callback
MAX_PENDING = 8
MAX_INFLIGHT = 4
MAX_FRAME = 1 << 20  # what the service accepts from the computer
RESULT_MAX_BYTES = 512 * 1024  # until `welcome` says otherwise
TOOL_NAMES = ("memory_context", "memory_search", "memory_get", "memory_propose", "memory_proposals")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
CLOUD_PREFIX = "cloud-"
TOO_LARGE = "The answer from your computer was too large to relay. Ask for less (fewer ids, a smaller max_chars)."


class CloudError(Exception):
    """Something the person should read: MyMemo couldn't be reached, or refused."""


# ---- where the link lives ----------------------------------------------------------------------------------


def service_url() -> str:
    """The MyMemo service this installation links to: ``OPENMYMEMO_CLOUD_URL``, or MyMemo's own."""
    return (os.environ.get("OPENMYMEMO_CLOUD_URL", "").strip() or DEFAULT_SERVICE).rstrip("/")


def endpoint(service: str) -> str:
    """The URL a person pastes into ChatGPT or claude.ai, exactly (Claude compares it, so no trailing slash)."""
    return f"{service.rstrip('/')}/openmymemo/mcp"


def cloud_dir(environ: Mapping[str, str] | None = None, system: str | None = None, home: Path | None = None) -> Path:
    """The folder that holds ``cloud.json``: per computer and per user, never inside the memory folder."""
    env = os.environ if environ is None else environ
    system = system or sys.platform
    home = home or Path.home()
    if env.get("OPENMYMEMO_CLOUD_DIR", "").strip():
        return Path(env["OPENMYMEMO_CLOUD_DIR"].strip()).expanduser()
    if system == "darwin":
        return home / "Library" / "Application Support" / "OpenMyMemo"
    if system == "win32":
        return (Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming") / "OpenMyMemo"
    return (Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config") / "openmymemo"


def link_path() -> Path:
    return cloud_dir() / "cloud.json"


@dataclass
class Link:
    service: str
    device_id: str
    device_token: str
    private_key: str  # raw X25519 private key, base64url
    key_id: str
    vault: str  # the folder this link serves
    member: dict = field(default_factory=dict)  # {"display_name"}
    plan: dict = field(default_factory=dict)  # {"tier", "live", "queue"}
    linked_at: str = ""

    @property
    def display_name(self) -> str:
        return str(self.member.get("display_name") or "")

    def serves(self, vault_root: Path) -> bool:
        return same_folder(self.vault, vault_root)

    def to_json(self) -> str:
        return json.dumps({"format": 1, **asdict(self)}, ensure_ascii=False, indent=1) + "\n"


def same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def load_link() -> Link | None:
    try:
        data = json.loads(link_path().read_text(encoding="utf-8"))
        return Link(**{k: data[k] for k in Link.__dataclass_fields__ if k in data})
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as e:
        log.warning("MyMemo Cloud: ignoring an unreadable %s (%s)", link_path(), type(e).__name__)
        return None


def _write_private(path: Path, text: str) -> None:
    """Atomic, and readable by this user only from the first byte (it holds a token and a private key)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            if hasattr(os, "fchmod"):
                os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save_link(link: Link) -> None:
    _write_private(link_path(), link.to_json())


def delete_link(device_id: str | None = None) -> bool:
    """Delete ``cloud.json``; with `device_id`, only while it still belongs to that device (a newer link stays)."""
    path = link_path()
    if device_id is not None:
        current = load_link()
        if current is None or current.device_id != device_id:
            return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _update_link(device_id: str, **changes: Any) -> None:
    current = load_link()
    if current is None or current.device_id != device_id:
        return
    if all(getattr(current, k) == v for k, v in changes.items()):
        return
    for k, v in changes.items():
        setattr(current, k, v)
    save_link(current)


# ---- keys and PKCE -----------------------------------------------------------------------------------------


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pkce_challenge(verifier: str) -> str:
    return b64u(hashlib.sha256(verifier.encode()).digest())


def key_id(public_key: bytes) -> str:
    """The first 16 hex characters of SHA-256 of the raw public key (what `hello` and `item` carry)."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def new_key_pair() -> tuple[str, str]:
    """A fresh X25519 key pair as base64url raw bytes: (private, public)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    private = X25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64u(raw), b64u(public)


def device_name() -> str:
    """What the MyMemo consent page calls this computer: the Mac's name, or the host name."""
    name = ""
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                                  timeout=2, stdin=subprocess.DEVNULL).stdout.strip()
    return (name or socket.gethostname() or "Computer")[:64]


def authorize_url(service: str, redirect_uri: str, challenge: str, state: str, name: str) -> str:
    params = {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect_uri,
        "resource": f"{service}/openmymemo/bridge", "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "device_name": name,
    }
    return f"{service}/openmymemo/oauth/authorize?{urlencode(params, quote_via=quote)}"


def link_request(code: str, verifier: str, redirect_uri: str, public_key: str, app_version: str = __version__) -> dict:
    """The body of ``POST /openmymemo/device/link`` (tests/fixtures/cloud/link/request.json)."""
    return {"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri, "public_key": public_key,
            "key_alg": "x25519", "app_version": app_version}


# ---- talking to the service over HTTP ------------------------------------------------------------------------


def _loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request(method: str, url: str, *, token: str | None = None, body: dict | None = None,
             timeout: float = 20.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": f"OpenMyMemo/{__version__}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # A local service (tests, development) is never reached through a proxy.
    opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if _loopback(url) else []))
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise CloudError(f"MyMemo couldn't be reached ({getattr(e, 'reason', None) or type(e).__name__}).") from None
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    return status, payload


def _service_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        for key in ("message", "detail", "error_description", "error"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()[:300]
    return f"HTTP {status}"


# ---- linking -------------------------------------------------------------------------------------------------


@dataclass
class PendingLink:
    state: str
    verifier: str
    private_key: str
    public_key: str
    redirect_uri: str
    service: str
    vault: str
    started: float


@dataclass
class Outcome:
    """What a callback did, for the browser tab (`title`, `message`) and the caller (`link`)."""

    status: int
    title: str
    message: str
    link: Link | None = None
    used: bool = False  # it answered a link this process started (so that link is over)


class Linker:
    """Links started in this process. Each ``state`` answers one callback, within ten minutes."""

    def __init__(self, ttl: float = LINK_TTL):
        self.ttl = ttl
        self._pending: dict[str, PendingLink] = {}
        self._lock = threading.Lock()
        self.error: str | None = None  # why the last link didn't work, for the panel

    def _expire(self) -> None:
        now = time.monotonic()
        for state in [s for s, p in self._pending.items() if now - p.started > self.ttl]:
            del self._pending[state]

    def begin(self, vault_root: Path, redirect_uri: str, service: str | None = None) -> str:
        """Start a link: a PKCE pair, a state and a key pair; returns the authorize URL to open in a browser."""
        service = (service or service_url()).rstrip("/")
        verifier = secrets.token_urlsafe(48)
        private, public = new_key_pair()
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._expire()
            while len(self._pending) >= MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[state] = PendingLink(state, verifier, private, public, redirect_uri, service,
                                               str(vault_root), time.monotonic())
            self.error = None
        return authorize_url(service, redirect_uri, pkce_challenge(verifier), state, device_name())

    def pending(self) -> bool:
        with self._lock:
            self._expire()
            return bool(self._pending)

    def _take(self, state: str) -> PendingLink | None:
        with self._lock:
            self._expire()
            return self._pending.pop(state, None) if state else None

    def finish(self, params: Mapping[str, str], before_exchange: Callable[[], None] | None = None) -> Outcome:
        """Answer the browser coming back: check the state, exchange the code. The caller saves the link."""
        p = self._take(params.get("state") or "")
        if p is None:
            return Outcome(400, "This sign-in link isn't valid anymore",
                           "It was already used, or it is more than ten minutes old. Start again in OpenMyMemo: "
                           "Connectors → MyMemo Cloud → Sign in.")
        outcome = self._exchange(p, params, before_exchange)
        outcome.used = True
        if outcome.link is None:
            self.error = outcome.message
        return outcome

    def _exchange(self, p: PendingLink, params: Mapping[str, str],
                  before_exchange: Callable[[], None] | None) -> Outcome:
        if params.get("error"):
            why = params.get("error_description") or params["error"]
            if params["error"] == "access_denied":
                return Outcome(400, "Linking was cancelled", "Nothing was linked. You can close this tab.")
            return Outcome(400, "MyMemo didn't link this computer", f"{why[:300]}. You can close this tab.")
        iss = (params.get("iss") or "").rstrip("/")
        if iss and iss != p.service:
            return Outcome(400, "MyMemo didn't link this computer",
                           "The answer came from an unexpected server, so OpenMyMemo ignored it.")
        code = params.get("code") or ""
        if not code:
            return Outcome(400, "MyMemo didn't link this computer", "The answer had no code.")
        if before_exchange:
            before_exchange()
        try:
            status, data = _request("POST", f"{p.service}/openmymemo/device/link",
                                    body=link_request(code, p.verifier, p.redirect_uri, p.public_key))
        except CloudError as e:
            return Outcome(502, "MyMemo couldn't be reached", str(e))
        ok = (status in (200, 201) and isinstance(data, dict) and isinstance(data.get("device_id"), str)
              and isinstance(data.get("device_token"), str))
        if not ok:
            return Outcome(502, "MyMemo didn't link this computer", _service_message(data, status))
        member = data.get("member") if isinstance(data.get("member"), dict) else {}
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        link = Link(service=p.service, device_id=data["device_id"], device_token=data["device_token"],
                    private_key=p.private_key, key_id=key_id(unb64u(p.public_key)), vault=p.vault,
                    member={"display_name": str(member.get("display_name") or "")},
                    plan={k: plan[k] for k in ("tier", "live", "queue") if k in plan}, linked_at=now_iso())
        who = f"{link.display_name}'s" if link.display_name else "your"
        return Outcome(200, "Linked", f"This computer is linked to {who} MyMemo account. You can close this tab.",
                       link=link)


def callback_page(outcome: Outcome) -> str:
    title, message = html.escape(outcome.title), html.escape(outcome.message)
    mark = "✓" if outcome.link else "!"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · OpenMyMemo</title>
<style>
:root {{ color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
main {{ max-width: 420px; padding: 32px 24px; text-align: center; }}
.mark {{ font-size: 32px; }}
h1 {{ font-size: 20px; margin: 12px 0 8px; }}
p {{ margin: 0; line-height: 1.5; opacity: .75; }}
</style></head>
<body><main><div class="mark" aria-hidden="true">{mark}</div><h1>{title}</h1><p>{message}</p></main></body></html>
"""


CALLBACK_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


def revoke_web_apps(vault: Vault) -> list[str]:
    """Revoke every ``cloud-*`` connector, keeping tombstones (§5.5): after unlinking, nothing comes back silently."""
    cons = Connectors(vault)
    ids = [c.id for c in cons.list() if c.kind == "cloud"]
    for cid in ids:
        with contextlib.suppress(NotFound):
            cons.remove(cid)
    return ids


def _linked_vault(link: Link) -> Vault | None:
    v = Vault(link.vault)
    return v if v.is_initialized() else None


def unlink() -> str:
    """Tell MyMemo to forget this computer, delete ``cloud.json`` and revoke the web apps. Returns what happened."""
    link = load_link()
    if link is None:
        return "This computer isn't linked to MyMemo Cloud."
    try:
        status, data = _request("DELETE", f"{link.service}/openmymemo/device", token=link.device_token)
        reached = status in (200, 202, 204, 401, 403, 404)  # 401/403/404: MyMemo had already forgotten it
        why = "" if reached else f" ({_service_message(data, status)})"
    except CloudError as e:
        reached, why = False, f" ({e})"
    delete_link(link.device_id)
    vault = _linked_vault(link)
    if vault is not None:
        revoke_web_apps(vault)
    if reached:
        return "Unlinked this computer from MyMemo Cloud. ChatGPT and claude.ai on the web can't reach your memory anymore."
    return ("Unlinked this computer here, but MyMemo couldn't be told" + why.rstrip(".") + ". It stops working either "
            "way; remove it from your MyMemo account settings too.")


# ---- the bridge ------------------------------------------------------------------------------------------------


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with full jitter: uniform in [0, min(cap, base·2^attempt)]."""
    return rand() * min(cap, base * 2 ** min(attempt, 32))


def web_name(client: Mapping[str, Any]) -> str:
    """The local connector's name for a web app: "ChatGPT (web)", or "… (web, unverified)"."""
    name = " ".join(str(client.get("name") or client.get("slug") or "Web app").split())[:60]
    if client.get("verified") is False:
        return f"{name.removesuffix(' (unverified)')} (web, unverified)"
    return f"{name} (web)"


def _error(call_id: str, code: str, message: str) -> dict:
    return {"type": "result", "id": call_id, "error": {"code": code, "message": message}}


def _parse(raw: Any) -> dict | None:
    if not isinstance(raw, str):
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


@dataclass
class Timing:
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    reset_after: float = 60.0  # connected this long: the next backoff starts from the beginning again
    plan_retry: float = 3600.0  # after 4403
    restart_jitter: float = 5.0  # after 1012 without a `bye`
    open_timeout: float = 20.0
    welcome_timeout: float = 20.0


class Bridge:
    """One outbound WebSocket to the service while linked; runs in its own event loop (``Cloud`` gives it a thread)."""

    def __init__(self, vault: Vault, link: Link, timing: Timing | None = None):
        from .mcp_server import build_server

        self.vault = vault
        self.link = link
        self.timing = timing or Timing()
        self.state = "connecting"  # connecting | online | waiting | stopped
        self.reason: str | None = None  # offline | plan | replaced | unlinked | key | closed
        self.close_code: int | None = None
        self.welcome: dict | None = None
        self.queued: int | None = None
        self.last_call_at: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started = threading.Event()
        self._caller: ContextVar[Connector | None] = ContextVar(f"openmymemo-cloud-{id(self)}", default=None)
        self._server = build_server(vault, relayed=self._current)
        self._ws: Any = None
        self._session_task: asyncio.Task | None = None
        self._stopping = False
        self._wake: asyncio.Event | None = None
        self._tasks: set[asyncio.Task] = set()
        self._bye_ms: float | None = None
        self._result_cap = RESULT_MAX_BYTES
        self._heartbeat = 25.0

    # -- what the service is told --

    async def tools(self) -> list[dict]:
        return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in await self._server.list_tools()]

    async def hello(self) -> dict:
        from .mcp_server import INSTRUCTIONS

        return {"type": "hello", "v": 1, "app_version": __version__, "key_id": self.link.key_id,
                "max_inflight": MAX_INFLIGHT, "tools": await self.tools(), "instructions": INSTRUCTIONS}

    # -- lifecycle --

    def _current(self) -> Connector:
        c = self._caller.get()
        if c is None:
            from mcp.server.mcpserver.exceptions import ToolError

            raise ToolError("No MyMemo Cloud call in progress.")
        return c

    def _set(self, state: str, reason: str | None = None) -> None:
        self.state, self.reason = state, reason

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._inflight = asyncio.Semaphore(MAX_INFLIGHT)
        self._send_lock = asyncio.Lock()
        self.started.set()
        attempt = 0
        try:
            while not self._stopping:
                current = load_link()
                if current is None or current.device_id != self.link.device_id:
                    return self._set("stopped", "unlinked")
                self.link = current
                self._set("connecting", self.reason if self.reason in ("offline", "plan") else None)
                self._bye_ms = None
                self._session_task = task = asyncio.create_task(self._session())
                await asyncio.wait({task})
                if task.cancelled() or self._stopping:
                    break
                code, online_for = None, 0.0
                try:
                    code, online_for = task.result()
                except Exception as e:  # noqa: BLE001 - a failed connection is ordinary; only its kind is logged
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in (401, 403):  # refused before the upgrade: the device token is no longer valid
                        log.info("MyMemo Cloud: the service refused this computer (HTTP %s); unlinking", status)
                        self._forget()
                        return self._set("stopped", "unlinked")
                    log.info("MyMemo Cloud: couldn't connect (%s%s)", type(e).__name__, f" {status}" if status else "")
                self.close_code = code
                if online_for >= self.timing.reset_after:
                    attempt = 0
                if code == 4401:
                    self._forget()
                    return self._set("stopped", "unlinked")
                if code == 4409:
                    return self._set("stopped", "replaced")
                if code == 4410:
                    return self._set("stopped", "key")
                if code == 4403:
                    self._set("waiting", "plan")
                    attempt = 0
                    await self._sleep(self.timing.plan_retry)
                    continue
                if code == 1012:
                    delay = (self._bye_ms / 1000 if self._bye_ms is not None
                             else random.uniform(0, self.timing.restart_jitter))
                    attempt = 0
                else:
                    delay = backoff_delay(attempt, self.timing.backoff_base, self.timing.backoff_cap)
                    attempt += 1
                self._set("waiting", "offline")
                await self._sleep(delay)
        finally:
            if self.state != "stopped":
                self._set("stopped", self.reason if self.reason in ("unlinked", "replaced", "key") else "closed")

    def _forget(self) -> None:
        """The service no longer knows this computer: delete our link (if it is still ours) and revoke the web apps."""
        if delete_link(self.link.device_id):
            with contextlib.suppress(VaultError, OSError):
                revoke_web_apps(self.vault)

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, seconds))

    def wake(self) -> None:
        """Retry now instead of waiting out a backoff or the hourly plan check (thread-safe)."""
        if self.loop and self._wake and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._wake.set)

    async def close(self, code: int = 1000) -> None:
        self._stopping = True
        if self._wake:
            self._wake.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code, "OpenMyMemo is closing")
        elif self._session_task and not self._session_task.done():
            self._session_task.cancel()

    # -- one connection --

    def _ws_url(self) -> str:
        url = urlsplit(self.link.service)
        scheme = "wss" if url.scheme == "https" else "ws"
        return f"{scheme}://{url.netloc}{url.path.rstrip('/')}/openmymemo/bridge"

    async def _session(self) -> tuple[int | None, float]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        extra = {"proxy": None} if _loopback(self.link.service) else {}
        async with connect(self._ws_url(), additional_headers={"Authorization": f"Bearer {self.link.device_token}"},
                           subprotocols=[SUBPROTOCOL], user_agent_header=f"OpenMyMemo/{__version__}",
                           compression=None, open_timeout=self.timing.open_timeout, max_size=MAX_FRAME,
                           close_timeout=5, **extra) as ws:
            self._ws = ws
            try:
                await self._send(ws, await self.hello())
                try:
                    msg = _parse(await asyncio.wait_for(ws.recv(), timeout=self.timing.welcome_timeout))
                except ConnectionClosed:
                    return ws.close_code, 0.0
                except TimeoutError:
                    msg = None
                if not msg or msg.get("type") != "welcome":
                    await ws.close(4400, "expected welcome")
                    return ws.close_code, 0.0
                self._on_welcome(msg)
                since = time.monotonic()
                self._set("online")
                log.info("MyMemo Cloud: connected")
                await self._serve(ws)
                return ws.close_code, time.monotonic() - since
            finally:
                self._ws = None

    def _on_welcome(self, msg: dict) -> None:
        self.welcome = msg
        limits = msg.get("limits") if isinstance(msg.get("limits"), dict) else {}
        with contextlib.suppress(TypeError, ValueError):
            self._result_cap = int(limits.get("result_max_bytes") or RESULT_MAX_BYTES)
        with contextlib.suppress(TypeError, ValueError):
            self._heartbeat = float(msg.get("heartbeat_s") or 25)
        queued = msg.get("queued")
        self.queued = queued if isinstance(queued, int) and queued >= 0 else None
        member = msg.get("member") if isinstance(msg.get("member"), dict) else None
        plan = msg.get("plan") if isinstance(msg.get("plan"), dict) else None
        changes: dict[str, Any] = {}
        if member and isinstance(member.get("display_name"), str):
            changes["member"] = {"display_name": member["display_name"]}
        if plan:
            changes["plan"] = {k: plan[k] for k in ("tier", "live", "queue") if k in plan}
        if changes:
            with contextlib.suppress(OSError):
                _update_link(self.link.device_id, **changes)

    async def _serve(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        while True:
            # The service pings every `heartbeat_s`; silence much longer than that means the connection is gone.
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._heartbeat * 2 + 10)
            except TimeoutError:
                log.info("MyMemo Cloud: no heartbeat from the service; reconnecting")
                with contextlib.suppress(Exception):
                    await ws.close(4408, "no heartbeat")
                return
            except ConnectionClosed:
                return
            msg = _parse(raw)
            if msg is None:
                continue
            kind = msg.get("type")
            if kind == "call":
                self._spawn(self._call(ws, msg))
            elif kind == "ping":
                await self._send(ws, {"type": "pong", "t": msg.get("t")})
            elif kind == "bye":
                retry = msg.get("retry_after_ms")
                self._bye_ms = float(retry) if isinstance(retry, (int, float)) and retry >= 0 else None
            elif kind == "welcome":
                self._on_welcome(msg)
            # anything else (pong, frames from a newer protocol) is ignored

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, ws, frame: dict) -> None:
        from websockets.exceptions import ConnectionClosed

        with contextlib.suppress(ConnectionClosed):
            async with self._send_lock:
                await ws.send(json.dumps(frame, ensure_ascii=False))

    # -- relayed calls --

    async def _call(self, ws, msg: dict) -> None:
        call_id = msg.get("id")
        if not isinstance(call_id, str) or not call_id:
            return
        deadline = msg.get("deadline_ms")
        timeout = deadline / 1000 if isinstance(deadline, (int, float)) and deadline > 0 else None
        async with self._inflight:
            try:
                frame = await asyncio.wait_for(self.answer(call_id, msg), timeout)
            except TimeoutError:  # the service has answered "didn't answer in time" by now
                log.info("MyMemo Cloud: a relayed call outlived its deadline")
                return
            except Exception as e:  # noqa: BLE001 - never logs arguments or results, only what kind of failure
                log.warning("MyMemo Cloud: a relayed call failed (%s)", type(e).__name__)
                frame = _error(call_id, "internal", "OpenMyMemo couldn't answer this call.")
        await self._send(ws, frame)

    def _connector(self, client: Mapping[str, Any]) -> Connector:
        cid = CLOUD_PREFIX + str(client["slug"])
        cons = Connectors(self.vault)
        try:
            return cons.ensure(cid, web_name(client), kind="cloud")
        except Revoked:
            known = next((c for c in cons._load() if c.id == cid), None)
            name = known.name if known else web_name(client)
            raise Revoked(f"The person revoked {name}'s access in OpenMyMemo.") from None

    async def answer(self, call_id: str, msg: Mapping[str, Any]) -> dict:
        """Run one relayed ``tools/call`` as the web app's connector; the result frame for the service."""
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
        from mcp_types import CallToolResult, TextContent

        client, params = msg.get("client"), msg.get("params")
        if not isinstance(client, dict) or not isinstance(params, dict):
            return _error(call_id, "bad_request", "The call needs a client and params.")
        if not isinstance(client.get("slug"), str) or not SLUG.fullmatch(client["slug"]):
            return _error(call_id, "bad_request", "The call names no valid app.")
        name, args = params.get("name"), params.get("arguments")
        if name not in TOOL_NAMES:
            return _error(call_id, "bad_request", "OpenMyMemo has no such tool.")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _error(call_id, "bad_request", "The tool's arguments must be an object.")
        try:
            connector = await asyncio.to_thread(self._connector, client)
        except Revoked as e:
            return _error(call_id, "revoked", str(e))
        token = self._caller.set(connector)
        try:
            result = await self._server.call_tool(name, args)
        except UnexpectedToolError:
            raise
        except ToolError as e:  # what the local MCP server answers too: the model reads it
            result = CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
        finally:
            self._caller.reset(token)
        self.last_call_at = now_iso()
        data = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        # The fields of a 2025-11-25 CallToolResult (result_ok.json); the SDK also adds the newer `resultType`.
        frame = {"type": "result", "id": call_id,
                 "result": {k: data[k] for k in ("content", "structuredContent", "isError") if k in data}}
        if len(json.dumps(frame, ensure_ascii=False).encode()) > self._result_cap:
            frame["result"] = {"content": [{"type": "text", "text": TOO_LARGE}], "isError": True}
        return frame


# ---- one per panel process -------------------------------------------------------------------------------------


class Cloud:
    """MyMemo Cloud in the process that serves the panel for `vault` on `port`: linking, the bridge, status."""

    def __init__(self, vault: Vault, port: int, timing: Timing | None = None):
        self.vault = vault
        self.port = port
        self.timing = timing or Timing()
        self.linker = Linker()
        self._bridge: Bridge | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._serving = False
        self._atexit = False

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/cloud/callback"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the bridge if this computer is linked to this folder and it isn't running yet (thread-safe)."""
        with self._lock:
            self._serving = True
            if self.running:
                return True
            link = load_link()
            if link is None or not link.serves(self.vault.root):
                return False
            bridge = Bridge(self.vault, link, self.timing)
            thread = threading.Thread(target=lambda: asyncio.run(bridge.run()), name="openmymemo-cloud", daemon=True)
            self._bridge, self._thread = bridge, thread
            thread.start()
            bridge.started.wait(5)
            if not self._atexit:
                atexit.register(self.stop)  # a panel served from an AI app's MCP process ends with that process
                self._atexit = True
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Close the bridge with 1000 and wait for it (thread-safe; nothing happens when it isn't running)."""
        with self._lock:
            bridge, thread = self._bridge, self._thread
            self._thread = None
        if bridge is None or thread is None:
            return
        loop = bridge.loop
        if thread.is_alive() and loop is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(bridge.close(1000), loop).result(timeout)
        thread.join(timeout)

    def reconnect(self) -> None:
        """The panel asks to try again (after a plan change, or to take over from another OpenMyMemo)."""
        with self._lock:
            if self.running and self._bridge is not None:
                self._bridge.wake()
                return
        self.start()

    # -- linking --

    def begin_link(self) -> str:
        return self.linker.begin(self.vault.root, self.redirect_uri)

    def callback(self, params: Mapping[str, str]) -> Outcome:
        """The browser came back to ``/cloud/callback``. A new link replaces the old one, bridge included."""
        outcome = self.linker.finish(params, before_exchange=self.stop)  # the old bridge mustn't see the kick
        if outcome.link is not None:
            save_link(outcome.link)
        if outcome.used:
            self.start()  # the new link, or the old one again when linking failed
        return outcome

    def unlink(self) -> str:
        self.stop()
        message = unlink()
        with self._lock:
            self._bridge = None
        return message

    # -- status --

    def status(self) -> dict:
        link = load_link()
        service = link.service if link else service_url()
        out: dict[str, Any] = {
            "linked": link is not None, "service": service, "endpoint": endpoint(service),
            "pending": self.linker.pending(), "link_error": self.linker.error,
            "folder": None, "this_folder": False, "account": None, "plan": None, "linked_at": None,
            "state": "stopped", "reason": None, "online": False, "close_code": None, "last_call_at": None,
            "queued": None,
        }
        if link is None:
            return out
        out.update(folder=link.vault, this_folder=link.serves(self.vault.root), linked_at=link.linked_at,
                   account={"display_name": link.display_name}, plan=link.plan)
        if not out["this_folder"]:
            out["reason"] = "folder"
            return out
        with self._lock:
            bridge = self._bridge
            stale = bridge is None or (bridge.link.device_id != link.device_id and not self.running)
            if self._serving and stale:  # linked by another process (the CLI) while this panel was running
                self.start()
                bridge = self._bridge
        if bridge is None or bridge.link.device_id != link.device_id:
            out["reason"] = "not_running"
            return out
        out.update(state=bridge.state, reason=bridge.reason, online=bridge.state == "online",
                   close_code=bridge.close_code, last_call_at=bridge.last_call_at, queued=bridge.queued)
        return out


# ---- linking from the CLI without a panel ----------------------------------------------------------------------


def link_with_listener(vault: Vault, open_url: Callable[[str], Any], timeout: float = LINK_TTL,
                       service: str | None = None) -> Outcome:
    """Link this computer when no panel is running: a short-lived listener on 127.0.0.1 takes the callback."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qsl

    linker = Linker(ttl=timeout)
    done = threading.Event()
    result: dict[str, Outcome] = {}
    hosts: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str, headers: Mapping[str, str] | None = None) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.headers.get("Host", "") not in hosts:
                return self._reply(421, "Unexpected Host header")
            url = urlsplit(self.path)
            if url.path != "/cloud/callback":
                return self._reply(404, "Not found")
            outcome = linker.finish(dict(parse_qsl(url.query)))
            if outcome.link is not None:
                save_link(outcome.link)
            self._reply(outcome.status, callback_page(outcome), CALLBACK_HEADERS)
            if outcome.used:
                result["outcome"] = outcome
                done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet; the URL holds the code
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    hosts.update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    url = linker.begin(vault.root, f"http://127.0.0.1:{port}/cloud/callback", service)
    thread = threading.Thread(target=server.serve_forever, name="openmymemo-cloud-link", daemon=True)
    thread.start()
    try:
        open_url(url)
        if not done.wait(timeout):
            return Outcome(408, "Timed out", "Nobody signed in within ten minutes. Run `openmymemo cloud link` again.")
        return result["outcome"]
    finally:
        server.shutdown()
        server.server_close()


def plan_label(plan: Mapping[str, Any] | None) -> str:
    """ "Ultra · live relay and offline queue" for the CLI."""
    if not plan:
        return ""
    tier = str(plan.get("tier") or "").replace("_", " ")
    tier = {"pro": "Pro", "ultra": "Ultra", "free": "Free", "appsumo tier 3": "AppSumo tier 3"}.get(tier, tier.title())
    modes = [m for m, on in (("live relay", plan.get("live")), ("offline queue", plan.get("queue"))) if on]
    return " · ".join(x for x in (tier, " and ".join(modes)) if x)

