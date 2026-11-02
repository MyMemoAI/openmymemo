"""Asking in the app (product-design.md §22): a change that waits is put to the person in the app that proposed it,
when that app can show forms, and the answer applies it (as the app's change) or turns it down."""

import json

import anyio
import pytest
from mcp.client.client import Client
from mcp_types import ElicitResult, ErrorData

from openmymemo import ask
from openmymemo.connectors import Connectors
from openmymemo.mcp_server import build_server
from openmymemo.proposals import Proposals
from openmymemo.vault import NotFound

LEGACY, MODERN = "legacy", "2026-07-28"  # elicitation/create during the call; an input_required round trip


def run(vault, steps, *, answer=None, mode=MODERN, connector="claude-code"):
    """Run `steps(client)` against this computer's server; `answer` is the app's elicitation callback (None: the app
    can't show forms). Returns what steps returned and the questions the app was asked."""
    asked: list[str] = []

    async def callback(context, params):
        asked.append(params.message)
        return await answer(params) if callable(answer) else ElicitResult(action=answer)

    async def main():
        srv = build_server(vault, stdio_connector=connector)
        async with Client(srv, mode=mode, elicitation_callback=callback if answer else None) as c:
            return await steps(c)

    return anyio.run(main), asked


def propose(**args):
    return lambda c: c.call_tool("memory_propose", args)


@pytest.mark.parametrize("mode", [LEGACY, MODERN])
def test_accepting_in_the_app_applies_it_as_the_apps_change_into_the_digest(vault, mode):
    m = vault.add("how-to-work-with-me", "Answers start with the conclusion")
    r, asked = run(vault, propose(op="update", memory_id=m.id, content="Answers start with the conclusion, then why",
                                  reason="The user asked for the reasons too."), answer="accept", mode=mode)
    out = r.structured_content
    assert (out["status"], out["hold"]) == ("applied", None)
    assert out["message"] == ask.ANSWERED["accept"]
    assert asked == [
        "Claude Code wants to change a memory you wrote (How to work with me).\n\n"
        "Now: Answers start with the conclusion\nNew: Answers start with the conclusion, then why\n\n"
        "Why: The user asked for the reasons too.\n\n"
        "Accept makes the change now; you can still undo it in OpenMyMemo. Decline turns it down. "
        "Cancel leaves it waiting in OpenMyMemo."]

    after = vault.get(m.id)
    assert after.content == "Answers start with the conclusion, then why"
    assert after.last_actor == "connector:claude-code"  # the app's text, so no longer protected
    props = Proposals(vault)
    (p,) = props.list()  # the second round of the question filed nothing new
    assert (p.status, p.answer, p.hold) == ("applied", "accept", None) and p.asked_at
    assert [x.id for x in props.unreviewed()] == [p.id] and props.can_undo(p)
    props.undo(p.id)  # undo puts back the person's own text, protected again
    assert vault.get(m.id).content == "Answers start with the conclusion" and vault.get(m.id).last_actor == "user"
    assert [(a["action"], a.get("result")) for a in vault.activity("claude-code")][:2] == [
        ("ask", "accept"), ("propose", "pending")]


@pytest.mark.parametrize("mode", [LEGACY, MODERN])
def test_declining_rejects_it_and_the_app_can_see_that(vault, mode):
    m = vault.add("work", "Works at Acme as CTO")

    async def steps(c):
        prop = await c.call_tool("memory_propose", {"op": "archive", "memory_id": m.id})
        return prop, await c.call_tool("memory_proposals", {})

    (prop, listed), asked = run(vault, steps, answer="decline", mode=mode)
    assert prop.structured_content["status"] == "rejected"
    assert prop.structured_content["message"] == ask.ANSWERED["decline"]
    assert asked[0].startswith("Claude Code wants to archive a memory you wrote (Work).\n\nWorks at Acme as CTO\n")
    assert vault.get(m.id).status == "active"
    assert listed.structured_content["proposals"][0]["status"] == "rejected"
    (p,) = Proposals(vault).list()
    assert p.answer == "decline" and p.decided_at
    assert Connectors(vault).get("claude-code").blocked_topics == []  # declining isn't blocking the topic


@pytest.mark.parametrize("mode", [LEGACY, MODERN])
def test_cancelling_leaves_it_waiting(vault, mode):
    m = vault.add("work", "Works at Acme as CTO")
    r, asked = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer="cancel",
                   mode=mode)
    assert (r.structured_content["status"], r.structured_content["hold"]) == ("pending", "protected")
    assert r.structured_content["message"] == ask.ANSWERED["cancel"] and len(asked) == 1
    (p,) = Proposals(vault).list("pending")
    assert p.answer == "cancel" and p.asked_at
    assert vault.get(m.id).content == "Works at Acme as CTO"
    last = vault.activity("claude-code")[0]
    assert (last["action"], last["result"], last["proposal_id"]) == ("ask", "cancel", p.id)


