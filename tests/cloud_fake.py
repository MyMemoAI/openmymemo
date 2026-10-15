"""A fake MyMemo service for the MyMemo Cloud tests, built from tests/fixtures/cloud/ (the MyMemo service tests against the same files).

It runs on 127.0.0.1 in a thread of its own: the link endpoints over HTTP and the bridge over a WebSocket, on one
port, like the service. Tests drive it from the main thread: authorize a link the way the consent page would, send
frames to a connected computer and wait for its answers, close the socket with any code.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from openmymemo.cloud import SUBPROTOCOL, pkce_challenge, unb64u

FIXTURES = Path(__file__).parent / "fixtures" / "cloud"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02):
    """Poll until `predicate()` is truthy; returns its value (fails the test after `timeout`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting")


class FakeConn:
    """One computer's WebSocket as the service sees it."""

    def __init__(self, service: FakeService, ws: WebSocket, headers: dict[str, str]):
        self.service = service
        self.ws = ws
        self.headers = headers
        self.hello: dict | None = None
        self.frames: list[dict] = []  # what the computer sent after hello
        self.client_close: int | None = None  # the code the computer closed with
        self.closed = threading.Event()
        self._lock = threading.Lock()

    def send(self, frame: dict) -> None:
        self.service.run(self.ws.send_text(json.dumps(frame)))

    def close(self, code: int, reason: str = "") -> None:
        self.service.run(self.ws.close(code, reason))

    def wait_frame(self, predicate, timeout: float = 10.0) -> dict:
        def find():
            with self._lock:
                return next((f for f in self.frames if predicate(f)), None)

        return wait_until(find, timeout)

    def call(self, frame: dict, timeout: float = 10.0) -> dict:
        """Send a `call` frame and wait for the computer's `result` for it."""
        self.send(frame)
        return self.wait_frame(lambda f: f.get("type") == "result" and f.get("id") == frame["id"], timeout)

    def _got(self, frame: dict) -> None:
        with self._lock:
            self.frames.append(frame)


class FakeService:
    def __init__(self):
        response = fixture("link/response.json")
        self.token = response["device_token"]
        self.link_response = response
        self.welcome = fixture("bridge/welcome.json")
        self.codes: dict[str, dict] = {}  # authorization codes the "consent page" issued
        self.issued = False  # a device was linked through this fake (linking again replaces it)
        self.authorize_params: list[dict] = []
        self.link_requests: list[dict] = []
        self.unlinks: list[str | None] = []
        self.handshakes: list[dict[str, str]] = []
        self.handshake_times: list[float] = []  # when each upgrade request arrived (monotonic)
        self.conns: list[FakeConn] = []
        self.refuse_upgrades = 0  # how many upgrades to answer with HTTP 403 even for a device it knows
        self.busy: tuple[int, str] | None = None  # (status, Retry-After) for every upgrade: 429 or 503
        self.device_known = True  # False once unlinked: its token gets 401 invalid_token, its upgrades 403
        self.waf = False  # a firewall in front of the service: an HTML 403 for every request
        self.device_redirect = False  # GET /openmymemo/device redirects to a page that says invalid_token
        self.device_checks: list[str | None] = []  # the Authorization header of each GET /openmymemo/device
        self.choose_subprotocol = True  # False: accept without echoing the offered subprotocol
        self.close_after_hello: int | None = None  # close with this code instead of answering welcome
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app = self._build()
        self.base = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    # -- running --

    def start(self) -> FakeService:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning",
                                                     lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="fake-mymemo")
        self._thread.start()
        wait_until(lambda: self._server.started)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._server.force_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    def run(self, coro, timeout: float = 10.0):
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # -- the consent page, simulated --

    def authorize(self, url: str) -> str:
        """What the browser does after the person allows the link: returns the callback URL with a code."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == self.base and parts.path == "/openmymemo/oauth/authorize"
        return self._consent(dict(parse_qsl(parts.query)))

    def _consent(self, params: dict) -> str:
        self.authorize_params.append(params)
        code = "omc_" + secrets.token_urlsafe(16)
        self.codes[code] = params
        return f"{params['redirect_uri']}?{urlencode({'code': code, 'state': params['state'], 'iss': self.base})}"

    def connection(self, index: int = -1, timeout: float = 10.0) -> FakeConn:
        """A computer that said hello (by default the latest)."""
        wait_until(lambda: len([c for c in self.conns if c.hello]) > (index if index >= 0 else 0), timeout)
        return [c for c in self.conns if c.hello][index]

    # -- the service --

    def _build(self) -> FastAPI:
        app = FastAPI()
        invalid = {"WWW-Authenticate": 'Bearer error="invalid_token"'}

        @app.middleware("http")
        async def firewall(request: Request, call_next):
            if self.waf:
                return HTMLResponse("<html><body>Sorry, you have been blocked</body></html>", status_code=403)
            return await call_next(request)

        @app.get("/openmymemo/device")
        async def device(request: Request):  # what the service answers
            auth = request.headers.get("authorization")
            self.device_checks.append(auth)
            if self.device_redirect:
                return RedirectResponse("/elsewhere/device", status_code=302)
            if auth != f"Bearer {self.token}" or not self.device_known:
                return JSONResponse({"error": "invalid_token"}, status_code=401, headers=invalid)
            return JSONResponse({"member": self.link_response["member"], "plan": self.link_response["plan"],
                                 "online": any(not c.closed.is_set() for c in self.conns), "queued": 0,
                                 "last_call_at": None, "clients": []})

        @app.get("/elsewhere/device")
        async def elsewhere():
            return JSONResponse({"error": "invalid_token"}, status_code=401, headers=invalid)

        @app.get("/openmymemo/oauth/authorize")
        async def consent(request: Request):  # a real browser, for trying the panel by hand: allows at once
            return RedirectResponse(self._consent(dict(request.query_params)), status_code=302)

        @app.post("/openmymemo/device/link")
        async def link(request: Request):
            body = await request.json()
            self.link_requests.append(body)
            issued = self.codes.pop(body.get("code"), None)
            if issued is None:
                return JSONResponse({"error": "invalid_grant", "error_description": "Unknown or used code"}, 400)
            if (pkce_challenge(body.get("code_verifier", "")) != issued["code_challenge"]
                    or body.get("redirect_uri") != issued["redirect_uri"] or body.get("key_alg") != "x25519"
                    or len(unb64u(body.get("public_key", ""))) != 32):
                return JSONResponse({"error": "invalid_request", "error_description": "Bad link request"}, 400)
            if self.issued or any(not c.closed.is_set() for c in self.conns):
                # Linking again replaces the device: a new id and token, and the old socket is told so with 4409
                # (see bridge/close_codes.json).
                device_id, self.token = str(uuid.uuid4()), "omd_TEST_ONLY_" + secrets.token_urlsafe(24)
                self.link_response = {**self.link_response, "device_id": device_id, "device_token": self.token}
                self.welcome = {**self.welcome, "device_id": device_id}
                for conn in self.conns:
                    if not conn.closed.is_set():
                        try:
                            await conn.ws.close(4409, "replaced")
                        except RuntimeError:
                            pass
            self.issued, self.device_known = True, True
            return JSONResponse(self.link_response)

        @app.delete("/openmymemo/device")
        async def unlink(request: Request):
            auth = request.headers.get("authorization")
            self.unlinks.append(auth)
            if auth != f"Bearer {self.token}" or not self.device_known:
                return JSONResponse({"error": "invalid_token"}, status_code=401, headers=invalid)
            self.device_known = False
            for conn in self.conns:
                if not conn.closed.is_set():
                    try:
                        await conn.ws.close(4401, "unlinked")
                    except RuntimeError:
                        pass
            return Response(status_code=204)

        @app.websocket("/openmymemo/bridge")
        async def bridge(ws: WebSocket):
            self.loop = asyncio.get_running_loop()
            headers = {k.lower(): v for k, v in ws.headers.items()}
            self.handshakes.append(headers)
            self.handshake_times.append(time.monotonic())
            if self.busy:  # handshake.json: 429 or 503, with Retry-After, before the upgrade
                await ws.send_denial_response(Response(status_code=self.busy[0], headers={"Retry-After": self.busy[1]}))
                return
            # handshake.json: auth is checked before the upgrade is accepted; any Origin is refused.
            if (self.refuse_upgrades or self.waf or not self.device_known or "origin" in headers
                    or headers.get("authorization") != f"Bearer {self.token}"):
                self.refuse_upgrades = max(0, self.refuse_upgrades - 1)
                await ws.close(code=1008)
                return
            offered = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
            chosen = SUBPROTOCOL if SUBPROTOCOL in offered and self.choose_subprotocol else None
            await ws.accept(subprotocol=chosen)
            conn = FakeConn(self, ws, headers)
            self.conns.append(conn)
            try:
                conn.hello = json.loads(await ws.receive_text())
                if self.close_after_hello is not None:
                    await ws.close(self.close_after_hello)
                    return
                await ws.send_text(json.dumps(self.welcome))
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        conn.client_close = msg.get("code")
                        return
                    if msg.get("text"):
                        conn._got(json.loads(msg["text"]))
            except Exception:  # the computer went away mid-way
                return
            finally:
                conn.closed.set()

        return app


if __name__ == "__main__":
    # Try the panel's sign-in by hand: `uv run python tests/cloud_fake.py 18765`, then start the panel with
    # OPENMYMEMO_CLOUD_URL=http://127.0.0.1:18765 and a scratch OPENMYMEMO_CLOUD_DIR. Its consent page allows at once.
    import sys

    service = FakeService()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18765
    service.base = f"http://127.0.0.1:{port}"
    service._server = uvicorn.Server(uvicorn.Config(service.app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="off"))
    print(f"Fake MyMemo service at {service.base} (Ctrl-C to stop)", flush=True)
    service._server.run()
