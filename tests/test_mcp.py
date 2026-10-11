import json
import os
import sys

import anyio
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from openmymemo.connectors import Connectors
from openmymemo.mcp_server import build_server
from openmymemo.proposals import Proposals


def call(client_target, steps):
    async def main():
        async with Client(client_target) as c:
            return await steps(c)
    return anyio.run(main)


def test_tools_are_listed_with_annotations(vault):
    srv = build_server(vault, stdio_connector="claude")

    async def steps(c):
        return await c.list_tools(), await c.list_prompts(), await c.list_resources()

    tools, prompts, resources = call(srv, steps)
    names = {t.name: t for t in tools.tools}
    assert set(names) == {"memory_context", "memory_search", "memory_get", "memory_propose", "memory_proposals"}
    assert names["memory_context"].annotations.read_only_hint is True
    assert names["memory_propose"].annotations.read_only_hint is False
    assert set(names["memory_propose"].input_schema["properties"]["op"]["enum"]) == {"add", "update", "archive"}
    assert [p.name for p in prompts.prompts] == ["recall"]
    assert [str(r.uri) for r in resources.resources] == ["openmymemo://context"]


def test_read_propose_round_trip_in_process(vault):
    m = vault.add("how-to-work-with-me", "回答先给结论")
    vault.add("health", "对青霉素过敏")
    srv = build_server(vault, stdio_connector="claude")

    async def steps(c):
        ctx = await c.call_tool("memory_context", {})
        found = await c.call_tool("memory_search", {"query": "结论"})
        hidden = await c.call_tool("memory_get", {"ids": [m.id, "zzzzz"]})
        prop = await c.call_tool("memory_propose", {"op": "add", "content": "女儿今年五岁", "reason": "user mentioned",
                                                   "evidence": "我女儿今年五岁了"})
        mine = await c.call_tool("memory_propose", {"op": "update", "memory_id": m.id, "content": "回答先给结论，再给理由"})
        bad = await c.call_tool("memory_propose", {"op": "update", "content": "x"})
        listed = await c.call_tool("memory_proposals", {})
        return ctx, found, hidden, prop, mine, bad, listed

    ctx, found, hidden, prop, mine, bad, listed = call(srv, steps)
    text = ctx.content[0].text
    assert "回答先给结论" in text and "青霉素" not in text
    assert "the user sees every change and can undo it" in text
    assert found.structured_content["results"][0]["id"] == m.id
    assert hidden.structured_content["missing"] == ["zzzzz"]

    # The addition applied at once; the change to a memory the user wrote waits.
    p = prop.structured_content
    assert (p["status"], p["hold"], p["topic"]) == ("applied", None, "people") and "review_url" not in p
    assert p["message"] == "Applied. The user will see it in their OpenMyMemo digest and can undo it."
    assert "女儿今年五岁" in (vault.memories_dir / "people.md").read_text()
    w = mine.structured_content
    assert (w["status"], w["hold"]) == ("pending", "protected")
    assert w["message"] == "Waiting for the user: they wrote this memory themselves."
    assert vault.get(m.id).content == "回答先给结论"
    assert bad.is_error and "memory_id is required" in bad.content[0].text
    rows = listed.structured_content["proposals"]
    assert [(r["status"], r["hold"]) for r in rows] == [("pending", "protected"), ("applied", None)]

    assert [x.id for x in Proposals(vault).unreviewed()] == [p["proposal_id"]]
    actions = [a["action"] for a in vault.activity("claude")][::-1]
    assert actions == ["context", "search", "get", "propose", "propose"]
    assert Connectors(vault).get("claude").kind == "stdio"


def test_instructions_describe_apply_now_and_review_later(vault):
    from openmymemo.mcp_server import INSTRUCTIONS

    flat = " ".join(INSTRUCTIONS.split())
    assert "apply right away" in flat and "can undo them" in flat
    assert "wait for the user" in flat and "Never ask the user to approve anything in the chat" in flat
    srv = build_server(vault, stdio_connector="claude")

    async def steps(c):
        return await c.list_tools()

    tools = {t.name: t for t in call(srv, steps).tools}
    assert '"protected"' in tools["memory_propose"].description
    assert "rejected or undone" in tools["memory_proposals"].description


