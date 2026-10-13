"""A fake MyMemo service for the MyMemo Cloud tests, built from tests/fixtures/cloud/ (shared with mymemo-service).

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
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response

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
        self.authorize_params: list[dict] = []
        self.link_requests: list[dict] = []
        self.unlinks: list[str | None] = []
        self.handshakes: list[dict[str, str]] = []
        self.conns: list[FakeConn] = []
        self.refuse_upgrades = 0  # how many upgrades to answer with HTTP 403, as for a token the service doesn't know
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
            return JSONResponse(self.link_response)

        @app.delete("/openmymemo/device")
        async def unlink(request: Request):
            auth = request.headers.get("authorization")
            self.unlinks.append(auth)
            if auth != f"Bearer {self.token}":
                return Response(status_code=401)
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
            # handshake.json: auth is checked before the upgrade is accepted; any Origin is refused.
            if self.refuse_upgrades or headers.get("authorization") != f"Bearer {self.token}" or "origin" in headers:
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
