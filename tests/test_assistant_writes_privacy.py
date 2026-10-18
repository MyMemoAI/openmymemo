"""End to end: the Assistant's writes (proposal checks, chat-export import, session-end extraction) never send private
or out-of-scope memory text to the model, through the panel's routes, the CLI and the hook. Every assertion is on
the raw requests the fake endpoint received."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_assistant import turn_on
from test_assistant_privacy import WORK, leaked
from test_assistant_privacy import fake as fake  # noqa: F401 - the fixture
from typer.testing import CliRunner

from openmymemo.assistant import load_settings, save_settings
from openmymemo.assistant import session as sessions
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
SECRET = "Takes sumatriptan for cluster headaches every Thursday morning"
runner = CliRunner()


def remote(vault, **kw):
    return turn_on(vault, kind="key", model="m", api_key="k", base_url="https://api.example.com/v1", **kw)


def setup_secrets(vault):
    """A private memory, a memory outside the Assistant's read scope, and one it may read."""
    vault.add("health", SECRET)
    vault.add("work", WORK)
    vault.add("interests", "Collects vintage fountain pens from Japan")
    Connectors(vault).update("assistant", read_topics=["interests", "lifestyle", "people", "identity"])


def chatgpt_export(*messages: str) -> str:
    convs = []
    for i, text in enumerate(messages):
        convs.append({"title": f"Chat {i}", "update_time": 1_760_000_000 + i, "current_node": "u", "mapping": {
            "u": {"parent": None, "message": {"author": {"role": "user"}, "create_time": 1_760_000_000 + i,
                                              "content": {"content_type": "text", "parts": [text]}}}}})
    return json.dumps(convs)


def no_memories(body):
    return json.dumps({"memories": [], "duplicate_of": None, "contradicts": None, "sensitive": "none", "note": ""})


def test_checks_never_send_private_or_out_of_scope_memories(vault, fake):
    remote(vault)
    setup_secrets(vault)
    fake.answer = no_memories
    c, token = Connectors(vault).create("Script", "http", include_private=True)
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    for content in ("Uses ibuprofen when tired", "Meeting a partner firm about pens",
                    "Collects fountain pens and inks"):
        r = api.post("/v1/propose", headers={"Authorization": f"Bearer {token}"}, json={"op": "add", "content": content})
        assert r.status_code == 200
    assert fake.requests  # the checks did run
    assert not leaked(fake, SECRET) and not leaked(fake, WORK)
    # A proposal quoting the private memory isn't sent at all.
    before = len(fake.requests)
    api.post("/v1/propose", headers={"Authorization": f"Bearer {token}"},
             json={"op": "add", "content": "Still " + SECRET.lower()})
    assert len(fake.requests) == before


def test_panel_chat_import_leaves_out_conversations_with_private_text(vault, fake):
    remote(vault)
    setup_secrets(vault)
    fake.answer = no_memories
    export = Path.home() / "conversations.json"
    export.write_text(chatgpt_export("I love hiking in the Alps", "Reminder: " + SECRET + ", ok?",
                                     "Work update: " + WORK, "My cat is called Miso"))
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    plan = api.post("/api/import/chats", headers=H, json={"source": "chatgpt-export", "path": str(export)}).json()
    api.post(f"/api/import/chats/{plan['job']}/start", headers=H, json={})
    import time

    for _ in range(100):
        if api.get(f"/api/import/chats/{plan['job']}", headers=H).json()["state"] == "done":
            break
        time.sleep(0.05)
    assert fake.requests and "Miso" in json.dumps(fake.bodies())
    assert not leaked(fake, SECRET) and not leaked(fake, WORK)


def test_cli_chat_import_never_sends_private_text(vault, fake, tmp_path):
    remote(vault)
    setup_secrets(vault)
    fake.answer = no_memories
    export = tmp_path / "conversations.json"
    export.write_text(chatgpt_export("TAKES SUMATRIPTAN FOR CLUSTER HEADACHES, again", "Planting tulips this spring"))
    out = runner.invoke(app, ["--vault", str(vault.root), "import", "chatgpt-export", str(export), "--yes"])
    assert out.exit_code == 0, out.output
    assert "tulips" in json.dumps(fake.bodies()) and not leaked(fake, SECRET)


@pytest.mark.parametrize("app_id", ["claude-code", "codex"])
def test_session_end_never_sends_private_text(vault, fake, app_id):
    remote(vault)
    setup_secrets(vault)
    save_settings(features={"session": True}, session_apps={app_id: "allowed"})
    fake.answer = no_memories
    if app_id == "claude-code":
        path = Path.home() / ".claude/projects/p/s.jsonl"
        rows = [{"type": "user", "sessionId": "s", "timestamp": "2026-10-17T09:00:00Z",
                 "message": {"role": "user", "content": text}} for text in
                ("Reminder that I " + SECRET.lower(), "Also " + WORK)]
    else:
        path = Path.home() / ".codex/sessions/2026/10/17/rollout-x-s.jsonl"
        rows = [{"timestamp": "2026-10-17T09:00:00Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
                for text in ("Reminder that I " + SECRET.lower(), "Also " + WORK)]
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    hook = sessions.hook(vault.root, app_id, json.dumps({"session_id": "s", "transcript_path": str(path)}),
                         spawn=False)
    assert hook.startswith("spawn:")
    from openmymemo.assistant import Assistant

    result = sessions.work(vault, app_id, path, "s", assistant=Assistant(vault, load_settings()))
    assert result["result"] == "refused" and fake.requests == []
    assert not Proposals(vault).list(connector_id="assistant")
    # A clean session afterwards goes through, still without the private text.
    shutil.copy(path, path.with_name("t.jsonl"))
    clean = path.with_name("t.jsonl")
    clean.write_text(clean.read_text().replace(SECRET.lower(), "like long walks").replace(WORK, "the new office"))
    sessions.work(vault, app_id, clean, "t")
    assert fake.requests and not leaked(fake, SECRET) and not leaked(fake, WORK)
