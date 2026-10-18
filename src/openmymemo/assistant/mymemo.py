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
* **Each sign-in is its own generation.** A new sign-in gets a random ``generation`` stored with its tokens and its
  normalized service. A request captures a ``Session`` (service, generation, token) when it starts; refreshing,
  reusing a token another process refreshed, the retry after a 401, pauses and sign-out all go ahead only while the
  stored sign-in is still that generation at that service, checked under the lock. Otherwise the request stops and
  the feature uses its rules: a request never carries another sign-in's token, never sends its own to another
  service, and never writes over a newer sign-in.
* **The account** shown is only one this sign-in authenticated: the MyMemo Cloud account whose device token was
  exchanged, or a masked ``member`` in the service's token or ``/models`` answer. A browser sign-in is otherwise
  "signed in to MyMemo at <host>".
* **Pauses.** MyMemo answers 402 when the account has no Assistant plan (``plan_required``) or this month's
  allowance is used up (``quota_exceeded``), and 429 or 503 with ``Retry-After`` when busy. Each is kept as a
  ``hold`` with an end time; until then the Assistant makes no request and every feature takes its rule-based path
  quietly. The panel's plan view (``account``) lifts a ``plan_required`` hold as soon as the plan is active.
* **Signing out** revokes the grant at MyMemo and forgets the tokens.

Prices, allowances and plan details are the service's; this file only passes on what it answers.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
CHANGED = "The MyMemo sign-in changed while this request was running, so OpenMyMemo used its rules this time."


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


