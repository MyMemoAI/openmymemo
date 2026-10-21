"""The Assistant's writes (product-design.md §17.7, §17.5): checking an app's proposal before it is filed, session-end
extraction from Claude Code and Codex transcripts, and chat-export import. All against the fake endpoint of
tests/test_assistant.py; nothing reaches a real model."""

import json
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_assistant import FakeEndpoint, calls, turn_on  # noqa: F401 - FakeEndpoint for the fixture's type
from typer.testing import CliRunner

from openmymemo import codex, transfer
from openmymemo.assistant import chats, load_settings, save_settings
from openmymemo.assistant import client as endpoint
from openmymemo.assistant import session as sessions
from openmymemo.assistant.checks import check_proposal
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

FIXTURES = Path(__file__).parent / "fixtures" / "chats"
H = {"X-OpenMyMemo": "1"}
runner = CliRunner()


@pytest.fixture
def fake(monkeypatch):
    import httpx2

    f = FakeEndpoint()
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(f))
    return f


def system_of(body: dict) -> str:
    return body["messages"][0]["content"]


def user_of(body: dict) -> str:
    return body["messages"][1]["content"]


def app_connector(vault, cid="claude-code", **perms):
    c = Connectors(vault).ensure(cid, "Claude Code")
    if perms:
        c = Connectors(vault).update(cid, **perms)
    return c


def verdict(**kw):
    return json.dumps({"duplicate_of": None, "contradicts": None, "topic": None, "sensitive": "none", "note": "",
                       **kw})


# ---- checking proposals (§17.7, "Writes") ------------------------------------------------------------------------


def test_check_turns_a_paraphrase_into_an_update_and_records_itself(vault, fake):
    turn_on(vault)
    old = vault.add("lifestyle", "Lives in Shanghai", actor="connector:cursor", source="connector:cursor")
    fake.answer = lambda body: verdict(contradicts=old.id, note="They moved.")
    out = Proposals(vault).propose(app_connector(vault), "add", content="Moved to Lisbon in 2026-09 from Shanghai",
                                   reason="said so", check=True)
    p = Proposals(vault).get(out["proposal_id"])
    assert p.op == "update" and p.original_op == "add" and p.memory_id == old.id and p.conflict
    assert p.checked_by == "assistant" and "Contradicts" in p.check_note and "They moved." in p.check_note
    assert out["status"] == "applied" and out["checked_by"] == "assistant"  # an AI-written memory: undoable
    assert vault.get(old.id).content == "Moved to Lisbon in 2026-09 from Shanghai"
    sent = user_of(fake.bodies()[0])
    assert "Moved to Lisbon" in sent and old.id in sent
    assert [c["feature"] for c in calls(vault)] == ["check"]


def test_check_cannot_override_a_protected_memory(vault, fake):
    turn_on(vault)
    mine = vault.add("work", "Works at Acme as a designer")
    fake.answer = lambda body: verdict(contradicts=mine.id)
    out = Proposals(vault).propose(app_connector(vault), "add", content="Quit the job to travel for a year",
                                   check=True)
    assert out["status"] == "pending" and out["hold"] == "protected" and out["op"] == "update"
    assert vault.get(mine.id).content == "Works at Acme as a designer"


def test_check_picks_a_topic_only_among_the_apps_non_private_topics(vault, fake):
    turn_on(vault)
    fake.answer = lambda body: verdict(topic="interests")
    out = Proposals(vault).propose(app_connector(vault), "add", content="Collects vinyl records", check=True)
    assert out["topic"] == "interests" and out["status"] == "applied"
    topics = user_of(fake.bodies()[0])
    assert "- interests:" in topics and "health" not in topics and "finance" not in topics  # private: not sent
    fake.answer = lambda body: verdict(topic="health")  # not offered, so ignored
    out = Proposals(vault).propose(app_connector(vault), "add", content="Plays chess every Sunday", check=True)
    assert out["topic"] != "health"
    Connectors(vault).update("claude-code", blocked_topics=["interests"])
    fake.answer = lambda body: verdict(topic="interests")
    v = check_proposal(vault, app_connector(vault), "add", content="Likes jazz concerts", memory_id=None, topic=None)
    assert v is not None and v.topic is None and "- interests:" not in user_of(fake.bodies()[-1])


