"""MyMemo Memory sync (product-design.md §20) against the shared protocol fixtures in tests/fixtures/cloud/sync/ and a
fake service built from them (tests/cloud_fake.py): the state file, what goes up (private topics, tombstones, `set`,
earlier versions), filing what comes down (proposals, the person's own edits, decisions, the first merge), the panel
API and CLI, and a whole round trip over the bridge.

Every test has its own HOME, OPENMYMEMO_CLOUD_DIR and a local OPENMYMEMO_CLOUD_URL (conftest.py)."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from cloud_fake import FakeService, FakeSync, fixture, wait_until
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import cloud, sync
from openmymemo.cli import app as cli
from openmymemo.cloud import Bridge, Cloud, Link, Timing
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
FIX = Path(__file__).parent / "fixtures" / "cloud" / "sync"
# The digest of tests/fixtures/cloud/sync/ (every file, by name). The MyMemo service checks its byte-identical copy,
# tests/fixtures/openmymemo_cloud/sync/, against the same constant: change both folders and both constants together.
FIXTURES_SHA256 = "d514111395ecf255098f9f6ece190eb55deb243e998d652ec7cb1d3fb061ba4a"
FAST = Timing(backoff_base=0.02, backoff_cap=0.1, plan_retry=60.0, restart_jitter=0.05, open_timeout=5,
              welcome_timeout=5)
VECTOR = fixture("hpke_vector.json")


def fx(name: str) -> dict:
    return fixture(f"sync/{name}")


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(sync.Session, "TICK", 0.05)
    monkeypatch.setattr(cloud, "SYNC_WATCH_S", 0.05)


@pytest.fixture
def fake():
    service = FakeService().start()
    service.sync = FakeSync()
    yield service
    service.stop()


def linked(vault, service: str = "http://127.0.0.1:9") -> Link:
    link = Link(service=service, device_id=VECTOR["device_id"], device_token=fixture("link/response.json")["device_token"],
                private_key=VECTOR["private_key"], key_id=VECTOR["key_id"], vault=str(vault.root),
                member={"display_name": "Sam"}, plan={"tier": "ultra", "live": True, "queue": True},
                linked_at="2026-10-12T20:00:00+00:00")
    cloud.save_link(link)
    return link


def seeded(vault) -> None:
    """The folder the fixtures describe: k2p9d and m7f3a (as in sync_push_merge.json), q8r7s, and the memory that makes
    MyMemo's c-2b8m4q a duplicate."""
    vault.add("work", "Engineering lead at Acme; the stack is FastAPI, Vue 3 and Swift", memory_id="k2p9d")
    vault.add("how-to-work-with-me", "Prefers answers in Chinese, with code comments in English", memory_id="m7f3a",
              kind="preference")
    vault.add("lifestyle", "Cycles to work", memory_id="q8r7s")


# ---- the fixtures and the state ---------------------------------------------------------------------------------


def test_the_fixtures_are_the_ones_the_service_has():
    assert digest(FIX) == FIXTURES_SHA256, "the sync fixtures changed: change the service's copy and both digests"
    other = os.environ.get("MYMEMO_SERVICE_SRC")
    if other:
        assert digest(Path(other) / "tests" / "fixtures" / "openmymemo_cloud" / "sync") == digest(FIX)


def test_hello_lists_sync_only_while_it_is_on(vault):
    import asyncio

    link = linked(vault)
    bridge = Bridge(vault, link)
    frame = asyncio.run(bridge.hello())
    assert "features" not in frame  # off: exactly the launch hello
    sync.turn_on(vault, [])
    assert asyncio.run(bridge.hello())["features"] == ["sync"]
    sync.turn_off()
    assert "features" not in asyncio.run(bridge.hello())


