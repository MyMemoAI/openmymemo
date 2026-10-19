"""Recall (product-design.md §17.4): the embedding index, hybrid search, the Assistant's expansion and rerank, the
remote embeddings endpoint's privacy rule, local model downloads (fake HTTP, never the network), the switch's CLI
and panel routes. Real models never run here: ``ConceptEmbedder`` (tests/recall) stands in for one."""

import collections
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import context
from openmymemo.assistant import client as endpoint
from openmymemo.assistant import ensure_connector, save_settings
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.mcp_server import build_server
from openmymemo.recall import EmbeddingIndex, embed, local, rrf, search, semantic, similar
from openmymemo.recall.embed import EmbedError, EndpointEmbedder
from openmymemo.recall.hybrid import recall
from openmymemo.web import create_api

sys.path.insert(0, str(Path(__file__).parent / "recall"))
from recall_harness import ConceptEmbedder, ReplayEndpoint, build_vault, load_set  # noqa: E402

H = {"X-OpenMyMemo": "1"}


@pytest.fixture
def demo(tmp_path):
    return build_vault(tmp_path / "demo")


class Counting(ConceptEmbedder):
    def __init__(self, name="concepts-v1", delay=0.0):
        super().__init__(name)
        self.embedded: list[str] = []
        self.delay = delay

    def embed(self, texts, kind, memory_ids=()):
        if kind == "passage":
            self.embedded.extend(memory_ids)
            time.sleep(self.delay)
        return super().embed(texts, kind, memory_ids)


# ---- the index ----------------------------------------------------------------------------------------------------


def test_index_is_incremental_and_follows_edits_in_other_editors(demo):
    idx = EmbeddingIndex(demo)
    e = Counting()
    first = idx.refresh(e, demo.memories())
    total = len(demo.memories())
    assert first.indexed == first.total == total and len(e.embedded) == total
    assert (idx.dir / ".gitignore").read_text().strip().endswith("*")
    manifest = json.loads(idx.manifest_path.read_text())
    assert manifest["model"] == e.id and manifest["count"] == total
    assert manifest["dims"] == len(e.embed(["x"], "query")[0])
    vectors = idx.dir / manifest["vectors"]
    assert vectors.stat().st_size == total * manifest["dims"] * 4

    e.embedded.clear()
    again = idx.refresh(e, demo.memories())
    assert again.embedded == 0 and e.embedded == [] and again.complete
    assert json.loads(idx.manifest_path.read_text())["vectors"] == manifest["vectors"]  # nothing rewritten

    # An edit made in another editor: the file changes, the next refresh re-embeds that memory only.
    f = demo.memories_dir / "interests.md"
    f.write_text(f.read_text().replace("usually oolong", "usually green tea"))
    idx.refresh(e, demo.memories())
    assert e.embedded == ["tea01"]
    # A memory that is archived is dropped from the index.
    demo.archive("bass1")
    e.embedded.clear()
    r = idx.refresh(e, demo.memories())
    snap = idx.load()
    assert "bass1" not in snap.ids and r.indexed == total - 1 and e.embedded == []


def test_a_model_change_rebuilds_and_indexes_never_mix(demo):
    idx = EmbeddingIndex(demo)
    a, b = Counting("model-a"), Counting("model-b")
    idx.refresh(a, demo.memories())
    idx.refresh(b, demo.memories())
    assert len(b.embedded) == len(demo.memories())
    snap = idx.load()
    assert snap.model == b.id and len(snap.ids) == len(demo.memories())
    assert len(list(idx.dir.glob("vectors-*.f32"))) == 1
    # Search with model A sees no index of its own (it would rebuild), never model B's vectors.
    assert EmbeddingIndex(demo).status(a)["current"] is False


def test_a_budget_leaves_the_rest_for_next_time(demo):
    idx = EmbeddingIndex(demo)
    slow = Counting(delay=0.05)
    r = idx.refresh(slow, demo.memories(), budget=0.0)
    assert r.indexed < r.total and not r.complete
    r = idx.refresh(slow, demo.memories())
    assert r.complete