@pytest.mark.parametrize("mode", ["propose", "auto"])
def test_sensitive_goes_to_a_private_topic_or_waits(vault, fake, mode):
    turn_on(vault)
    con = app_connector(vault, write=mode)
    fake.answer = lambda body: verdict(sensitive="health")
    out = Proposals(vault).propose(con, "add", content="Takes insulin for diabetes", topic="lifestyle", check=True)
    assert out["status"] == "pending" and out["hold"] == "private" and out["topic"] == "health"
    p = Proposals(vault).get(out["proposal_id"])
    assert "Health" in p.check_note and "private" in p.check_note

    fake.answer = lambda body: verdict(sensitive="credentials")
    out = Proposals(vault).propose(con, "add", content="Their wifi password is hunter2-blue", topic="lifestyle",
                                   check=True)
    assert out["status"] == "pending" and out["hold"] == "sensitive" and "sensitive" in out["message"]
    assert not any("hunter2" in m.content for m in vault.memories())


def test_private_memories_are_never_sent_to_a_remote_check(vault, fake):
    turn_on(vault, kind="key", protocol="openai", base_url="https://models.example/v1", api_key="sk-x")
    secret = vault.add("health", "Allergic to penicillin since childhood")
    vault.add("lifestyle", "Allergic to cats, sneezes around them")
    fake.answer = lambda body: verdict()
    con = app_connector(vault, include_private=True)
    Proposals(vault).propose(con, "add", content="Allergic to cats and to dogs too", check=True)
    sent = user_of(fake.bodies()[0])
    assert "Allergic to cats" in sent and "penicillin" not in sent and secret.id not in sent
    # A proposal that quotes a private memory isn't sent at all: filed by the rules.
    out = Proposals(vault).propose(con, "add", content="Allergic to penicillin since childhood, badly", check=True)
    assert len(fake.requests) == 1 and "checked_by" not in out


def test_the_check_cant_pull_a_topicless_add_out_of_a_private_topic(vault, fake):
    turn_on(vault)
    fake.answer = lambda body: verdict(topic="work")
    for mode, content in (("propose", "Earns 90k salary a year"), ("auto", "Pays a mortgage on a salary of 90k")):
        out = Proposals(vault).propose(app_connector(vault, write=mode), "add", content=content, check=True)
        assert out["topic"] == "finance" and out["status"] == "pending" and out["hold"] == "private"
        assert out["checked_by"] == "assistant"


CASES = [  # (content, topic, setup memories [(topic, content, actor)], verdict maker)
    ("Earns 90k salary a year", None, [], lambda ids: {"topic": "work"}),
    ("Earns 90k salary a year", None, [("work", "Works at Acme as an engineer", "connector:x")],
     lambda ids: {"contradicts": ids[0]}),
    ("Quit the job to travel", None, [("work", "Works at Acme as a designer", "user")],
     lambda ids: {"contradicts": ids[0], "topic": "interests"}),
    ("Prefers green tea", "lifestyle", [("health", "Avoids caffeine for heart reasons", "user")],
     lambda ids: {"duplicate_of": ids[0]}),
    ("Collects vinyl records", None, [], lambda ids: {"topic": "health"}),
    ("Has a cat named Miso", "people", [("people", "Has a dog named Rex", "connector:x")],
     lambda ids: {"contradicts": ids[0], "sensitive": "money"}),
]


TIDY = {"hold": "review", "until": "2027-01", "batch_id": "tidy-2026-10-25-abcd"}  # how a tidy-up proposes


