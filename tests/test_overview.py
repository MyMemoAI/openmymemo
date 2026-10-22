import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from openmymemo import overview
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}


def test_a_new_folder_is_a_first_run(vault):
    o = overview.build(vault)
    assert o["first_run"] is True
    assert o["memories"]["total"] == 0 and o["apps"] == [] and o["waiting"] == {
        "pending": 0, "unreviewed": 0, "review_due": 0}
    assert o["safety"]["score"] == 85 and [i["id"] for i in o["safety"]["top"]] == ["backup"]
    assert len(o["memories"]["growth"]) == 30 and len(o["activity"]) == 7


def test_counts_sources_growth_and_private(vault):
    vault.add("work", "Works at Acme")
    vault.add("work", "Leads the Q3 launch", created_at=(datetime.now(UTC) - timedelta(days=40)).isoformat())
    vault.add("health", "Allergic to penicillin")
    vault.add("work", "Uses pnpm", source="import:chatgpt", actor="import:chatgpt")
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    m = overview.build(vault)["memories"]
    assert m["total"] == 5 and m["private"] == 1
    assert m["sources"] == {"you": 3, "imports": 1, "apps": 1}
    assert sum(g["count"] for g in m["groups"]) == 5
    growth = m["growth"]
    assert growth[0]["total"] == 1 and growth[-1]["total"] == 5  # the 40-day-old memory was there already


def test_apps_and_activity_in_the_last_week(vault):
    cons = Connectors(vault)
    claude = cons.ensure("claude", "Claude")
    cons.ensure("cursor", "Cursor")
    vault.log_activity("claude", "context")
    vault.log_activity("claude", "search")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    o = overview.build(vault)
    first = o["apps"][0]
    assert first["id"] == "claude" and first["reads"] == 2 and first["writes"] == 1 and first["last_seen"]
    assert first["write"] == "propose" and first["include_private"] is False
    assert o["apps"][1] == {**o["apps"][1], "id": "cursor", "reads": 0, "writes": 0, "last_seen": None}
    assert o["activity"][-1]["reads"] == 2 and o["activity"][-1]["writes"] == 1
    assert o["waiting"]["unreviewed"] == 1 and o["first_run"] is False
    later = overview.build(vault, now=datetime.now(UTC) + timedelta(days=8))
    assert later["apps"][0]["reads"] == 0 and sum(d["reads"] for d in later["activity"]) == 0


def test_no_memory_text_in_the_overview(vault):
    vault.add("work", "Works at Acme")
    vault.add("health", "Allergic to penicillin")
    text = json.dumps(overview.build(vault))
    assert "Acme" not in text and "penicillin" not in text


def test_the_overview_over_the_api(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    assert api.get("/api/overview").status_code == 403  # the panel's header, like every API call
    r = api.get("/api/overview", headers=H)
    assert r.status_code == 200 and r.json()["first_run"] is True
