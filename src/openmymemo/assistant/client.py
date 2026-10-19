"""The endpoint client: one chat request to an OpenAI-compatible ``/chat/completions`` or to Anthropic's Messages
API, over ``httpx2`` (product-design.md §17.2). No vendor SDK.

* **Structured output.** With a schema, the request asks for JSON matching it (``response_format`` with a
  ``json_schema``, or Anthropic's ``output_config.format``). Endpoints that refuse the option (some local servers and
  OpenAI-compatible providers) get the same request once more without it; the instructions describe the schema
  either way, and the answer is checked here (``schema.clean``) regardless.
* **Never followed:** redirects (a local server that redirects elsewhere would send memory text there). A local
  endpoint is never reached through a proxy, and must still be on loopback when the request is made.
* **Failures** are returned, not raised: a ``Failure`` with a message for the person and a short code for the
  activity log. Its message never contains the key or the text sent.
* **MyMemo's hosted model** (kind ``mymemo``, §17.8) takes no prompts: ``task`` names one of the service's fixed
  tasks and sends only the material (labelled blocks) and the task's structured inputs to ``/tasks``; the service
  holds the instructions and the schema (``tests/fixtures/cloud/assistant/tasks.json``). ``chat`` refuses this kind.
  The access token is the bearer. It is refreshed before it expires and once on a 401 (then the request is sent
  again); a 402, 429 or 503 becomes a pause (``mymemo.note_failure``) during which the Assistant makes no requests
  and features use their rules. Each request is bound to the sign-in it started with (``mymemo.Session``): if another
  sign-in, a sign-out or another service replaced it meanwhile, the request stops instead of borrowing the new
  sign-in's token or sending its own elsewhere.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

from .. import __version__
from . import mymemo as _mymemo
from .settings import Settings, is_loopback, url_problem

ANTHROPIC_VERSION = "2023-06-01"
TRANSPORT: httpx2.BaseTransport | None = None  # tests put a fake endpoint here


@dataclass
class Failure:
    """A call that didn't produce an answer. ``code`` is short and stable (it goes in the activity log)."""

    message: str
    code: str = "error"
    status: int | None = None

    def __str__(self) -> str:
        return self.message


@dataclass
class Reply:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    ms: int

    @property
    def tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def _status_message(status: int, payload: Any) -> Failure:
    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or err.get("code") or "")
            code = err.get("code") or err.get("type")
        else:
            detail = str(err or payload.get("detail") or payload.get("message") or "")
            code = payload.get("code")
        if status == 402:
            kind = "quota" if code == "quota_exceeded" else "plan"
            text = ("This month's allowance is used up." if kind == "quota"
                    else "Needs the OpenMyMemo Assistant plan.")
            return Failure(text, kind, status)
    detail = " ".join(detail.split())[:200]
    texts = {
        401: "The endpoint didn't accept the key.",
        403: "The endpoint refused the request.",
        404: "The endpoint wasn't found at that address, or doesn't know that model.",
        408: "The endpoint took too long.",
        413: "The request was too large for the endpoint.",
        429: "The endpoint is busy or rate-limited; try again later.",
    }
    text = texts.get(status) or (f"The endpoint had a problem (HTTP {status})." if status >= 500
                                 else f"The endpoint refused the request (HTTP {status}).")
    return Failure(f"{text} {detail}".strip() if detail and status not in (401, 403) else text,
                         "rate_limited" if status == 429 else "http", status)


def _client(settings: Settings, timeout: float) -> httpx2.Client:
    local = is_loopback(settings.endpoint)
    return httpx2.Client(transport=TRANSPORT, timeout=httpx2.Timeout(timeout, connect=min(10.0, timeout)),
                         follow_redirects=False, trust_env=not local)


def _post(client: httpx2.Client, url: str, headers: dict, body: dict | None,
          method: str = "POST") -> tuple[int, Any, dict[str, str]] | Failure:
    try:
        r = client.request(method, url, json=body, headers=headers)
    except httpx2.TimeoutException:
        return Failure("The endpoint took too long to answer.", "timeout")
    except httpx2.HTTPError:
        return Failure("The endpoint couldn't be reached. Check that it is running and the address is right.",
                       "unreachable")
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if 300 <= r.status_code < 400:
        return Failure("The endpoint redirected elsewhere; enter its final address.", "redirect", r.status_code)
    return r.status_code, payload, {k.lower(): v for k, v in r.headers.items()}