def test_concurrent_refreshes_and_reads_stay_consistent(demo):
    """Two writers (as two processes would: separate files, flock) and a reader: the reader always sees a whole
    index, and the result has every memory once."""
    errors: list[BaseException] = []
    total = len(demo.memories())

    def writer(name):
        try:
            EmbeddingIndex(demo).refresh(Counting(delay=0.01), demo.memories())
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader(stop):
        while not stop.is_set():
            snap = EmbeddingIndex(demo).load()
            if snap is not None:
                assert len(snap.vectors) == len(snap.ids) * snap.dims
                assert len(set(snap.ids)) == len(snap.ids)

    stop = threading.Event()
    r = threading.Thread(target=reader, args=(stop,))
    r.start()
    ws = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    stop.set()
    r.join()
    assert not errors
    snap = EmbeddingIndex(demo).load()
    assert sorted(snap.ids) == sorted(m.id for m in demo.memories()) and len(snap.ids) == total


def test_an_unreadable_index_is_ignored(demo):
    idx = EmbeddingIndex(demo)
    idx.refresh(ConceptEmbedder(), demo.memories())
    manifest = json.loads(idx.manifest_path.read_text())
    (idx.dir / manifest["vectors"]).write_bytes(b"\0\0")
    assert idx.load() is None
    idx.manifest_path.write_text(json.dumps({**manifest, "vectors": "../../memories/work.md"}))
    assert idx.load() is None
    idx.manifest_path.write_text("{")
    assert idx.load() is None
    assert idx.refresh(ConceptEmbedder(), demo.memories()).complete  # rebuilt from the files


# ---- hybrid search --------------------------------------------------------------------------------------------


def test_rrf_sums_reciprocal_ranks():
    fused = rrf([["a", "b", "c"], ["c", "a"]], k=60)
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert sorted(fused, key=lambda x: -fused[x]) == ["a", "c", "b"]


def test_without_an_index_search_is_the_keyword_search_unchanged(demo, monkeypatch):
    monkeypatch.setattr(embed, "active_embedder", lambda vault: None)
    for q in ["Claude Agent SDK", "吉他", "tea", "nothing at all"]:
        r = search(demo, None, q, limit=10)
        assert r.mode == "keyword"
        assert [(h.score, h.memory.id) for h in r.hits] == \
            [(s, m.id) for s, m in context.search(demo, None, q, limit=10)]
    assert not (demo.state_dir / "index").exists()


def test_hybrid_finds_by_meaning_and_exact_matches_still_win(demo):
    e = ConceptEmbedder()
    r = search(demo, None, "What do I like to drink?", embedder=e)
    assert r.mode == "hybrid" and r.hits[0].memory.id == "tea01" and r.hits[0].match == "meaning"
    assert context.search(demo, None, "What do I like to drink?") == []  # keywords alone find nothing
    zh = search(demo, None, "我喜欢喝什么？", embedder=e)
    assert zh.hits[0].memory.id == "tea01"
    # The query as a phrase, or an id, comes first even when vectors rank something else higher.
    exact = search(demo, None, "covers band", embedder=e)
    assert exact.hits[0].memory.id == "bass1" and exact.hits[0].match == "exact"
    by_id = search(demo, None, "gtr01", embedder=e)
    assert by_id.hits[0].memory.id == "gtr01"


def test_hybrid_search_keeps_the_callers_scope(demo):
    e = ConceptEmbedder()
    c, _ = Connectors(demo).create("Cursor", read_topics=["work", "interests"])
    r = search(demo, c, "What do I like to drink?", embedder=e)
    assert {h.memory.topic for h in r.hits} <= {"work", "interests"}
    # Private memories are in the index (the local model sees everything) but never in a default-scope result.
    assert "alrg1" in EmbeddingIndex(demo).load().ids
    assert all(h.memory.id != "alrg1" for h in search(demo, None, "peanut allergy food", embedder=e).hits)
    panel = search(demo, None, "Allergic to peanuts", embedder=e, everything=True)
    assert panel.hits[0].memory.id == "alrg1"


def test_a_failing_embedder_falls_back_to_keywords(demo):
    class Broken(ConceptEmbedder):
        def embed(self, texts, kind, memory_ids=()):
            raise EmbedError("down")

    r = search(demo, None, "Claude Agent SDK", embedder=Broken())
    assert r.mode == "keyword" and r.hits[0].memory.id == "agent"


