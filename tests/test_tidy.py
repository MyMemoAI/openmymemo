"""Tidy up (product-design.md §17.6) against a fake endpoint: each kind of suggestion, what waits and what applies,
"not twice", batches (Keep all / Undo all, log.md, `openmymemo review`), the schedule, limits, private topics and
what happens when the model fails."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_assistant import FakeEndpoint, turn_on
from typer.testing import CliRunner

from openmymemo.assistant import save_settings, tidy
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.vault import VaultError
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
AI = "connector:claude-code"
EMPTY = {"merges": [], "contradictions": [], "outdated": [], "until": [], "rewrites": []}
LONG = ("As discussed above in this project, the person said that for this one they would like the summaries to be "
        "short and in English, and that this applies to the reports we talked about earlier in the conversation")


@pytest.fixture
def fake(monkeypatch):
    import httpx2

    from openmymemo.assistant import client as endpoint

    f = FakeEndpoint()
    f.answer = lambda body: json.dumps(EMPTY)
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(f))
    return f


def tidy_on(vault, **kw):
    return turn_on(vault, features={"tidy": True}, **kw)


def seed(vault):
    v = vault
    m = {
        "tea1": v.add("lifestyle", "Prefers green tea over coffee in the morning", actor=AI,
                      created_at="2026-01-01T09:00:00+00:00"),
        "tea2": v.add("lifestyle", "Prefers green tea over coffee in the mornings", actor=AI,
                      created_at="2026-02-01T09:00:00+00:00"),
        "city1": v.add("identity", "Lives in Shanghai", actor=AI, created_at="2025-05-01T09:00:00+00:00"),
        "city2": v.add("identity", "Moved to Lisbon in 2026-09", actor=AI, created_at="2026-09-10T09:00:00+00:00"),
        "job": v.add("work", "Looking for a new job", actor=AI, created_at="2026-04-01T09:00:00+00:00"),
        "trip": v.add("lifestyle", "Travelling in Japan in November 2026", actor=AI),
        "long": v.add("how-to-work-with-me", LONG, actor=AI),
        "secret": v.add("health", "Takes medication for a chronic condition every morning", actor=AI),
    }
    return {k: x.id for k, x in m.items()}


def full_answer(ids):
    return {
        "merges": [{"ids": [ids["tea2"], ids["tea1"]], "content": "Prefers green tea over coffee, mostly in the morning",
                    "reason": "Both say the same."}],
        "contradictions": [{"ids": [ids["city2"], ids["city1"]], "reason": "Moved since."}],
        "outdated": [{"id": ids["job"], "reason": "Said 2026-04; still true?"}],
        "until": [{"id": ids["trip"], "until": "2026-12", "reason": "A trip in November."}],
        "rewrites": [{"id": ids["long"], "content": "Wants short summaries in English for reports.",
                      "reason": "Long and depends on its context."}],
    }


def by_memory(vault):
    out = {}
    for p in Proposals(vault).list():
        out.setdefault(p.memory_id, []).append(p)
    return out


def text_sent(fake):
    return "\n".join(json.dumps(b, ensure_ascii=False) for b in fake.bodies())


# ---- what it suggests and how it is filed ------------------------------------------------------------------------


def test_each_kind_is_filed_in_one_batch(vault, fake):
    ids = seed(vault)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps(full_answer(ids))
    r = tidy.run(vault)
    assert r.ok and r.calls == 1 and r.batch_id and r.batch_id.startswith("tidy-")
    assert [i.kind for i in r.items] == ["merge", "contradiction", "outdated", "rewrite", "until"]
    assert [ids["tea1"], ids["tea2"]] in r.candidates or [ids["tea2"], ids["tea1"]] in r.candidates
    props = Proposals(vault).list()
    assert {p.batch_id for p in props} == {r.batch_id} and all(p.connector_id == "assistant" for p in props)
    pm = by_memory(vault)
    # Merge: the earlier memory is updated, the later one archived as its copy.
    assert pm[ids["tea1"]][0].op == "update" and pm[ids["tea1"]][0].status == "applied"
    assert pm[ids["tea2"]][0].op == "archive" and pm[ids["tea2"]][0].status == "applied"
    # Contradiction: the later one is proposed as an update of the earlier one, which is the "before".
    c = pm[ids["city1"]][0]
    assert c.op == "update" and c.content == "Moved to Lisbon in 2026-09" and c.before_content == "Lives in Shanghai"
    assert vault.get(ids["city2"]).status == "archived"
    # Possibly outdated: an archive that waits.
    o = pm[ids["job"]][0]
    assert o.op == "archive" and o.status == "pending" and o.hold == "review" and o.reason == "Said 2026-04; still true?"
    assert vault.get(ids["job"]).status == "active"
    # A review date and a rewrite.
    assert vault.get(ids["trip"]).until == "2026-12" and pm[ids["trip"]][0].before_until is None
    assert vault.get(ids["long"]).content == "Wants short summaries in English for reports."
    assert r.applied == 6 and r.waiting == 1 and "Tidy-up, " in r.message and "7 changes" in r.message
    # Private memories were never looked at.
    assert "medication" not in text_sent(fake) and ids["secret"] not in pm


def test_changes_to_what_the_person_wrote_always_wait_even_in_auto_mode(vault, fake):
    a = vault.add("lifestyle", "Prefers green tea over coffee in the morning", created_at="2026-01-01T09:00:00+00:00")
    b = vault.add("lifestyle", "Prefers green tea over coffee in the mornings", actor=AI)
    long = vault.add("how-to-work-with-me", LONG)  # the person's own
    job = vault.add("work", "Looking for a new job", actor=AI)
    tidy_on(vault)
    Connectors(vault).update("assistant", write="auto")
    fake.answer = lambda body: json.dumps({**EMPTY, "merges": [{"ids": [a.id, b.id], "content": "Prefers green tea, mornings",
                                                                "reason": "Same."}],
                                           "outdated": [{"id": job.id, "reason": "Old."}],
                                           "rewrites": [{"id": long.id, "content": "Short summaries.",
                                                         "reason": "Too long."}]})
    r = tidy.run(vault)
    pm = by_memory(vault)
    assert pm[a.id][0].status == "pending" and pm[a.id][0].hold == "protected"
    assert pm[b.id][0].status == "pending" and pm[b.id][0].hold == "review"  # its first step waits, so it does
    assert pm[long.id][0].hold == "protected" and vault.get(long.id).content == LONG
    assert pm[job.id][0].hold == "review"  # outdated never applies, not even with `auto`
    assert r.applied == 0 and r.waiting == 4


def test_the_default_mode_applies_changes_to_ai_written_memories(vault, fake):
    m = vault.add("how-to-work-with-me", LONG, actor=AI)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": m.id, "content": "Short.",
                                                                  "reason": "Too long."}]})
    assert tidy.run(vault).applied == 1 and vault.get(m.id).content == "Short."


def test_bad_suggestions_are_dropped(vault, fake):
    a = vault.add("identity", "Lives in Shanghai", actor=AI)
    b = vault.add("work", "Works at Acme as an engineer", actor=AI)
    s = vault.add("health", "Takes medication daily for something", actor=AI)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps({
        "merges": [{"ids": [a.id, b.id], "content": "x y z", "reason": "not a candidate pair"}],
        "contradictions": [{"ids": [a.id, a.id], "reason": "same id"}],
        "outdated": [{"id": "nope", "reason": "unknown id"}, {"id": s.id, "reason": "private"}],
        "until": [{"id": b.id, "until": "2001-01", "reason": "in the past"}],
        "rewrites": [{"id": a.id, "content": "Lives in Shanghai", "reason": "unchanged"}]})
    r = tidy.run(vault)
    assert r.items == [] and Proposals(vault).list() == [] and r.ok


# ---- not twice ---------------------------------------------------------------------------------------------------


def test_a_rejected_or_undone_suggestion_is_not_made_again(vault, fake):
    m = vault.add("how-to-work-with-me", LONG, actor=AI)
    job = vault.add("work", "Looking for a new job", actor=AI)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps({**EMPTY, "outdated": [{"id": job.id, "reason": "Old."}],
                                           "rewrites": [{"id": m.id, "content": "Short.", "reason": "Long."}]})
    r1 = tidy.run(vault)
    props = Proposals(vault)
    pm = by_memory(vault)
    props.reject(pm[job.id][0].id)
    props.undo(pm[m.id][0].id)
    assert vault.get(m.id).content == LONG
    r2 = tidy.run(vault)
    assert r2.seen_before == 2 and r2.items == [] and r2.batch_id is None and "suggested before" in r2.message
    assert len(props.list()) == 2 and r1.batch_id
    # Once the memory reads differently, it's a new suggestion.
    vault.update(m.id, content=LONG + " and more", actor=AI)
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": m.id, "content": "Short.",
                                                                  "reason": "Long."}]})
    assert len(tidy.run(vault).items) == 1


# ---- batches -----------------------------------------------------------------------------------------------------


def test_keep_all_and_undo_all_in_the_panel_and_log_md(vault, fake):
    ids = seed(vault)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps(full_answer(ids))
    batch = tidy.run(vault).batch_id
    log = vault.log_md_path.read_text(encoding="utf-8")
    assert "- Tidy-up, " in log and ": 5 changes" in log and "\n  - **OpenMyMemo Assistant**" in log
    client = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    digest = client.get("/api/proposals", params={"unreviewed": True}, headers=H).json()
    assert {p["batch_id"] for p in digest} == {batch} and len(digest) == 6
    res = client.post(f"/api/proposals/batches/{batch}/undo", headers=H).json()
    assert res == {"undone": 6, "kept": 0}
    assert vault.get(ids["tea2"]).status == "active" and vault.get(ids["city1"]).content == "Lives in Shanghai"
    assert vault.get(ids["trip"]).until is None and vault.get(ids["long"]).content == LONG
    assert client.get("/api/proposals", params={"unreviewed": True}, headers=H).json() == []
    assert Proposals(vault).list("pending")[0].memory_id == ids["job"]  # what waits is decided on its own

    vault.update(ids["long"], content=LONG + " Also tables.", actor=AI)
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": ids["long"], "content": "Short; tables.",
                                                                   "reason": "Long."}]})
    batch2 = tidy.run(vault).batch_id
    assert client.post(f"/api/proposals/batches/{batch2}/reviewed", headers=H).json() == {"reviewed": 1}
    assert Proposals(vault).unreviewed() == []


def test_undo_all_leaves_what_changed_since(vault, fake):
    m = vault.add("how-to-work-with-me", LONG, actor=AI)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": m.id, "content": "Short.", "reason": "L"}]})
    batch = tidy.run(vault).batch_id
    vault.update(m.id, content="My own words.")
    assert Proposals(vault).undo_batch(batch) == {"undone": 0, "kept": 1}
    assert vault.get(m.id).content == "My own words."


def test_review_offers_the_batch_as_a_whole(vault, fake):
    ids = seed(vault)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps(full_answer(ids))
    tidy.run(vault)
    vault.add("work", "Uses Python at work", actor=AI)
    res = CliRunner().invoke(app, ["--vault", str(vault.root), "review"], input="u\ns\ns\n")
    assert res.exit_code == 0, res.output
    assert "Tidy-up, " in res.output and "6 changes by OpenMyMemo Assistant" in res.output
    assert "undid 6" in res.output and vault.get(ids["tea2"]).status == "active"


# ---- cost, limits and failures -----------------------------------------------------------------------------------


def test_dry_run_makes_no_calls_and_estimates(vault, fake):
    ids = seed(vault)
    r = tidy.run(vault, dry_run=True)  # works with the Assistant off, too
    assert fake.requests == [] and r.ok and r.estimate["calls"] == 1 and r.estimate["tokens"] > 500
    assert r.memories == 7 and r.left_out == 1 and "Turn it on" in r.message
    tidy_on(vault)
    r = tidy.run(vault, dry_run=True)
    assert fake.requests == [] and Proposals(vault).list() == [] and "would send 7 memories" in r.message
    assert sorted(r.candidates[0]) == sorted([ids["tea1"], ids["tea2"]])


def test_one_run_stops_at_its_call_limit(vault, fake, monkeypatch):
    monkeypatch.setattr(tidy, "CHUNK_MEMORIES", 2)
    seed(vault)
    tidy_on(vault)
    r = tidy.run(vault, max_calls=2)
    assert r.calls == 2 and len(fake.requests) == 2 and r.stopped == "limit" and not r.ok
    assert "the limit for one tidy-up" in r.message and r.chunks > 2


def test_the_monthly_limit_stops_it_with_a_message(vault, fake, monkeypatch):
    monkeypatch.setattr(tidy, "CHUNK_MEMORIES", 2)
    seed(vault)
    tidy_on(vault, caps={"calls": 2})
    r = tidy.run(vault)
    assert len(fake.requests) == 2 and r.stopped == "cap" and "limit" in r.message and "Nothing was changed" in r.message


@pytest.mark.parametrize("failure", ["status", "invalid"])
def test_when_the_model_fails_nothing_is_filed(vault, fake, failure):
    seed(vault)
    tidy_on(vault)
    if failure == "status":
        fake.status = 500
    else:
        fake.answer = lambda body: "Sure! Here are some ideas."
    r = tidy.run(vault)
    assert not r.ok and r.stopped == "error" and Proposals(vault).list() == []
    assert r.message.startswith("Stopped: ") and "Nothing was changed." in r.message


def test_off_or_feature_off_says_so(vault, fake):
    seed(vault)
    r = tidy.run(vault)
    assert not r.ok and r.stopped == "unavailable" and "The Assistant is off." in r.message
    turn_on(vault)  # tidy-up is off by default
    r = tidy.run(vault)
    assert r.stopped == "unavailable" and "isn't used for tidy" in r.message and fake.requests == []


def test_read_only_assistant_only_advises(vault, fake):
    m = vault.add("how-to-work-with-me", LONG, actor=AI)
    tidy_on(vault)
    Connectors(vault).update("assistant", write="none")
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": m.id, "content": "Short.", "reason": "L"}]})
    r = tidy.run(vault)
    assert len(r.items) == 1 and Proposals(vault).list() == [] and "nothing was filed" in r.message


# ---- privacy -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["key", "local"])
def test_private_topics_and_memories_are_never_sent(vault, fake, kind):
    vault.add("health", "Takes medication for a chronic condition every morning", actor=AI)
    vault.add("work", "Salary negotiation notes kept between us", actor=AI, private=True)
    vault.add("work", "Works at Acme as an engineer", actor=AI)
    if kind == "key":
        tidy_on(vault, kind="key", protocol="openai", model="gpt-x", api_key="sk-test")
    else:  # even with a model on this computer and the topic opted in: the Assistant can't write there anyway
        tidy_on(vault, private_topics=["health"])
    r = tidy.run(vault)
    sent = text_sent(fake)
    assert r.memories == 1 and r.left_out == 2 and "medication" not in sent and "Salary" not in sent
    assert "Acme" in sent


# ---- the schedule --------------------------------------------------------------------------------------------------


def test_schedule_due_and_not_due(vault, fake):
    seed(vault)
    tidy_on(vault)
    assert tidy.schedule(vault)["schedule"] == "off" and tidy.run_if_due(vault) is None  # off until turned on
    tidy.set_schedule("weekly")
    s = tidy.schedule(vault)
    assert s["due"] and s["every_days"] == 7 and s["last_run"] is None
    r = tidy.run_if_due(vault)
    assert r is not None and r.calls == 1
    assert not tidy.schedule(vault)["due"] and tidy.run_if_due(vault) is None and len(fake.requests) == 1
    later = datetime.now(UTC) + timedelta(days=8)
    assert tidy.schedule(vault, later)["due"]
    save_settings(features={"tidy": False})
    assert tidy.run_if_due(vault, later) is None and len(fake.requests) == 1  # the feature switch wins
    with pytest.raises(Exception, match="schedule"):
        tidy.set_schedule("hourly")


def test_last_run_is_per_folder(vault, fake, tmp_path):
    from openmymemo.vault import Vault

    other = Vault.create(tmp_path / "other")
    seed(vault)
    tidy_on(vault)
    tidy.set_schedule("daily")
    tidy.run(vault)
    assert not tidy.schedule(vault)["due"] and tidy.schedule(other)["due"]


def test_scheduler_runs_in_the_background_and_stops(vault, fake):
    seed(vault)
    tidy_on(vault)
    tidy.set_schedule("weekly")
    s = tidy.Scheduler(vault, interval=0.05, first=0.01)
    s.start()
    try:
        for _ in range(200):
            if fake.requests:
                break
            import time

            time.sleep(0.02)
    finally:
        s.stop()
    assert len(fake.requests) == 1 and tidy.schedule(vault)["last_run"]


# ---- panel and CLI -------------------------------------------------------------------------------------------------


def test_panel_routes(vault, fake):
    m = vault.add("how-to-work-with-me", LONG, actor=AI)
    client = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    s = client.get("/api/tidy", headers=H).json()
    assert s["available"] is False and s["schedule"] == "off" and s["reason"]
    assert client.post("/api/tidy", json={}, headers=H).json()["stopped"] == "unavailable"
    tidy_on(vault)
    assert client.patch("/api/tidy", json={"schedule": "monthly"}, headers=H).json()["every_days"] == 30
    assert client.patch("/api/tidy", json={"schedule": "nope"}, headers=H).status_code == 400
    dry = client.post("/api/tidy", json={"dry_run": True}, headers=H).json()
    assert dry["estimate"]["calls"] == 1 and fake.requests == []
    fake.answer = lambda body: json.dumps({**EMPTY, "rewrites": [{"id": m.id, "content": "Short.", "reason": "L"}]})
    res = client.post("/api/tidy", json={"topic": "how-to-work-with-me"}, headers=H).json()
    assert res["ok"] and res["applied"] == 1 and res["items"][0]["kind"] == "rewrite"
    assert client.get("/api/tidy", headers=H).json()["last_run"]
    assert client.post("/api/tidy", json={}, headers={}).status_code == 403  # the panel guard


def test_cli_tidy(vault, fake):
    ids = seed(vault)
    run = lambda *a: CliRunner().invoke(app, ["--vault", str(vault.root), "tidy", *a])  # noqa: E731
    res = run()
    assert res.exit_code == 1 and "needs the OpenMyMemo Assistant" in res.output
    res = run("--dry-run")
    assert res.exit_code == 0 and "Estimated: 1 calls" in res.output and "Likely duplicates" in res.output
    tidy_on(vault)
    fake.answer = lambda body: json.dumps(full_answer(ids))
    res = run("--topic", "lifestyle")
    assert res.exit_code == 0, res.output
    assert "Merge (applied, applied)" in res.output and "Review date (applied)" in res.output
    assert "Review them with `openmymemo review`" in res.output
    res = run("--schedule", "weekly")
    assert res.exit_code == 0 and "runs weekly" in res.output and "Nothing runs when no panel does" in res.output
    assert run("--topic", "nope").exit_code == 1


# ---- proposals: until, hold, batch ---------------------------------------------------------------------------------


def test_proposals_set_until_and_undo_restores_it(vault):
    from openmymemo.connectors import Connectors as C

    c, _ = C(vault).create("Some app", "stdio")
    m = vault.add("lifestyle", "Travelling in Japan", actor=AI, until="2026-11")
    props = Proposals(vault)
    res = props.propose(c, "update", memory_id=m.id, content=m.content, until="2027-01", batch_id="b1")
    assert res["status"] == "applied" and vault.get(m.id).until == "2027-01"
    p = props.get(res["proposal_id"])
    assert p.before_until == "2026-11" and p.batch_id == "b1" and props.batch("b1") == [p]
    props.undo(p.id)
    assert vault.get(m.id).until == "2026-11"
    with pytest.raises(VaultError):
        props.propose(c, "update", memory_id=m.id, content=m.content, until="soon")
    with pytest.raises(VaultError):
        props.propose(c, "add", content="x y", topic="work", hold="because")
    held = props.propose(c, "add", content="Plays chess on Sundays", topic="lifestyle", hold="review")
    assert held["status"] == "pending" and held["hold"] == "review" and "only made when they agree" in held["message"]


# ---- changes while the model looks, review dates changed since, passes over several runs ---------------------------


def test_a_memory_edited_or_archived_during_the_call_is_left_alone(vault, fake):
    long = vault.add("how-to-work-with-me", LONG, actor=AI)
    job = vault.add("work", "Looking for a new job", actor=AI)
    a = vault.add("lifestyle", "Prefers green tea over coffee in the morning", actor=AI,
                  created_at="2026-01-01T09:00:00+00:00")
    b = vault.add("lifestyle", "Prefers green tea over coffee in the mornings", actor=AI)
    tidy_on(vault)

    def answer(body):  # another app changes things while the model is thinking
        vault.update(long.id, content="Newer words from another app.", actor=AI)
        vault.archive(job.id, actor="user")
        vault.update(b.id, content="Prefers green tea, and oolong at night", actor=AI)
        return json.dumps({**EMPTY, "rewrites": [{"id": long.id, "content": "Short.", "reason": "Long."}],
                           "outdated": [{"id": job.id, "reason": "Old."}],
                           "merges": [{"ids": [a.id, b.id], "content": "Prefers green tea, mornings",
                                       "reason": "Same."}]})

    fake.answer = answer
    r = tidy.run(vault)
    assert r.changed == 3 and r.applied == 0 and r.waiting == 0 and Proposals(vault).list() == []
    assert "changed while the Assistant looked" in r.message
    assert vault.get(long.id).content == "Newer words from another app." and vault.get(job.id).status == "archived"
    assert vault.get(a.id).content == "Prefers green tea over coffee in the morning"  # its pair changed: no merge
    assert all(i.changed and i.filed == [] for i in r.items)


def test_a_review_date_set_since_is_not_undone(vault, fake):
    trip = vault.add("lifestyle", "Travelling in Japan in November 2026", actor=AI)
    tidy_on(vault)
    fake.answer = lambda body: json.dumps({**EMPTY, "until": [{"id": trip.id, "until": "2026-12", "reason": "Trip."}]})
    batch = tidy.run(vault).batch_id
    props = Proposals(vault)
    p = props.batch(batch)[0]
    assert props.can_undo(p) and props.as_dicts([p])[0]["can_undo"]
    vault.update(trip.id, until="2027-02")  # the person sets another date: no new version
    assert not props.can_undo(p) and not props.as_dicts([p])[0]["can_undo"]
    with pytest.raises(VaultError):
        props.undo(p.id)
    assert props.undo_batch(batch) == {"undone": 0, "kept": 1}
    assert vault.get(trip.id).until == "2027-02"


def test_consecutive_capped_runs_reach_the_rest_of_the_folder(vault, fake, monkeypatch):
    monkeypatch.setattr(tidy, "CHUNK_MEMORIES", 2)
    seed(vault)
    tidy_on(vault)
    sent: list[set[str]] = []

    def answer(body):
        text = json.dumps(body, ensure_ascii=False)
        sent.append({m.id for m in vault.memories() if f"[{m.id}]" in text})
        return json.dumps(EMPTY)

    fake.answer = answer
    r1 = tidy.run(vault, max_calls=2)
    assert r1.stopped == "limit" and r1.remaining > 0 and "next run continues" in r1.message
    first = set().union(*sent)
    assert tidy.run(vault, dry_run=True, max_calls=2).chunks == r1.remaining
    sent.clear()
    r2 = tidy.run(vault, max_calls=10)
    second = set().union(*sent)
    assert r2.ok and r2.calls == r1.remaining and not first & second
    readable = {m.id for m in vault.memories() if m.topic != "health"}
    assert first | second == readable
    sent.clear()
    r3 = tidy.run(vault, max_calls=10)  # the pass is complete: a new one starts from the beginning
    assert r3.ok and set().union(*sent) == readable
    # A memory whose text changed is looked at again within the pass.
    m = next(iter(vault.memories()))
    vault.update(m.id, content=m.content + " (edited)", actor=AI)
    sent.clear()
    r4 = tidy.run(vault, max_calls=10)
    assert r4.calls == 1 and m.id in set().union(*sent)


@pytest.mark.parametrize("kind", ["key", "local"])
def test_end_to_end_the_endpoint_never_gets_private_or_out_of_scope_memories(vault, fake, kind):
    vault.add("health", "Sees a cardiologist about an arrhythmia every spring", actor=AI)
    vault.add("finance", "Keeps savings in an account at Banco Inventado", actor=AI)
    vault.add("work", "Negotiating a confidential retention bonus with the CFO", actor=AI, private=True)
    hidden = vault.add("people", "Sister Mireille lives in Grenoble with two cats", actor=AI)
    near = vault.add("people", "Sister Mireille lives in Grenoble with her two cats", actor=AI)
    shown = vault.add("work", "Works at Acme as an engineer on the payments team", actor=AI)
    shown2 = vault.add("work", "Works at Acme as an engineer on the payment team", actor=AI)
    if kind == "key":
        tidy_on(vault, kind="key", protocol="openai", model="gpt-x", api_key="sk-test")
    else:  # a model on this computer that may read health: tidy still leaves private topics out
        tidy_on(vault, private_topics=["health"])
    topics = [t["slug"] for t in vault.topics() if t["slug"] not in ("people",)]
    Connectors(vault).update("assistant", read_topics=topics)  # people is out of the Assistant's read scope
    fake.answer = lambda body: json.dumps({**EMPTY, "merges": [
        {"ids": [hidden.id, near.id], "content": "x y z", "reason": "out of scope"},
        {"ids": [shown.id, shown2.id], "content": "Works at Acme as an engineer on the payments team.",
         "reason": "Same."}]})
    r = tidy.run(vault)
    sent = text_sent(fake)
    assert len(fake.requests) == 1 and "Acme" in sent
    for secret in ("cardiologist", "Banco Inventado", "retention bonus", "Mireille", hidden.id, near.id,
                   "health", "finance"):
        assert secret not in sent
    assert [i.memory_ids for i in r.items] == [(shown.id, shown2.id)]  # nothing about what it wasn't shown
    assert vault.get(hidden.id).status == "active" and vault.get(near.id).status == "active"
