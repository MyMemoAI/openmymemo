"""Recall against an embeddings endpoint as things change: the send rules are read again for every request (a
memory or topic made private, a narrower read scope, another endpoint while the index is being built), stored
vectors of memories that may no longer be sent are not used, the endpoint's identity is its full address and model
(and its dimensions), and the panel's status reads the job before the index."""

import json

import httpx2
import pytest

from openmymemo.assistant import client as endpoint
from openmymemo.assistant import ensure_connector, load_settings, save_settings
from openmymemo.connectors import Connectors
from openmymemo.recall import EmbeddingIndex, embed, hybrid, local, search, semantic
from openmymemo.recall import index as index_mod
from openmymemo.recall.embed import endpoint_id


class Recording:
    """An OpenAI-compatible /embeddings that records each request's host and inputs, and can run a change after a
    given request (as if the person changed something while the index was being built)."""

    def __init__(self, dims=4):
        self.requests: list[tuple[str, list[str]]] = []
        self.after: dict[int, object] = {}
        self.dims = dims

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((f"{request.url.host}:{request.url.port}", body["input"]))
        action = self.after.get(len(self.requests))
        if action:
            action()
        vecs = [[float((len(t) + i) % 5 + 1) for i in range(self.dims)] for t in body["input"]]
        return httpx2.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]})

    def sent(self, start=0) -> str:
        return "\n".join(t for _, inputs in self.requests[start:] for t in inputs)


@pytest.fixture
def indexing(vault, monkeypatch):
    """A folder whose index takes several small requests, a model on this computer with embeddings, and the
    private topic `health` opted in for it."""
    monkeypatch.setattr(index_mod, "BATCH", 2)
    monkeypatch.setattr(embed, "ENDPOINT_BATCH", 2)
    fake = Recording()
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(fake))
    for i in range(4):
        vault.add("interests", f"Collects vintage postcards from series {i}")
    target = vault.add("lifestyle", "Swims in the lake before breakfast")
    vault.add("health", "Sees a physiotherapist for a sore knee")
    save_settings(enabled=True, kind="local", model="m", embeddings_model="emb", private_topics=["health"])
    ensure_connector(vault, regrant=True)
    return vault, fake, target


@pytest.mark.parametrize("change", ["memory_private", "topic_private", "read_scope", "remote"])
def test_permission_changes_during_indexing_reach_the_next_request(indexing, change):
    vault, fake, target = indexing

    def act():
        if change == "memory_private":
            vault.update(target.id, private=True)
        elif change == "topic_private":
            vault.update_topic("lifestyle", private=True)
        elif change == "read_scope":
            Connectors(vault).update("assistant", read_topics=[t["slug"] for t in vault.topics()
                                                               if t["slug"] != "lifestyle"])
        else:
            save_settings(kind="key", protocol="openai", base_url="https://api.example.com/v1", api_key="sk-1",
                          embeddings_model="emb")

    fake.after[1] = act
    e = embed.active_embedder(vault)
    assert e is not None and not e.remote
    r = EmbeddingIndex(vault).refresh(e, vault.memories())
    assert fake.requests and "Swims in the lake" not in fake.sent(start=1)
    if change == "remote":
        assert r.error and len(fake.requests) == 1  # another endpoint: this embedder stops
        assert all(host.startswith("127.0.0.1") for host, _ in fake.requests)
        assert "physiotherapist" not in fake.sent()
    else:
        assert "physiotherapist" in fake.sent()  # still opted in for the model on this computer
        snap = EmbeddingIndex(vault).load()
        assert target.id not in snap.ids and len(snap.ids) == 5


def test_vectors_stored_before_a_memory_became_private_are_not_used(indexing, monkeypatch):
    vault, fake, target = indexing
    save_settings(kind="key", protocol="openai", base_url="https://api.example.com/v1", api_key="sk-1",
                  embeddings_model="emb", private_topics=[])
    e = embed.active_embedder(vault)
    assert e.remote
    EmbeddingIndex(vault).refresh(e, vault.memories())
    assert target.id in EmbeddingIndex(vault).load().ids
    vault.update(target.id, private=True)
    # With the stored vector still in the index (no refresh), a search by a remote embedder doesn't use it.
    monkeypatch.setattr(EmbeddingIndex, "refresh", lambda self, *a, **k: None)
    found = search(vault, None, "zzz", everything=True, embedder=embed.active_embedder(vault))
    assert found.mode == "hybrid" and target.id in EmbeddingIndex(vault).load().ids
    assert target.id not in [h.memory.id for h in found.hits]


def test_the_endpoint_id_is_the_full_address_and_model(vault):
    save_settings(enabled=True, kind="local", model="m", embeddings_model="emb", base_url="http://127.0.0.1:11434/v1")
    a = endpoint_id(load_settings())
    save_settings(base_url="http://127.0.0.1:1234/v1")
    b = endpoint_id(load_settings())
    save_settings(base_url="http://127.0.0.1:1234/other")
    c = endpoint_id(load_settings())
    save_settings(base_url="http://localhost:1234/other")
    assert len({a, b, c, endpoint_id(load_settings())}) == 4
    assert a == "endpoint/http://127.0.0.1:11434/v1/emb"


def test_a_new_server_or_new_dimensions_rebuild_the_index_and_forget_cached_queries(vault, monkeypatch):
    fake = Recording(dims=2)
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(fake))
    for i in range(3):
        vault.add("interests", f"Hobby number {i}")
    save_settings(enabled=True, kind="local", model="m", embeddings_model="emb", base_url="http://127.0.0.1:11434/v1")
    ensure_connector(vault, regrant=True)
    assert search(vault, None, "hobby").mode == "hybrid"
    assert EmbeddingIndex(vault).load().dims == 2
    # The same address and model name now answering in 3 dimensions (another model loaded under that name).
    fake.dims = 3
    before = len(fake.requests)
    r = search(vault, None, "hobby")
    snap = EmbeddingIndex(vault).load()
    assert r.mode == "hybrid" and snap.dims == 3 and len(snap.ids) == 3
    assert len(fake.sent(start=before).splitlines()) >= 4  # every memory again, and the query
    assert not [k for k in hybrid._query_cache if k[0].startswith("endpoint/")]  # endpoint queries are never cached
    # Another port: another embedder, so another index; the old one's cached query isn't used.
    save_settings(base_url="http://127.0.0.1:11435/v1")
    before = len(fake.requests)
    assert search(vault, None, "hobby").mode == "hybrid"
    snap2 = EmbeddingIndex(vault).load()
    assert snap2.model != snap.model and snap2.model.endswith("127.0.0.1:11435/v1/emb")
    assert "hobby" in fake.sent(start=before).splitlines()
    assert all(host == "127.0.0.1:11435" for host, _ in fake.requests[before:])


def test_status_reads_the_job_before_the_index(vault, monkeypatch):
    job = local.Job(phase="index", model="multilingual-e5-small", done=1, total=3)
    monkeypatch.setattr(local, "_job", job)
    real = EmbeddingIndex.status

    def finishing(self, emb=None):
        out = real(self, emb)
        job.phase = "done"  # the job ends between the index read and the job read
        return out

    monkeypatch.setattr(EmbeddingIndex, "status", finishing)
    assert semantic.status(vault)["job"]["phase"] == "index"  # so the panel asks again
    assert semantic.status(vault)["job"]["phase"] == "done"