def test_similar_for_teammates_and_its_remote_rule(demo, monkeypatch):
    e = ConceptEmbedder()
    pairs = similar(demo, "Drinks green tea every day", embedder=e, limit=3)
    assert pairs and pairs[0][1].id == "tea01" and 0 < pairs[0][0] <= 1.0001
    assert similar(demo, "x", embedder=None) == []

    class Remote(ConceptEmbedder):
        remote = True

    assert similar(demo, "Drinks green tea", embedder=Remote()) == []  # no topic: the text isn't sent
    assert similar(demo, "Drinks green tea", topic="health", embedder=Remote()) == []


def test_memory_search_over_mcp_uses_the_hybrid_ranking(demo, monkeypatch):
    import anyio
    from mcp.client.client import Client

    monkeypatch.setattr(embed, "active_embedder", lambda vault: ConceptEmbedder())
    srv = build_server(demo, stdio_connector="claude")

    async def main():
        async with Client(srv) as c:
            return await c.call_tool("memory_search", {"query": "What do I like to drink?", "limit": 3})

    found = anyio.run(main).structured_content["results"]
    assert found[0]["id"] == "tea01" and found[0]["match"] == "meaning"
    assert demo.activity("claude")[0]["memory_ids"][0] == "tea01"


# ---- expansion and rerank (the Assistant) --------------------------------------------------------------------


def _assistant_on(vault, monkeypatch, handler, **kw):
    monkeypatch.setattr(endpoint, "TRANSPORT", httpx2.MockTransport(handler))
    save_settings(**{"enabled": True, "kind": "local", "model": "m", **kw})
    ensure_connector(vault, regrant=True)


def test_expansion_without_an_index_and_rerank_of_many_candidates(demo, monkeypatch):
    _, questions = load_set()
    replay = ReplayEndpoint(questions)
    _assistant_on(demo, monkeypatch, replay)
    monkeypatch.setattr(embed, "active_embedder", lambda vault: None)
    r = search(demo, None, "What do I like to drink?", model=True)
    assert r.mode == "expanded" and r.hits[0].memory.id == "tea01"
    calls = [a for a in demo.activity("assistant") if a["action"] == "call"]
    assert calls and calls[0]["feature"] == "recall"
    # memory_context never asks the model.
    before = replay.requests
    context.compile_context(demo, None)
    assert replay.requests == before


def test_rerank_never_sends_private_memories_to_a_remote_model(demo, monkeypatch):
    sent: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body["messages"][1]["content"])
        user = body["messages"][1]["content"]
        ids = [x for x in ("chk01", "tea01", "run01") if f"[{x}]" in user]
        return httpx2.Response(200, json={"model": "m", "choices": [{"message": {"content": json.dumps({"ids": ids})}}]})

    _assistant_on(demo, monkeypatch, handler, kind="key", protocol="openai", base_url="https://api.example.com/v1",
                  api_key="sk-1")
    c, _ = Connectors(demo).create("Doctor app", include_private=True)
    class Everything(ConceptEmbedder):
        min_similarity = -1.0  # every memory is a candidate: more than the limit

    r = search(demo, c, "every year", limit=1, model=True, embedder=Everything())
    assert sent, "the rerank was asked"
    text = "\n".join(sent)
    assert "每年十月体检" not in text and "Allergic" not in text and "[chk01]" not in text
    assert r.hits  # the private memory keeps its place, the others are in the model's order


def test_model_failures_fall_back_silently(demo, monkeypatch):
    _assistant_on(demo, monkeypatch, lambda request: httpx2.Response(500, json={"error": "boom"}))
    monkeypatch.setattr(embed, "active_embedder", lambda vault: None)
    r = search(demo, None, "Claude Agent SDK", model=True)
    assert r.mode == "keyword" and r.hits[0].memory.id == "agent"


# ---- a remote /embeddings endpoint -------------------------------------------------------------------------------


class FakeEmbeddings:
    def __init__(self):
        self.inputs: list[str] = []
        self.paths: list[str] = []

    def __call__(self, request):
        self.paths.append(request.url.path)
        body = json.loads(request.content)
        texts = body["input"]
        self.inputs.extend(texts)
        vecs = ConceptEmbedder().embed(texts, "passage")
        return httpx2.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)],
                                          "usage": {"prompt_tokens": 7, "total_tokens": 7}})


