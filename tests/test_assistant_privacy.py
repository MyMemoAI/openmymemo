"""Adversarial checks that private memories and memories outside the Assistant's read scope never reach a model
endpoint, through every public path (json_call, the panel's import and sort routes, the CLI). Every assertion is on
the raw requests the fake endpoint received."""

import json
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_assistant import SCHEMA, FakeEndpoint, split_answer, turn_on
from typer.testing import CliRunner

from openmymemo import transfer
from openmymemo.assistant import Assistant, Data, features, load_settings
from openmymemo.assistant import client as endpoint
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
SECRET = "Takes sumatriptan for cluster headaches every Thursday morning"
SECRET_ZH = "每周四早上服用舒马曲坦治疗丛集性头痛"
WORK = "Negotiating the Zephyrine acquisition with Halvorsen Partners"


def norm(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def sent(fake: FakeEndpoint) -> str:
    """Everything the endpoint received, normalized (escapes decoded, punctuation and spaces gone)."""
    return norm(" ".join(json.dumps(b, ensure_ascii=False) for b in fake.bodies()))


def leaked(fake: FakeEndpoint, secret: str, n: int = 8) -> bool:
    body, s = sent(fake), norm(secret)
    return any(s[i:i + n] in body for i in range(max(1, len(s) - n + 1)))


@pytest.fixture
def fake(monkeypatch):
    f = FakeEndpoint()
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(f))
    return f


def remote(vault, **kw):
    return turn_on(vault, kind="key", model="m", api_key="k", base_url="https://api.example.com/v1", **kw)


# ---- the content check: reformatted, partial and hidden copies ----------------------------------------------------


@pytest.mark.parametrize("pasted", [
    "- takes sumatriptan for cluster headaches",  # truncated
    "Notes: Takes sumatriptan  for *cluster* headaches, every Thursday.",  # markdown, punctuation, spacing
    "takes-sumatriptan-for-cluster-headaches-every-thursday-morning",
    "TAKES SUMATRIPTAN\nFOR CLUSTER\nHEADACHES",  # split over lines
    "Likes tea. Takes sumatriptan for cluster headaches every Thursday morning. Has a cat.",  # inside other text
    "Ｔａｋｅｓ ｓｕｍａｔｒｉｐｔａｎ ｆｏｒ ｃｌｕｓｔｅｒ",  # full-width
])
def test_reformatted_private_text_is_refused(vault, fake, pasted):
    vault.add("health", SECRET)
    a = remote(vault)
    assert a.json_call("import", "x", [pasted], SCHEMA) is None
    assert fake.requests == []


def test_everyday_phrases_shared_with_a_private_memory_still_go(vault, fake):
    vault.add("health", "Prefers not to discuss his divorce with anyone")
    a = remote(vault)
    assert a.json_call("import", "x", ["Prefers not to talk about politics"], SCHEMA) is not None
    assert a.json_call("import", "x", ["prefers not to discuss his divorce"], SCHEMA) is None


def test_cjk_private_text_without_spaces_is_refused(vault, fake):
    vault.add("health", SECRET_ZH)
    a = remote(vault)
    assert a.json_call("import", "x", ["记录：服用舒马曲坦治疗丛集性头痛，别忘了"], SCHEMA) is None
    assert fake.requests == []
    assert a.json_call("import", "x", ["我喜欢喝茶，住在里斯本"], SCHEMA) is not None  # unrelated text still goes


def test_archived_history_and_proposed_private_text_is_refused(vault, fake):
    archived = vault.add("health", "Was treated for Lyme disease in Oregon in 2019")
    vault.archive(archived.id)
    edited = vault.add("health", "Allergic to amoxicillin and related penicillins")
    vault.update(edited.id, content="Allergic to penicillins")  # the old wording lives on in history
    c, _ = Connectors(vault).create("Other app", "stdio")
    Proposals(vault).propose(c, "add", content="Sees a cardiologist at Mercy Clinic monthly", topic="finance")
    a = remote(vault)
    for text in ("Lyme disease in Oregon in 2019", "allergic to amoxicillin and related",
                 "a cardiologist at Mercy Clinic monthly"):
        assert a.json_call("import", "x", [text], SCHEMA) is None, text
    assert fake.requests == []


def test_instructions_and_labels_are_checked_too(vault, fake):
    vault.add("health", SECRET)
    a = remote(vault)
    assert a.json_call("import", f"Context: {SECRET}", ["hello"], SCHEMA) is None
    assert a.json_call("import", "x", [Data("hello", label="takes sumatriptan for cluster")], SCHEMA) is None
    assert fake.requests == []


def test_declared_ids_must_be_known_and_sendable(vault, fake):
    m = vault.add("health", SECRET)
    vault.archive(m.id)
    a = remote(vault)
    assert a.json_call("sort", "x", [Data("x", memory_ids=(m.id,))], SCHEMA) is None  # archived, still private
    assert a.json_call("sort", "x", [Data("x", memory_ids=("nosuchid",))], SCHEMA) is None
    assert fake.requests == []


def test_memory_data_keeps_only_what_may_be_sent(vault, fake):
    ok = vault.add("work", "Leads the platform team")
    private = vault.add("health", SECRET)
    marked = vault.add("work", "Salary is 180k before bonus", private=True)
    a = remote(vault)
    blocks = a.memory_data([ok, private, marked])
    assert [b.memory_ids for b in blocks] == [(ok.id,)]
    assert a.json_call("sort", "x", blocks, SCHEMA) is not None
    assert not leaked(fake, SECRET) and not leaked(fake, "Salary is 180k before bonus")