def test_stdio_subprocess_end_to_end(vault):
    vault.add("work", "在 MyMemo 做 CTO")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "openmymemo", "--vault", str(vault.root), "mcp", "--connector", "claude-code"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    async def steps(c):
        ctx = await c.call_tool("memory_context", {"max_chars": 2000})
        prop = await c.call_tool("memory_propose", {"op": "add", "content": "正在学习日语", "idempotency_key": "a1"})
        listed = await c.call_tool("memory_proposals", {"status": "applied"})
        return ctx, prop, listed

    ctx, prop, listed = call(params, steps)
    assert "在 MyMemo 做 CTO" in ctx.content[0].text
    assert prop.structured_content["status"] == "applied"
    assert listed.structured_content["proposals"][0]["content"] == "正在学习日语"
    on_disk = list(vault.proposals_dir.glob("*.json"))
    assert len(on_disk) == 1 and json.loads(on_disk[0].read_text())["connector_id"] == "claude-code"


@pytest.mark.parametrize("write", ["none"])
def test_read_only_connector_gets_tool_error(vault, write):
    Connectors(vault).create("Reader", connector_id="reader", write=write)
    srv = build_server(vault, stdio_connector="reader")

    async def steps(c):
        return await c.call_tool("memory_propose", {"op": "add", "content": "something durable"})

    r = call(srv, steps)
    assert r.is_error and "read-only" in r.content[0].text


def test_removed_stdio_connector_stays_revoked(vault):
    cons = Connectors(vault)
    cons.ensure("claude-desktop", "Claude Desktop")
    cons.remove("claude-desktop")
    srv = build_server(vault, stdio_connector="claude-desktop")

    async def steps(c):
        return await c.call_tool("memory_context", {}), await c.call_tool("memory_propose", {"op": "add", "content": "x y z"})

    ctx, prop = call(srv, steps)
    assert ctx.is_error and "revoked" in ctx.content[0].text
    assert prop.is_error
    assert [c.id for c in cons.list()] == []  # not silently re-registered
    assert "revoked_at" in vault.connectors_path.read_text()
    assert not any(a["connector"] == "claude-desktop" for a in vault.activity())

    # Adding it again is an explicit re-grant.
    cons.create("Claude Desktop", connector_id="claude-desktop")
    ok = call(build_server(vault, stdio_connector="claude-desktop"), lambda c: c.call_tool("memory_context", {}))
    assert not ok.is_error


def test_plugin_style_stdio_launch_creates_vault_and_serves_panel(tmp_path):
    """What a Claude Desktop / Claude Code plugin runs: --init on a fresh folder plus an in-process panel."""
    import socket

    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    root = tmp_path / "fresh-vault"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "openmymemo", "--vault", str(root), "mcp", "--connector", "claude-desktop", "--init",
              "--panel-port", str(port)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    async def steps(c):
        ctx = await c.call_tool("memory_context", {})
        prop = await c.call_tool("memory_propose", {"op": "add", "content": "Prefers green tea"})
        async with httpx.AsyncClient() as http:  # the panel runs inside the same process
            state = (await http.get(f"http://127.0.0.1:{port}/api/state", headers={"X-OpenMyMemo": "1"})).json()
        return ctx, prop, state

    ctx, prop, state = call(params, steps)
    assert not ctx.is_error and "No memories shared yet" in ctx.content[0].text
    assert prop.structured_content["status"] == "applied"
    assert prop.structured_content["review_url"] == f"http://127.0.0.1:{port}/#/changes"
    assert state["vault"] == str(root) and state["counts"]["unreviewed"] == 1 and state["counts"]["pending"] == 0
    assert state["settings"] == {"require_approval": False}  # a fresh vault starts with the new default