def test_remote_embeddings_never_include_private_topics(demo, monkeypatch):
    fake = FakeEmbeddings()
    _assistant_on(demo, monkeypatch, fake, kind="key", protocol="openai", base_url="https://api.example.com/v1",
                  api_key="sk-1", embeddings_model="text-embedding-3-small")
    e = embed.active_embedder(demo)
    assert isinstance(e, EndpointEmbedder) and e.remote and e.id == "endpoint/https://api.example.com/v1/text-embedding-3-small"
    r = search(demo, None, "What do I like to drink?")
    assert r.mode == "hybrid" and r.hits[0].memory.id == "tea01"
    assert set(fake.paths) == {"/v1/embeddings"}
    sent = "\n".join(fake.inputs)
    assert "Allergic to peanuts" not in sent and "每年十月体检" not in sent and "down payment" not in sent
    ids = EmbeddingIndex(demo).load().ids
    assert not {"alrg1", "chk01", "save1"} & set(ids)
    calls = [a for a in demo.activity("assistant") if a["action"] == "call" and a.get("embeddings")]
    assert calls and all(a["feature"] == "recall" for a in calls)
    assert "Allergic" not in json.dumps(demo.activity("assistant"))  # the log has ids, never text


def test_remote_embeddings_are_off_for_anthropic_and_without_a_model(demo, monkeypatch):
    _assistant_on(demo, monkeypatch, FakeEmbeddings(), kind="key", protocol="anthropic",
                  base_url="https://api.anthropic.com", api_key="sk-1", embeddings_model="x")
    assert embed.active_embedder(demo) is None
    save_settings(protocol="openai", base_url="https://api.example.com/v1", embeddings_model="")
    assert embed.active_embedder(demo) is None


def test_a_model_on_this_computer_may_embed_opted_in_private_topics(demo, monkeypatch):
    fake = FakeEmbeddings()
    _assistant_on(demo, monkeypatch, fake, embeddings_model="nomic-embed-text", private_topics=["health"])
    e = embed.active_embedder(demo)
    assert not e.remote
    EmbeddingIndex(demo).refresh(e, demo.memories())
    ids = set(EmbeddingIndex(demo).load().ids)
    assert {"alrg1", "chk01"} <= ids and "save1" not in ids


# ---- recall -------------------------------------------------------------------------------------------------


def test_recall_is_ranked_and_cut_to_the_budget(demo, monkeypatch):
    monkeypatch.setattr(embed, "active_embedder", lambda vault: ConceptEmbedder())
    out = recall(demo, "How do I exercise?", max_chars=150)
    assert out["text"].startswith("Memories relevant") and out["chars"] <= 150 and out["omitted"] > 0
    assert out["memory_ids"][0] in {"run01", "marat", "hike1"}
    monkeypatch.setattr(embed, "active_embedder", lambda vault: None)
    assert recall(demo, "zzzz qqqq", model=False)["text"] == ""


def test_recall_cli_keyword_only_without_the_assistant(demo):
    r = CliRunner().invoke(app, ["--vault", str(demo.root), "recall", "Claude Agent SDK", "--max-chars", "300"])
    assert r.exit_code == 0 and "[agent]" in r.output
    c, _ = Connectors(demo).create("Codex")
    r = CliRunner().invoke(app, ["--vault", str(demo.root), "recall", "Claude Agent SDK", "--connector", c.id])
    assert r.exit_code == 0 and demo.activity(c.id)[0]["action"] == "search"
    r = CliRunner().invoke(app, ["--vault", str(demo.root), "search", "Claude Agent SDK"])
    assert r.output.startswith("agent  ")


# ---- local models: the list, pins, downloads, the runtime --------------------------------------------------------


def _pin(monkeypatch, tmp_path, files: dict[str, bytes], model_id="multilingual-e5-small"):
    model = local.BY_ID[model_id]
    pins = {"format": 1, "models": {model_id: {"repo": model.repo, "revision": "a" * 40, "files": {
        f.path: {"sha256": hashlib.sha256(files[f.path]).hexdigest(), "size": len(files[f.path])}
        for f in model.files}}}}
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(pins))
    monkeypatch.setattr(local, "PINS_PATH", path)
    return model


class FakeHub:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.urls: list[str] = []
        self.fail = False
        self.tamper = False

    def __call__(self, request):
        self.urls.append(str(request.url))
        if self.fail:
            raise httpx2.ConnectError("no network")
        path = str(request.url).split("/resolve/" + "a" * 40 + "/", 1)[1]
        body = self.files[path]
        return httpx2.Response(200, content=body[:-1] + b"X" if self.tamper else body)


