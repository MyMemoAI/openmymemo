"""MyMemo's hosted model for the Assistant (product-design.md §17.8): signing in, its tokens, and how its answers
change what the Assistant does. The endpoint itself is OpenAI-compatible and called by ``client.py`` like any other.

* **Signing in** reuses MyMemo Cloud's machinery (``cloud.py``): the service URL setting, PKCE with a loopback
  redirect to ``/cloud/callback`` and the ``iss`` check (``Linker``), and the per-computer folder. A computer already
  linked to MyMemo Cloud needs no browser: its device token is exchanged for an ``assistant`` token (RFC 8693).
  Otherwise the browser signs in with ``scope=assistant offline_access``. The token is scoped to the model endpoint.
* **Tokens** are kept like the API key (``settings.py``): the Keychain on macOS when possible, else the 0600
  ``assistant.json``. An access token is refreshed shortly before it expires and when the endpoint answers 401; the
  refresh token rotates with each refresh. Refreshing happens under the settings lock, so two processes never spend
  the same refresh token. A refresh MyMemo refuses signs this computer out.
* **Pauses.** MyMemo answers 402 when the account has no Assistant plan (``plan_required``) or this month's
  allowance is used up (``quota_exceeded``), and 429 or 503 with ``Retry-After`` when busy. Each is kept as a
  ``hold`` with an end time; until then the Assistant makes no request and every feature takes its rule-based path
  quietly. The panel's plan view (``account``) lifts a ``plan_required`` hold as soon as the plan is active.
* **Signing out** revokes the grant at MyMemo and forgets the tokens.

Prices, allowances and plan details are the service's; this file only passes on what it answers.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import cloud
from ..cloud import CLIENT_ID, CloudError, Linker, Outcome, PendingLink, member_fields
from . import settings as st
from .settings import MYMEMO_PATH, Settings, SettingsError

log = logging.getLogger("openmymemo.assistant")

SCOPE = "assistant"
SIGNIN_SCOPE = "assistant offline_access"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
DEVICE_TOKEN_TYPE = "urn:openmymemo:params:oauth:token-type:device_token"
EARLY = 60  # seconds before expiry that a token is refreshed
PLAN_HOLD = timedelta(hours=1)  # without a plan, ask again after this (or as soon as the panel sees the plan)
QUOTA_HOLD_DEFAULT = timedelta(days=1)  # a used-up allowance without a reset time
HOLD_MAX = timedelta(days=32)
RETRY_DEFAULT = {"rate_limited": 60, "unavailable": 30}
RETRY_MAX = 3600
SIGNED_OUT = "Signed out of MyMemo. Sign in again in Settings → Assistant."


def resource(service: str) -> str:
    return f"{service.rstrip('/')}/openmymemo/assistant"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def service_of(s: Settings) -> str:
    """The service this computer signed in to, else the one a sign-in would use (MyMemo Cloud's setting)."""
    service = s.mymemo.get("service")
    return (service if isinstance(service, str) and service else cloud.service_url()).rstrip("/")


# ---- talking to the OAuth endpoints ------------------------------------------------------------------------------


def _oauth(service: str, path: str, form: dict[str, str]) -> tuple[int, Any]:
    """POST a form to MyMemo's OAuth endpoints; raises CloudError when MyMemo can't be reached."""
    return cloud._request("POST", f"{service}/openmymemo/oauth/{path}", form=form, follow_redirects=False)


def _tokens(status: int, data: Any) -> dict | None:
    """A token response as kept, or None when it isn't a usable one."""
    if status != 200 or not isinstance(data, dict):
        return None
    access, refresh = data.get("access_token"), data.get("refresh_token")
    expires = data.get("expires_in")
    if (not isinstance(access, str) or not access or str(data.get("token_type", "")).lower() != "bearer"
            or (refresh is not None and not isinstance(refresh, str))):
        return None
    if isinstance(expires, bool) or not isinstance(expires, int | float) or expires <= 0:
        expires = 3600
    scopes = str(data.get("scope") or SCOPE).split()
    if SCOPE not in scopes:
        return None
    return {"access": access, "refresh": refresh or "", "expires_in": min(float(expires), 86400.0)}


def _error_code(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str):
            return err
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return err["code"]
    return ""


def _store(raw: dict[str, Any], service: str, tokens: Mapping[str, Any], *, via: str | None = None,
           account: Mapping[str, Any] | None = None) -> None:
    """Put fresh tokens into the raw settings (under the settings lock). Keeps the refresh token when a refresh
    answer carries none. Raises SettingsError for tokens that can't be stored."""
    st.set_secret(raw, "mymemo_token", tokens["access"])
    if tokens.get("refresh") or via is not None:
        st.set_secret(raw, "mymemo_refresh", tokens.get("refresh") or "")
    current = raw.get("mymemo") if isinstance(raw.get("mymemo"), dict) else {}
    info = dict(current) if via is None else {"via": via, "signed_in_at": _iso(_now()),
                                               "account": member_fields(account or {})}
    info.update(service=service, expires_at=_iso(_now() + timedelta(seconds=tokens["expires_in"])))
    info.pop("signed_out", None)
    raw["mymemo"] = info


def keep(service: str, tokens: Mapping[str, Any], *, via: str, account: Mapping[str, Any] | None = None) -> None:
    """Save a new sign-in (replacing any earlier one)."""
    with st._lock():
        raw = st._read_raw()
        _store(raw, service, tokens, via=via, account=account)
        st._write(raw)


def _forget(raw: dict[str, Any], reason: str | None) -> None:
    for name in ("mymemo_token", "mymemo_refresh"):
        st.set_secret(raw, name, "")
    info = raw.get("mymemo") if isinstance(raw.get("mymemo"), dict) else {}
    raw["mymemo"] = {k: v for k, v in info.items() if k == "service"} | ({"signed_out": reason} if reason else {})


# ---- signing in --------------------------------------------------------------------------------------------------


EXCHANGE_MESSAGES = {
    "signed_in": "Signed in to MyMemo with this computer's MyMemo Cloud link.",
    "use_browser": "MyMemo no longer knows this computer's link. Sign in with the browser instead.",
    "unreachable": "MyMemo couldn't be reached. Check your connection and try again.",
    "busy": "MyMemo is busy. Try again in a minute.",
    "failed": "MyMemo didn't sign this computer in. Try again later.",
}


def exchange(link: cloud.Link) -> str:
    """A computer linked to MyMemo Cloud: trade its device token for an ``assistant`` token, no browser. Returns a
    key of ``EXCHANGE_MESSAGES``: ``signed_in``; ``use_browser`` when MyMemo no longer accepts the device token; or
    why it didn't work."""
    form = {"grant_type": TOKEN_EXCHANGE, "client_id": CLIENT_ID, "subject_token": link.device_token,
            "subject_token_type": DEVICE_TOKEN_TYPE, "scope": SCOPE}
    try:
        status, data = _oauth(link.service, "token", form)
    except CloudError:
        return "unreachable"
    tokens = _tokens(status, data)
    if tokens is None:
        if status in (400, 401) and _error_code(data) in ("invalid_grant", "invalid_token"):
            return "use_browser"
        return "busy" if status == 429 else "failed"
    try:
        keep(link.service, tokens, via="cloud", account=link.member)
    except SettingsError:
        return "failed"
    return "signed_in"


def authorize_url(service: str, redirect_uri: str, challenge: str, state: str, name: str) -> str:
    from urllib.parse import quote, urlencode

    params = {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect_uri,
        "resource": resource(service), "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": SIGNIN_SCOPE, "state": state, "device_name": name,
    }
    return f"{service}/openmymemo/oauth/authorize?{urlencode(params, quote_via=quote)}"


class SignIn(Linker):
    """Browser sign-ins for the Assistant started in this process: PKCE, a loopback redirect to ``/cloud/callback``
    (shared with MyMemo Cloud's linking; the ``state`` tells them apart) and the ``iss`` check, as ``Linker``."""

    STALE = ("It was already used, or it is more than ten minutes old. Start again in OpenMyMemo: "
             "Settings → Assistant → Sign in.")
    FAILED = "MyMemo didn't sign you in"
    CANCELLED = ("Sign-in was cancelled", "Nothing changed. You can close this tab.")

    def begin(self, redirect_uri: str, service: str | None = None) -> str:  # type: ignore[override]
        import secrets
        import time

        service = (service or service_url_for_signin()).rstrip("/")
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._expire()
            while len(self._pending) >= cloud.MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[state] = PendingLink(state, verifier, "", "", redirect_uri, service, "", time.monotonic())
            self.error = None
        return authorize_url(service, redirect_uri, cloud.pkce_challenge(verifier), state, cloud.device_name())

    def _exchange(self, p: PendingLink, params: Mapping[str, str],
                  before_exchange: Callable[[], None] | None) -> Outcome:
        code = self._code(p, params)
        if isinstance(code, Outcome):
            return code
        form = {"grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code,
                "redirect_uri": p.redirect_uri, "code_verifier": p.verifier}
        try:
            status, data = _oauth(p.service, "token", form)
        except CloudError:
            return Outcome(502, "MyMemo couldn't be reached", "Check your connection and sign in again.")
        tokens = _tokens(status, data)
        if tokens is None:
            return Outcome(502, self.FAILED, "MyMemo didn't accept the sign-in. Start again in OpenMyMemo.")
        link = cloud.load_link()
        account = link.member if link and link.service.rstrip("/") == p.service else {}
        who = cloud.account_label(account) if account else "your MyMemo account"
        return Outcome(200, "Signed in to MyMemo",
                       f"The OpenMyMemo Assistant can now use MyMemo's hosted model with {who}. You can close this "
                       "tab.", result={"service": p.service, "tokens": tokens, "account": account})


def service_url_for_signin() -> str:
    """Where a new sign-in goes: the service of this computer's MyMemo Cloud link, else MyMemo Cloud's setting."""
    link = cloud.load_link()
    return (link.service if link else cloud.service_url()).rstrip("/")


def keep_signin(result: Mapping[str, Any]) -> None:
    """``SignIn.finish``'s ``keep``: save the tokens a browser sign-in made."""
    keep(result["service"], result["tokens"], via="browser", account=result.get("account"))


def sign_in_with_browser(open_url: Callable[[str], Any], timeout: float = cloud.LINK_TTL,
                         service: str | None = None) -> Outcome:
    """The CLI's sign-in: a short-lived listener on 127.0.0.1 takes the callback."""
    signin = SignIn(ttl=timeout)
    return cloud.listen_for_callback(
        signin, lambda redirect_uri: signin.begin(redirect_uri, service), keep_signin, open_url, timeout,
        "Nobody signed in within ten minutes. Run `openmymemo assistant signin` again.")


def sign_out(reason: str | None = None) -> str:
    """Revoke the grant at MyMemo (the refresh token, else the access token) and forget both tokens. Returns what
    happened, for the person."""
    s = st.load_settings(environ={})
    service = service_of(s)
    told = True
    token = s.mymemo_refresh or s.mymemo_token
    if token:
        try:
            status, _ = _oauth(service, "revoke", {"token": token, "client_id": CLIENT_ID,
                                                   "token_type_hint": "refresh_token" if s.mymemo_refresh
                                                   else "access_token"})
            told = status in (200, 204)
        except CloudError:
            told = False
    with st._lock():
        raw = st._read_raw()
        _forget(raw, reason)
        st._write(raw)
    if not token:
        return "This computer isn't signed in to MyMemo for the Assistant."
    if told:
        return "Signed out of MyMemo. The Assistant uses its rules until you sign in again or choose another model."
    return ("Signed out here, but MyMemo couldn't be told. The sign-in stops working when it expires; you can also "
            "remove this computer in your MyMemo account settings.")


# ---- tokens in use ---------------------------------------------------------------------------------------------


def refresh(s: Settings) -> str | None:
    """A new access token for ``s`` (updated in place), or None. When another process refreshed meanwhile, its
    token is used instead of spending the refresh token again. A refresh MyMemo refuses signs this computer out
    (``s.mymemo_token`` becomes ""); no network or a busy service leaves everything as it was."""
    if s.key_store == "env":
        return None  # a token from OPENMYMEMO_ASSISTANT_API_KEY is the script's to renew
    used = s.mymemo_token
    with st._lock():
        raw = st._read_raw()
        current = st._from_raw(raw)
        if current.mymemo_token and current.mymemo_token != used:
            s.mymemo_token, s.mymemo, s.mymemo_refresh = current.mymemo_token, current.mymemo, current.mymemo_refresh
            return s.mymemo_token
        service = service_of(current)
        if not current.mymemo_refresh:
            if current.mymemo_token:
                _forget(raw, "expired")
                st._write(raw)
                s.mymemo = dict(raw["mymemo"])
            s.mymemo_token = s.mymemo_refresh = ""
            return None
        try:
            status, data = _oauth(service, "token", {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                                     "refresh_token": current.mymemo_refresh})
        except CloudError:
            return None
        tokens = _tokens(status, data)
        if tokens is not None:
            try:
                _store(raw, service, tokens)
            except SettingsError:
                tokens = None
            else:
                st._write(raw)
                fresh = st._from_raw(raw)
                s.mymemo_token, s.mymemo_refresh, s.mymemo = fresh.mymemo_token, fresh.mymemo_refresh, fresh.mymemo
                return s.mymemo_token
        if status in (400, 401) and _error_code(data) in ("invalid_grant", "invalid_client", "unauthorized_client",
                                                          "invalid_token"):
            _forget(raw, "expired")
            st._write(raw)
            s.mymemo_token, s.mymemo_refresh, s.mymemo = "", "", dict(raw["mymemo"])
        return None


def access_token(s: Settings) -> str:
    """The access token to send now: refreshed first when it expires within a minute."""
    expires = _parse(s.mymemo.get("expires_at"))
    if s.mymemo_token and s.mymemo_refresh and expires and expires - _now() < timedelta(seconds=EARLY):
        return refresh(s) or s.mymemo_token
    return s.mymemo_token


# ---- pauses the service asked for ------------------------------------------------------------------------------


def hold(s: Settings) -> dict | None:
    """The pause in force (``code``: plan_required, quota_exceeded, rate_limited, unavailable), or None."""
    h = s.mymemo.get("hold")
    if not isinstance(h, dict):
        return None
    until = _parse(h.get("until"))
    return h if until and until > _now() else None


def _date(value: Any) -> str:
    dt = _parse(value)
    return f"{dt:%B} {dt.day}" if dt else ""


def hold_message(h: Mapping[str, Any]) -> str:
    code = h.get("code")
    if code == "plan_required":
        return "Needs the OpenMyMemo Assistant plan. Until then, OpenMyMemo uses its rules."
    if code == "quota_exceeded":
        when = _date(h.get("resets_at"))
        return (f"This month's allowance is used up, resets {when}. Until then, OpenMyMemo uses its rules." if when
                else "This month's allowance is used up. Until it resets, OpenMyMemo uses its rules.")
    if code == "unavailable":
        return "MyMemo's model is briefly unavailable. OpenMyMemo uses its rules and tries again shortly."
    return "MyMemo asked OpenMyMemo to slow down. It uses its rules and tries again shortly."


def _plan_url(value: Any, service: str) -> str | None:
    """A plan page from the service's answer, if it is an https page (or on this computer, for a local service)."""
    if not isinstance(value, str) or len(value) > 500:
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme == "https" and parts.hostname:
        return value
    if parts.scheme == "http" and cloud._loopback(value) and cloud._loopback(service):
        return value
    return None


def _retry_after(headers: Mapping[str, str] | None, default: int) -> int:
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        seconds = int(float(value)) if value is not None else default
    except ValueError:
        seconds = default
    return min(max(seconds, 1), RETRY_MAX)


def note_failure(s: Settings, status: int, payload: Any, headers: Mapping[str, str] | None) -> dict | None:
    """Keep a pause for a 402, 429 or 503 from the endpoint; returns it (None for any other answer)."""
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    code = err.get("code") or err.get("type")
    now = _now()
    if status == 402:
        resets = _parse(err.get("resets_at"))
        if code == "quota_exceeded":
            until = resets if resets and now < resets <= now + HOLD_MAX else now + QUOTA_HOLD_DEFAULT
            h = {"code": "quota_exceeded", "resets_at": _iso(resets) if resets else None}
        else:
            until = now + PLAN_HOLD
            h = {"code": "plan_required", "resets_at": None}
        h["plan_url"] = _plan_url(err.get("plan_url"), service_of(s))
    elif status in (429, 503):
        kind = "rate_limited" if status == 429 else "unavailable"
        until = now + timedelta(seconds=_retry_after(headers, RETRY_DEFAULT[kind]))
        h = {"code": kind, "resets_at": None, "plan_url": None}
    else:
        return None
    h["until"] = _iso(until)
    _set_hold(s, h)
    return h


def _set_hold(s: Settings, h: dict | None) -> None:
    with st._lock():
        raw = st._read_raw()
        info = raw.get("mymemo") if isinstance(raw.get("mymemo"), dict) else {}
        if h is None:
            info.pop("hold", None)
        else:
            info["hold"] = h
        raw["mymemo"] = info
        st._write(raw)
    if h is None:
        s.mymemo.pop("hold", None)
    else:
        s.mymemo["hold"] = h


def clear_hold(s: Settings, codes: tuple[str, ...] | None = None) -> None:
    """Lift a pause (any, or only these codes) after the endpoint answered normally."""
    h = s.mymemo.get("hold")
    if isinstance(h, dict) and (codes is None or h.get("code") in codes):
        with contextlib.suppress(OSError):
            _set_hold(s, None)


# ---- what the panel shows ----------------------------------------------------------------------------------------


def public_status(s: Settings) -> dict[str, Any]:
    """MyMemo's part of ``Assistant.status()``: never a token."""
    h = hold(s)
    account = s.mymemo.get("account") if isinstance(s.mymemo.get("account"), dict) else {}
    link = cloud.load_link()
    return {
        "signed_in": bool(s.mymemo_token), "service": service_of(s),
        "via": s.mymemo.get("via") if s.mymemo_token else None,
        "account": member_fields(account) or None, "signed_in_at": s.mymemo.get("signed_in_at"),
        "signed_out": s.mymemo.get("signed_out") if not s.mymemo_token else None,
        "cloud_linked": link is not None,
        "hold": ({"code": h.get("code"), "message": hold_message(h), "plan_url": h.get("plan_url"),
                  "resets_at": h.get("resets_at"), "until": h.get("until")} if h else None),
    }


def _models_shape(data: Mapping[str, Any], service: str) -> dict[str, Any]:
    """The parts of ``GET /models`` the panel shows, as the service answered them (nothing hard-coded)."""
    def text(v: Any) -> str | None:
        return " ".join(v.split())[:120] if isinstance(v, str) and v.strip() else None

    def num(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    return {
        "model": text(data.get("model")), "display_name": text(data.get("display_name")),
        "provider": text(data.get("provider")),
        "plan": {"status": text(plan.get("status")), "active": plan.get("active") is True,
                 "period_start": text(plan.get("period_start")), "period_end": text(plan.get("period_end")),
                 "resets_at": text(plan.get("resets_at")), "cancel_at_period_end": plan.get("cancel_at_period_end") is True,
                 "plan_url": _plan_url(plan.get("plan_url"), service)},
        "usage": {k: num(usage.get(k)) for k in ("input_tokens", "output_tokens", "input_tokens_included",
                                                 "output_tokens_included", "requests")},
        "limits": {k: num(limits.get(k)) for k in ("requests_per_minute", "requests_per_day")},
    }


def account(s: Settings) -> dict[str, Any]:
    """The model, plan and this period's usage from ``GET /models`` (refreshing the token once on 401):
    ``{"ok": True, ...}`` or ``{"ok": False, "error", "signed_out"}``. An active plan lifts a plan pause."""
    from . import client

    if not s.mymemo_token:
        return {"ok": False, "error": "Sign in to MyMemo to use its hosted model.", "signed_out": True}
    got = client.get_json(s, "/models")
    if isinstance(got, client.Failure):
        return {"ok": False, "error": got.message, "signed_out": got.code == "signed_out"}
    status, data, headers = got
    if status != 200 or not isinstance(data, dict):
        h = note_failure(s, status, data, headers)
        return {"ok": False, "error": hold_message(h) if h else "MyMemo couldn't show your plan right now.",
                "signed_out": False}
    out = _models_shape(data, service_of(s))
    if out["plan"]["active"]:
        clear_hold(s, ("plan_required",))
    return {"ok": True, **out}


__all__ = [
    "EXCHANGE_MESSAGES", "MYMEMO_PATH", "SIGNED_OUT", "SignIn", "access_token", "account", "clear_hold", "exchange",
    "hold", "hold_message", "keep", "keep_signin", "note_failure", "public_status", "refresh", "resource",
    "service_of", "sign_in_with_browser", "sign_out",
]