def _signin_failure(why: str) -> Failure:
    """Why a MyMemo request can't go on (``mymemo.refresh``'s reasons), as a Failure."""
    if why == "signed_out":
        return Failure(_mymemo.SIGNED_OUT, "signed_out", 401)
    if why == "unavailable":
        return Failure("MyMemo couldn't renew the sign-in just now. OpenMyMemo used its rules this time.",
                       "unreachable", 401)
    return Failure(_mymemo.CHANGED, "signin_changed")


def _format_refused(status: int) -> bool:
    return status in (400, 422)


def chat(settings: Settings, system: str, user: str, *, max_tokens: int, schema: dict | None = None,
         feature: str | None = None, timeout: float | None = None) -> Reply | Failure:
    """One request: the reply, or a Failure. ``schema`` (already the wire form) asks for structured output."""
    if settings.kind == "mymemo":  # the hosted model takes fixed tasks, never a prompt: see ``task``
        return Failure("MyMemo's hosted model doesn't take this request.", "unsupported")
    bad = url_problem(settings.endpoint, settings.kind)  # environment overrides aren't checked when saved
    if bad:
        return Failure(bad, "bad_address")
    if settings.kind == "local" and not is_loopback(settings.endpoint):
        return Failure("A model on this computer must have a local address.", "not_local")
    base = settings.endpoint
    timeout = timeout or settings.timeout
    headers = {"User-Agent": f"OpenMyMemo/{__version__}", "Accept": "application/json"}
    started = time.monotonic()
    with _client(settings, timeout) as client:
        if settings.protocol == "anthropic" and settings.kind == "key":
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
            headers |= {"x-api-key": settings.api_key, "anthropic-version": ANTHROPIC_VERSION}
            body: dict[str, Any] = {"model": settings.model, "max_tokens": max_tokens, "system": system,
                                    "messages": [{"role": "user", "content": user}]}
            if schema:
                body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
            sent = _post(client, url, headers, body)
            if schema and not isinstance(sent, Failure) and _format_refused(sent[0]):
                body.pop("output_config")
                sent = _post(client, url, headers, body)
            if isinstance(sent, Failure):
                return sent
            status, payload, _ = sent
            if status != 200 or not isinstance(payload, dict):
                return _status_message(status, payload)
            if payload.get("stop_reason") == "refusal":
                return Failure("The model declined to answer.", "refused")
            blocks = payload.get("content") if isinstance(payload.get("content"), list) else []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        else:
            url = base + "/chat/completions"
            if settings.secret:
                headers["Authorization"] = f"Bearer {settings.secret}"
            body = {"model": settings.model or "default", "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "result", "schema": schema, "strict": False}}

            def send() -> tuple[int, Any, dict[str, str]] | Failure:
                sent = _post(client, url, headers, body)
                if schema and not isinstance(sent, Failure) and _format_refused(sent[0]):
                    body.pop("response_format", None)
                    sent = _post(client, url, headers, body)
                return sent

            sent = send()
            if isinstance(sent, Failure):
                return sent
            status, payload, _ = sent
            if status != 200 or not isinstance(payload, dict):
                return _status_message(status, payload)
            choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str):
                if isinstance(message, dict) and message.get("refusal"):
                    return Failure("The model declined to answer.", "refused")
                return Failure("The endpoint's answer had no text.", "invalid")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    ms = int((time.monotonic() - started) * 1000)
    model = payload.get("model") if isinstance(payload.get("model"), str) else settings.model
    as_int = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
    return Reply(text=text, model=model or settings.model, input_tokens=as_int(tokens_in),
                 output_tokens=as_int(tokens_out), ms=ms)