@pytest.mark.parametrize("tidy", [False, True], ids=["app", "tidy-style"])
@pytest.mark.parametrize("mode", ["propose", "auto"])
@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("case", range(len(CASES)))
def test_a_check_is_never_less_restrictive_than_the_rules(tmp_path, fake, mode, approval, case, tidy):
    """Also for a proposal that asks to wait (`hold="review"`) and carries a review date and a batch, as the
    tidy-up's do: the check keeps the wait it asked for, the date and the batch."""
    from openmymemo.vault import Vault

    content, topic, memories, make = CASES[case]
    results = []
    for name, check in (("rules", False), ("checked", True)):
        v = Vault.create(tmp_path / name)
        v.update_settings(require_approval=approval)
        ids = [v.add(t, c, actor=a, source="user" if a == "user" else a).id for t, c, a in memories]
        turn_on(v)
        con = app_connector(v, write=mode, include_private=True)
        fake.answer = lambda body, ids=ids: verdict(**make(ids))
        try:
            out = Proposals(v).propose(con, "add", content=content, topic=topic, check=check,
                                       **(TIDY if tidy else {}))
        except Exception as e:  # noqa: BLE001
            out = {"status": "refused", "error": type(e).__name__}
        private = v.private_topics()
        filed = Proposals(v).get(out["proposal_id"]) if out.get("proposal_id") else None
        results.append((out, private, filed))
    (base, private, _), (checked, _, filed) = results
    rank = {"applied": 0, "duplicate": 1, "pending": 2, "refused": 3}
    assert rank[checked["status"]] >= rank[base["status"]] or base["status"] == "duplicate", (base, checked)
    if base.get("topic") in private:
        assert checked.get("topic") in private or checked["status"] == "pending", (base, checked)
    if tidy and checked["status"] == "pending":
        assert base["status"] == "pending" and checked["hold"], (base, checked)  # some wait, at least as strict
        assert filed.until == TIDY["until"] and filed.batch_id == TIDY["batch_id"], filed
    if tidy:  # it asked to wait: neither version applies without the person
        assert "applied" not in (base["status"], checked["status"]), (base, checked)


def test_a_slow_check_doesnt_hold_up_the_app(vault, fake):
    turn_on(vault)
    save_settings(check_budget=0.5)
    release = threading.Event()

    def slow(body):
        release.wait(5)
        return verdict(sensitive="credentials")

    fake.answer = slow
    started = time.monotonic()
    out = Proposals(vault).propose(app_connector(vault), "add", content="Prefers dark mode everywhere", check=True)
    took = time.monotonic() - started
    release.set()
    assert took < 2.5 and out["status"] == "applied" and "checked_by" not in out
    assert Proposals(vault).get(out["proposal_id"]).checked_by is None


@pytest.mark.parametrize("failure", ["http", "invalid", "raises"])
def test_a_failed_check_files_by_todays_rules(vault, fake, monkeypatch, failure):
    turn_on(vault)
    if failure == "http":
        fake.status = 500
    elif failure == "invalid":
        fake.answer = lambda body: "not json"
    else:
        import openmymemo.assistant.checks as checks

        monkeypatch.setattr(checks, "_check", lambda *a, **k: 1 / 0)
    out = Proposals(vault).propose(app_connector(vault), "add", content="Prefers tea over coffee", check=True)
    assert out["status"] == "applied" and "checked_by" not in out


def test_no_check_when_off_unneeded_or_for_the_assistant_itself(vault, fake):
    con = app_connector(vault)
    out = Proposals(vault).propose(con, "add", content="Runs marathons", check=True)  # Assistant off
    assert out["status"] == "applied" and not fake.requests
    a = turn_on(vault)
    save_settings(features={"check": False})
    Proposals(vault).propose(con, "add", content="Speaks Portuguese", check=True)
    assert not fake.requests
    save_settings(features={"check": True})
    Proposals(vault).propose(con, "add", content="Speaks Portuguese", check=True)  # a duplicate by the rules
    Proposals(vault).propose(con, "add", content="Bakes sourdough bread", topic="health", check=True)  # private
    Proposals(vault).propose(con, "add", content="Has a cat named Miso", check=False)  # an import, say
    a.propose("add", content="Lives near the sea", reason="test")
    assert check_proposal(vault, a.connector(), "add", content="x y z", memory_id=None, topic=None) is None
    assert not fake.requests


def test_mcp_and_http_proposals_are_checked(vault, fake):
    turn_on(vault)
    fake.answer = lambda body: verdict(sensitive="money")
    c, token = Connectors(vault).create("Script", "http")
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    out = api.post("/v1/propose", headers={"Authorization": f"Bearer {token}"},
                   json={"op": "add", "content": "Earns 90k a year", "topic": "work"}).json()
    assert out["status"] == "pending" and out["topic"] == "finance" and out["checked_by"] == "assistant"
    rows = api.get("/api/proposals", headers=H).json()
    assert rows[0]["checked_by"] == "assistant" and rows[0]["check_note"]


# ---- reading chats ----------------------------------------------------------------------------------------------