def test_turning_on_needs_a_link_for_this_folder_and_private_topics(vault, tmp_path):
    with pytest.raises(sync.SyncError, match="Link this computer"):
        sync.turn_on(vault, [])
    linked(vault)
    with pytest.raises(sync.SyncError, match="Not a private topic"):
        sync.turn_on(vault, ["work"])
    st = sync.turn_on(vault, ["health"])
    assert st.enabled and len(st.sync_id) == 32 and st.picked == ["health"] and st.consent_version == sync.CONSENT_VERSION
    again = sync.turn_on(vault, [])
    assert again.sync_id != st.sync_id  # a new consent, never mistaken for the old one


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_state_lives_next_to_cloud_json_never_holds_memory_text(vault):
    linked(vault)
    canary = "zebra-canary-in-the-folder"
    vault.add("work", f"{canary} works at Acme")
    st = sync.turn_on(vault, [])
    frame, commit = sync.build_push(vault, st)
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    sync.save_state(st)
    path = sync.state_path()
    assert path.parent == cloud.cloud_dir() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert canary not in path.read_text() and canary in json.dumps(frame)
    assert not str(path).startswith(str(vault.root))


# ---- what goes up -----------------------------------------------------------------------------------------------


def _keys(frame: dict) -> dict[str, list[str]]:
    out = {}
    for op in frame.get("ops") or []:
        out[("past" if op.get("past") else op["op"])] = sorted(op)
    return out


def test_a_first_push_has_the_fixtures_shape(vault):
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    st.merge = "confirmed"
    frame, commit = sync.build_push(vault, st)
    expected = fx("sync_push_merge.json")
    assert set(frame) == set(expected) - {"outcomes", "cursor"}
    assert _keys(frame)["create"] == sorted(expected["ops"][0])
    assert {op["memory_id"] for op in frame["ops"]} == {"k2p9d", "m7f3a", "q8r7s"}
    topics = {t["slug"]: t for t in frame["topics"]}
    assert sorted(topics["work"]) == sorted(expected["topics"][1])
    assert topics["health"]["private"] and topics["health"]["title"] is None and topics["health"]["description"] is None
    assert frame["settings"] == expected["settings"]
    assert [g["id"] for g in frame["groups"]][:3] == ["who", "doing", "ways"]
    assert frame["merge"] == "done" and sorted(frame["known"]) == ["k2p9d", "m7f3a", "q8r7s"]
    sync.apply_ack(st, commit, fx("sync_ack.json") | {"seq": commit.seq})
    assert st.merge == "done" and set(st.pushed) == {"k2p9d", "m7f3a", "q8r7s"}
    assert sync.build_push(vault, st) == (None, None)  # nothing changed since


def test_private_topics_and_memories_stay_here_unless_picked(vault):
    linked(vault)
    vault.add("health", "Sees a physiotherapist on Mondays", memory_id="h4j5k")
    vault.add("work", "Works at Acme", memory_id="w1x2y")
    st = sync.turn_on(vault, [])
    st.merge = "done"

    def push():
        frame, commit = sync.build_push(vault, st)
        if frame is not None:
            sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
        return frame or {}

    first = push()
    assert {op["memory_id"] for op in first["ops"]} == {"w1x2y"}
    sync.set_picked(vault, ["health"])
    st.picked = ["health"]
    picked = push()
    assert [(op["memory_id"], op["op"]) for op in picked["ops"]] == [("h4j5k", "create")]
    assert {t["slug"]: t["title"] for t in picked["topics"]}["health"] == "Health"
    # Unpicking it, marking a memory private, moving one into a private topic: tombstones.
    st.picked = []
    vault.update("w1x2y", private=True)
    gone = push()
    assert sorted((op["memory_id"], op["op"]) for op in gone["ops"]) == [("h4j5k", "tombstone"), ("w1x2y", "tombstone")]
    assert set(gone["ops"][0]) == set(fx("sync_push.json")["ops"][-1])
    vault.update("w1x2y", private=False)
    assert [(op["memory_id"], op["op"]) for op in push()["ops"]] == [("w1x2y", "create")]  # private: no new version
    vault.update("w1x2y", topic="finance")
    assert [(op["memory_id"], op["op"]) for op in push()["ops"]] == [("w1x2y", "tombstone")]
    assert all("Acme" not in json.dumps(op) for op in gone["ops"])


