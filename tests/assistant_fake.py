"""A fake MyMemo service for the OpenMyMemo Assistant's hosted model (product-design.md §17.8), built from
tests/fixtures/cloud/assistant/ on top of the MyMemo Cloud fake (its consent page, device link and device token).

It adds the OAuth token and revoke endpoints for client ``openmymemo-desktop`` (authorization code with PKCE, token
exchange of the device token, rotating refresh tokens with reuse detection) and the endpoint itself: chat
completions and ``/models``. Tests queue error answers from errors.json with ``fail(...)``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import parse_qsl

from cloud_fake import FakeService, fixture
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from openmymemo.cloud import pkce_challenge

TOKEN = fixture("assistant/token.json")
MODELS = fixture("assistant/models.json")
ERRORS = fixture("assistant/errors.json")
PATH = "/openmymemo/assistant/v1"


class FakeAssistantService(FakeService):
    def __init__(self):
        self.access: set[str] = set()  # access tokens that work
        self.refresh: str | None = None  # the one refresh token that works now
        self.spent: set[str] = set()  # refresh tokens already rotated (presenting one again revokes everything)
        self.token_forms: list[dict] = []
        self.revoked: list[dict] = []
        self.chats: list[dict] = []  # {"headers", "body"} of each chat request that got past auth
        self.chat_auth: list[str | None] = []  # the Authorization header of every chat request
        self.model_calls: list[str | None] = []
        self.failures: list[str] = []  # names in errors.json, answered in order before the normal answer
        self.answer = lambda body: json.dumps({"items": [{"text": "ok"}]})
        self.models = json.loads(json.dumps(MODELS["response"]))
        self.expires_in = TOKEN["response"]["expires_in"]
        self.member: dict | None = None  # the optional masked `member` in /models (models.json), when set
        # Runs inside a chat request before it is answered (off the event loop, so it may call this service):
        # tests use it to change the sign-in while a request is in flight. Returns an errors.json name or None.
        self.on_chat = None
        self.paths: list[str] = []  # every request's path, in order
        super().__init__()

    def fail(self, *names: str) -> None:
        self.failures.extend(names)

    def _issue(self) -> dict:
        access = "oma_TEST_ONLY_" + secrets.token_urlsafe(12)
        refresh = "omr_TEST_ONLY_" + secrets.token_urlsafe(12)
        self.access.add(access)
        self.refresh = refresh
        return {**TOKEN["response"], "access_token": access, "refresh_token": refresh, "expires_in": self.expires_in}

    def tokens_for_keep(self) -> dict:
        """A fresh grant as `mymemo.keep` takes it: a sign-in made directly (as another process would)."""
        t = self._issue()
        return {"access": t["access_token"], "refresh": t["refresh_token"], "expires_in": t["expires_in"], "member": {}}

    def expire_access(self) -> None:
        """Every access token stops working (as after an hour); the refresh token still works."""
        self.access.clear()

    def _error(self, name: str) -> Response:
        e = ERRORS[name]
        headers = {"Retry-After": e["retry_after"]} if "retry_after" in e else {}
        if e["status"] == 401:
            headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        body = json.loads(json.dumps(e["body"]).replace("https://app.mymemo.ai", self.base))
        return JSONResponse(body, status_code=e["status"], headers=headers)

    def _build(self):
        app = super()._build()

        @app.middleware("http")
        async def record(request: Request, call_next):
            self.paths.append(request.url.path)
            return await call_next(request)
        bad = lambda error, status=400: JSONResponse({"error": error}, status_code=status)  # noqa: E731

        @app.post("/openmymemo/oauth/token")
        async def token(request: Request):
            if request.headers.get("content-type") != "application/x-www-form-urlencoded":
                return bad("invalid_request")
            form = dict(parse_qsl((await request.body()).decode()))
            self.token_forms.append(form)
            if form.get("client_id") != "openmymemo-desktop":
                return bad("invalid_client", 401)
            grant = form.get("grant_type")
            if grant == "authorization_code":
                issued = self.codes.pop(form.get("code"), None)
                if (issued is None or pkce_challenge(form.get("code_verifier", "")) != issued["code_challenge"]
                        or form.get("redirect_uri") != issued["redirect_uri"]
                        or "assistant" not in issued.get("scope", "").split()):
                    return bad("invalid_grant")
                return JSONResponse(self._issue())
            if grant == TOKEN["token_exchange"]["grant_type"]:
                if form.get("subject_token_type") != TOKEN["token_exchange"]["subject_token_type"]:
                    return bad("invalid_request")
                if form.get("scope") != "assistant":
                    return bad("invalid_scope")
                if form.get("subject_token") != self.token or not self.device_known:
                    return bad("invalid_grant")
                return JSONResponse({**self._issue(), "issued_token_type": TOKEN["issued_token_type"]})
            if grant == "refresh_token":
                presented = form.get("refresh_token")
                if presented in self.spent:  # reuse: the whole grant is revoked
                    self.access.clear()
                    self.refresh = None
                    return bad("invalid_grant")
                if not presented or presented != self.refresh:
                    return bad("invalid_grant")
                self.spent.add(presented)
                return JSONResponse(self._issue())
            return bad("unsupported_grant_type")

        @app.post("/openmymemo/oauth/revoke")
        async def revoke(request: Request):
            form = dict(parse_qsl((await request.body()).decode()))
            self.revoked.append(form)
            if form.get("token") == self.refresh:
                self.refresh = None
                self.access.clear()
            self.access.discard(form.get("token", ""))
            return Response(status_code=200)

        def authorized(request: Request) -> bool:
            auth = request.headers.get("authorization") or ""
            return auth.startswith("Bearer ") and auth[7:] in self.access

        @app.post(f"{PATH}/chat/completions")
        async def chat(request: Request):
            self.chat_auth.append(request.headers.get("authorization"))
            hook, self.on_chat = self.on_chat, None  # once
            if hook is not None:
                forced = await asyncio.to_thread(hook)
                if forced:
                    return self._error(forced)
            if not authorized(request):
                return self._error("invalid_token")
            body = await request.json()
            self.chats.append({"headers": {k.lower(): v for k, v in request.headers.items()}, "body": body})
            if self.failures:
                return self._error(self.failures.pop(0))
            return JSONResponse({
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": self.models["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": self.answer(body)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                          "prompt_tokens_details": {"cached_tokens": 0}}})

        @app.get(f"{PATH}/models")
        async def models(request: Request):
            self.model_calls.append(request.headers.get("authorization"))
            if not authorized(request):
                return self._error("invalid_token")
            plan = {**self.models["plan"], "plan_url": f"{self.base}/openmymemo/assistant"}
            return JSONResponse({**self.models, "plan": plan, **({"member": self.member} if self.member else {})})

        return app
