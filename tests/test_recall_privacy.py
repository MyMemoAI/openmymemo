"""Recall end to end against a remote endpoint (chat and /embeddings): private memories, memories outside the
Assistant's read scope, and public memories that quote them never reach it — through memory_search (MCP), the
panel's search box, `openmymemo recall` and `openmymemo search`. Every assertion is on the raw requests received."""

import json
import unicodedata

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.client import Client
from typer.testing import CliRunner

from openmymemo.assistant import client as endpoint
from openmymemo.assistant import ensure_connector, save_settings
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.mcp_server import build_server
from openmymemo.recall import EmbeddingIndex, search
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
SECRET = "Takes sumatriptan for cluster headaches every Thursday morning"
SECRET_ZH = "每周四早上服用舒马曲坦治疗丛集性头痛"
WORK = "Negotiating the Zephyrine acquisition with Halvorsen Partners"
QUOTE = "Reminder for myself: takes sumatriptan for cluster headaches every Thursday morning, keep it quiet"


def norm(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


class Remote:
    """A remote OpenAI-compatible endpoint with chat and embeddings; records every body."""

    def __init__(self):
        self.bodies: list[dict] = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if request.url.path.endswith("/embeddings"):
            texts = body["input"]
            vecs = [[1.0 if w in t.lower() else 0.0 for w in ("tea", "drink", "headache", "medicine", "work")] + [0.1]
                    for t in texts]
            return httpx2.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]})
        user = body["messages"][1]["content"]
        system = body["messages"][0]["content"]
        if "rank" in system:
            ids = [x.split("]")[0] for x in user.split("[")[1:] if "]" in x]
            answer = {"ids": ids[::-1]}
        else:
            answer = {"keywords": ["medicine", "headache", "drink", "tea"]}
        return httpx2.Response(200, json={"model": "m", "choices": [{"message": {"content": json.dumps(answer)}}]})

    def leaked(self, secret: str, n: int = 16) -> bool:
        body = norm(" ".join(json.dumps(b, ensure_ascii=False) for b in self.bodies))
        s = norm(secret)
        return any(s[i:i + n] in body for i in range(max(1, len(s) - n + 1)))


@pytest.fixture
def setup(vault, monkeypatch):
    fake = Remote()
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(fake))
    vault.add("health", SECRET)
    vault.add("health", SECRET_ZH)
    vault.add("work", WORK)
    vault.add("interests", "Prefers tea over coffee")
    vault.add("interests", "Drinks a lot of water")
    vault.add("lifestyle", QUOTE)  # public, but quotes a private memory
    for i in range(12):
        vault.add("lifestyle", f"Headache medicine note {i}")
    save_settings(enabled=True, kind="key", protocol="openai", base_url="https://api.example.com/v1", model="m",
                  api_key="sk-1", embeddings_model="emb")
    c = ensure_connector(vault, regrant=True)
    Connectors(vault).update(c.id, read_topics=[t["slug"] for t in vault.topics() if t["slug"] != "work"])
    return fake


def assert_clean(fake: Remote):
    assert fake.bodies, "the endpoint was used"
    for secret in (SECRET, SECRET_ZH, WORK, QUOTE):
        assert not fake.leaked(secret), secret


def test_memory_search_over_mcp(vault, setup):
    # The app may read private topics; the Assistant's model still may not see them.
    Connectors(vault).create("Claude", connector_id="claude", include_private=True)
    srv = build_server(vault, stdio_connector="claude")

    async def main():
        async with Client(srv) as cl:
            return await cl.call_tool("memory_search", {"query": "what medicine do I take for headaches", "limit": 3})

    found = anyio.run(main).structured_content["results"]
    assert found
    assert any("rank" in b["messages"][0]["content"] for b in setup.bodies if "messages" in b), "reranked"
    assert_clean(setup)
    ids = set(EmbeddingIndex(vault).load().ids)
    assert not {m.id for m in vault.memories() if m.topic in ("health", "work") or m.content == QUOTE} & ids


def test_panel_search_and_cli(vault, setup):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    r = api.get("/api/search", params={"q": "headache medicine"}, headers=H).json()
    assert r["mode"] == "hybrid" and r["results"]
    runner = CliRunner()
    assert runner.invoke(app, ["--vault", str(vault.root), "recall", "headache medicine"]).exit_code == 0
    assert runner.invoke(app, ["--vault", str(vault.root), "search", "headache medicine"]).exit_code == 0
    assert_clean(setup)


def test_a_query_quoting_a_private_memory_is_not_sent(vault, setup):
    found = search(vault, None, SECRET, model=True)
    assert all(SECRET not in json.dumps(b) for b in setup.bodies)
    assert not setup.leaked(SECRET)
    assert found.mode in ("keyword", "hybrid", "expanded")
    calls = [a for a in vault.activity("assistant") if a.get("result") == "refused_private"]
    assert calls
