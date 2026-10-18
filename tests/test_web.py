import socket
import threading
import time

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api, create_app

H = {"X-OpenMyMemo": "1"}


@pytest.fixture
def api(vault):
    return TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")


def test_guard_blocks_foreign_hosts_origins_and_bare_requests(vault, api):
    assert api.get("/api/state").status_code == 403
    assert api.get("/api/state", headers={**H, "Origin": "https://evil.example"}).status_code == 403
    evil = TestClient(create_api(vault, 8787), base_url="http://evil.example:8787")
    assert evil.get("/api/state", headers=H).status_code == 421
    ok = api.get("/api/state", headers={**H, "Origin": "http://127.0.0.1:8787"})
    assert ok.status_code == 200 and "lang" not in ok.json()
    assert [g["name"] for g in ok.json()["groups"]] == ["Who I am", "What I'm doing", "How I work", "My world", "Private"]


def test_memory_crud_over_api(vault, api):
    r = api.post("/api/memories", headers=H, json={"topic": "work", "content": "在 Acme 做 CTO"})
    assert r.status_code == 200
    mid = r.json()["id"]
    r = api.patch(f"/api/memories/{mid}", headers=H, json={"content": "在 Acme 做 CTO 和产品", "pinned": True})
    assert r.json()["version"] == 2 and r.json()["pinned"] is True
    assert api.post(f"/api/memories/{mid}/archive", headers=H).json()["status"] == "archived"
    assert [m["id"] for m in api.get("/api/memories?status=archived", headers=H).json()] == [mid]
    api.post(f"/api/memories/{mid}/restore", headers=H)
    api.post(f"/api/memories/{mid}/rollback", headers=H, json={"version": 1})
    assert api.get("/api/memories", headers=H).json()[0]["content"] == "在 Acme 做 CTO"
    hist = api.get(f"/api/memories/{mid}/history", headers=H).json()
    assert [h["op"] for h in hist][::-1] == ["create", "update", "archive", "restore", "update"]
    assert api.post("/api/memories", headers=H, json={"topic": "nope", "content": "x"}).status_code == 404
    assert api.get("/api/memories/zzzzz/history", headers=H).status_code == 404


def test_update_and_archive_refuse_a_memory_that_moved_on(vault, api):
    m = api.post("/api/memories", headers=H, json={"topic": "identity", "content": "Prefers Sam"}).json()
    assert m["version"] == 1
    vault.update(m["id"], content="Prefers Sammy")  # say, edited in the Markdown file meanwhile
    r = api.patch(f"/api/memories/{m['id']}", headers=H, json={"content": "Prefers Samuel", "expected_version": 1})
    assert r.status_code == 409 and "changed meanwhile" in r.json()["detail"]
    r = api.post(f"/api/memories/{m['id']}/archive", headers=H, json={"expected_version": 1})
    assert r.status_code == 409
    assert vault.get(m["id"]).content == "Prefers Sammy" and vault.get(m["id"]).status == "active"
    r = api.patch(f"/api/memories/{m['id']}", headers=H, json={"content": "Prefers Samuel", "expected_version": 2})
    assert r.status_code == 200 and r.json()["version"] == 3
    assert api.post(f"/api/memories/{m['id']}/archive", headers=H, json={"expected_version": 3}).status_code == 200
    assert api.post(f"/api/memories/{m['id']}/archive", headers=H).status_code == 200  # no body: as before


def test_import_preview_commit_and_export(vault, api):
    items = api.post("/api/import/preview", headers=H,
                     json={"source": "chatgpt", "text": "用户在 Acme 担任 CTO\n删除\n用户喜欢科幻小说"}).json()
    assert [i["topic"] for i in items] == [None, None]
    assert set(items[0]) == {"content", "topic", "kind", "created_at", "hint", "duplicate_of", "similar_to",
                             "similar_content", "include", "by", "flag"}
    assert items[0]["by"] is None  # the Assistant is off
    items[1]["topic"] = "values"
    ids = api.post("/api/import/commit", headers=H, json={"source": "chatgpt", "items": items}).json()["memory_ids"]
    assert len(ids) == 2 and vault.get(ids[0]).topic == "from-chatgpt" and vault.get(ids[1]).topic == "values"
    assert api.post("/api/import/commit", headers=H, json={"source": "mem0", "items": items}).status_code == 400
    r = api.get("/api/export?format=claude", headers=H)
    assert "attachment" in r.headers["content-disposition"] and "– 用户在 Acme 担任 CTO" in r.text