def test_chatgpt_export_follows_the_shown_branch():
    convs = chats.parse_export("chatgpt-export", (FIXTURES / "chatgpt_conversations.json").read_bytes())
    assert [c.title for c in convs] == ["Lisbon trip", "Old chat"]
    lisbon = convs[0]
    assert [(m.role, m.text[:20]) for m in lisbon.messages] == [
        ("user", "I'm vegetarian and I"), ("assistant", "Try Ao 26 for vegan "), ("user", "Thanks! My API key i")]
    assert lisbon.messages[0].at == "2025-09-16T05:20:00+00:00" and lisbon.id == "c-new"
    with pytest.raises(ValueError, match="Claude's export"):
        chats.parse_export("chatgpt-export", (FIXTURES / "claude_conversations.json").read_text())
    with pytest.raises(ValueError):
        chats.parse_export("claude-export", "{not json")


def test_claude_export():
    [conv] = chats.parse_export("claude-export", (FIXTURES / "claude_conversations.json").read_text())
    assert conv.title == "Garden planning" and conv.updated_at == "2026-05-01T10:05:00+00:00"
    assert [(m.role, m.text) for m in conv.messages] == [
        ("user", "I grow tomatoes on my balcony in Berlin."), ("assistant", "Great, here are some tips."),
        ("user", "Please always answer in German.")]


def test_claude_code_transcript_keeps_only_what_people_typed_and_replies():
    conv = chats.parse_claude_code((FIXTURES / "claude_code.jsonl").read_text().splitlines(), "x")
    assert conv.id == "s-123" and conv.title == "Fix the login bug (shop)"
    assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert conv.messages[0].text.startswith("I prefer pnpm") and conv.messages[2].text.startswith("Also: I'm based")
    assert not any("subagent" in m.text or "login()" in m.text or "/clear" in m.text for m in conv.messages)


def test_codex_session_skips_environment_and_instructions():
    conv = chats.parse_codex((FIXTURES / "codex_rollout.jsonl").read_text().splitlines(), "x")
    assert conv.id == "0199-abcd" and conv.title.endswith("(api)")
    assert [(m.role, m.text[:20]) for m in conv.messages] == [("user", "I always write commi"),
                                                               ("assistant", "Understood.")]


def test_chunks_go_newest_first_mask_secrets_and_leave_out_private_memories(vault):
    convs = chats.parse_export("chatgpt-export", (FIXTURES / "chatgpt_conversations.json").read_bytes())
    out = chats.chunks(convs, forbidden=["i work as a nurse in porto."], chunk_chars=200)
    text = "\n".join(c.text for c in out)
    assert text.index("Lisbon trip") < text.index("Old chat") if "Old chat" in text else True
    assert "nurse" not in text and "sk-abcdef" not in text and "[secret]" in text
    assert all(c.chars <= 400 for c in out) and len(out) >= 2
    est = chats.estimate(out)
    assert est["calls"] == len(out) and est["tokens"] > est["tokens_in"] > 0


# ---- chat-export import (§17.5) ---------------------------------------------------------------------------------


def extraction(memories):
    return lambda body: json.dumps({"memories": memories})


def test_export_import_previews_with_evidence_and_commits_it(vault, fake):
    turn_on(vault)
    vault.add("health", "Has asthma and uses an inhaler")
    convs = chats.parse_export("claude-export", (FIXTURES / "claude_conversations.json").read_text())

    def answer(body):
        assert "Has asthma" not in user_of(body)
        return json.dumps({"memories": [
            {"content": "Grows tomatoes on a balcony in Berlin", "message": 1, "quote": "I grow tomatoes on my balcony",
             "topic": "interests", "kind": "fact"},
            {"content": "Wants answers in German", "message": 3, "quote": "made up words", "kind": "instruction"},
            {"content": "Gives gardening tips", "message": 2, "quote": "tips"},  # the assistant's: dropped
            {"content": "Has a heart condition", "message": 1, "quote": "x", "sensitive": True},
        ]})

    fake.answer = answer
    job = chats.ChatImport(vault, "claude-export", convs)
    plan = job.plan()
    assert plan["ready"] and plan["conversations"] == 1 and plan["estimate"]["calls"] == 1
    items = job.run()
    assert [i.content for i in items] == ["Grows tomatoes on a balcony in Berlin", "Wants answers in German",
                                          "Has a heart condition"]
    tomato, german, heart = items
    assert tomato.evidence == '“I grow tomatoes on my balcony” — Claude, "Garden planning", 2026-05-01'
    assert german.evidence.startswith("“Please always answer in German.”")  # the real words, not the model's
    assert tomato.topic == "interests" and tomato.created_at == "2026-05-01" and tomato.by == "assistant"
    assert heart.flag == "sensitive" and not heart.include
    ids = transfer.commit(vault, items, "claude-export")
    assert len(ids) == 2 and vault.get(ids[1]).evidence == german.evidence
    assert vault.get(ids[1]).topic == "from-claude-export" and vault.get(ids[0]).topic == "interests"