def test_changes_go_up_as_versions_and_sets(vault):
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    st.merge = "done"
    frame, commit = sync.build_push(vault, st)
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    vault.update("k2p9d", content="Engineering lead at Acme; the stack is FastAPI and Vue 3")
    vault.update("k2p9d", content="Head of engineering at Acme; the stack is FastAPI and Vue 3", pinned=True,
                 until="2027-03")
    vault.update("m7f3a", kind="instruction")  # no new version: a `set`
    frame, commit = sync.build_push(vault, st)
    ops = [(op["memory_id"], op["version"], op["op"], bool(op.get("past"))) for op in frame["ops"]]
    assert ops == [("k2p9d", 2, "update", True), ("k2p9d", 3, "update", False), ("m7f3a", 1, "set", False)]
    expected = {("past" if op.get("past") else op["op"]): sorted(op) for op in fx("sync_push.json")["ops"]}
    assert _keys(frame) == {"past": expected["past"], "update": expected["update"], "set": expected["set"]}
    final = frame["ops"][1]
    assert (final["pinned"], final["until"], final["actor"]) == (True, "2027-03", "user")


# ---- what comes down --------------------------------------------------------------------------------------------


def _merged_state(vault) -> sync.SyncState:
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    st.merge = "confirmed"
    outcomes, _ = sync.file_ops(vault, st, fx("sync_ops_merge.json")["ops"])
    assert outcomes == fx("sync_push_merge.json")["outcomes"]
    return st


def test_the_first_merge_files_mymemos_memories_keeping_their_ids(vault):
    st = _merged_state(vault)
    m = vault.get("c-7f3a9k")
    assert (m.content, m.topic, m.last_actor) == ("Writes the MyMemo newsletter every Friday", "work",
                                                  "connector:cloud-mymemo")
    connector = Connectors(vault).get("cloud-mymemo")
    assert (connector.kind, connector.name) == ("cloud", "MyMemo")
    assert st.cursor == 2
    # Filed again (MyMemo resends what wasn't acknowledged): nothing new.
    assert sync.file_ops(vault, st, fx("sync_ops_merge.json")["ops"])[0] == []


def test_mymemos_ops_are_filed_as_the_fixtures_say(vault):
    st = _merged_state(vault)
    # Two proposals MyMemo sent earlier: they wait here (k2p9d was written by the person).
    for pid in ("p19a2b3c4d5e6f71", "p19a2b3c4d5e6f72"):
        out, _ = sync.file_ops(vault, st, [{
            "seq": st.cursor + 1, "from": "app", "proposal_id": pid,
            "connector": {"id": "cloud-agent", "name": "MyMemo Agent"}, "op": "update", "memory_id": "k2p9d",
            "new_memory_id": None, "content": "Head of engineering at Acme", "topic": None, "kind": None,
            "reason": None, "evidence": None, "until": None, "hold": None, "created_at": "2026-10-26T09:01:00+00:00"}])
        assert out[0]["status"] == "pending" and out[0]["hold"] == "protected"
    st.cursor = 2  # the fixture's ops follow the merge's
    outcomes, touched = sync.file_ops(vault, st, fx("sync_ops.json")["ops"])
    assert outcomes == fx("sync_push.json")["outcomes"][:7]
    marathon = vault.get("c-9x1y2z")  # AskMemo's add kept MyMemo's id
    assert (marathon.topic, marathon.until, marathon.last_actor) == ("goals", "2027-09", "connector:cloud-askmemo")
    assert vault.get("c-7f3a9k").content == "Writes the MyMemo newsletter every other Friday"
    assert vault.get("c-7f3a9k").last_actor == "user"  # the person's own edit
    assert vault.get("c-4n5p6q").topic == "identity"
    assert vault.topic("reading")["title"] == "Reading"
    assert vault.get("q8r7s").status == "archived" and "q8r7s" in st.forgotten
    assert Proposals(vault).get(st.filed["p19a2b3c4d5e6f71"]["local"]).status == "rejected"
    assert "c-7f3a9k" in touched
    # Deciding the other one here goes up later, with `seq: null` (the fixture's last outcome).
    Proposals(vault).accept(st.filed["p19a2b3c4d5e6f72"]["local"])
    frame, _ = sync.build_push(vault, st)
    assert frame["outcomes"][-1] == fx("sync_push.json")["outcomes"][-1]