FILES = {"onnx/model_quantized.onnx": b"model-bytes" * 100, "tokenizer.json": b'{"tok": 1}'}


def test_every_listed_model_has_url_size_languages_and_license():
    assert local.DEFAULT_MODEL == "multilingual-e5-small"
    ids = [m.id for m in local.MODELS]
    assert ids == ["multilingual-e5-small", "granite-embedding-97m-multilingual-r2", "embeddinggemma-300m",
                   "qwen3-embedding-0.6b"]
    for m in local.MODELS:
        assert m.size_mb > 0 and m.languages and m.license and m.license_url.startswith("https://")
        assert any(f.role == "model" for f in m.files) and any(f.role == "tokenizer" for f in m.files)
        assert m.url(m.files[0], "abc").startswith(f"https://huggingface.co/{m.repo}/resolve/abc/")
    assert local.BY_ID["embeddinggemma-300m"].terms_url
    assert local.BY_ID["multilingual-e5-small"].query_prefix == "query: "


def test_unpinned_models_are_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "PINS_PATH", tmp_path / "none.json")
    for m in local.MODELS:
        assert local.pin_of(m) is None
        with pytest.raises(local.SemanticError) as e:
            local.ensure_downloaded(m)
        assert e.value.code == "unpinned"
    assert not any(m["available"] for m in local.models_info())
    with pytest.raises(local.SemanticError, match="isn't available yet"):
        local.turn_on("multilingual-e5-small")
    # A pin with a missing hash counts as no pin.
    (tmp_path / "half.json").write_text(json.dumps({"models": {"multilingual-e5-small": {
        "revision": "a" * 40, "files": {"tokenizer.json": {"sha256": "0" * 64, "size": 1}}}}}))
    monkeypatch.setattr(local, "PINS_PATH", tmp_path / "half.json")
    assert local.pin_of(local.BY_ID["multilingual-e5-small"]) is None


def test_download_checks_size_and_sha256_and_deletes_a_mismatch(monkeypatch, tmp_path):
    model = _pin(monkeypatch, tmp_path, FILES)
    hub = FakeHub(FILES)
    monkeypatch.setattr(local, "HTTP_TRANSPORT", httpx2.MockTransport(hub))
    hub.tamper = True
    with pytest.raises(local.SemanticError) as e:
        local.ensure_downloaded(model)
    assert e.value.code == "checksum"
    assert not any(local.model_dir(model).glob("*")) or not any(
        p.suffix in (".onnx", ".json", ".part") for p in local.model_dir(model).iterdir())
    hub.tamper = False
    seen = []
    local.ensure_downloaded(model, lambda done, total: seen.append((done, total)))
    assert local.is_downloaded(model)
    assert seen[-1] == (sum(map(len, FILES.values())),) * 2
    assert all(u.startswith("https://huggingface.co/Xenova/multilingual-e5-small/resolve/") for u in hub.urls)
    # Never into the memory folder: the per-computer settings folder.
    from openmymemo.cloud import cloud_dir

    assert str(local.model_dir(model)).startswith(str(cloud_dir()))
    # A file replaced afterwards no longer counts as downloaded.
    p = local.model_path(model, model.files[0])
    p.write_bytes(b"other")
    assert not local.is_downloaded(model)


def test_download_network_failure_leaves_nothing(monkeypatch, tmp_path):
    model = _pin(monkeypatch, tmp_path, FILES)
    hub = FakeHub(FILES)
    hub.fail = True
    monkeypatch.setattr(local, "HTTP_TRANSPORT", httpx2.MockTransport(hub))
    with pytest.raises(local.SemanticError) as e:
        local.ensure_downloaded(model)
    assert e.value.code == "network" and "internet" in str(e.value)
    assert not list(local.models_dir().rglob("*.part"))
    with pytest.raises(local.SemanticError):
        local.download("http://huggingface.co/x", tmp_path / "x", "0" * 64, 1)


def test_terms_must_be_accepted_before_embeddinggemma_downloads(monkeypatch, tmp_path):
    files = {"onnx/model_q4.onnx": b"m", "onnx/model_q4.onnx_data": b"d", "tokenizer.json": b"t"}
    _pin(monkeypatch, tmp_path, files, "embeddinggemma-300m")
    monkeypatch.setattr(local, "runtime_available", lambda: True)
    monkeypatch.setattr(local, "HTTP_TRANSPORT", httpx2.MockTransport(FakeHub(files)))
    with pytest.raises(local.SemanticError) as e:
        local.turn_on("embeddinggemma-300m")
    assert e.value.code == "terms"
    s = local.turn_on("embeddinggemma-300m", accept_terms=True)
    assert s.enabled and s.model == "embeddinggemma-300m" and "embeddinggemma-300m" in s.accepted_terms


