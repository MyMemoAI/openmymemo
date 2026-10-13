"""The local server behind ``openmymemo serve``: the panel, its JSON API and the HTTP MCP endpoint.

Only 127.0.0.1 is bound, but that alone doesn't stop a web page in the user's browser from calling us,
so every request must carry a localhost ``Host`` (defeats DNS rebinding), API calls must carry an
``X-OpenMyMemo`` header and a localhost ``Origin`` if any (a cross-site page can't add custom headers
without a CORS preflight, which we never grant), and ``/mcp`` requires a connector's bearer token.

``/cloud/callback`` is where the browser comes back after signing in to MyMemo (product-design.md §16.4). It is
a navigation, so it can't carry the header; it isn't under ``/api/``, it still needs a localhost ``Host``, and it
does nothing unless its ``state`` matches a link this process started in the last ten minutes, once.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import urllib.request
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from . import __version__, clients, transfer
from .cloud import CALLBACK_HEADERS, Cloud, callback_page
from .connectors import Connector, Connectors
from .context import compile_context
from .mcp_server import build_server
from .proposals import PermissionDenied, Proposals
from .snippets import http_snippets, stdio_snippets
from .textutil import KINDS, classify_topic
from .topics import IMPORTED_GROUP
from .vault import NotFound, Vault, VaultError

STATIC_DIR = Path(str(files("openmymemo") / "static"))


class MemoryIn(BaseModel):
    topic: str | None = None  # None: file it automatically
    content: str
    kind: str | None = None
    section: str | None = None
    private: bool = False
    until: str | None = None
    pinned: bool = False


class MemoryPatch(BaseModel):
    content: str | None = None
    topic: str | None = None
    kind: str | None = None
    pinned: bool | None = None
    private: bool | None = None
    until: str | None = None


class RollbackIn(BaseModel):
    version: int


class AcceptIn(BaseModel):
    content: str | None = None
    topic: str | None = None
    kind: str | None = None
    as_new: bool = False


class RejectIn(BaseModel):
    block_topic: bool = False


class UndoIn(BaseModel):
    block_topic: bool = False


class EditAppliedIn(BaseModel):
    content: str
    topic: str | None = None


class ConnectorIn(BaseModel):
    name: str
    kind: str = "stdio"
    write: str = "propose"


class ConnectorPatch(BaseModel):
    name: str | None = None
    write: str | None = None
    read_topics: list[str] | str | None = None
    include_private: bool | None = None
    blocked_topics: list[str] | None = None


class ImportPreviewIn(BaseModel):
    source: str
    text: str


class ImportCommitIn(BaseModel):
    source: str
    items: list[dict[str, Any]]


class SortMoveIn(BaseModel):
    memory_id: str
    topic: str


class SortIn(BaseModel):
    moves: list[SortMoveIn]


class ProposeIn(BaseModel):
    op: str
    content: str | None = None
    memory_id: str | None = None
    topic: str | None = None
    kind: str | None = None
    reason: str | None = None
    evidence: str | None = None
    idempotency_key: str | None = None


class ExtImportIn(BaseModel):
    source: str
    text: str


class TopicIn(BaseModel):
    slug: str
    title: str
    description: str | None = None
    group: str | None = None
    private: bool = False


class SettingsPatch(BaseModel):
    require_approval: bool | None = None


class TopicPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    private: bool | None = None


def create_api(vault: Vault, port: int, cloud: Cloud | None = None) -> FastAPI:
    api = FastAPI(title="OpenMyMemo", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    cloud = cloud or Cloud(vault, port)
    api.state.cloud = cloud
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    origins = {f"http://{h}" for h in hosts}
    connectors = Connectors(vault)
    proposals = Proposals(vault)
    base_url = f"http://127.0.0.1:{port}"

    @api.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"detail": "Unexpected Host header"}, status_code=421)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if (origin and origin not in origins) or request.headers.get("x-openmymemo") != "1":
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return await call_next(request)

    @api.exception_handler(VaultError)
    async def vault_error(_: Request, exc: VaultError):
        code = 404 if isinstance(exc, NotFound) else 403 if isinstance(exc, PermissionDenied) else 400
        return JSONResponse({"detail": str(exc)}, status_code=code)

    def mem_dict(m) -> dict:
        return m.to_dict(m.topic in vault.private_topics())

    def connector_dict(c) -> dict:
        d = c.to_public()
        acts = vault.activity(c.id, limit=500)
        d["last_seen"] = acts[0]["at"] if acts else None
        d["reads"] = sum(1 for a in acts if a["action"] in ("context", "search", "get"))
        d["proposals"] = sum(1 for a in acts if a["action"] == "propose")
        return d

    def snippets_for(c, token: str | None = None) -> dict:
        if c.kind == "http":
            return http_snippets(f"{base_url}/mcp", token)
        return stdio_snippets(vault.root, c.id)

    # ---- overview ------------------------------------------------------------------------------

    @api.get("/api/state")
    def state() -> dict:
        topics = vault.topics()
        mems = vault.memories()
        groups: list[dict] = []
        for t in topics:
            if not groups or groups[-1]["id"] != t["group"]:
                groups.append({"id": t["group"], "name": t["group_name"], "topics": []})
            groups[-1]["topics"].append(t["slug"])
        props = proposals.list()  # one pass over the proposal files: the panel polls this
        return {
            "version": __version__, "vault": str(vault.root), "kinds": list(KINDS),
            "topics": topics, "groups": groups, "settings": vault.settings(),
            "counts": {
                "active": len(mems),
                "archived": len(vault.memories("archived")),
                "pending": sum(1 for p in props if p.status == "pending"),
                "unreviewed": sum(1 for p in props if p.status == "applied" and not p.reviewed_at),
                "review_due": sum(1 for m in mems if mem_dict(m)["review_due"]),
            },
            "mcp_url": f"{base_url}/mcp",
        }

    # ---- memories ------------------------------------------------------------------------------

    @api.get("/api/memories")
    def list_memories(status: str = "active") -> list[dict]:
        return [mem_dict(m) for m in vault.memories(status)]

    @api.post("/api/memories")
    def create_memory(body: MemoryIn) -> dict:
        topic = body.topic
        if not topic:
            topics = [t for t in vault.topics() if t["group"] != IMPORTED_GROUP]
            topic = classify_topic(body.content, topics)[0] or topics[0]["slug"]
        return mem_dict(vault.add(topic, body.content, kind=body.kind, section=body.section,
                                  private=body.private, until=body.until, pinned=body.pinned))

    @api.patch("/api/memories/{memory_id}")
    def patch_memory(memory_id: str, body: MemoryPatch) -> dict:
        return mem_dict(vault.update(memory_id, **body.model_dump(exclude_unset=True)))

    @api.post("/api/memories/{memory_id}/archive")
    def archive_memory(memory_id: str) -> dict:
        return mem_dict(vault.archive(memory_id))

    @api.post("/api/memories/{memory_id}/restore")
    def restore_memory(memory_id: str) -> dict:
        return mem_dict(vault.restore(memory_id))

    @api.post("/api/memories/{memory_id}/rollback")
    def rollback_memory(memory_id: str, body: RollbackIn) -> dict:
        return mem_dict(vault.rollback(memory_id, body.version))

    @api.get("/api/memories/{memory_id}/history")
    def memory_history(memory_id: str) -> list[dict]:
        vault.get(memory_id)
        return vault.history(memory_id)

    # ---- settings ------------------------------------------------------------------------------

    @api.get("/api/settings")
    def get_settings() -> dict:
        return vault.settings()

    @api.patch("/api/settings")
    def patch_settings(body: SettingsPatch) -> dict:
        return vault.update_settings(**body.model_dump(exclude_unset=True))

    # ---- topics --------------------------------------------------------------------------------

    @api.post("/api/topics")
    def create_topic(body: TopicIn) -> dict:
        return vault.add_topic(body.slug, body.title, body.description, body.group, body.private)

    @api.patch("/api/topics/{slug}")
    def patch_topic(slug: str, body: TopicPatch) -> dict:
        return vault.update_topic(slug, **body.model_dump(exclude_unset=True))

    # ---- proposals -----------------------------------------------------------------------------

    # A proposal dict is the stored proposal (with `hold`, `reviewed_at` and `applied_version`) plus
    # `can_undo`: whether the change is still the memory's latest version. Unreviewed = the digest.

    @api.get("/api/proposals")
    def list_proposals(status: str | None = None, unreviewed: bool = False, limit: int = 200) -> list[dict]:
        rows = proposals.unreviewed() if unreviewed else proposals.list(status)
        return proposals.as_dicts(rows[:limit])

    @api.post("/api/proposals/reviewed")
    def mark_all_reviewed() -> dict:
        # "Looks good": every applied change in the digest is marked as seen.
        return {"reviewed": proposals.mark_all_reviewed()}

    @api.post("/api/proposals/{pid}/accept")
    def accept(pid: str, body: AcceptIn) -> dict:
        return proposals.as_dict(proposals.accept(pid, content=body.content, topic=body.topic, kind=body.kind,
                                                  as_new=body.as_new))

    @api.post("/api/proposals/{pid}/reject")
    def reject(pid: str, body: RejectIn) -> dict:
        return proposals.as_dict(proposals.reject(pid, block_topic=body.block_topic))

    @api.post("/api/proposals/{pid}/undo")
    def undo(pid: str, body: UndoIn | None = None) -> dict:
        return proposals.as_dict(proposals.undo(pid, block_topic=bool(body and body.block_topic)))

    @api.post("/api/proposals/{pid}/reviewed")
    def mark_reviewed(pid: str) -> dict:
        return proposals.as_dict(proposals.mark_reviewed(pid))

    @api.post("/api/proposals/{pid}/edit")
    def edit_applied(pid: str, body: EditAppliedIn) -> dict:
        return proposals.as_dict(proposals.edit_applied(pid, body.content, topic=body.topic))

    # ---- connectors ----------------------------------------------------------------------------

    @api.get("/api/connectors")
    def list_connectors() -> list[dict]:
        return [connector_dict(c) for c in connectors.list()]

    @api.post("/api/connectors")
    def create_connector(body: ConnectorIn) -> dict:
        if body.kind not in ("stdio", "http"):  # web apps come through MyMemo Cloud, not by name
            raise HTTPException(400, "kind must be stdio or http")
        c, token = connectors.create(body.name, body.kind, write=body.write)
        return {"connector": connector_dict(c), "token": token, "snippets": snippets_for(c, token)}

    @api.patch("/api/connectors/{cid}")
    def patch_connector(cid: str, body: ConnectorPatch) -> dict:
        return connector_dict(connectors.update(cid, **body.model_dump(exclude_unset=True)))

    @api.post("/api/connectors/{cid}/rotate")
    def rotate_connector(cid: str) -> dict:
        c, token = connectors.rotate(cid)
        return {"connector": connector_dict(c), "token": token, "snippets": snippets_for(c, token)}

    @api.delete("/api/connectors/{cid}")
    def delete_connector(cid: str) -> dict:
        connectors.remove(cid)
        return {"ok": True}

    @api.get("/api/connectors/{cid}/snippets")
    def connector_snippets(cid: str) -> dict:
        return snippets_for(connectors.get(cid))

    # ---- AI apps: the catalog and one-click connect (product-design.md §15.1) --------------------

    def app_dict(row: dict, known: dict[str, Connector]) -> dict:
        c = known.get(row["id"])
        return {**row, "connector": connector_dict(c) if c and row["has_connector"] else None}

    @api.get("/api/clients")
    def list_clients() -> dict:
        env = clients.Env.current()
        known = {c.id: c for c in connectors.list()}
        ids = {c.id for c in clients.CLIENTS if c.id != clients.OTHER}  # "other" is a button, not an app
        return {"system": env.system, "apps": [app_dict(r, known) for r in clients.catalog(vault, env)],
                "custom": [connector_dict(c) for c in known.values() if c.id not in ids and c.kind != "cloud"]}

    def client_action(app_id: str, action) -> dict:
        env = clients.Env.current()
        result = action(vault, app_id, env)
        known = {c.id: c for c in connectors.list()}
        return {"result": result.to_dict(),
                "app": app_dict(clients.app_status(vault, clients.get(app_id), env), known)}

    @api.get("/api/clients/{app_id}/setup")
    def client_setup(app_id: str) -> dict:
        return clients.setup(vault, app_id)

    @api.post("/api/clients/{app_id}/connect")
    def connect_client(app_id: str) -> dict:
        return client_action(app_id, clients.connect)

    @api.post("/api/clients/{app_id}/disconnect")
    def disconnect_client(app_id: str) -> dict:
        return client_action(app_id, clients.disconnect)

    # ---- MyMemo Cloud (product-design.md §16) ----------------------------------------------------

    def cloud_status() -> dict:
        s = cloud.status()
        apps = []
        for c in Connectors(vault)._load():  # tombstones too: a revoked web app can be allowed again
            if c.kind != "cloud":
                continue
            d = connector_dict(c) if not c.revoked_at else None
            apps.append({"id": c.id, "name": c.name, "revoked": bool(c.revoked_at),
                         "last_seen": d["last_seen"] if d else None, "connector": d})
        s["apps"] = apps
        seen = [a["last_seen"] for a in apps if a["last_seen"]] + ([s["last_call_at"]] if s["last_call_at"] else [])
        s["last_call_at"] = max(seen) if seen else None
        return s

    @api.get("/api/cloud")
    def get_cloud() -> dict:
        return cloud_status()

    @api.post("/api/cloud/link")
    def start_cloud_link() -> dict:
        """Start linking: the panel opens this URL in the browser; MyMemo sends it back to /cloud/callback."""
        return {"url": cloud.begin_link()}

    @api.delete("/api/cloud")
    def unlink_cloud() -> dict:
        message = cloud.unlink()
        return {"message": message, "cloud": cloud_status()}

    @api.post("/api/cloud/reconnect")
    def reconnect_cloud() -> dict:
        cloud.reconnect()
        return cloud_status()

    @api.post("/api/cloud/apps/{cid}/allow")
    def allow_cloud_app(cid: str) -> dict:
        """Allow a web app again after its access was removed (an explicit re-grant, §5.5)."""
        c = next((x for x in connectors._load() if x.id == cid and x.kind == "cloud"), None)
        if c is None:
            raise NotFound(f"No MyMemo Cloud app {cid}")
        if c.revoked_at:
            connectors.create(c.name, "cloud", connector_id=cid)
        return cloud_status()

    @api.get("/cloud/callback", include_in_schema=False)
    def cloud_callback(request: Request) -> HTMLResponse:
        outcome = cloud.callback(dict(request.query_params))
        return HTMLResponse(callback_page(outcome), status_code=outcome.status, headers=CALLBACK_HEADERS)

    # ---- what AI sees, activity ----------------------------------------------------------------

    @api.get("/api/context")
    def context(connector: str | None = None, max_chars: int = 8000) -> dict:
        c = connectors.get(connector) if connector else None
        return compile_context(vault, c, max_chars=max(500, min(max_chars, 50_000)))

    @api.get("/api/activity")
    def activity(connector: str | None = None, limit: int = 200) -> list[dict]:
        return vault.activity(connector, limit=min(limit, 2000))

    # ---- import / export -----------------------------------------------------------------------

    @api.post("/api/import/preview")
    def import_preview(body: ImportPreviewIn) -> list[dict]:
        try:
            items = transfer.parse(body.source, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return [i.to_dict() for i in transfer.plan(vault, items)]

    @api.post("/api/import/commit")
    def import_commit(body: ImportCommitIn) -> dict:
        fields = transfer.ImportItem.__dataclass_fields__
        items = [transfer.ImportItem(**{k: v for k, v in row.items() if k in fields}) for row in body.items]
        try:
            return {"memory_ids": transfer.commit(vault, items, body.source)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @api.get("/api/sort")
    def sort_preview(topic: str | None = None) -> list[dict]:
        return transfer.sort_plan(vault, topic or None)

    @api.post("/api/sort")
    def sort_apply(body: SortIn) -> dict:
        return {"moved": transfer.sort_commit(vault, [(m.memory_id, m.topic) for m in body.moves])}

    @api.get("/api/export")
    def export(format: str = "markdown", include_private: bool = False) -> PlainTextResponse:
        try:
            text = transfer.export(vault, format, include_private=include_private)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        ext = {"json": "json", "markdown": "md"}.get(format, "txt")
        return PlainTextResponse(text, headers={
            "Content-Disposition": f'attachment; filename="openmymemo-{format}.{ext}"'})

    # ---- connector API (browser extension) --------------------------------------------------------
    # Bearer-token endpoints for clients that can't speak MCP, such as the browser extension. The token
    # is the credential, so these don't need the panel's header/Origin checks (Host is still checked).

    def token_connector(authorization: str = Header(default="")) -> Connector:  # noqa: B008
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        c = connectors.by_token(token) if token else None
        if c is None:
            raise HTTPException(401, "Unknown or revoked OpenMyMemo token", headers={"WWW-Authenticate": "Bearer"})
        return c

    bearer = Depends(token_connector)

    @api.get("/v1/status")
    def v1_status(c: Connector = bearer) -> dict:
        return {"ok": True, "version": __version__, "connector": {"id": c.id, "name": c.name, "write": c.write},
                "require_approval": vault.settings()["require_approval"], "panel_url": base_url}

    @api.get("/v1/context")
    def v1_context(max_chars: int = 8000, c: Connector = bearer) -> dict:
        result = compile_context(vault, c, max_chars=max(500, min(max_chars, 50_000)))
        vault.log_activity(c.id, "context", memory_ids=result["memory_ids"], chars=result["chars"])
        return result

    @api.post("/v1/propose")
    def v1_propose(body: ProposeIn, c: Connector = bearer) -> dict:
        out = proposals.propose(c, body.op, **body.model_dump(exclude={"op"}))  # includes `hold`
        if out.get("status") in ("applied", "pending"):
            out["review_url"] = f"{base_url}/#/changes"
        return out

    @api.post("/v1/import")
    def v1_import(body: ExtImportIn, c: Connector = bearer) -> dict:
        """Turn a pasted/scraped memory list into proposals, so it goes through review and dedupe."""
        try:
            items = transfer.plan(vault, transfer.parse(body.source, body.text))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        counts = {"pending": 0, "applied": 0, "duplicate": 0}
        for it in items:
            if it.duplicate_of:
                counts["duplicate"] += 1
                continue
            key = "import-" + hashlib.sha256(it.content.encode()).hexdigest()[:16]
            r = proposals.propose(c, "add", content=it.content, topic=it.topic, kind=it.kind, idempotency_key=key,
                                  reason=f"Imported from {body.source}", evidence=it.content)
            counts[r["status"] if r["status"] in counts else "pending"] += 1
        return {**counts, "review_url": f"{base_url}/#/changes"}

    # ---- panel ---------------------------------------------------------------------------------

    index = STATIC_DIR / "index.html"
    if (STATIC_DIR / "assets").is_dir():
        api.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @api.get("/{path:path}", include_in_schema=False)
    def panel(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index)
        return HTMLResponse("<p>The OpenMyMemo panel isn't built. Run <code>pnpm build</code> in <code>web/</code>.</p>")

    return api


def _panel_server_class():
    import uvicorn

    class _PanelServer(uvicorn.Server):
        """uvicorn, plus MyMemo Cloud: the bridge starts once the port is ours and closes (1000) when we stop.

        Starting it only after the port is bound matters: two AI apps may start the panel for the same folder at
        once, and only the one that gets the port may connect, or the other would be told it was replaced.
        """

        def __init__(self, config: uvicorn.Config, cloud: Cloud):
            super().__init__(config)
            self.cloud = cloud

        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            if self.started:
                self.cloud.start()

        async def shutdown(self, sockets=None) -> None:
            import asyncio

            await asyncio.to_thread(self.cloud.stop)
            await super().shutdown(sockets=sockets)

    return _PanelServer


def panel_server(vault: Vault, port: int, log_level: str = "warning"):
    """The uvicorn server for the panel, `/mcp` and MyMemo Cloud on 127.0.0.1:`port`."""
    import uvicorn

    cloud = Cloud(vault, port)
    config = uvicorn.Config(create_app(vault, port=port, cloud=cloud), host="127.0.0.1", port=port,
                            log_level=log_level, access_log=False, log_config=None, lifespan="on", ws="none")
    return _panel_server_class()(config, cloud)


def serve_panel(vault: Vault, port: int) -> None:
    """Run the panel in the foreground until interrupted (``openmymemo serve``, the Mac app's server)."""
    panel_server(vault, port).run()


def start_background_panel(vault: Vault, port: int) -> str | None:
    """Serve the panel from inside another process (e.g. the stdio MCP server a plugin launches).

    Returns the panel URL, or None if the port is taken by something else. If the port already serves
    this same vault (another app got there first), its URL is returned and nothing new is started.
    Nothing is ever written to stdout: the stdio MCP protocol owns it.
    """
    url = f"http://127.0.0.1:{port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        try:
            req = urllib.request.Request(f"{url}/api/state", headers={"X-OpenMyMemo": "1"})
            with urllib.request.urlopen(req, timeout=1) as resp:
                same = json.load(resp).get("vault") == str(vault.root)
        except Exception:
            same = False
        return url if same else None

    server = panel_server(vault, port, log_level="error")
    threading.Thread(target=server.run, name="openmymemo-panel", daemon=True).start()
    return url


def create_app(vault: Vault, port: int = 8787, cloud: Cloud | None = None):
    """Compose the panel API with the MCP endpoint at ``/mcp`` (bearer token required)."""
    vault.ensure()
    api = create_api(vault, port, cloud)
    connectors = Connectors(vault)
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    mcp_app = build_server(vault, panel_url=f"http://127.0.0.1:{port}").streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=[f"http://{h}" for h in hosts]),
    )

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await mcp_app(scope, receive, send)  # runs the MCP session manager
            return
        if scope["type"] == "websocket":  # the panel has none; refused before the upgrade (HTTP 403)
            await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not token or connectors.by_token(token) is None:
                resp = JSONResponse({"error": "invalid_token",
                                     "error_description": "Create a connector in OpenMyMemo to get a token."},
                                    status_code=401, headers={"WWW-Authenticate": 'Bearer realm="openmymemo"'})
                await resp(scope, receive, send)
                return
            await mcp_app(scope, receive, send)
            return
        await api(scope, receive, send)

    return app
