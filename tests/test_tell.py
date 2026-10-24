"""Tell OpenMyMemo (product-design.md §19): the preview the Assistant builds, the checks on its answer, the rules
fallback and saving what the person kept."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_assistant import FakeEndpoint, H, turn_on

from openmymemo.assistant import client as endpoint
from openmymemo.assistant import save_settings, tell
from openmymemo.web import create_api

GITHUB = "ghp" + "_" + "x9Y" * 12  # built at run time, so no secret scanner mistakes this file for a leak


@pytest.fixture
def fake(monkeypatch):
    f = FakeEndpoint()
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(f))
    return f


def answer_with(changes, skipped=None):
    return lambda body: json.dumps({"changes": changes, "skipped": skipped})


def change(op, memory_id=None, content=None, topic=None, kind=None, reason="said so"):
    return {"op": op, "memory_id": memory_id, "content": content, "topic": topic, "kind": kind, "reason": reason}


def user_text(fake):
    body = fake.bodies()[-1]
    return body["messages"][-1]["content"]


def test_the_assistant_adds_updates_and_archives_against_what_search_finds(vault, fake):
    lyon = vault.add("interests", "Lives in Lyon")
    coffee = vault.add("interests", "Drinks coffee every morning")
    turn_on(vault, kind="key", model="m", api_key="k")
    fake.answer = answer_with([
        change("update", lyon.id, "Lives in Berlin since September 2026", reason="moved"),
        change("archive", coffee.id, reason="stopped drinking coffee"),
        change("add", content="Has a cat called Miso", topic="interests", kind="fact"),
    ], skipped="The weather remark.")
    p = tell.preview(vault, "I moved from Lyon to Berlin last month, no more coffee, and my cat Miso says hi. Nice weather.")
    assert p["by"] == "assistant" and p["skipped"] == "The weather remark." and p["note"] is None
    ops = [(c["op"], c["memory_id"]) for c in p["changes"]]
    assert ops == [("update", lyon.id), ("archive", coffee.id), ("add", None)]
    assert p["changes"][0]["current"] == "Lives in Lyon" and p["changes"][0]["version"] == lyon.version
    sent = user_text(fake)
    assert "Lyon" in sent and lyon.id in sent  # the memory it may update went with the text
    assert vault.get(lyon.id).content == "Lives in Lyon"  # a preview writes nothing

    done = tell.apply(vault, p["changes"])
    assert (done["added"], done["updated"], done["archived"], done["errors"]) == (1, 1, 1, [])
    assert vault.get(lyon.id).content == "Lives in Berlin since September 2026"
    assert vault.get(coffee.id).status == "archived"
    added = vault.get(done["ids"][-1])
    assert added.content == "Has a cat called Miso" and added.source == "user" and added.kind == "fact"


def test_the_answer_may_only_change_memories_it_was_sent(vault, fake):
    secret = vault.add("health", "Takes insulin daily")  # a private topic: never sent to a remote model
    marked = vault.add("interests", "Lives in Lyon", private=True)
    open_ = vault.add("interests", "Lives near the river Rhône in Lyon")
    turn_on(vault, kind="key", model="m", api_key="k")
    fake.answer = answer_with([
        change("archive", secret.id),
        change("update", marked.id, "Lives in Berlin"),
        change("update", "nope", "Lives in Berlin"),
        change("update", open_.id, "Lives near the Spree in Berlin", topic="no-such-topic"),
        change("archive", open_.id),  # the same memory twice: the first one stands
        change("update", open_.id, "Lives near the river Rhône in Lyon"),
        change("add", content="ok"),  # too short to be a memory
        change("add", content="Works   from   home", topic="health"),  # a private topic isn't a choice
    ])
    p = tell.preview(vault, "I live in Berlin now and work from home. Lyon insulin.")
    sent = user_text(fake)
    assert secret.id not in sent and marked.id not in sent and open_.id in sent
    assert [(c["op"], c["memory_id"], c["topic"]) for c in p["changes"]] == [
        ("update", open_.id, None), ("add", None, p["changes"][1]["topic"])]
    assert p["changes"][1]["content"] == "Works from home" and p["changes"][1]["topic"] != "health"
    assert p["private_skipped"] >= 1


def test_keys_in_what_was_typed_are_masked(vault, fake):
    turn_on(vault, kind="key", model="m", api_key="k")
    fake.answer = answer_with([])
    tell.preview(vault, f"My deploy key is {GITHUB}, remember I use GitHub")
    assert GITHUB not in user_text(fake)


def test_without_the_assistant_the_text_is_kept_by_rules(vault, fake):
    p = tell.preview(vault, "Prefers green tea\n\nVolunteers at the food bank on Saturdays")
    assert p["by"] == "rules" and p["note"] == tell.NO_ASSISTANT
    assert [c["content"] for c in p["changes"]] == ["Prefers green tea", "Volunteers at the food bank on Saturdays"]
    assert all(c["op"] == "add" and c["topic"] for c in p["changes"])
    assert fake.requests == []

    turn_on(vault, kind="key", model="m", api_key="k")
    fake.status = 500
    p = tell.preview(vault, "Prefers green tea")
    assert p["by"] == "rules" and p["note"] and len(p["changes"]) == 1  # a failed call: still nothing lost

    fake.status = 200
    save_settings(features={"tell": False})  # switched off: rules, and nothing is sent
    before = len(fake.requests)
    assert tell.preview(vault, "Prefers green tea")["by"] == "rules" and len(fake.requests) == before


def test_long_text_is_cut_into_memory_sized_parts_and_limits_are_checked(vault):
    p = tell.preview(vault, "word " * 450)
    assert len(p["changes"]) == 3 and all(len(c["content"]) <= 1000 for c in p["changes"])
    with pytest.raises(tell.TellError):
        tell.preview(vault, "   ")
    with pytest.raises(tell.TellError):
        tell.preview(vault, "x" * (tell.MAX_TEXT + 1))


def test_every_paragraph_kept_by_rules_is_saved(vault):
    p = tell.preview(vault, "\n\n".join(f"Fact number {i}" for i in range(41)))
    assert p["by"] == "rules" and len(p["changes"]) == 41
    done = tell.apply(vault, p["changes"])
    assert done["added"] == 41 and done["errors"] == []
    assert {m.content for m in vault.memories()} >= {f"Fact number {i}" for i in range(41)}
    with pytest.raises(tell.TellError):  # more than any preview makes: refused whole, nothing written
        tell.apply(vault, [{"op": "add", "content": "x", "topic": "identity"}] * (tell.MAX_TEXT + 1))
    assert len(vault.memories()) == 41


def test_an_edited_change_too_long_for_a_memory_is_refused_not_cut(vault):
    lyon = vault.add("identity", "Lives in Lyon")
    long = "Lives in Berlin " + "and more " * 130
    done = tell.apply(vault, [
        {"op": "update", "memory_id": lyon.id, "content": long, "version": lyon.version},
        {"op": "add", "content": long, "topic": "identity"},
        {"op": "add", "content": "x" * tell.MAX_MEMORY, "topic": "identity"},
    ])
    assert (done["updated"], done["added"], len(done["errors"])) == (0, 1, 2)
    assert "at most 1,000 characters" in done["errors"][0]["error"]
    assert vault.get(lyon.id).content == "Lives in Lyon"


def test_a_memory_edited_since_the_preview_is_refused_and_the_rest_saved(vault, fake):
    lyon = vault.add("identity", "Lives in Lyon")
    turn_on(vault, kind="key", model="m", api_key="k")
    fake.answer = answer_with([change("update", lyon.id, "Lives in Berlin"),
                               change("add", content="Has a cat called Miso", topic="interests")])
    p = tell.preview(vault, "I moved to Berlin and I have a cat called Miso")
    vault.update(lyon.id, content="Lives in Lyon, France")  # edited elsewhere meanwhile
    done = tell.apply(vault, p["changes"])
    assert done["added"] == 1 and done["updated"] == 0
    assert "changed since the preview" in done["errors"][0]["error"]
    assert vault.get(lyon.id).content == "Lives in Lyon, France"


def test_tell_over_the_panel(vault, fake):
    lyon = vault.add("identity", "Lives in Lyon")  # no word in common with what's said: found as a profile memory
    turn_on(vault, kind="key", model="m", api_key="k")
    fake.answer = answer_with([change("update", lyon.id, "Lives in Berlin")])
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    assert api.post("/api/tell/preview", headers=H, json={"text": " "}).status_code == 400
    p = api.post("/api/tell/preview", headers=H, json={"text": "I moved to Berlin"}).json()
    assert p["changes"][0]["memory_id"] == lyon.id
    done = api.post("/api/tell/apply", headers=H, json={"changes": p["changes"]}).json()
    assert done["updated"] == 1 and vault.get(lyon.id).content == "Lives in Berlin"
    assert api.post("/api/tell/preview", json={"text": "x"}).status_code == 403  # the panel's guard
