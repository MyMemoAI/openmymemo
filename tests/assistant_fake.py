"""A fake MyMemo service for the OpenMyMemo Assistant's hosted model (product-design.md §17.8), built from
tests/fixtures/cloud/assistant/ on top of the OpenMyMemo Cloud fake (its consent page, device link and device token).

It adds the OAuth token and revoke endpoints for client ``openmymemo-desktop`` (authorization code with PKCE, token
exchange of the device token, rotating refresh tokens with reuse detection) and the endpoint itself: ``/tasks``
(checked against tasks.json: the task, its block labels and counts, its inputs, and no other field) and ``/models``.
There is no chat-completions route, as on the real service. Tests queue error answers from errors.json with
``fail(...)``.
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
TASKS = fixture("assistant/tasks.json")


def task_problem(body) -> tuple[str, str | None] | None:
    """Why the service would refuse this task request (an error code and param), or None."""
    if not isinstance(body, dict):
        return "invalid_json", None
    extra = sorted(set(body) - {"task", "blocks", "inputs"})
    if extra:
        return "unsupported_parameter", extra[0]
    spec = TASKS["tasks"].get(body.get("task"))
    if spec is None:
        return "unknown_task", "task"
    counts: dict[str, int] = {}
    for block in body.get("blocks") or []:
        if not isinstance(block, dict) or set(block) - {"label", "text"} or not isinstance(block.get("text"), str):
            return "invalid_blocks", "blocks"
        if block.get("label") not in spec["blocks"]:
            return "invalid_blocks", "blocks"
        counts[block["label"]] = counts.get(block["label"], 0) + 1
    for label, rule in spec["blocks"].items():
        if not rule["min"] <= counts.get(label, 0) <= rule["max"]:
            return "invalid_blocks", "blocks"
    if set(body.get("inputs") or {}) - set(spec["inputs"]):
        return "invalid_inputs", "inputs"
    return None
PATH = "/openmymemo/assistant/v1"


class FakeAssistantService(FakeService):
    def __init__(self):
        self.access: set[str] = set()  # access tokens that work
        self.refresh: str | None = None  # the one refresh token that works now
        self.spent: set[str] = set()  # refresh tokens already rotated (presenting one again revokes everything)
        self.token_forms: list[dict] = []
        self.revoked: list[dict] = []
        self.tasks: list[dict] = []  # {"headers", "body"} of each task request that got past auth
        self.task_auth: list[str | None] = []  # the Authorization header of every task request
        self.model_calls: list[str | None] = []
        self.failures: list[str] = []  # names in errors.json, answered in order before the normal answer
        self.answer = lambda body: json.dumps({"items": [{"text": "ok"}]})  # the task's result, as JSON text
        self.finish = "stop"
        self.models = json.loads(json.dumps(MODELS["response"]))
        self.expires_in = TOKEN["response"]["expires_in"]
        self.member: dict | None = None  # the optional masked `member` in /models (models.json), when set
        # Runs inside a task request before it is answered (off the event loop, so it may call this service):
        # tests use it to change the sign-in while a request is in flight. Returns an errors.json name or None.
        self.on_task = None
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

        @app.post(f"{PATH}/tasks")
        async def task(request: Request):
            self.task_auth.append(request.headers.get("authorization"))
            hook, self.on_task = self.on_task, None  # once
            if hook is not None:
                forced = await asyncio.to_thread(hook)
                if forced:
                    return self._error(forced)
            if not authorized(request):
                return self._error("invalid_token")
            body = await request.json()
            problem = task_problem(body)
            if problem:
                code, param = problem
                return JSONResponse({"error": {"message": code, "type": "invalid_request_error", "param": param,
                                               "code": code}}, status_code=400)
            self.tasks.append({"headers": {k.lower(): v for k, v in request.headers.items()}, "body": body})
            if self.failures:
                return self._error(self.failures.pop(0))
            stopped = self.finish == "stop"
            return JSONResponse({
                "id": "task_test", "object": "assistant.task", "created": 0, "task": body["task"],
                "model": self.models["model"], "finish": self.finish,
                "result": json.loads(self.answer(body)) if stopped else None,
                "refusal": "No." if self.finish == "refusal" else None,
                "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}})

        @app.get(f"{PATH}/models")
        async def models(request: Request):
            self.model_calls.append(request.headers.get("authorization"))
            if not authorized(request):
                return self._error("invalid_token")
            plan = {**self.models["plan"], "plan_url": f"{self.base}/openmymemo/assistant"}
            return JSONResponse({**self.models, "plan": plan, **({"member": self.member} if self.member else {})})

        return app