def test_an_edit_on_an_older_version_is_a_conflict_and_the_folder_stays(vault):
    st = _merged_state(vault)
    vault.update("k2p9d", content="CTO at Acme")  # version 2 here
    put = {"seq": 3, "from": "user", "edit_id": "e1", "op": "put", "memory_id": "k2p9d", "base_version": 1,
           "memory": {"content": "Engineering lead (web)", "topic": "work", "section": None, "kind": "fact",
                      "until": None, "private": False, "pinned": False, "status": "active"},
           "at": "2026-10-26T09:06:00+00:00"}
    missing = {**put, "seq": 4, "edit_id": "e2", "memory_id": "zzzzz"}
    outcomes, touched = sync.file_ops(vault, st, [put, missing])
    assert [o["status"] for o in outcomes] == ["conflict", "missing"]
    assert vault.get("k2p9d").content == "CTO at Acme" and "k2p9d" in touched


def test_a_revoked_cloud_connector_refuses(vault):
    st = _merged_state(vault)
    Connectors(vault).ensure("cloud-askmemo", "AskMemo (MyMemo)", kind="cloud")
    Connectors(vault).remove("cloud-askmemo")
    op = fx("sync_ops.json")["ops"][0]
    outcomes, _ = sync.file_ops(vault, st, [op])
    assert outcomes == [{"seq": 3, "status": "refused", "code": "revoked", "proposal_id": op["proposal_id"],
                         "memory_id": None}]


def test_the_merge_preview_counts(vault):
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    ops = fx("sync_ops_merge.json")["ops"] + [
        {"seq": 3, "from": "merge", "memory_id": "c-zzzzzz", "created_at": None,
         "memory": {"content": "Engineering lead at Acme, working with FastAPI and Swift", "topic": "work",
                    "section": None, "kind": "fact", "until": None, "private": False, "pinned": False,
                    "status": "active"}}]
    assert sync.merge_preview(vault, st, ops) == {"cloud": 3, "here": 3, "same": 1, "similar": 1, "new": 1}


# ---- over the bridge, against the fake service -------------------------------------------------------------------


def _online(vault, fake, clouds: list) -> tuple[Cloud, object]:
    linked(vault, fake.base)
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    assert c.start()
    conn = fake.connection()
    wait_until(lambda: c.status()["online"])
    return c, conn


@pytest.fixture
def clouds():
    made: list[Cloud] = []
    yield made
    for c in made:
        c.stop()


def test_a_whole_round_trip_over_the_bridge(vault, fake, clouds):
    seeded(vault)
    linked(vault, fake.base)
    sync.turn_on(vault, [])
    c, conn = _online(vault, fake, clouds)
    assert conn.hello["features"] == ["sync"]
    wait_until(lambda: fake.sync.hellos)
    assert fake.sync.hellos[0] == {**fx("sync_hello.json"), "sync_id": sync.load_state(VECTOR["device_id"]).sync_id}
    # The first merge: MyMemo's memories come down and wait for the preview.
    conn.send(fx("sync_ops_merge.json"))
    preview = wait_until(lambda: c.sync_status()["preview"])
    assert preview == {"cloud": 2, "here": 3, "same": 1, "similar": 0, "new": 1}
    assert c.sync_status()["state"] == "preview" and not fake.sync.pushes
    c.sync_merge()
    push = wait_until(lambda: fake.sync.pushes and fake.sync.pushes[-1])
    assert push["merge"] == "done" and push["outcomes"] == fx("sync_push_merge.json")["outcomes"]
    assert push["cursor"] == 2 and sorted(push["known"]) == ["c-7f3a9k", "k2p9d", "m7f3a", "q8r7s"]
    wait_until(lambda: c.sync_status()["state"] == "syncing")
    assert vault.activity("mymemo-cloud")[0]["action"] == "sync"
    # A change here goes up by itself; one from MyMemo comes down and is filed.
    vault.update("k2p9d", content="Head of engineering at Acme")
    up = wait_until(lambda: next((p for p in fake.sync.pushes if any(
        op["memory_id"] == "k2p9d" and op["version"] == 2 for op in p.get("ops") or [])), None))
    assert up["seq"] == push["seq"] + 1
    conn.send({"type": "sync_ops", "v": 1, "cursor": 3, "more": False, "ops": [fx("sync_ops.json")["ops"][0]]})
    filed = wait_until(lambda: next((p for p in fake.sync.pushes if p.get("cursor") == 3), None))
    assert filed["outcomes"][0]["status"] == "applied" and vault.get("c-9x1y2z").topic == "goals"
    assert any(op["memory_id"] == "c-9x1y2z" for op in filed.get("ops") or []) or wait_until(
        lambda: any(op["memory_id"] == "c-9x1y2z" for p in fake.sync.pushes for op in p.get("ops") or []))
    # Turning it off here, deleting the copy: MyMemo is told, and the next hello doesn't list sync.
    c.sync_off(delete_copy=True)
    off = wait_until(lambda: fake.sync.offs)
    assert off[0] == {**fx("sync_off.json"), "sync_id": fake.sync.hellos[0]["sync_id"]}
    wait_until(lambda: len([x for x in fake.conns if x.hello]) >= 2 and "features" not in fake.connection().hello)
    wait_until(lambda: not sync.load_state(VECTOR["device_id"]).pending_off)
    assert c.sync_status()["state"] == "off"