# ---- the read scope -----------------------------------------------------------------------------------------------


def test_out_of_scope_text_is_refused_even_undeclared(vault, fake):
    vault.add("work", WORK)
    a = remote(vault)
    Connectors(vault).update("assistant", read_topics=["identity", "interests"])
    assert a.json_call("import", "x", ["fyi: negotiating the Zephyrine acquisition"], SCHEMA) is None
    assert a.json_call("import", "x", [Data("y", topic="work")], SCHEMA) is None
    assert fake.requests == []


def test_private_opt_in_still_needs_the_read_scope(vault, fake):
    m = vault.add("health", SECRET)
    a = turn_on(vault, kind="local", private_topics=["health"])
    Connectors(vault).update("assistant", read_topics=["identity"])
    assert a.json_call("sort", "x", [Data("x", topic="health", memory_ids=(m.id,))], SCHEMA) is None
    assert fake.requests == []


def test_topic_lists_only_name_topics_in_scope(vault, fake):
    vault.update_topic("projects", description="Includes the secret Zephyrine deal")
    turn_on(vault)
    fake.answer = split_answer
    Connectors(vault).update("assistant", read_topics=["identity", "work", "interests"])
    features.import_preview(vault, "markdown", "- Likes jazz\n- Plays chess on Sundays")
    body = sent(fake)
    assert "zephyrine" not in body and "projectsprojects" not in body and "health" not in body
    assert norm("- work: ") in body


# ---- public paths end to end ---------------------------------------------------------------------------------------


def test_panel_import_and_sort_never_send_private_or_out_of_scope_text(vault, fake):
    vault.add("health", SECRET)
    vault.add("work", WORK)
    ids = transfer.commit(vault, [transfer.ImportItem(content=c) for c in
                                  ("Prefers tea over coffee", "Has a cat named Miso")], "chatgpt")
    other = transfer.commit(vault, [transfer.ImportItem(content="Lives near the old Zephyrine mill")], "claude")
    remote(vault)
    Connectors(vault).update("assistant", read_topics=["from-chatgpt", "identity", "interests", "lifestyle"])
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    paste = "- likes tea\n- " + SECRET.upper().replace(" ", "  ") + "\n- " + WORK.lower()
    items = api.post("/api/import/preview", headers=H, json={"source": "text", "text": paste}).json()
    assert {i["by"] for i in items} == {"rules"}  # refused: the rules split it
    r = api.post("/api/import/file", headers=H, json={"name": "CLAUDE.md", "text": paste}).json()
    assert all(i["by"] == "rules" for i in r["items"])
    fake.answer = lambda body: json.dumps({"suggestions": [
        {"id": ids[0], "topic": "lifestyle", "confidence": 0.9},
        {"id": ids[1], "topic": "work", "confidence": 0.9},  # outside its read scope: not a choice
    ]})
    rows = {r["memory_id"]: r for r in api.get("/api/sort", headers=H).json()}
    assert rows[ids[0]]["by"] == "assistant" and rows[ids[1]]["by"] == "rules"
    assert rows[other[0]]["by"] == "rules"
    for secret in (SECRET, WORK, "Lives near the old Zephyrine mill"):
        assert not leaked(fake, secret), secret
    assert "zephyrine" not in sent(fake)


def test_cli_import_and_sort_never_send_private_text(vault, fake, tmp_path):
    vault.add("health", SECRET)
    transfer.commit(vault, [transfer.ImportItem(content="Plays the cello")], "chatgpt")
    remote(vault)
    f = tmp_path / "notes.md"
    f.write_text("- Plays chess\n- " + SECRET.replace("cluster", "**cluster**") + "\n")
    runner = CliRunner()
    r = runner.invoke(app, ["--vault", str(vault.root), "import", "markdown", str(f), "--yes"])
    assert r.exit_code == 0 and "Split by rules" in r.stdout
    fake.answer = lambda body: json.dumps({"suggestions": []})
    r = runner.invoke(app, ["--vault", str(vault.root), "sort", "--yes"])
    assert r.exit_code == 0
    assert not leaked(fake, SECRET)


def test_activity_log_and_errors_carry_no_text(vault, fake):
    vault.add("health", SECRET)
    a = remote(vault)
    a.json_call("import", "x", [SECRET], SCHEMA)
    fake.status = 400
    a.json_call("import", "x", ["Prefers tea over coffee, always"], SCHEMA)
    log = vault.activity_path.read_text()
    assert "sumatriptan" not in log and "Prefers tea" not in log
    assert "sumatriptan" not in (a.last_error or "")


# ---- where requests go -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://localhost.attacker.example/v1",
    "http://evil.example\\@127.0.0.1:1/v1",
    "http://127.0.0.1.nip.io/v1",
])
def test_a_local_model_must_really_be_local(vault, fake, monkeypatch, url):
    vault.add("work", "Leads the platform team")
    turn_on(vault, kind="local")
    monkeypatch.setenv("OPENMYMEMO_ASSISTANT_BASE_URL", url)
    a = Assistant.from_settings(vault)
    assert not a.settings.local and not a.available("import")
    assert a.json_call("import", "x", ["hello there"], SCHEMA) is None
    assert not a.test()["ok"]
    assert fake.requests == []


def test_redirects_are_never_followed(vault):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.2:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        a = turn_on(vault, base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
        assert a.json_call("import", "x", ["d"], SCHEMA) is None and "redirected" in a.last_error
        assert hits == ["/v1/chat/completions"]
    finally:
        server.shutdown()
    assert load_settings().enabled