def test_export_import_needs_the_assistant_and_stops_at_the_limit(vault, fake):
    convs = chats.parse_export("chatgpt-export", (FIXTURES / "chatgpt_conversations.json").read_bytes())
    plan = chats.ChatImport(vault, "chatgpt-export", convs).plan()
    assert not plan["ready"] and "off" in plan["reason"]
    turn_on(vault)
    fake.answer = extraction([])
    job = chats.ChatImport(vault, "chatgpt-export", convs)
    job.chunks = chats.chunks(convs, chunk_chars=150)
    assert len(job.chunks) >= 2
    assert job.run(max_calls=1) == [] and len(fake.requests) == 1
    assert "Lisbon" in user_of(fake.bodies()[0])  # newest first


def test_cli_imports_a_chatgpt_export_zip(vault, fake, tmp_path):
    turn_on(vault)
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(FIXTURES / "chatgpt_conversations.json", "conversations.json")
    fake.answer = extraction([{"content": "Is vegetarian", "message": 1, "quote": "I'm vegetarian", "kind": "fact"}])
    out = runner.invoke(app, ["--vault", str(vault.root), "import", "chatgpt-export", str(z), "--yes"])
    assert out.exit_code == 0, out.output
    assert "calls" in out.output and "tokens" in out.output and "“I'm vegetarian”" in out.output
    [m] = [m for m in vault.memories() if m.topic == "from-chatgpt-export"]
    assert m.content == "Is vegetarian" and "Lisbon trip" in m.evidence


def test_cli_chat_import_says_it_needs_the_assistant(vault):
    out = runner.invoke(app, ["--vault", str(vault.root), "import", "claude-export",
                              str(FIXTURES / "claude_conversations.json"), "--yes"])
    assert out.exit_code == 1 and "needs the OpenMyMemo Assistant" in out.output


def test_panel_reads_chats_from_the_home_folder_only(vault, fake, tmp_path):
    turn_on(vault)
    home = Path.home()
    shutil.copy(FIXTURES / "claude_conversations.json", home / "conversations.json")
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    plan = api.post("/api/import/chats", headers=H,
                    json={"source": "claude-export", "path": "~/conversations.json"}).json()
    assert plan["state"] == "planned" and plan["estimate"]["calls"] == 1 and plan["ready"] and not fake.requests
    outside = tmp_path / "outside.json"
    shutil.copy(FIXTURES / "claude_conversations.json", outside)
    r = api.post("/api/import/chats", headers=H, json={"source": "claude-export", "path": str(outside)})
    assert r.status_code == 400 and "home folder" in r.json()["detail"]
    r = api.post("/api/import/chats", headers=H, json={"source": "claude-export", "path": "~/../.."})
    assert r.status_code == 400

    fake.answer = extraction([{"content": "Grows tomatoes", "message": 1, "quote": "I grow tomatoes"}])
    api.post(f"/api/import/chats/{plan['job']}/start", headers=H, json={})
    for _ in range(100):
        status = api.get(f"/api/import/chats/{plan['job']}", headers=H).json()
        if status["state"] == "done":
            break
        time.sleep(0.05)
    assert status["state"] == "done" and status["items"][0]["evidence"].startswith("“I grow tomatoes”")
    assert api.post(f"/api/import/chats/{plan['job']}/start", headers=H, json={}).status_code == 409
    r = api.post("/api/import/commit", headers=H, json={"source": "claude-export", "items": status["items"]})
    assert r.status_code == 200 and len(r.json()["memory_ids"]) == 1
    assert api.delete(f"/api/import/chats/{plan['job']}", headers=H).json() == {"ok": True}
    assert api.get(f"/api/import/chats/{plan['job']}", headers=H).status_code == 404