def test_mymemo_refusing_or_turning_it_off_is_shown(vault, fake, clouds):
    linked(vault, fake.base)
    sync.turn_on(vault, [])
    fake.sync.status = fx("sync_status_refused.json")
    c, conn = _online(vault, fake, clouds)
    st = wait_until(lambda: c.sync_status()["state"] == "refused" and c.sync_status())
    assert st["reason"] == "memory_off" and not fake.sync.pushes
    # Turned off on MyMemo's memory page: this computer turns it off too, and says so.
    conn.send(fx("sync_status_off.json"))
    wait_until(lambda: not sync.load_state(VECTOR["device_id"]).enabled)
    assert c.sync_status()["off_by"] == "mymemo"


# ---- the panel API and the CLI ----------------------------------------------------------------------------------


def test_the_panel_api(vault):
    linked(vault)
    client = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    status = client.get("/api/cloud", headers=H).json()["sync"]
    assert status["available"] and status["state"] == "off" and [t["slug"] for t in status["private_topics"]] == [
        "health", "finance"]
    assert client.post("/api/cloud/sync", headers=H, json={"on": True}).status_code == 400  # no consent
    bad = client.post("/api/cloud/sync", headers=H, json={"on": True, "consent": True, "private_topics": ["work"]})
    assert bad.status_code == 400
    on = client.post("/api/cloud/sync", headers=H, json={"on": True, "consent": True, "private_topics": ["health"]})
    assert on.status_code == 200 and on.json()["sync"]["enabled"] and on.json()["sync"]["picked"] == ["health"]
    assert client.patch("/api/cloud/sync", headers=H, json={"private_topics": []}).json()["sync"]["picked"] == []
    assert client.post("/api/cloud/sync/merge", headers=H).json()["sync"]["merge"] == "confirmed"
    off = client.post("/api/cloud/sync", headers=H, json={"on": False, "delete_copy": True}).json()["sync"]
    assert off["state"] == "off" and off["pending_off"]


def test_the_cli(vault, tmp_path):
    runner = CliRunner()
    args = ["--vault", str(vault.root)]
    result = runner.invoke(cli, [*args, "cloud", "sync", "on", "--yes"])
    assert result.exit_code == 1 and "Link this computer" in result.output
    linked(vault)
    result = runner.invoke(cli, [*args, "cloud", "sync", "on", "--yes", "--private-topic", "health"])
    assert result.exit_code == 0, result.output
    assert "MyMemo stores your memories" in result.output and "MyMemo Memory sync is on" in result.output
    st = sync.load_state(VECTOR["device_id"])
    assert st.enabled and st.picked == ["health"] and st.merge == "confirmed"
    assert "waiting for OpenMyMemo to connect" in runner.invoke(cli, [*args, "cloud", "sync", "status"]).output
    assert "Private topics synced: none" in runner.invoke(cli, [*args, "cloud", "sync", "topics", "--none"]).output
    result = runner.invoke(cli, [*args, "cloud", "sync", "off", "--delete-copy"])
    assert result.exit_code == 0 and "deletes its copy" in result.output
    st = sync.load_state(VECTOR["device_id"])
    assert not st.enabled and st.pending_off["delete_copy"] is True
