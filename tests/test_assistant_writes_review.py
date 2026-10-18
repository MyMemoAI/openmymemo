"""Regression tests from the review of the Assistant's writes: keys in what describes a conversation, settings that
change while chats are being read, and long conversations read from their end. Assertions are on the complete
request bodies the fake endpoint received."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_assistant import turn_on
from test_assistant_privacy import fake as fake  # noqa: F401 - the fixture

from openmymemo import transfer
from openmymemo.assistant import chats, load_settings, save_settings
from openmymemo.assistant import session as sessions
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
KEYS = ["sk-ant-api03-TITLEKEYxxxxxxxxxxxxxxxx", "ghp_NAMEKEYabcdefghijklmnopqrstu", "AKIAPATHKEY123456789",
        "xoxb-1234567890-CWDKEYabcdef", "sk-proj-TIMEKEYyyyyyyyyyyyyyyyyy", "github_pat_IDKEY_aaaaaaaaaaaaaaaaaaaaaa",
        "AIzaSyMETAKEYzzzzzzzzzzzzzzzzzzzzzzzzzz"]


def bodies(fake) -> str:
    return " ".join(json.dumps(b, ensure_ascii=False) for b in fake.bodies())


def leaked_keys(text: str) -> list[str]:
    return [k for k in KEYS if k in text or k[:14] in text]


def extract_all(body) -> str:
    """Answer with one memory per numbered user message, so evidence is built from each."""
    text = body["messages"][1]["content"]
    nums = [int(line.split(" ", 1)[0][1:]) for line in text.splitlines() if line.startswith("#") and " user" in line]
    return json.dumps({"memories": [{"content": f"Fact number {n} about the person", "message": n, "quote": "x"}
                                    for n in nums]})


# ---- 1. keys in metadata ---------------------------------------------------------------------------------------


def chatgpt_with_keys() -> str:
    t, n, p, c, ts, i, m = KEYS
    return json.dumps([{
        "title": f"Deploy notes {t}", "id": i, "conversation_id": i, "create_time": ts, "update_time": 1_760_000_000,
        "current_node": n, "default_model_slug": m,
        "mapping": {n: {"id": n, "parent": None, "children": [], "message": {
            "id": n, "author": {"role": "user", "name": p, "metadata": {"k": c}}, "create_time": 1_760_000_000,
            "content": {"content_type": "text", "parts": ["I deploy on Fridays"]}, "metadata": {"model_slug": m}}}},
    }])


def claude_with_keys() -> str:
    t, n, p, c, ts, i, m = KEYS
    return json.dumps([{"uuid": i, "name": f"{t} rollout", "created_at": ts, "updated_at": "2026-05-01T10:00:00Z",
                        "account": {"uuid": p}, "chat_messages": [
                            {"uuid": n, "sender": "human", "text": "I run the on-call rota", "created_at": ts,
                             "attachments": [{"file_name": c, "extracted_content": m}],
                             "files": [{"file_name": p}]}]}])


def claude_code_with_keys() -> list[str]:
    t, n, p, c, ts, i, m = KEYS
    return [json.dumps({"type": "summary", "summary": f"Rotate {t}"}),
            json.dumps({"type": "user", "sessionId": i, "cwd": f"/home/ann/{c}", "gitBranch": n, "version": m,
                        "timestamp": ts, "message": {"role": "user", "content": "I prefer small commits"}}),
            json.dumps({"type": "user", "sessionId": i, "cwd": f"/home/ann/{c}", "timestamp": "2026-10-17T09:00:00Z",
                        "message": {"role": "user", "content": "I review PRs in the morning"}})]


def codex_with_keys() -> list[str]:
    t, n, p, c, ts, i, m = KEYS
    return [json.dumps({"timestamp": ts, "type": "session_meta",
                        "payload": {"id": i, "cwd": f"/home/ann/{c}", "instructions": t, "git": {"branch": n}}}),
            json.dumps({"timestamp": ts, "type": "response_item", "payload": {
                "type": "message", "role": "user", "id": p,
                "content": [{"type": "input_text", "text": f"{t[:0]}I write tests first"}]}})]


@pytest.mark.parametrize("source", ["chatgpt-export", "claude-export", "claude-code-sessions", "codex-sessions"])
def test_keys_in_conversation_metadata_never_go_out_or_into_evidence(vault, fake, source):
    turn_on(vault, kind="key", model="m", api_key="k", base_url="https://api.example.com/v1")
    fake.answer = extract_all
    if source == "chatgpt-export":
        convs = chats.parse_export(source, chatgpt_with_keys())
    elif source == "claude-export":
        convs = chats.parse_export(source, claude_with_keys())
    elif source == "claude-code-sessions":
        convs = [chats.parse_claude_code(claude_code_with_keys(), KEYS[2])]
    else:
        convs = [chats.parse_codex(codex_with_keys(), KEYS[2])]
    job = chats.ChatImport(vault, source, convs)
    items = job.run()
    assert fake.requests and items
    sent = bodies(fake)
    assert leaked_keys(sent) == [], sent
    kept = json.dumps([i.to_dict() for i in items], ensure_ascii=False)
    assert leaked_keys(kept) == []
    ids = transfer.commit(vault, items, source)
    assert all(not leaked_keys(vault.get(m).evidence or "") for m in ids)


def test_keys_in_a_session_and_in_what_the_app_saved_never_go_out(vault, fake):
    turn_on(vault, kind="key", model="m", api_key="k", base_url="https://api.example.com/v1")
    save_settings(features={"session": True}, session_apps={"claude-code": "allowed"})
    from openmymemo.connectors import Connectors

    con = Connectors(vault).ensure("claude-code", "Claude Code")
    Proposals(vault).propose(con, "add", content=f"Uses the deploy key {KEYS[0]} for staging",
                             created_at="2026-10-17T08:00:00+00:00")
    path = Path.home() / f".claude/projects/{KEYS[3]}/s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(claude_code_with_keys()) + "\n")
    fake.answer = extract_all
    out = sessions.work(vault, "claude-code", path, "s")
    assert out["result"] == "ok" and fake.requests
    assert leaked_keys(bodies(fake)) == []
    assert all(not leaked_keys(p.evidence or "") for p in Proposals(vault).list(connector_id="assistant"))


# ---- 3. settings that change while reading -----------------------------------------------------------------------


def long_conversation(n: int = 40) -> chats.Conversation:
    msgs = [chats.Message("user", f"Message {i:02d}: " + "words " * 60, f"2026-10-{1 + i % 28:02d}T09:00:00+00:00")
            for i in range(n)]
    return chats.Conversation(id="c", title="Long", source="chatgpt-export", updated_at="2026-10-30T00:00:00+00:00",
                              messages=msgs)


@pytest.mark.parametrize("change", ["off", "feature", "endpoint", "model"])
def test_chat_import_stops_when_the_settings_change_between_calls(vault, fake, change):
    turn_on(vault)
    job = chats.ChatImport(vault, "chatgpt-export", [long_conversation()])
    job.chunks = chats.chunks(job.conversations, chunk_chars=800)
    assert len(job.chunks) >= 3

    def answer(body):
        if len(fake.requests) == 1:
            if change == "off":
                save_settings(enabled=False)
            elif change == "feature":
                save_settings(features={"import": False})
            elif change == "endpoint":
                save_settings(base_url="http://127.0.0.1:1234/v1")
            else:
                save_settings(model="another-model")
        return json.dumps({"memories": []})

    fake.answer = answer
    job.run()
    assert len(fake.requests) == 1 and job.error == chats.SETTINGS_CHANGED


def test_chat_import_doesnt_start_when_the_settings_changed_after_the_estimate(vault, fake):
    turn_on(vault)
    (Path.home() / "conversations.json").write_text(json.dumps([{
        "title": "T", "update_time": 1_760_000_000, "current_node": "u", "mapping": {"u": {"parent": None, "message": {
            "author": {"role": "user"}, "create_time": 1_760_000_000,
            "content": {"content_type": "text", "parts": ["I like rowing"]}}}}}]))
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    for change in ({"base_url": "http://127.0.0.1:1234/v1"}, {"enabled": False}):
        plan = api.post("/api/import/chats", headers=H,
                        json={"source": "chatgpt-export", "path": "~/conversations.json"}).json()
        save_settings(**change)
        r = api.post(f"/api/import/chats/{plan['job']}/start", headers=H, json={})
        assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert fake.requests == []


@pytest.mark.parametrize("change", ["consent", "off", "endpoint"])
def test_session_worker_stops_and_files_nothing_when_settings_change(vault, fake, change):
    turn_on(vault)
    save_settings(features={"session": True}, session_apps={"codex": "allowed"})
    path = Path.home() / ".codex/sessions/2026/10/17/rollout-x-s.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"timestamp": "2026-10-17T09:00:00Z", "type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Fact {i}: " + "words " * 400}]}}
        for i in range(30)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def answer(body):
        if len(fake.requests) == 1:
            if change == "consent":
                save_settings(session_apps={"codex": "declined"})
            elif change == "off":
                save_settings(enabled=False)
            else:
                save_settings(base_url="http://127.0.0.1:1234/v1")
        return json.dumps({"memories": [{"content": "Writes tests first", "message": 1, "quote": "Fact"}]})

    fake.answer = answer
    out = sessions.work(vault, "codex", path, "s")
    assert out["result"] == "stopped" and len(fake.requests) == 1
    assert Proposals(vault).list(connector_id="assistant") == []
    assert not sessions.state_path().exists()


def test_session_worker_rereads_consent_after_waiting_for_the_lock(vault, fake, monkeypatch):
    turn_on(vault)
    save_settings(features={"session": True}, session_apps={"claude-code": "allowed"})
    path = Path.home() / ".claude/projects/p/s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(claude_code_with_keys()) + "\n")
    real = sessions._session_lock

    def lock_then_decline():
        save_settings(session_apps={"claude-code": "declined"})  # decided while this worker waited
        return real()

    monkeypatch.setattr(sessions, "_session_lock", lock_then_decline)
    assert sessions.work(vault, "claude-code", path, "s")["result"] == "no-consent"
    assert fake.requests == [] and load_settings().session_apps["claude-code"] == "declined"


# ---- 4. a long conversation is read from its end -----------------------------------------------------------------


def test_a_long_conversation_is_read_newest_first(vault, fake):
    turn_on(vault)
    conv = long_conversation(40)
    out = chats.chunks([conv], chunk_chars=3000)
    assert len(out) >= 4
    first = out[0].text
    assert "Message 39" in first and "Message 00" not in first
    numbers = [int(line.split("Message ")[1][:2]) for line in first.splitlines() if "Message " in line]
    assert numbers == sorted(numbers)  # in order inside the chunk
    fake.answer = lambda body: json.dumps({"memories": []})
    job = chats.ChatImport(vault, "chatgpt-export", [conv])
    job.chunks = out
    job.run(max_calls=1)
    sent = bodies(fake)
    assert len(fake.requests) == 1 and "Message 39" in sent and "Message 00" not in sent