def test_panel_takes_an_uploaded_transcript(vault, fake):
    turn_on(vault)
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    plan = api.post("/api/import/chats", headers=H, json={
        "source": "codex-sessions", "name": "rollout.jsonl",
        "text": (FIXTURES / "codex_rollout.jsonl").read_text()}).json()
    assert plan["conversations"] == 1 and plan["estimate"]["calls"] == 1
    r = api.post("/api/import/chats", headers=H, json={"source": "nope", "name": "x", "text": "y"})
    assert r.status_code == 400


# ---- session-end extraction (§17.7, "After a session") ----------------------------------------------------------


def place_transcript(name="claude_code.jsonl", app_id="claude-code"):
    if app_id == "claude-code":
        folder = Path.home() / ".claude/projects/-home-ann-shop"
    else:
        folder = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions/2026/10/16"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("s-123.jsonl" if app_id == "claude-code" else "rollout-2026-10-16-0199-abcd.jsonl")
    shutil.copy(FIXTURES / name, target)
    return target


def hook_json(path, event="SessionEnd", **kw):
    return json.dumps({"session_id": "s-123", "transcript_path": str(path), "hook_event_name": event, **kw})


def test_hook_asks_first_and_does_nothing_without_consent(vault, fake):
    path = place_transcript()
    assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "off"
    turn_on(vault)
    assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "off"  # session is off by default
    save_settings(features={"session": True})
    assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "asked"
    assert load_settings().session_apps == {"claude-code": "asked"}
    assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "asked"
    save_settings(session_apps={"claude-code": "declined"})
    assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "declined"
    save_settings(session_apps={"claude-code": "allowed"})
    out = sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False)
    assert out.startswith("spawn:") and "--worker" in out and str(path) in json.loads(out.removeprefix("spawn:"))
    assert not fake.requests  # the hook itself never calls the model


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "{" * 70_000])
def test_hook_is_silent_and_never_fails(vault, stdin):
    turn_on(vault)
    save_settings(features={"session": True}, session_apps={"claude-code": "allowed"})
    out = runner.invoke(app, ["--vault", str(vault.root), "session-end", "--hook", "--connector", "claude-code"],
                        input=stdin)
    assert out.exit_code == 0 and out.output == ""
    assert sessions.hook(vault.root, "claude-code", stdin, spawn=False) in ("no-input", "error")


def test_hook_only_reads_transcripts_where_the_app_keeps_them(vault, tmp_path):
    turn_on(vault)
    save_settings(features={"session": True}, session_apps={"claude-code": "allowed", "codex": "allowed"})
    elsewhere = Path.home() / "Documents/notes.jsonl"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("{}")
    outside = tmp_path / "t.jsonl"
    outside.write_text("{}")
    for path in (elsewhere, outside, Path.home() / ".claude/../.bashrc", "/etc/passwd"):
        assert sessions.hook(vault.root, "claude-code", hook_json(path), spawn=False) == "no-transcript"
    assert sessions.hook(vault.root, "unknown", hook_json(outside), spawn=False) == "unknown-app"
    assert sessions.hook(vault.root, "claude-code", hook_json(outside, stop_hook_active=True), spawn=False) \
        == "stop-active"
    # Codex without a path: found by its session id.
    codex_file = place_transcript("codex_rollout.jsonl", "codex")
    out = sessions.hook(vault.root, "codex", json.dumps({"session_id": "0199-abcd", "hook_event_name": "Stop"}),
                        spawn=False)
    assert str(codex_file) in out and "Stop" in out