def task(settings: Settings, name: str, blocks: Sequence[tuple[str, str]], *, inputs: dict | None = None,
         timeout: float | None = None) -> Reply | Failure:
    """One of MyMemo's fixed tasks (kind ``mymemo``): the reply (its ``text`` is the task's result as JSON), or a
    Failure. Sends ``{"task", "blocks": [{"label", "text"}], "inputs"}`` and nothing else: the service writes the
    instructions and asks the model for the task's schema."""
    if settings.kind != "mymemo":
        return Failure("Tasks are only for MyMemo's hosted model.", "unsupported")
    sess = _mymemo.session(settings)  # bound to the sign-in this request starts with, its service included
    if sess is None:
        return _signin_failure("changed")
    sess, why = _mymemo.access_token(settings, sess)
    if sess is None:
        return _signin_failure(why)
    base = _mymemo.endpoint_for(settings, sess)
    bad = url_problem(base, settings.kind)
    if bad:
        return Failure(bad, "bad_address")
    headers = {"User-Agent": f"OpenMyMemo/{__version__}", "Accept": "application/json",
               "Authorization": f"Bearer {sess.token}"}
    body = {"task": name, "blocks": [{"label": label, "text": text} for label, text in blocks],
            "inputs": dict(inputs or {})}
    started = time.monotonic()
    with _client(settings, timeout or settings.timeout) as client:
        sent = _post(client, base + "/tasks", headers, body)
        if not isinstance(sent, Failure) and sent[0] == 401:
            fresh, why = _mymemo.refresh(settings, sess)  # only this request's sign-in, at its service
            if not fresh:
                return _signin_failure(why)
            sess = dataclasses.replace(sess, token=fresh)
            headers["Authorization"] = f"Bearer {fresh}"
            sent = _post(client, base + "/tasks", headers, body)
        if isinstance(sent, Failure):
            return sent
        status, payload, response_headers = sent
        if status == 401:
            return Failure(_mymemo.SIGNED_OUT, "signed_out", 401)
        if status != 200:
            held = _mymemo.note_failure(settings, sess, status, payload, response_headers)
            if held:
                return Failure(_mymemo.hold_message(held), held["code"], status)
            return _status_message(status, payload)
        if not isinstance(payload, dict):
            return _status_message(status, payload)
        _mymemo.clear_hold(settings, sess)
    ms = int((time.monotonic() - started) * 1000)
    if payload.get("finish") == "refusal":
        return Failure("The model declined to answer.", "refused")
    result = payload.get("result")
    if payload.get("finish") != "stop" or not isinstance(result, dict):
        return Failure("The endpoint's answer had no result.", "invalid")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    as_int = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
    model = payload.get("model") if isinstance(payload.get("model"), str) else settings.model
    return Reply(text=json.dumps(result, ensure_ascii=False), model=model or settings.model,
                 input_tokens=as_int(usage.get("input_tokens")), output_tokens=as_int(usage.get("output_tokens")),
                 ms=ms)


def get_json(settings: Settings, sess: _mymemo.Session, path: str, *, timeout: float = 20.0
             ) -> tuple[int, Any, dict[str, str], _mymemo.Session] | Failure:
    """GET ``path`` at the service ``sess`` signed in to, with its token (refreshed once on 401, only while ``sess``
    is still the stored sign-in), for ``GET /models``. Returns the session in use last. Same rules as ``chat``: the
    address is checked, redirects aren't followed."""
    current, why = _mymemo.access_token(settings, sess)
    if current is None:
        return _signin_failure(why)
    sess = current
    base = _mymemo.endpoint_for(settings, sess)
    bad = url_problem(base, settings.kind)
    if bad:
        return Failure(bad, "bad_address")
    headers = {"User-Agent": f"OpenMyMemo/{__version__}", "Accept": "application/json",
               "Authorization": f"Bearer {sess.token}"}
    with _client(settings, timeout) as client:
        got = _post(client, base + path, headers, None, method="GET")
        if not isinstance(got, Failure) and got[0] == 401:
            fresh, why = _mymemo.refresh(settings, sess)
            if not fresh:
                return _signin_failure(why)
            sess = dataclasses.replace(sess, token=fresh)
            headers["Authorization"] = f"Bearer {fresh}"
            got = _post(client, base + path, headers, None, method="GET")
        if isinstance(got, Failure):
            return got
        if got[0] == 401:
            return Failure(_mymemo.SIGNED_OUT, "signed_out", 401)
        return (*got, sess)