def norm_service(url: str) -> str:
    """A service URL as sign-ins are compared: scheme and host in lower case, no trailing slash, query or fragment."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Session:
    """The sign-in a request started with. ``generation`` is "env" for a token from OPENMYMEMO_ASSISTANT_API_KEY."""

    service: str
    generation: str
    token: str = ""

    def __repr__(self) -> str:  # never the token
        return f"Session({self.service!r}, {self.generation!r})"


def session(s: Settings) -> Session | None:
    """Capture the sign-in for a request about to start; None when there is no complete, consistent one (the token
    read doesn't belong to the stored sign-in), and the request must not be made."""
    if not s.mymemo_token:
        return None
    if s.key_store == "env":
        return Session(norm_service(service_of(s)), "env", s.mymemo_token)
    gen, service = s.mymemo.get("generation"), s.mymemo.get("service")
    if (not isinstance(gen, str) or not gen or not isinstance(service, str) or not service
            or s.mymemo.get("access_hash") != _hash(s.mymemo_token)):
        return None
    return Session(norm_service(service), gen, s.mymemo_token)


def _matches(info: Any, sess: Session) -> bool:
    """Whether the stored sign-in (``raw["mymemo"]``) is still the one ``sess`` started with."""
    return (isinstance(info, dict) and sess.generation != "env" and info.get("generation") == sess.generation
            and isinstance(info.get("service"), str) and norm_service(info["service"]) == sess.service)


def endpoint_for(s: Settings, sess: Session) -> str:
    """Where a request with ``sess`` goes: the service it signed in to (or an explicit address override)."""
    return s.base_url.rstrip("/") if s.base_url else sess.service + MYMEMO_PATH


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
    return {"access": access, "refresh": refresh or "", "expires_in": min(float(expires), 86400.0),
            "member": member_fields(data.get("member"))}  # only if the service names the account it signed in


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
    if via is None:  # a refresh: the same sign-in, generation and account
        info = dict(current)
        if tokens.get("member"):
            info["account"] = dict(tokens["member"])
    else:  # a new sign-in: a new generation; nothing of the earlier one (account, pause) carries over
        info = {"via": via, "signed_in_at": _iso(_now()), "generation": secrets.token_hex(12),
                "account": member_fields(account or {})}
    info.update(service=norm_service(service), access_hash=_hash(tokens["access"]),
                expires_at=_iso(_now() + timedelta(seconds=tokens["expires_in"])))
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
    try:  # the account whose device token was exchanged, unless the service names the one it signed in
        keep(link.service, tokens, via="cloud", account=tokens["member"] or link.member)
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
        account = tokens["member"]  # only what this sign-in says, never another sign-in's (such as Cloud's)
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
    generation = s.mymemo.get("generation")
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
        info = raw.get("mymemo") if isinstance(raw.get("mymemo"), dict) else {}
        if info.get("generation") == generation:  # a sign-in made meanwhile (another process) stays
            _forget(raw, reason)
            st._write(raw)
    if not token:
        return "This computer isn't signed in to MyMemo for the Assistant."
    if told:
        return "Signed out of MyMemo. The Assistant uses its rules until you sign in again or choose another model."
    return ("Signed out here, but MyMemo couldn't be told. The sign-in stops working when it expires; you can also "
            "remove this computer in your MyMemo account settings.")


# ---- tokens in use ---------------------------------------------------------------------------------------------


def _adopt(s: Settings, raw: dict[str, Any]) -> None:
    fresh = st._from_raw(raw)
    s.mymemo_token, s.mymemo_refresh, s.mymemo = fresh.mymemo_token, fresh.mymemo_refresh, fresh.mymemo


def refresh(s: Settings, sess: Session) -> tuple[str | None, str]:
    """A new access token for the request that started with ``sess``: ``(token, "ok")``; or ``(None, why)`` with
    why ``changed`` (the stored sign-in is no longer ``sess``'s: another sign-in, sign-out or service; nothing is
    touched and the request must stop), ``signed_out`` (MyMemo refused the refresh: this sign-in is forgotten) or
    ``unavailable`` (no network or a busy service: nothing changes).

    Everything happens under the settings lock and only after checking ``sess`` is still the stored sign-in, so
    a token another process refreshed is reused only when it belongs to the same sign-in and service, and a refresh
    never writes over a newer sign-in."""
    if sess.generation == "env":
        return None, "unavailable"  # a token from OPENMYMEMO_ASSISTANT_API_KEY is the script's to renew
    with st._lock():
        raw = st._read_raw()
        info = raw.get("mymemo")
        if not _matches(info, sess):
            return None, "changed"
        current = st._from_raw(raw)
        if current.mymemo_token and current.mymemo_token != sess.token:
            # The same sign-in, refreshed by another process meanwhile: use its token, don't spend the refresh
            # token again. Only when that token is the one the stored sign-in names.
            if info.get("access_hash") != _hash(current.mymemo_token):
                return None, "changed"
            _adopt(s, raw)
            return s.mymemo_token, "ok"
        if not current.mymemo_refresh:
            _forget(raw, "expired")
            st._write(raw)
            _adopt(s, raw)
            return None, "signed_out"
        try:
            status, data = _oauth(sess.service, "token", {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                                          "refresh_token": current.mymemo_refresh})
        except CloudError:
            return None, "unavailable"
        tokens = _tokens(status, data)
        if tokens is not None:
            try:
                _store(raw, sess.service, tokens)
            except SettingsError:
                return None, "unavailable"
            st._write(raw)
            _adopt(s, raw)
            return s.mymemo_token, "ok"
        if status in (400, 401) and _error_code(data) in ("invalid_grant", "invalid_client", "unauthorized_client",
                                                          "invalid_token"):
            _forget(raw, "expired")
            st._write(raw)
            _adopt(s, raw)
            return None, "signed_out"
        return None, "unavailable"


def access_token(s: Settings, sess: Session) -> tuple[Session | None, str]:
    """The session to send now: refreshed first when its token expires within a minute. ``(None, why)`` when the
    request must not be made (``refresh``'s reasons); a refresh that can't reach MyMemo keeps the old token."""
    expires = _parse(s.mymemo.get("expires_at"))
    if sess.generation != "env" and s.mymemo_refresh and expires and expires - _now() < timedelta(seconds=EARLY):
        token, why = refresh(s, sess)
        if token:
            return replace(sess, token=token), "ok"
        if why != "unavailable":
            return None, why
    return sess, "ok"


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


def note_failure(s: Settings, sess: Session, status: int, payload: Any,
                 headers: Mapping[str, str] | None) -> dict | None:
    """Keep a pause for a 402, 429 or 503 from the endpoint; returns it (None for any other answer). It is kept only
    while ``sess`` is still the stored sign-in: one account's answer never pauses another's."""
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
        h["plan_url"] = _plan_url(err.get("plan_url"), sess.service)
    elif status in (429, 503):
        kind = "rate_limited" if status == 429 else "unavailable"
        until = now + timedelta(seconds=_retry_after(headers, RETRY_DEFAULT[kind]))
        h = {"code": kind, "resets_at": None, "plan_url": None}
    else:
        return None
    h["until"] = _iso(until)
    _set_hold(s, sess, h)
    return h


def _set_hold(s: Settings, sess: Session, h: dict | None) -> None:
    with st._lock():
        raw = st._read_raw()
        info = raw.get("mymemo")
        if not _matches(info, sess):
            return
        if h is None:
            info.pop("hold", None)
        else:
            info["hold"] = h
        st._write(raw)
    if h is None:
        s.mymemo.pop("hold", None)
    else:
        s.mymemo["hold"] = h


def clear_hold(s: Settings, sess: Session, codes: tuple[str, ...] | None = None) -> None:
    """Lift a pause (any, or only these codes) after the endpoint answered ``sess`` normally."""
    h = s.mymemo.get("hold")
    if isinstance(h, dict) and (codes is None or h.get("code") in codes):
        with contextlib.suppress(OSError):
            _set_hold(s, sess, None)


def _keep_member(s: Settings, sess: Session, member: Mapping[str, str]) -> None:
    """The account the service named for this sign-in (``/models``), kept while it is still the stored one."""
    with st._lock():
        raw = st._read_raw()
        info = raw.get("mymemo")
        if not _matches(info, sess) or info.get("account") == dict(member):
            return
        info["account"] = dict(member)
        st._write(raw)
    s.mymemo["account"] = dict(member)


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
    sess = session(s)
    if sess is None:
        return {"ok": False, "error": CHANGED, "signed_out": False}
    got = client.get_json(s, sess, "/models")
    if isinstance(got, client.Failure):
        return {"ok": False, "error": got.message, "signed_out": got.code == "signed_out"}
    status, data, headers, sess = got
    if status != 200 or not isinstance(data, dict):
        h = note_failure(s, sess, status, data, headers)
        return {"ok": False, "error": hold_message(h) if h else "MyMemo couldn't show your plan right now.",
                "signed_out": False}
    out = _models_shape(data, sess.service)
    member = member_fields(data.get("member"))
    if member:
        _keep_member(s, sess, member)
    if out["plan"]["active"]:
        clear_hold(s, sess, ("plan_required",))
    return {"ok": True, **out}


__all__ = [
    "CHANGED", "EXCHANGE_MESSAGES", "MYMEMO_PATH", "SIGNED_OUT", "Session", "SignIn", "access_token", "account", "clear_hold", "exchange",
    "hold", "hold_message", "keep", "keep_signin", "note_failure", "public_status", "refresh", "resource",
    "service_of", "session", "sign_in_with_browser", "sign_out",
]
