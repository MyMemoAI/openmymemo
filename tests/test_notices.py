from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from openmymemo import notices
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
GITHUB = "ghp" + "_" + "x9Y" * 12  # built at run time, so no secret scanner mistakes this file for a leak


def at(day_offset: int, hour: int) -> datetime:
    """A local time ``day_offset`` days from today, as UTC."""
    local = datetime.now().astimezone()
    return (local.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)).astimezone(UTC)


def kinds(found):
    return [n["kind"] for n in found]


def test_the_brief_comes_once_after_its_time_and_not_for_a_quiet_day(vault):
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    assert notices.due(vault, kinds=("brief",), now=at(0, 10)) == []  # today's news is tomorrow's brief
    assert notices.due(vault, kinds=("brief",), now=at(1, 8)) == []   # before 9:00
    assert len(notices.due(vault, kinds=("brief",), brief_at=time(7), now=at(1, 8))) == 1  # the person's own time
    [n] = notices.due(vault, kinds=("brief",), now=at(1, 9))
    assert n["title"] == "Yesterday in your memory" and n["body"] == "Claude saved 1 memory."
    assert "tea" not in n["body"]
    notices.sent(vault, n["id"])
    assert notices.due(vault, kinds=("brief",), now=at(1, 15)) == []  # once a day
    assert notices.due(vault, kinds=("brief",), now=at(2, 9)) == []   # a quiet day: nothing


def test_a_missed_brief_is_skipped_not_piled_up(vault):
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    assert notices.due(vault, kinds=("brief",), now=at(2, 9)) == []  # the computer was off all of tomorrow


def test_changes_waiting_more_than_three_days_once_a_week(vault):
    vault.update_settings(require_approval=True)
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    assert notices.due(vault, kinds=("waiting",), now=at(2, 12)) == []
    [n] = notices.due(vault, kinds=("waiting",), now=at(4, 12))
    assert n["body"] == "1 change has waited more than 3 days for your decision."
    notices.sent(vault, n["id"])
    assert notices.due(vault, kinds=("waiting",), now=at(4, 18)) == []
    assert kinds(notices.due(vault, kinds=("waiting",), now=at(11, 12))) == ["waiting"]  # a week later


def test_safety_alerts_only_for_new_issues_once_each(vault):
    cons = Connectors(vault)
    cons.ensure("claude", "Claude")
    cons.update("claude", include_private=True)
    assert notices.due(vault, kinds=("safety",)) == []  # the first look only notes what is there
    m = vault.add("work", f"Deploy key {GITHUB}")
    [n] = notices.due(vault, kinds=("safety",))
    assert n["body"] == "A memory in Work looks like it contains an API key or token."
    assert GITHUB not in n["body"] and m.id in n["id"]
    notices.sent(vault, n["id"])
    assert notices.due(vault, kinds=("safety",)) == []
    cons.ensure("cursor", "Cursor")
    cons.update("cursor", include_private=True)
    vault.add("work", "password: hunter2!")
    [n] = notices.due(vault, kinds=("safety",))
    assert n["body"] == "2 new things to check on Home."


def test_switched_off_kinds_and_bad_ids(vault):
    vault.update_settings(require_approval=True)
    claude = Connectors(vault).ensure("claude", "Claude")
    Proposals(vault).propose(claude, "add", content="Prefers tea over coffee", reason="said so")
    assert notices.due(vault, kinds=(), now=at(5, 12)) == []
    with pytest.raises(ValueError):
        notices.sent(vault, "other:1")
    assert notices.parse_time("7:30") == time(9) and notices.parse_time("07:30") == time(7, 30)


def test_notices_over_the_api(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    Connectors(vault).ensure("claude", "Claude")
    assert api.get("/api/notices", headers=H).json() == []
    vault.add("work", f"Deploy key {GITHUB}")
    [n] = api.get("/api/notices?kinds=safety,brief&brief_at=23:59", headers=H).json()
    assert api.post("/api/notices/sent", headers=H, json={"id": n["id"]}).json() == {"ok": True}
    assert api.get("/api/notices?kinds=safety", headers=H).json() == []
    assert api.post("/api/notices/sent", headers=H, json={"id": "nope"}).status_code == 400