@pytest.mark.parametrize("mode", [LEGACY, MODERN])
def test_an_app_that_cant_show_forms_isnt_asked(vault, mode):
    m = vault.add("work", "Works at Acme as CTO")
    r, _ = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer=None, mode=mode)
    assert r.structured_content["status"] == "pending"
    (p,) = Proposals(vault).list()
    assert p.asked_at is None and "asked_at" not in json.loads((vault.proposals_dir / f"{p.id}.json").read_text())
    assert [a["action"] for a in vault.activity("claude-code")] == ["propose"]


def test_never_about_private_topics_changes_that_apply_or_an_app_with_asking_off(vault):
    vault.add("health", "Allergic to penicillin")
    m = vault.add("work", "Works at Acme as CTO")
    Connectors(vault).ensure("claude-code", "Claude Code")
    Connectors(vault).update("claude-code", include_private=True)

    async def steps(c):
        private = await c.call_tool("memory_propose", {"op": "add", "content": "Takes vitamin D daily",
                                                       "topic": "health"})
        applied = await c.call_tool("memory_propose", {"op": "add", "content": "Has a daughter aged five"})
        return private, applied

    (private, applied), asked = run(vault, steps, answer="accept")
    assert (private.structured_content["status"], private.structured_content["hold"]) == ("pending", "private")
    assert applied.structured_content["status"] == "applied"
    assert asked == []

    Connectors(vault).update("claude-code", ask=False)
    r, asked = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer="accept")
    assert r.structured_content["status"] == "pending" and asked == []
    assert "ask: false" in vault.connectors_path.read_text()


def test_with_approval_on_an_addition_is_asked_about_too(vault):
    vault.update_settings(require_approval=True)
    r, asked = run(vault, propose(op="add", content="Is learning Japanese", topic="learning"), answer="accept",
                   mode=LEGACY)
    assert r.structured_content["status"] == "applied"
    assert asked[0].startswith("Claude Code wants to remember something (Learning).\n\nIs learning Japanese\n")
    assert "you wrote" not in asked[0]


def test_a_retry_with_the_same_key_doesnt_ask_again(vault):
    m = vault.add("work", "Works at Acme as CTO")
    args = {"op": "update", "memory_id": m.id, "content": "Works at Acme as CEO", "idempotency_key": "k1"}

    async def steps(c):
        return await c.call_tool("memory_propose", args), await c.call_tool("memory_propose", args)

    (first, again), asked = run(vault, steps, answer="cancel")
    assert len(asked) == 1 and again.structured_content["deduplicated"] is True
    assert again.structured_content["status"] == "pending"


@pytest.mark.parametrize("mode", [LEGACY, MODERN])
def test_a_memory_that_changes_while_the_question_is_open_is_not_overwritten(vault, mode):
    m = vault.add("work", "Works at Acme as CTO")

    async def edit_then_accept(params):
        vault.update(m.id, content="Works at Acme as CTO and co-founder")  # the person, in an editor meanwhile
        return ElicitResult(action="accept")

    r, _ = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer=edit_then_accept,
               mode=mode)
    assert r.structured_content["status"] == "pending"
    assert r.structured_content["message"] == ask.ANSWERED["changed"]
    assert vault.get(m.id).content == "Works at Acme as CTO and co-founder"
    (p,) = Proposals(vault).list("pending")
    assert p.answer == "accept"  # shown in the panel: accepted there, but the memory had changed
    assert vault.activity("claude-code")[0]["result"] == "changed"


def test_a_memory_made_private_meanwhile_waits_for_the_person(vault):
    m = vault.add("work", "Works at Acme as CTO")

    async def hide_then_accept(params):
        vault.update(m.id, private=True)
        return ElicitResult(action="accept")

    r, _ = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer=hide_then_accept,
               mode=LEGACY)
    assert r.structured_content["status"] == "pending" and vault.get(m.id).content == "Works at Acme as CTO"


