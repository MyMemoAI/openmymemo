import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import clients, score
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
NOW = datetime.now(UTC)
GITHUB = "ghp" + "_" + "x9Y" * 12  # built at run time, so no secret scanner mistakes this file for a leak
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")


def ids(result, *, dismissed=False):
    return [i["id"] for i in result["issues"] if i["dismissed"] == dismissed]


def area(result, name):
    return next(a["points"] for a in result["areas"] if a["id"] == name)


def git(cwd, *args, days_ago=0):
    when = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_a_new_folder_loses_only_the_backup(vault):
    r = score.compute(vault)
    assert ids(r) == ["backup"]
    assert r["score"] == 85 and r["level"] == "Good" and r["week_change"] is None
    assert [(a["id"], a["max"]) for a in r["areas"]] == [
        ("sensitive", 25), ("apps", 25), ("review", 20), ("upkeep", 15), ("backup", 15)]


def test_levels():
    assert [score.level_of(s) for s in (100, 90, 89, 70, 69, 50, 49, 0)] == [
        "Excellent", "Excellent", "Good", "Good", "Needs attention", "Needs attention", "At risk", "At risk"]


def test_secrets_outside_private_topics_cost_points_and_never_show_their_text(vault):
    m = vault.add("work", f"Deploy key {GITHUB}")
    vault.add("finance", "Visa 4111 1111 1111 1111")  # private: where it belongs
    r = score.compute(vault)
    issue = next(i for i in r["issues"] if i["id"] == f"secret:{m.id}:key")
    assert issue["points"] == 5 and issue["target"] == m.id and issue["fix"] == "memory"
    assert issue["message"] == "A memory in Work looks like it contains an API key or token."
    assert GITHUB not in json.dumps(r)
    assert area(r, "sensitive") == 20


def test_an_area_never_goes_below_zero(vault):
    for site in ("bank", "email", "router", "work laptop", "gym locker", "school portal", "tax office"):
        vault.add("work", f"The {site} password is {len(site)}x{site[::-1].replace(' ', '')}!")
    r = score.compute(vault)
    assert sum(i["points"] for i in r["issues"] if i["area"] == "sensitive") == 35
    assert area(r, "sensitive") == 0 and r["score"] == sum(a["points"] for a in r["areas"])


def test_no_private_topics(vault):
    for slug in vault.private_topics():
        vault.update_topic(slug, private=False)
    assert "no-private-topics" in ids(score.compute(vault))


def test_app_access(vault):
    cons = Connectors(vault)
    cons.ensure("claude", "Claude")
    cons.update("claude", include_private=True, write="auto")
    cons.ensure("cursor", "Cursor")
    vault.log_activity("cursor", "context")
    r = score.compute(vault)
    assert {"app-private:claude", "app-auto:claude"} <= set(ids(r))
    assert not any(i.startswith("app-unused") for i in ids(r))
    later = score.compute(vault, now=NOW + timedelta(days=31))
    assert {"app-unused:claude", "app-unused:cursor"} <= set(ids(later))


def test_a_broken_app_config_costs_points(vault, monkeypatch):
    monkeypatch.setattr(clients, "catalog", lambda v: [{"id": "cursor", "name": "Cursor", "broken": True}])
    issue = next(i for i in score.compute(vault)["issues"] if i["id"] == "app-broken:cursor")
    assert issue["points"] == 4 and issue["fix"] == "app"


def test_a_failing_app_catalog_doesnt_hide_the_score(vault, monkeypatch):
    def boom(v):
        raise RuntimeError("unreadable config")

    monkeypatch.setattr(clients, "catalog", boom)
    assert score.compute(vault)["score"] == 85


def test_changes_that_waited_more_than_a_week(vault):
    claude = Connectors(vault).ensure("claude", "Claude")
    props = Proposals(vault)
    props.propose(claude, "add", content="Prefers tea over coffee", reason="said so")  # applied, unseen
    mine = vault.add("work", "Works at Acme")
    props.propose(claude, "update", memory_id=mine.id, content="Works at Globex", reason="said so")  # waits
    assert not [i for i in ids(score.compute(vault)) if i.startswith("review")]
    r = score.compute(vault, now=NOW + timedelta(days=8))
    assert {"review-pending", "review-unreviewed"} <= set(ids(r)) and area(r, "review") == 0