def test_sort_endpoints(vault, api):
    items = api.post("/api/import/preview", headers=H,
                     json={"source": "gemini", "text": "Is the CTO of Acme\nHas a daughter\nGrows tomatoes"}).json()
    ids = api.post("/api/import/commit", headers=H, json={"source": "gemini", "items": items}).json()["memory_ids"]
    state = api.get("/api/state", headers=H).json()
    assert state["groups"][-1] == {"id": "imported", "name": "Imported", "topics": ["from-gemini"]}
    assert api.get("/api/sort").status_code == 403  # the guard applies

    rows = api.get("/api/sort", headers=H).json()
    assert [(r["memory_id"], r["from_topic"], r["topic"]) for r in rows] == [
        (ids[0], "from-gemini", "work"), (ids[1], "from-gemini", "people"), (ids[2], "from-gemini", None)]
    assert api.get("/api/sort?topic=from-gemini", headers=H).json() == rows
    assert api.get("/api/sort?topic=work", headers=H).status_code == 400
    assert api.get("/api/sort?topic=nope", headers=H).status_code == 404

    moves = {"moves": [{"memory_id": ids[0], "topic": "work"}, {"memory_id": ids[2], "topic": "interests"}]}
    assert api.post("/api/sort", json=moves).status_code == 403
    assert api.post("/api/sort", headers=H, json=moves).json() == {"moved": 2}
    assert vault.get(ids[0]).topic == "work" and vault.get(ids[1]).topic == "from-gemini"
    assert vault.get(ids[2]).topic == "interests"
    bad = {"moves": [{"memory_id": ids[1], "topic": "nope"}]}
    assert api.post("/api/sort", headers=H, json=bad).status_code == 404
    assert [r["memory_id"] for r in api.get("/api/sort", headers=H).json()] == [ids[1]]


def test_connectors_and_context_preview(vault, api):
    vault.add("work", "在 Acme 做 CTO")
    vault.add("health", "对青霉素过敏")
    r = api.post("/api/connectors", headers=H, json={"name": "Cursor", "kind": "http"}).json()
    assert r["token"].startswith("omm_") and "Bearer omm_" in r["snippets"]["json"]
    assert "token_hash" not in r["connector"]
    ctx = api.get("/api/context?connector=cursor", headers=H).json()
    assert "在 Acme 做 CTO" in ctx["text"] and "青霉素" not in ctx["text"]
    api.patch("/api/connectors/cursor", headers=H, json={"include_private": True, "write": "auto"})
    assert "青霉素" in api.get("/api/context?connector=cursor", headers=H).json()["text"]
    stdio = api.post("/api/connectors", headers=H, json={"name": "Claude Desktop"}).json()
    assert stdio["token"] is None and "mcp --connector claude-desktop" in stdio["snippets"]["claude_code"]
    assert api.delete("/api/connectors/cursor", headers=H).json() == {"ok": True}


def test_panel_fallback_route(api):
    r = api.get("/", headers={})
    assert r.status_code == 200 and "html" in r.headers["content-type"]
    assert api.get("/api/unknown", headers=H).status_code in (404, 405)


