import json
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import brief, score
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
NOW = datetime.now(UTC)
TODAY = NOW.astimezone().date()


def test_a_quiet_day(vault):
    b = brief.build(vault)
    assert b["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert b["quiet"] is True and b["learned"] == [] and b["used"] == [] and b["safety"] is None
    assert brief.counts_line(b) == "A quiet day."


def test_what_apps_learned_and_read_that_day(vault):
    cons = Connectors(vault)
    claude = cons.ensure("claude", "Claude")
    cons.ensure("chatgpt", "ChatGPT")
    props = Proposals(vault)
    props.propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    r = props.propose(claude, "add", content="Allergic to penicillin", topic="work", reason="said so")
    vault.update(r["memory_id"], private=True)  # marked private since
    r = props.propose(claude, "add", content="Earns 120k a year", topic="work", reason="said so")
    vault.update(r["memory_id"], topic="finance")  # moved into a private topic since
    work = vault.add("work", "Works at Acme")
    for _ in range(3):
        vault.log_activity("chatgpt", "context", memory_ids=[work.id])
    vault.log_activity("chatgpt", "propose")  # a write, not a read
    b = brief.build(vault, TODAY)
    assert b["quiet"] is False
    [learned] = b["learned"]
    assert learned["app"] == "Claude" and learned["private"] == 2
    assert [c["content"] for c in learned["changes"]] == ["Prefers tea over coffee"]
    assert b["used"] == [{"app": "ChatGPT", "reads": 3, "topics": ["Work"]}]
    assert "penicillin" not in json.dumps(b) and "120k" not in json.dumps(b)
    line = brief.counts_line(b)
    assert line == "Claude saved 3 memories, ChatGPT read 3."
    assert "tea" not in line
    assert brief.build(vault, TODAY - timedelta(days=1))["quiet"] is True  # another day


def test_what_waits_and_the_safety_change(vault):
    vault.update_settings(require_approval=True)
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    vault.add("work", "Leads the Q3 launch", until="2020-01")
    score.record(vault, {"score": 80, "areas": []}, now=NOW - timedelta(days=1))
    score.record(vault, {"score": 85, "areas": []}, now=NOW)
    b = brief.build(vault, TODAY)
    assert b["waiting"] == {"pending": 1, "review_due": 1}
    assert b["safety"] == {"score": 85, "change": 5}
    assert b["quiet"] is False  # the score changed
    assert brief.counts_line(b) == "A quiet day. 1 change is waiting for you. Memory safety +5, now 85."


def test_from_your_memory_is_old_not_private_and_the_same_all_day(vault):
    for n in range(5):
        vault.add("work", f"Old work fact number {n}")
    vault.add("health", "Old private fact")
    later = NOW + timedelta(days=60)  # nobody has changed these for two months
    day = later.astimezone().date()
    picks = {brief.build(vault, day, now=later)["resurface"]["memory_id"] for _ in range(3)}
    assert len(picks) == 1
    r = brief.build(vault, day, now=later)["resurface"]
    assert r["topic"] == "work" and r["content"].startswith("Old work fact")
    assert brief.build(vault, day, now=later)["quiet"] is True  # a resurfaced memory alone is no news
    assert brief.build(vault, TODAY)["resurface"] is None  # nothing has waited 30 days yet


def test_the_brief_over_the_api_and_the_cli(vault):
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    r = api.get(f"/api/brief?date={TODAY.isoformat()}", headers=H)
    assert r.status_code == 200 and r.json()["learned"][0]["app"] == "Claude"
    assert api.get("/api/brief?date=yesterday", headers=H).status_code == 400
    runner = CliRunner()
    out = runner.invoke(app, ["--vault", str(vault.root), "brief", "--date", TODAY.isoformat()])
    assert out.exit_code == 0
    assert "Claude saved 1 memory." in out.stdout and "added: Prefers tea over coffee" in out.stdout
    js = runner.invoke(app, ["--vault", str(vault.root), "brief", "--json"])
    assert json.loads(js.stdout)["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert runner.invoke(app, ["--vault", str(vault.root), "brief", "--date", "x"]).exit_code == 2
    assert f"{TODAY.day} in your memory" in out.stdout and str(TODAY.year) not in out.stdout.splitlines()[0]
    old = runner.invoke(app, ["--vault", str(vault.root), "brief", "--date", f"{TODAY.year - 1}-03-01"])
    assert old.stdout.splitlines()[0] == f"{date(TODAY.year - 1, 3, 1):%a}, Mar 1, {TODAY.year - 1} in your memory"
    assert isinstance(date.fromisoformat(json.loads(js.stdout)["date"]), date)