def test_no_answer_in_time_or_an_error_leaves_it_waiting(vault, monkeypatch):
    m = vault.add("work", "Works at Acme as CTO")
    monkeypatch.setattr(ask, "TIMEOUT", 0.3)

    async def never(params):
        await anyio.sleep(30)

    async def fails(params):
        return ErrorData(code=-32603, message="The app couldn't show it")

    r, _ = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CEO"), answer=never, mode=LEGACY)
    assert r.structured_content["status"] == "pending"
    assert vault.activity("claude-code")[0]["result"] == "timeout"
    r, _ = run(vault, propose(op="update", memory_id=m.id, content="Works at Acme as CFO"), answer=fails, mode=LEGACY)
    assert r.structured_content["status"] == "pending"
    assert vault.activity("claude-code")[0]["result"] == "error"
    assert [p.answer for p in Proposals(vault).list("pending")] == [None, None]
    assert vault.get(m.id).content == "Works at Acme as CTO"


def test_the_relayed_tools_never_ask(vault):
    m = vault.add("work", "Works at Acme as CTO")
    c = Connectors(vault).ensure("cloud-claude", "Claude (web)", kind="cloud")
    srv = build_server(vault, relayed=lambda: c)
    r = anyio.run(srv.call_tool, "memory_propose", {"op": "update", "memory_id": m.id, "content": "Works at Acme as CEO"})
    assert '"pending"' in r.content[0].text
    (p,) = Proposals(vault).list()
    assert p.asked_at is None


def test_only_the_app_that_proposed_can_answer(vault):
    m = vault.add("work", "Works at Acme as CTO")
    cons = Connectors(vault)
    claude, cursor = cons.ensure("claude-code", "Claude Code"), cons.ensure("cursor", "Cursor")
    props = Proposals(vault)
    pid = props.propose(claude, "update", memory_id=m.id, content="Works at Acme as CEO")["proposal_id"]
    with pytest.raises(NotFound):
        props.answered(pid, cursor.id, "accept")
    assert props.get(pid).status == "pending"
    props.reject(pid)
    assert props.answered(pid, claude.id, "accept")[1] == "changed"  # decided in the panel first
    assert props.get(pid).status == "rejected"


def test_the_question_names_a_move_and_cuts_a_long_reason(vault):
    m = vault.add("work", "Plays the cello")
    claude = Connectors(vault).ensure("claude-code", "Claude Code")
    props = Proposals(vault)
    pid = props.propose(claude, "update", memory_id=m.id, content="Plays the cello in an orchestra", topic="interests",
                        reason="word " * 100)["proposal_id"]
    text = ask.question(props.get(pid), {t["slug"]: t["title"] for t in vault.topics()})
    assert text.startswith("Claude Code wants to change a memory you wrote (Work → Interests and taste).")
    why = next(line for line in text.splitlines() if line.startswith("Why: "))
    assert len(why) == len("Why: ") + ask.REASON_MAX and why.endswith("…")


def test_the_setting_is_written_only_when_off_and_set_from_the_panel(vault):
    from fastapi.testclient import TestClient

    from openmymemo.web import create_api

    cons = Connectors(vault)
    cons.ensure("cursor", "Cursor")
    assert "ask:" not in vault.connectors_path.read_text() and cons.get("cursor").ask is True
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    r = api.patch("/api/connectors/cursor", json={"ask": False}, headers={"X-OpenMyMemo": "1"})
    assert r.status_code == 200 and r.json()["ask"] is False
    assert cons.get("cursor").ask is False and "ask: false" in vault.connectors_path.read_text()
    cons.update("cursor", ask=True)
    assert "ask:" not in vault.connectors_path.read_text()


def test_review_and_connector_list_say_what_was_answered_in_the_app(vault):
    from typer.testing import CliRunner

    from openmymemo.cli import app

    work, tools = vault.add("work", "Works at Acme as CTO"), vault.add("tools", "Uses npm")
    cons = Connectors(vault)
    claude = cons.ensure("claude-code", "Claude Code")
    props = Proposals(vault)
    accepted = props.propose(claude, "update", memory_id=work.id, content="Works at Acme as CEO")["proposal_id"]
    props.asked(accepted)
    props.answered(accepted, claude.id, "accept")
    props.asked(props.propose(claude, "update", memory_id=tools.id, content="Uses pnpm")["proposal_id"])
    cons.update("claude-code", ask=False)

    r = CliRunner().invoke(app, ["--vault", str(vault.root), "review"], input="s\ns\n")
    assert r.exit_code == 0, r.stdout
    assert "(you accepted this in Claude Code)" in r.stdout
    assert "(asked in Claude Code; not decided there)" in r.stdout
    listed = CliRunner().invoke(app, ["--vault", str(vault.root), "connector", "list"])
    assert "write=propose" in listed.stdout and listed.stdout.rstrip().endswith("ask=off")
