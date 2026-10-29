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
    # LF line endings: a Windows checkout (core.autocrlf) writes the same fixtures with CRLF.
    h = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        h.update(path.name.encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
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
    # Version 2 (made between two pushes) doesn't go up: the protocol's `past` ops are never sent (below).
    ops = [(op["memory_id"], op["version"], op["op"], bool(op.get("past"))) for op in frame["ops"]]
    assert ops == [("k2p9d", 3, "update", False), ("m7f3a", 1, "set", False)]
    expected = {("past" if op.get("past") else op["op"]): sorted(op) for op in fx("sync_push.json")["ops"]}
    assert _keys(frame) == {"update": expected["update"], "set": expected["set"]}
    final = frame["ops"][0]
    assert (final["pinned"], final["until"], final["actor"]) == (True, "2027-03", "user")


def test_text_written_while_a_memory_was_private_never_goes_up(vault):
    """Between two pushes a memory is made private, edited, edited again and made public: only its last state goes
    up, never the version written while it was private (history.jsonl doesn't record privacy)."""
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    st.merge = "done"
    frame, commit = sync.build_push(vault, st)
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    vault.update("q8r7s", private=True)
    vault.update("q8r7s", content="Cycles to work; private: sees a cardiologist every Tuesday")
    vault.update("q8r7s", content="Cycles to work every day")
    vault.update("q8r7s", private=False)
    frame, _ = sync.build_push(vault, st)
    assert [(op["memory_id"], op["version"], op["op"]) for op in frame["ops"]] == [("q8r7s", 3, "update")]
    assert "cardiologist" not in json.dumps(frame) and not any(op.get("past") for op in frame["ops"])


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


def _put(memory_id: str, base: int, content: str, **memory) -> dict:
    return {"seq": 3, "from": "user", "edit_id": "e1", "op": "put", "memory_id": memory_id, "base_version": base,
            "memory": {"content": content, "topic": "work", "section": None, "kind": "fact", "until": None,
                       "private": False, "pinned": False, "status": "active", **memory},
            "at": "2026-10-26T09:06:00+00:00"}


def test_an_edit_from_mymemo_never_undoes_a_privacy_choice_made_here(vault):
    """Marking a memory private makes no new version: an edit MyMemo made on the same version, before it heard,
    is a conflict and the memory stays private and as it was."""
    st = _merged_state(vault)
    frame, commit = sync.build_push(vault, st)
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    before = vault.get("k2p9d")
    vault.update("k2p9d", private=True)
    assert vault.get("k2p9d").version == before.version
    outcomes, _ = sync.file_ops(vault, st, [_put("k2p9d", before.version, "Engineering lead (web)")])
    assert [o["status"] for o in outcomes] == ["conflict"]
    after = vault.get("k2p9d")
    assert after.private and after.content == before.content
    # Likewise anything else a version doesn't count, changed here since the memory went up.
    st.cursor = 0
    vault.update("m7f3a", pinned=True)
    outcomes, _ = sync.file_ops(vault, st, [_put("m7f3a", vault.get("m7f3a").version, "Prefers answers in English",
                                                 topic="how-to-work-with-me", kind="preference")])
    assert [o["status"] for o in outcomes] == ["conflict"]
    assert vault.get("m7f3a").content == "Prefers answers in Chinese, with code comments in English"


def test_mymemos_own_edits_one_after_another_all_apply(vault):
    """Edits made one after another on MyMemo's memory page apply in turn, in one batch or across frames before the
    next push is acknowledged; a change made here in between (if only in what a version doesn't count) conflicts."""
    st = _merged_state(vault)
    frame, commit = sync.build_push(vault, st)
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    v = vault.get("k2p9d").version
    first, second = _put("k2p9d", v, "Engineering lead at Acme (web)"), _put("k2p9d", v + 1, "CTO at Acme (web)")
    outcomes, _ = sync.file_ops(vault, st, [first, {**second, "seq": 4, "edit_id": "e2"}])
    assert [o["status"] for o in outcomes] == ["applied", "applied"]
    assert (vault.get("k2p9d").content, vault.get("k2p9d").version) == ("CTO at Acme (web)", v + 2)
    # Separate frames, still before the push that carries them is acknowledged.
    for i, text in enumerate(["CTO at Acme since 2026", "CTO and co-founder at Acme"]):
        out, _ = sync.file_ops(vault, st, [{**_put("k2p9d", v + 2 + i, text), "seq": 5 + i, "edit_id": f"f{i}"}])
        assert out[0]["status"] == "applied"
    frame, commit = sync.build_push(vault, st)  # the folder states what it now has
    assert [(op["memory_id"], op["version"]) for op in frame["ops"]] == [("k2p9d", v + 4)]
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    assert "k2p9d" not in st.received
    # A change here between two of MyMemo's edits still conflicts.
    out, _ = sync.file_ops(vault, st, [{**_put("k2p9d", v + 4, "CTO at Acme, part time"), "seq": 7}])
    assert out[0]["status"] == "applied"
    vault.update("k2p9d", pinned=True)
    out, _ = sync.file_ops(vault, st, [{**_put("k2p9d", v + 5, "CTO at Acme, full time"), "seq": 8}])
    assert out[0]["status"] == "conflict"
    assert vault.get("k2p9d").content == "CTO at Acme, part time" and vault.get("k2p9d").pinned


def test_a_private_memory_from_the_first_merge_stays_private(vault):
    """MyMemo's memory marked private is filed as a private add: it waits, and accepting it keeps it private (even
    when it resembles a memory here that isn't private, it is not merged into that one)."""
    linked(vault)
    seeded(vault)
    st = sync.turn_on(vault, [])
    st.merge = "confirmed"
    merge = [{"seq": 1, "from": "merge", "memory_id": "c-pr1vat", "created_at": "2026-10-20T08:00:00+00:00",
              "memory": {"content": "Sees a cardiologist every Tuesday", "topic": "work", "section": None,
                         "kind": "fact", "until": None, "private": True, "pinned": False, "status": "active"}},
             {"seq": 2, "from": "merge", "memory_id": "c-pr2vat", "created_at": "2026-10-20T08:00:00+00:00",
              "memory": {"content": "Cycles to work most days", "topic": "lifestyle", "section": None,
                         "kind": "fact", "until": None, "private": True, "pinned": False, "status": "active"}}]
    outcomes, _ = sync.file_ops(vault, st, merge)
    assert [(o["status"], o["hold"]) for o in outcomes] == [("pending", "private"), ("pending", "private")]
    props = Proposals(vault)
    pending = {p.new_memory_id: p for p in props.list() if p.connector_id == "cloud-mymemo"}
    assert set(pending) == {"c-pr1vat", "c-pr2vat"}
    assert all(p.private and p.op == "add" for p in pending.values())  # read back from disk
    for p in pending.values():
        props.accept(p.id)
    assert vault.get("c-pr1vat").private and vault.get("c-pr2vat").private
    assert not vault.get("q8r7s").private and vault.get("q8r7s").content == "Cycles to work"
    st.merge = "done"
    frame, _ = sync.build_push(vault, st)
    assert "cardiologist" not in json.dumps(frame) and "most days" not in json.dumps(frame)


def _acked(vault, st: sync.SyncState) -> dict:
    """The next push, as MyMemo acknowledged it ({} when there was nothing to say)."""
    frame, commit = sync.build_push(vault, st)
    if frame is not None:
        sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    return frame or {}


def _edit(seq: int, op: str, **fields) -> dict:
    return {"seq": seq, "from": "user", "edit_id": f"e{seq}", "op": op, **fields, "at": "2026-10-29T09:00:00+00:00"}


def test_a_memory_from_the_first_merge_waits_as_long_as_it_takes(vault):
    """MyMemo keeps a memory of its own that waits here (private, or the person reviews every change), so here it
    never expires, though its `created_at` is when MyMemo got it, often long ago. The person's decision goes up later
    by `merge_id`, so MyMemo drops a rejected one. An edit of it on MyMemo's memory page is a conflict (MyMemo keeps its
    copy); forgetting it there rejects it here."""
    linked(vault)
    seeded(vault)
    vault.update_settings(require_approval=True)
    st = sync.turn_on(vault, [])
    st.merge = "confirmed"

    def merge(seq: int, mid: str, content: str, private: bool) -> dict:
        return {"seq": seq, "from": "merge", "memory_id": mid, "created_at": "2026-08-01T08:00:00+00:00",
                "memory": {"content": content, "topic": "work", "section": None, "kind": "fact", "until": None,
                           "private": private, "pinned": False, "status": "active"}}

    ops = [merge(1, "c-old111", "Sees a cardiologist every Tuesday", True),
           merge(2, "c-old222", "Keeps a diary in a paper notebook", False),
           merge(3, "c-old333", "Learns the cello on Saturdays", False),
           merge(4, "c-old444", "Collects vintage fountain pens", True)]
    outcomes, _ = sync.file_ops(vault, st, ops)
    assert [(o["merge_id"], o["status"], o["hold"]) for o in outcomes] == [
        ("c-old111", "pending", "private"), ("c-old222", "pending", "approval"), ("c-old333", "pending", "approval"),
        ("c-old444", "pending", "private")]
    props = Proposals(vault)
    waiting = {p.new_memory_id: p for p in props.list("pending")}  # listing is what expires old proposals
    assert set(waiting) == {"c-old111", "c-old222", "c-old333", "c-old444"}
    assert all(p.never_expires for p in waiting.values())
    st.merge = "done"
    _acked(vault, st)
    # Decided here later; and on MyMemo's memory page, one is edited and one forgotten.
    props.reject(waiting["c-old111"].id)
    props.accept(waiting["c-old222"].id)
    put = {**_put("c-old333", 1, "Learns the cello on Sundays"), "seq": 5, "edit_id": "e5"}
    outcomes, _ = sync.file_ops(vault, st, [put, _edit(6, "forget", memory_id="c-old444")])
    assert [o["status"] for o in outcomes] == ["conflict", "applied"]
    assert props.get(waiting["c-old333"].id).status == "pending"
    assert props.get(waiting["c-old444"].id).status == "rejected"
    frame = _acked(vault, st)
    later = {o["merge_id"]: (o["status"], o["memory_id"]) for o in frame["outcomes"] if o["seq"] is None}
    assert later == {"c-old111": ("rejected", None), "c-old222": ("accepted", "c-old222"),
                     "c-old444": ("rejected", None)}
    assert all(set(o) == set(fx("sync_push_merge.json")["outcomes"][0]) for o in frame["outcomes"])
    created = next(op for op in frame["ops"] if op["memory_id"] == "c-old222")
    assert created["op"] == "create" and created["proposal_id"] == waiting["c-old222"].id
    assert "cardiologist" not in json.dumps(frame) and "fountain" not in json.dumps(frame)
    # Said once; the one still waiting goes up when the person decides it.
    assert "outcomes" not in _acked(vault, st)
    props.accept(waiting["c-old333"].id)
    assert [(o["merge_id"], o["status"]) for o in _acked(vault, st)["outcomes"]] == [("c-old333", "accepted")]


def test_a_forget_from_mymemo_never_reaches_past_a_privacy_choice_or_a_newer_change(vault):
    """Forgetting a memory on MyMemo's memory page follows the rules of an edit there: a memory marked private here,
    or changed here since it went up (if only in what a version doesn't count), stays as it is, and the folder's
    memory goes up again."""
    st = _merged_state(vault)
    _acked(vault, st)
    vault.update("k2p9d", private=True)
    vault.update("m7f3a", pinned=True)
    forget = [_edit(3, "forget", memory_id="k2p9d"), _edit(4, "forget", memory_id="m7f3a"),
              _edit(5, "forget", memory_id="q8r7s")]
    outcomes, touched = sync.file_ops(vault, st, forget)
    assert [o["status"] for o in outcomes] == ["conflict", "conflict", "applied"]
    assert vault.get("k2p9d").status == "active" and vault.get("m7f3a").status == "active"
    assert vault.get("q8r7s").status == "archived" and st.forgotten == ["q8r7s"]
    for mid in touched:  # as Session._file does
        if mid in st.pushed:
            st.pushed[mid] = {**st.pushed[mid], "h": ""}
    frame = _acked(vault, st)
    assert sorted((op["memory_id"], op["op"]) for op in frame["ops"]) == [("k2p9d", "tombstone"), ("m7f3a", "set")]


def test_a_topic_change_from_mymemo_keeps_what_mymemo_never_had(vault):
    """A private topic the person didn't pick goes up as its slug only, and MyMemo keeps its slug as the title: a
    change there (here, moving it to another group) applies, but never with those stand-ins for the title and
    description. Changes made there one after another all apply; a topic MyMemo can read takes its new title."""
    linked(vault)
    vault.add_topic("therapy", "Therapy notes", "Sessions with Dr. K, what we covered", private=True)
    st = sync.turn_on(vault, [])
    st.merge = "done"
    sent = {t["slug"]: t for t in _acked(vault, st)["topics"]}["therapy"]
    assert sent["title"] is None and sent["description"] is None
    stand_in = {"slug": "therapy", "title": "therapy", "description": "", "private": True}
    outcomes, _ = sync.file_ops(vault, st, [_edit(1, "topic", topic={**stand_in, "group": "ways"}),
                                            _edit(2, "topic", topic={**stand_in, "group": "doing"})])
    assert [o["status"] for o in outcomes] == ["applied", "applied"]
    t = vault.topic("therapy")
    assert (t["title"], t["description"], t["group"], t["private"]) == (
        "Therapy notes", "Sessions with Dr. K, what we covered", "doing", True)
    again = {t["slug"]: t for t in _acked(vault, st)["topics"]}["therapy"]  # the folder says what it has
    assert (again["title"], again["description"], again["group"]) == (None, None, "doing")
    work = {"slug": "work", "title": "Work and side projects", "description": "Acme and the newsletter",
            "group": "doing", "private": False}
    outcomes, _ = sync.file_ops(vault, st, [_edit(3, "topic", topic=work)])
    assert outcomes[0]["status"] == "applied"
    assert (vault.topic("work")["title"], vault.topic("work")["description"]) == (work["title"], work["description"])


def test_mymemo_never_makes_a_topic_less_private_or_undoes_a_change_made_here(vault):
    """Privacy only grows from MyMemo: making a private topic public there is a conflict, and the topic and its
    memories stay here; making one private applies, and its memories leave MyMemo. A topic changed here since it went
    up keeps the folder's version."""
    linked(vault)
    vault.add("health", "Takes sertraline 50mg daily", memory_id="h1j2k")
    vault.add("work", "Works at Acme", memory_id="w1x2y")
    st = sync.turn_on(vault, [])
    st.merge = "done"
    assert "sertraline" not in json.dumps(_acked(vault, st))
    public = {"slug": "health", "title": "Health", "description": "", "group": "private", "private": False}
    outcomes, _ = sync.file_ops(vault, st, [_edit(1, "topic", topic=public)])
    assert outcomes[0]["status"] == "conflict" and vault.topic("health")["private"]
    restated = _acked(vault, st)
    assert "sertraline" not in json.dumps(restated)
    assert {t["slug"]: t for t in restated["topics"]}["health"]["private"]
    private = {"slug": "work", "title": "Work", "description": "", "group": "doing", "private": True}
    outcomes, _ = sync.file_ops(vault, st, [_edit(2, "topic", topic=private)])
    assert outcomes[0]["status"] == "applied" and vault.topic("work")["private"]
    gone = _acked(vault, st)
    assert [(op["memory_id"], op["op"]) for op in gone["ops"]] == [("w1x2y", "tombstone")]
    assert {t["slug"]: t for t in gone["topics"]}["work"]["title"] is None
    vault.update_topic("lifestyle", title="Daily life")  # after it last went up
    habits = {"slug": "lifestyle", "title": "Habits", "description": "", "group": "life", "private": False}
    outcomes, _ = sync.file_ops(vault, st, [_edit(3, "topic", topic=habits)])
    assert outcomes[0]["status"] == "conflict" and vault.topic("lifestyle")["title"] == "Daily life"


def test_mymemos_topic_changes_apply_one_after_another_across_late_acks_and_creation(vault):
    """MyMemo's own topic changes keep applying when a push sent before one of them is acknowledged after it, and a
    topic MyMemo creates can be changed again in the same batch; a change made here in between still wins."""
    linked(vault)
    st = sync.turn_on(vault, [])
    st.merge = "done"
    _acked(vault, st)
    topic = {"slug": "work", "description": "", "group": "doing", "private": False}
    outcomes, _ = sync.file_ops(vault, st, [_edit(1, "topic", topic={**topic, "title": "Work A"})])
    assert outcomes[0]["status"] == "applied"
    frame, commit = sync.build_push(vault, st)  # A's push, acknowledged only after B
    assert {t["slug"]: t for t in frame["topics"]}["work"]["title"] == "Work A"
    outcomes, _ = sync.file_ops(vault, st, [_edit(2, "topic", topic={**topic, "title": "Work B"})])
    assert outcomes[0]["status"] == "applied"
    sync.apply_ack(st, commit, {"seq": commit.seq, "skipped": []})
    outcomes, _ = sync.file_ops(vault, st, [_edit(3, "topic", topic={**topic, "title": "Work C"})])
    assert outcomes[0]["status"] == "applied" and vault.topic("work")["title"] == "Work C"
    assert {t["slug"]: t for t in _acked(vault, st)["topics"]}["work"]["title"] == "Work C"
    assert "work" not in st.received_topics  # the push said the same

    garden = {"slug": "garden", "description": "", "group": "life", "private": False}
    outcomes, _ = sync.file_ops(vault, st, [_edit(4, "topic", topic={**garden, "title": "Garden"}),
                                            _edit(5, "topic", topic={**garden, "title": "Garden plans"})])
    assert [o["status"] for o in outcomes] == ["applied", "applied"]
    assert vault.topic("garden")["title"] == "Garden plans"

    vault.update_topic("garden", title="Allotment")  # here, before MyMemo heard
    outcomes, _ = sync.file_ops(vault, st, [_edit(6, "topic", topic={**garden, "title": "Garden ideas"})])
    assert outcomes[0]["status"] == "conflict" and vault.topic("garden")["title"] == "Allotment"
    assert {t["slug"]: t for t in _acked(vault, st)["topics"]}["garden"]["title"] == "Allotment"


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
