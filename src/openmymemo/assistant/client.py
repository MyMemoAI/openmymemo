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
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx2

from .. import __version__
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
                    else "This account doesn't include the Assistant.")
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


def _post(client: httpx2.Client, url: str, headers: dict, body: dict) -> tuple[int, Any] | Failure:
    try:
        r = client.post(url, json=body, headers=headers)
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
    return r.status_code, payload


def _format_refused(status: int) -> bool:
    return status in (400, 422)


def chat(settings: Settings, system: str, user: str, *, max_tokens: int, schema: dict | None = None,
         feature: str | None = None, timeout: float | None = None) -> Reply | Failure:
    """One request: the reply, or a Failure. ``schema`` (already the wire form) asks for structured output."""
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
            status, payload = sent
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
            secret = settings.secret
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
            if settings.kind == "mymemo" and feature:
                headers["X-OpenMyMemo-Feature"] = feature
            body = {"model": settings.model or "default", "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "result", "schema": schema, "strict": False}}
            sent = _post(client, url, headers, body)
            if schema and not isinstance(sent, Failure) and _format_refused(sent[0]):
                body.pop("response_format")
                sent = _post(client, url, headers, body)
            if isinstance(sent, Failure):
                return sent
            status, payload = sent
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