def test_runtime_install_uses_uv_into_its_own_folder_or_names_the_command(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(local, "runtime_available", lambda: bool(ran))
    monkeypatch.setattr(local, "runtime_unsupported", lambda: False)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    monkeypatch.setenv("OPENMYMEMO_UV", str(uv))

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(local, "_run", lambda cmd, **kw: ran.append(cmd) or Done())
    local.install_runtime()
    cmd = ran[0]
    assert cmd[:3] == [str(uv), "pip", "install"] and "--target" in cmd
    assert cmd[cmd.index("--target") + 1] == str(local.runtime_dir())
    assert "onnxruntime>=1.20" in cmd and "tokenizers>=0.20" in cmd
    # Neither uv nor pip: the person gets the exact command.
    ran.clear()
    monkeypatch.delenv("OPENMYMEMO_UV")
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    monkeypatch.setattr(local.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(local.SemanticError) as e:
        local.install_runtime()
    assert e.value.code == "runtime" and "pip install" in str(e.value) and "--target" in str(e.value)


def test_an_intel_mac_gets_the_runtime_from_macos_13_and_is_told_before_that(demo, monkeypatch, tmp_path):
    """ONNX Runtime 1.23, its last release for Intel Macs, needs macOS 13 and Python 3.13 or earlier: there the
    install goes ahead (the installer picks 1.23); before that, say so instead of failing it or downloading a model."""
    py = collections.namedtuple("version_info", "major minor micro")
    monkeypatch.setattr(local.sys, "platform", "darwin")
    monkeypatch.setattr(local.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local.sys, "version_info", py(3, 12, 9))
    for version, unsupported in (("13.0", False), ("15.6.1", False), ("12.7.6", True), ("", False)):
        monkeypatch.setattr(local.platform, "mac_ver", lambda v=version: (v, ("", "", ""), "x86_64"))
        assert local.runtime_unsupported() is unsupported, version
    monkeypatch.setattr(local.platform, "mac_ver", lambda: ("14.5", ("", "", ""), "x86_64"))
    monkeypatch.setattr(local.sys, "version_info", py(3, 14, 0))
    assert local.runtime_unsupported()
    monkeypatch.setattr(local.sys, "version_info", py(3, 12, 9))
    monkeypatch.setattr(local.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(local.platform, "mac_ver", lambda: ("12.7", ("", "", ""), "arm64"))
    assert not local.runtime_unsupported()

    # macOS 13 on Intel: the installer runs.
    monkeypatch.setattr(local.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local.platform, "mac_ver", lambda: ("13.6", ("", "", ""), "x86_64"))
    ran = []
    monkeypatch.setattr(local, "runtime_available", lambda: bool(ran))
    monkeypatch.setattr(local, "install_command", lambda display=False: ["uv", "pip", "install"])

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(local, "_run", lambda cmd, **kw: ran.append(cmd) or Done())
    local.install_runtime()
    assert ran

    # macOS 12 on Intel: told before anything is installed or downloaded.
    monkeypatch.setattr(local.platform, "mac_ver", lambda: ("12.7.6", ("", "", ""), "x86_64"))
    monkeypatch.setattr(local, "runtime_available", lambda: False)
    monkeypatch.setattr(local, "_run", lambda *a, **kw: pytest.fail("nothing is installed"))
    monkeypatch.setattr(local, "ensure_downloaded", lambda *a, **kw: pytest.fail("nothing is downloaded"))
    _pin(monkeypatch, tmp_path, FILES)
    with pytest.raises(local.SemanticError) as e:
        local.turn_on()
    assert e.value.code == "runtime" and "Intel Mac" in str(e.value) and "macOS 13" in str(e.value)
    assert not local.load_semantic().enabled


def test_turning_off_deletes_the_model_and_the_index_and_keeps_the_choice(demo, monkeypatch, tmp_path):
    model = _pin(monkeypatch, tmp_path, FILES)
    monkeypatch.setattr(local, "HTTP_TRANSPORT", httpx2.MockTransport(FakeHub(FILES)))
    monkeypatch.setattr(local, "runtime_available", lambda: True)
    concept = ConceptEmbedder()
    concept.id = "local/multilingual-e5-small@test"
    monkeypatch.setattr(semantic, "local_embedder", lambda: concept)
    semantic.turn_on(demo, "multilingual-e5-small")
    assert local.is_downloaded(model) and EmbeddingIndex(demo).load().model == concept.id
    assert local.load_semantic().enabled
    semantic.turn_off(demo)
    assert not local.models_dir().exists() and EmbeddingIndex(demo).load() is None
    s = local.load_semantic()
    assert not s.enabled and s.model == "multilingual-e5-small"


def test_pooling_matches_the_model_cards():
    np = pytest.importorskip("numpy")
    from openmymemo.recall.embed import pool

    hidden = np.array([[[1.0, 0.0], [3.0, 2.0], [9.0, 9.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0]])
    assert pool(hidden, mask, "mean").tolist() == [[2.0, 1.0]]
    assert pool(hidden, mask, "cls").tolist() == [[1.0, 0.0]]
    assert pool(hidden, mask, "last").tolist() == [[3.0, 2.0]]


def test_onnx_embedder_prefixes_pools_and_normalizes():
    np = pytest.importorskip("numpy")
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer, models, pre_tokenizers

    vocab = {"<pad>": 0, "query": 1, "passage": 2, "tea": 3, "coffee": 4, "[UNK]": 5, ":": 6}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    seen = {}

    class Input:
        def __init__(self, name):
            self.name = name

    class Session:
        def get_inputs(self):
            return [Input("input_ids"), Input("attention_mask"), Input("token_type_ids")]

        def get_outputs(self):
            return [Input("last_hidden_state")]

        def run(self, names, feeds):
            seen.update(feeds)
            ids = feeds["input_ids"]
            return [np.stack([ids, ids * 0 + 1], axis=-1).astype(np.float32)]

    e = embed.OnnxEmbedder(local.BY_ID["multilingual-e5-small"], session=Session(), tokenizer=tok)
    assert tokenizers and e.id.startswith("local/multilingual-e5-small@")
    q = e.embed(["tea"], "query")[0]
    assert seen["input_ids"].tolist() == [[1, 6, 3]]  # "query: tea"
    assert abs(sum(x * x for x in q) - 1) < 1e-6
    e.embed(["tea coffee", "tea"], "passage")
    assert seen["input_ids"].tolist() == [[2, 6, 3, 4], [2, 6, 3, 0]] and seen["attention_mask"].tolist()[1] == [1, 1, 1, 0]


def test_onnx_embedder_gives_a_decoder_an_empty_key_value_cache():
    """Qwen3's export also takes past_key_values.N.key/value; with no past, each is an empty [batch, heads, 0, dims]."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer, models, pre_tokenizers

    tok = Tokenizer(models.WordLevel({"<|endoftext|>": 0, "tea": 1, "[UNK]": 2}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    seen = {}

    class Input:
        def __init__(self, name, shape=(), type="tensor(int64)"):
            self.name, self.shape, self.type = name, list(shape), type

    class Session:
        def get_inputs(self):
            return [Input("input_ids"), Input("attention_mask"), Input("position_ids"),
                    Input("past_key_values.0.key", ("batch_size", 8, "past_sequence_length", 128), "tensor(float)"),
                    Input("past_key_values.0.value", ("batch_size", 8, "past_sequence_length", 128), "tensor(float16)")]

        def get_outputs(self):
            return [Input("last_hidden_state"), Input("present.0.key")]

        def run(self, names, feeds):
            seen.update(feeds)
            ids = feeds["input_ids"]
            return [np.stack([ids + 1, ids * 0 + 1], axis=-1).astype(np.float32)]

    e = embed.OnnxEmbedder(local.BY_ID["qwen3-embedding-0.6b"], session=Session(), tokenizer=tok)
    assert len(e.embed(["tea", "tea tea"], "passage")) == 2
    assert seen["past_key_values.0.key"].shape == (2, 8, 0, 128) and seen["past_key_values.0.key"].dtype == np.float32
    assert seen["past_key_values.0.value"].dtype == np.float16
    assert embed.empty_past(("batch_size", 8, "past_sequence_length", 128), 3) == [3, 8, 0, 128]


# ---- panel routes and CLI ----------------------------------------------------------------------------------


def test_panel_routes(demo, monkeypatch, tmp_path):
    api = TestClient(create_api(demo, 8787), base_url="http://127.0.0.1:8787")
    s = api.get("/api/semantic", headers=H).json()
    assert s["enabled"] is False and s["model"] == "multilingual-e5-small" and s["embedder"] is None
    assert [m["id"] for m in s["models"]][0] == "multilingual-e5-small" and s["models"][0]["recommended"]
    assert api.get("/api/semantic").status_code == 403  # the panel guard holds
    # Nothing pinned: refused at once, with the reason.
    monkeypatch.setattr(local, "PINS_PATH", tmp_path / "none.json")
    r = api.post("/api/semantic", headers=H, json={"enabled": True})
    assert r.status_code == 400 and "isn't available yet" in r.json()["detail"]
    # Pinned, a fake hub, the runtime present: the job runs to the end and builds the index.
    _pin(monkeypatch, tmp_path, FILES)
    monkeypatch.setattr(local, "HTTP_TRANSPORT", httpx2.MockTransport(FakeHub(FILES)))
    monkeypatch.setattr(local, "runtime_available", lambda: True)
    concept = ConceptEmbedder()
    monkeypatch.setattr(semantic, "local_embedder", lambda: concept)
    monkeypatch.setattr(embed, "local_embedder", lambda: concept)
    r = api.post("/api/semantic", headers=H, json={"enabled": True, "model": "multilingual-e5-small"})
    assert r.status_code == 200
    for _ in range(100):
        s = api.get("/api/semantic", headers=H).json()
        if s["job"]["phase"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert s["job"]["phase"] == "done", s["job"]
    assert s["enabled"] and s["embedder"] == "local" and s["index"]["count"] == len(demo.memories())
    found = api.get("/api/search", params={"q": "What do I like to drink?"}, headers=H).json()
    assert found["mode"] == "hybrid" and found["results"][0]["id"] == "tea01"
    off = api.post("/api/semantic", headers=H, json={"enabled": False}).json()
    assert off["enabled"] is False
    monkeypatch.setattr(embed, "local_embedder", lambda: None)
    assert api.get("/api/search", params={"q": "tea"}, headers=H).json()["mode"] == "keyword"


def test_assistant_embeddings_model_setting(demo):
    api = TestClient(create_api(demo, 8787), base_url="http://127.0.0.1:8787")
    s = api.patch("/api/assistant", headers=H, json={"enabled": True, "kind": "local", "model": "llama3.2",
                                                      "embeddings_model": "nomic-embed-text"}).json()
    assert s["embeddings_model"] == "nomic-embed-text" and s["embeddings"] is True
    assert api.patch("/api/assistant", headers=H, json={"embeddings_model": "two words"}).status_code == 400


def test_semantic_cli(demo, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "PINS_PATH", tmp_path / "none.json")  # unpinned, whatever model_pins.json ships
    runner = CliRunner()
    r = runner.invoke(app, ["--vault", str(demo.root), "semantic", "status"])
    assert r.exit_code == 0 and "off" in r.output and "multilingual-e5-small" in r.output
    assert "not available yet" in r.output
    r = runner.invoke(app, ["--vault", str(demo.root), "semantic", "on"])
    assert r.exit_code == 1 and "isn't available yet" in r.output
    r = runner.invoke(app, ["--vault", str(demo.root), "semantic", "on", "--model", "embeddinggemma-300m"])
    assert r.exit_code == 2 and "--accept-terms" in r.output
    r = runner.invoke(app, ["--vault", str(demo.root), "semantic", "index"])
    assert r.exit_code == 1 and "No embedding model" in r.output
    r = runner.invoke(app, ["--vault", str(demo.root), "semantic", "off"])
    assert r.exit_code == 0 and "off" in r.output


def test_the_shipped_pins_are_complete():
    """Every model in model_pins.json names a 40-character revision and pins exactly the model's files."""
    shipped = local.load_pins()
    ids = {m.id for m in local.MODELS}
    assert set(shipped) <= ids
    for m in local.MODELS:
        if m.id in shipped:
            pin = local.pin_of(m, shipped)
            assert pin is not None and len(pin["revision"]) == 40
            assert set(pin["files"]) == {f.path for f in m.files}