def test_worker_files_what_the_app_didnt_save_with_evidence(vault, fake):
    a = turn_on(vault)
    save_settings(features={"session": True}, session_apps={"claude-code": "allowed"})
    con = app_connector(vault)
    Proposals(vault).propose(con, "add", content="Prefers pnpm over npm", reason="said so",
                             created_at="2026-10-17T09:01:30+00:00")
    vault.add("health", "Takes melatonin to sleep")
    path = place_transcript()

    def answer(body):
        text = user_of(body)
        assert "already saved" in text and "Prefers pnpm over npm" in text and "melatonin" not in text
        assert "tool_result" not in text and "login()" not in text
        return json.dumps({"memories": [
            {"content": "Prefers pnpm to npm in every project", "message": 1, "quote": "I prefer pnpm over npm"},
            {"content": "Is based in Lisbon", "message": 3, "quote": "I'm based in Lisbon", "kind": "fact"},
            {"content": "Has insomnia", "message": 3, "quote": "x", "sensitive": True},
        ]})

    fake.answer = answer
    result = sessions.work(vault, "claude-code", path, "s-123", assistant=load_and(a))
    assert result["result"] == "ok" and result["calls"] == 1 and result["applied"] == 1, result
    [p] = Proposals(vault).list(connector_id="assistant")
    assert p.content == "Is based in Lisbon" and p.status == "applied"
    assert p.evidence.startswith("“I'm based in Lisbon”") and "Claude Code" in p.evidence and "2026-10-17" in p.evidence
    assert "Claude Code" in p.reason
    assert [c["feature"] for c in calls(vault)] == ["session"]
    state = json.loads(sessions.state_path().read_text())
    assert state["claude-code:s-123"]["messages"] == 4 and "Lisbon" not in sessions.state_path().read_text()
    again = sessions.work(vault, "claude-code", path, "s-123")
    assert again["result"] == "too-little" and len(fake.requests) == 1  # nothing new since


def load_and(a):
    from openmymemo.assistant import Assistant

    return Assistant(a.vault, load_settings())


def test_worker_needs_the_feature_consent_and_enough_new_messages(vault, fake):
    turn_on(vault)
    path = place_transcript("codex_rollout.jsonl", "codex")
    assert sessions.work(vault, "codex", path, "x")["result"] == "off"
    save_settings(features={"session": True})
    assert sessions.work(vault, "codex", path, "x")["result"] == "no-consent"
    save_settings(session_apps={"codex": "allowed"})
    assert sessions.work(vault, "codex", path, "x", event="Stop")["result"] == "too-little"  # 1 of 3
    fake.status = 500
    assert sessions.work(vault, "codex", path, "x")["failed"] == 1
    assert not sessions.state_path().exists()  # tried again next time
    fake.status = 200
    fake.answer = extraction([])
    assert sessions.work(vault, "codex", path, "x", event="SessionEnd")["result"] == "ok"
    assert sessions.work(vault, "codex", Path("/etc/passwd"), "y")["result"] == "no-transcript"


def test_panel_and_cli_ask_for_consent(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    status = api.post("/api/assistant/sessions/codex", headers=H, json={"allow": True}).json()
    assert status["session_apps"] == {"claude-code": None, "codex": "allowed"}
    assert api.post("/api/assistant/sessions/cursor", headers=H, json={"allow": True}).status_code == 400
    out = runner.invoke(app, ["--vault", str(vault.root), "assistant", "sessions", "--decline", "codex"])
    assert out.exit_code == 0 and "Codex" in out.output and "not allowed" in out.output
    assert load_settings().session_apps == {"codex": "declined"}


def test_codex_setup_adds_and_removes_the_stop_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    path = tmp_path / "codex/hooks.json"
    foreign = {"hooks": [{"type": "command", "command": "./lint.sh"}]}
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))
    assert codex.install(codex.hook_command(), path, end_command=codex.end_command()) is True
    data = json.loads(path.read_text())
    assert data["hooks"]["Stop"][0] == foreign
    assert data["hooks"]["Stop"][1] == {"hooks": [codex.end_entry(codex.end_command())]}
    assert codex.install(codex.hook_command(), path, end_command=codex.end_command()) is False
    cli = codex.end_command(["openmymemo", "--vault", "/v"])
    assert "session-end --hook --connector codex" in cli and cli.endswith("exit 0")
    assert codex.remove(path) is True
    assert json.loads(path.read_text())["hooks"] == {"Stop": [foreign]}


def test_codex_stop_hook_never_fails_without_a_plugin(tmp_path):
    import subprocess

    env = {**os.environ, "CODEX_HOME": str(tmp_path / "none")}
    out = subprocess.run(["sh", "-c", codex.end_command()], capture_output=True, text=True, env=env, input="{}")
    assert out.returncode == 0 and out.stdout == ""


def test_the_claude_code_plugin_hands_session_end_to_the_script():
    root = Path(__file__).parent.parent
    hooks = json.loads((root / "integrations/claude-code/hooks/hooks.json").read_text())["hooks"]
    [handler] = hooks["SessionEnd"][0]["hooks"]
    assert handler["args"][0].endswith("integrations/hooks/session-end.sh") and handler["args"][2] == "claude-code"
    assert (root / "integrations/hooks/session-end.sh").is_file()