def test_memory_upkeep(vault):
    vault.add("work", "Leads the Q3 launch", until="2020-01")
    vault.add("work", "Prefers green tea over black tea in the morning")
    vault.add("work", "Prefers green tea over black tea in the mornings")
    vault.add("work", "x" * (score.LONG_CHARS + 1))
    r = score.compute(vault)
    assert {"upkeep-overdue", "upkeep-duplicates", "upkeep-long"} <= set(ids(r)) and area(r, "upkeep") == 0
    messages = {i["id"]: i["message"] for i in r["issues"]}
    assert messages["upkeep-overdue"] == "1 memory is past the date to check it."
    assert messages["upkeep-duplicates"] == "1 pair of memories look like duplicates."


def test_a_full_export_counts_as_a_backup_and_a_partial_one_doesnt(vault):
    score.note_export(vault, full=False)
    assert "backup" in ids(score.compute(vault))
    score.note_export(vault, full=True)
    assert "backup" not in ids(score.compute(vault))
    assert "backup" in ids(score.compute(vault, now=NOW + timedelta(days=31)))


@needs_git
def test_a_recent_commit_of_every_memory_file_counts_as_a_backup(vault):
    git(vault.root, "init", "-q")
    assert "backup" in ids(score.compute(vault))  # a repository with no commit is no copy
    git(vault.root, "add", "-A")
    git(vault.root, "commit", "-qm", "memory")
    assert "backup" not in ids(score.compute(vault))
    vault.add("work", "Edited since the commit")  # the commit can still be restored
    assert "backup" not in ids(score.compute(vault))
    vault.add_topic("hobbies", "Hobbies")  # a new, untracked topic file
    assert "backup" in ids(score.compute(vault))


@needs_git
def test_an_old_commit_is_no_backup(vault):
    git(vault.root, "init", "-q")
    git(vault.root, "add", "-A")
    git(vault.root, "commit", "-qm", "memory", days_ago=40)
    assert "backup" in ids(score.compute(vault))


@needs_git
def test_a_folder_its_repository_ignores_is_no_backup(vault, tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("vault/\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore the vault")
    assert "backup" in ids(score.compute(vault))


def test_dismissing_and_restoring(vault):
    score.dismiss(vault, "backup")  # "I back up another way"
    r = score.compute(vault)
    assert r["score"] == 100 and r["level"] == "Excellent"
    assert ids(r) == [] and ids(r, dismissed=True) == ["backup"]
    score.dismiss(vault, "backup", restore=True)
    assert score.compute(vault)["score"] == 85


def test_history_is_one_line_a_day_with_numbers_only(vault):
    vault.add("work", "Works at Acme")
    week_ago = NOW - timedelta(days=7)
    score.record(vault, {"score": 70, "areas": []}, now=week_ago)
    r = score.compute(vault)
    score.record(vault, r)
    score.record(vault, r)
    rows = score.history(vault)
    assert len(rows) == 2 and set(rows[-1]) == {"date", "score", "areas"}
    assert rows[-1]["areas"] == {"sensitive": 25, "apps": 25, "review": 20, "upkeep": 15, "backup": 0}
    assert score.compute(vault)["week_change"] == 15
    assert "Acme" not in (vault.state_dir / "score.jsonl").read_text(encoding="utf-8")


def test_the_score_over_the_api(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    r = api.get("/api/score", headers=H)
    assert r.status_code == 200 and r.json()["score"] == 85
    assert len(score.history(vault)) == 1
    r = api.post("/api/score/dismiss", headers=H, json={"id": "backup"})
    assert r.json()["score"] == 100
    r = api.post("/api/score/dismiss", headers=H, json={"id": "backup", "restore": True})
    assert r.json()["score"] == 85
    assert api.get("/api/export?include_private=true", headers=H).status_code == 200
    assert api.get("/api/score", headers=H).json()["score"] == 100


def test_status_shows_the_score(vault):
    runner = CliRunner()
    r = runner.invoke(app, ["--vault", str(vault.root), "status"])
    assert r.exit_code == 0 and r.stdout.startswith("Memory safety: 85 · Good\n")
    assert "-15  There's no copy of your memory from the last 30 days" in r.stdout
    r = runner.invoke(app, ["--vault", str(vault.root), "status", "--json"])
    assert json.loads(r.stdout)["safety"]["score"] == 85
    runner.invoke(app, ["--vault", str(vault.root), "export", "--include-private", "-o", str(vault.root.parent / "x.md")])
    assert json.loads(runner.invoke(app, ["--vault", str(vault.root), "status", "--json"]).stdout)["safety"]["score"] == 100