def test_panel_route_serves_nothing_outside_the_built_panel(api):
    # the containment check behind CodeQL's py/path-injection alerts on this route
    for path in ("/..%2f..%2f..%2fpyproject.toml", "/%2e%2e/%2e%2e/%2e%2e/pyproject.toml", "/..%2f__init__.py", "/..%2fweb.py"):
        r = api.get(path, headers={})
        assert r.status_code in (200, 404) and "[project]" not in r.text and "import" not in r.text, path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(vault):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(vault, port=port), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_http_mcp_end_to_end_with_panel_approval(vault, live):
    vault.update_settings(require_approval=True)
    vault.add("work", "在 Acme 做 CTO")
    with httpx.Client(base_url=live, headers=H) as panel:
        created = panel.post("/api/connectors", json={"name": "Cursor", "kind": "http"}).json()
        token = created["token"]

        assert httpx.post(f"{live}/mcp", json={}).status_code == 401
        bad = httpx.post(f"{live}/mcp", json={}, headers={"Authorization": "Bearer omm_wrong"})
        assert bad.status_code == 401

        async def steps():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
            async with Client(streamable_http_client(f"{live}/mcp", http_client=http)) as c:
                ctx = await c.call_tool("memory_context", {})
                prop = await c.call_tool("memory_propose", {
                    "op": "add", "content": "用户在评估把 iOS 端迁到 SwiftUI", "reason": "mentioned plans",
                    "evidence": "我们在考虑把 iOS 迁到 SwiftUI"})
                return ctx, prop

        ctx, prop = anyio.run(steps)
        assert "在 Acme 做 CTO" in ctx.content[0].text
        pid = prop.structured_content["proposal_id"]
        assert prop.structured_content["status"] == "pending" and prop.structured_content["hold"] == "approval"
        assert prop.structured_content["review_url"] == f"{live}/#/changes"

        pending = panel.get("/api/proposals?status=pending").json()
        assert [p["id"] for p in pending] == [pid] and pending[0]["connector_id"] == "cursor"
        assert pending[0]["hold"] == "approval" and pending[0]["can_undo"] is False
        accepted = panel.post(f"/api/proposals/{pid}/accept", json={"content": "用户在评估把 iOS 端迁到 SwiftUI（2026 Q4）"})
        assert accepted.json()["status"] == "edited_accepted" and accepted.json()["can_undo"] is True
        mid = accepted.json()["applied_memory_id"]

        # Revoking the connector cuts off its token immediately.
        assert panel.delete("/api/connectors/cursor").json() == {"ok": True}
        assert httpx.post(f"{live}/mcp", json={}, headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert panel.get("/api/connectors").json() == []

    text = (vault.memories_dir / "projects.md").read_text()
    assert "用户在评估把 iOS 端迁到 SwiftUI（2026 Q4）" in text and f"^{mid}" in text
    hist = vault.history(mid)
    assert hist[0]["op"] == "create" and hist[0]["proposal_id"] == pid and hist[0]["actor"] == "user"
    assert [a["action"] for a in vault.activity("cursor")][::-1] == ["context", "propose"]


def test_settings_api_toggles_approval(vault, api):
    assert api.get("/api/state", headers=H).json()["settings"] == {"require_approval": False, "onboarded": False}
    assert api.patch("/api/settings", headers=H, json={"require_approval": True}).json() == {
        "require_approval": True, "onboarded": False}
    assert vault.settings()["require_approval"] is True
    assert api.patch("/api/settings", json={"require_approval": False}).status_code == 403  # guard still applies
    assert vault.settings()["require_approval"] is True


def test_settings_api_exposes_and_sets_onboarded(vault, api):
    """The first-run guide: shown while `onboarded` is false, which finishing or skipping it sets to true."""
    assert api.get("/api/settings", headers=H).json()["onboarded"] is False
    r = api.patch("/api/settings", headers=H, json={"onboarded": True})
    assert r.status_code == 200 and r.json() == {"require_approval": False, "onboarded": True}
    assert api.get("/api/state", headers=H).json()["settings"]["onboarded"] is True
    assert api.patch("/api/settings", headers=H, json={"require_approval": True}).json()["onboarded"] is True
    assert api.patch("/api/settings", headers=H, json={"onboarded": "maybe"}).status_code == 422
    assert api.patch("/api/settings", json={"onboarded": False}).status_code == 403  # guard still applies
    assert vault.settings() == {"require_approval": True, "onboarded": True}


def test_digest_review_undo_and_edit_over_api(vault, api):
    claude = Connectors(vault).ensure("claude", "Claude")
    props = Proposals(vault)
    mine = vault.add("tools", "Uses npm")
    a = props.propose(claude, "add", content="Plays the piano", topic="interests")
    b = props.propose(claude, "add", content="Owns a cat", topic="lifestyle")
    c = props.propose(claude, "add", content="Drinks oolong", topic="lifestyle")
    waiting = props.propose(claude, "update", memory_id=mine.id, content="Uses pnpm")
    counts = api.get("/api/state", headers=H).json()["counts"]
    assert (counts["unreviewed"], counts["pending"]) == (3, 1)

    digest = api.get("/api/proposals?unreviewed=true", headers=H).json()
    assert [p["id"] for p in digest] == [c["proposal_id"], b["proposal_id"], a["proposal_id"]]
    assert all(p["can_undo"] and p["hold"] is None and p["reviewed_at"] is None for p in digest)
    pending = api.get("/api/proposals?status=pending", headers=H).json()
    assert pending[0]["id"] == waiting["proposal_id"] and pending[0]["hold"] == "protected"
    assert pending[0]["can_undo"] is False

    kept = api.post(f"/api/proposals/{a['proposal_id']}/reviewed", headers=H).json()
    assert kept["status"] == "applied" and kept["reviewed_at"] and kept["can_undo"]
    assert api.get("/api/state", headers=H).json()["counts"]["unreviewed"] == 2
    assert api.post(f"/api/proposals/{waiting['proposal_id']}/reviewed", headers=H).status_code == 400
    assert api.post("/api/proposals/pnope/reviewed", headers=H).status_code == 404

    edited = api.post(f"/api/proposals/{b['proposal_id']}/edit", headers=H,
                      json={"content": "Owns two cats", "topic": "people"}).json()
    assert (edited["status"], edited["final_content"], edited["final_topic"]) == ("edited_accepted", "Owns two cats",
                                                                                  "people")
    assert edited["reviewed_at"] and edited["can_undo"] and vault.get(b["memory_id"]).last_actor == "user"
    assert api.post(f"/api/proposals/{b['proposal_id']}/edit", headers=H, json={"content": ""}).status_code == 400

    # No body, as the panel sends it today (a JSON content type and nothing else).
    undone = api.post(f"/api/proposals/{c['proposal_id']}/undo", headers={**H, "Content-Type": "application/json"})
    assert undone.status_code == 200 and undone.json()["status"] == "undone" and undone.json()["can_undo"] is False
    assert vault.get(c["memory_id"]).status == "archived"

    vault.update(a["memory_id"], content="Plays the piano and the guitar")
    refused = api.post(f"/api/proposals/{a['proposal_id']}/undo", headers=H, json={"block_topic": True})
    assert refused.status_code == 400 and "changed since" in refused.json()["detail"]
    assert Connectors(vault).get("claude").blocked_topics == []

    d = props.propose(claude, "add", content="Reads before bed", topic="lifestyle")
    e = props.propose(claude, "add", content="Writes Go at work", topic="work")
    blocked = api.post(f"/api/proposals/{d['proposal_id']}/undo", headers=H, json={"block_topic": True}).json()
    assert blocked["status"] == "undone" and Connectors(vault).get("claude").blocked_topics == ["lifestyle"]

    assert api.post("/api/proposals/reviewed", headers=H).json() == {"reviewed": 1}  # only e was left
    assert props.get(e["proposal_id"]).reviewed_at
    assert api.get("/api/state", headers=H).json()["counts"]["unreviewed"] == 0
    assert api.post("/api/proposals/reviewed").status_code == 403  # guard still applies


def test_v1_token_api_for_browser_extension(vault, api):
    vault.add("work", "CTO at Acme")
    token = api.post("/api/connectors", headers=H, json={"name": "Chrome", "kind": "http"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}", "Origin": "chrome-extension://abcdefghijklmnop"}

    assert api.get("/v1/status").status_code == 401
    assert api.get("/v1/status", headers={"Authorization": "Bearer omm_nope"}).status_code == 401
    status = api.get("/v1/status", headers=auth).json()
    assert status["connector"]["id"] == "chrome" and status["require_approval"] is False

    ctx = api.get("/v1/context?max_chars=2000", headers=auth).json()
    assert "CTO at Acme" in ctx["text"]
    r = api.post("/v1/propose", headers=auth, json={"op": "add", "content": "Prefers green tea"}).json()
    assert r["status"] == "applied" and r["hold"] is None and r["review_url"].endswith("/#/changes")
    r = api.post("/v1/propose", headers=auth, json={"op": "add", "content": "Takes vitamin D", "topic": "health"}).json()
    assert r["status"] == "pending" and r["hold"] == "private" and r["review_url"].endswith("/#/changes")

    paste = "Saved memories\nCTO at Acme\nDelete\nLives in Shanghai\nDelete\nPlays the piano"
    out = api.post("/v1/import", headers=auth, json={"source": "chatgpt", "text": paste}).json()
    assert out["duplicate"] == 1 and out["applied"] == 2 and out["pending"] == 0
    again = api.post("/v1/import", headers=auth, json={"source": "chatgpt", "text": paste}).json()
    assert again["duplicate"] == 3 and len([p for p in vault.proposals_dir.glob("*.json")]) == 4  # nothing new
    assert [a["action"] for a in vault.activity("chrome")][-1] == "context"
