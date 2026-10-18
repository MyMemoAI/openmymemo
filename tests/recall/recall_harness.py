"""The recall test set (product-design.md §17.4): a demo folder (memories.json), English and Chinese questions with
the memory ids that answer them (questions.json), and hit@k for each way of searching.

Modes:

* ``keyword``: today's keyword search, no index, no model.
* ``vectors``: keyword fused with vectors. In CI the vectors come from ``ConceptEmbedder``, a deterministic stand-in
  with a small bilingual concept list: it measures the fusion pipeline, not a real model's quality.
* ``expand+rerank``: no index; the Assistant expands the query and reranks, replaying the recorded answers in
  questions.json through a fake endpoint (no network).
* ``vectors+rerank``: both.

Run it to print the table (CI runs it as a test, ``test_recall_set.py``):

    uv run python tests/recall/recall_harness.py
    uv run python tests/recall/recall_harness.py --local     # also with each downloaded local model

``--local`` uses the models downloaded on this computer (``openmymemo semantic on --model ID``); CI never downloads
one, so real models' numbers are measured by a maintainer and noted in the PR that changes the list.
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))

from openmymemo.textutil import grams, normalize  # noqa: E402
from openmymemo.vault import Vault  # noqa: E402

KS = (1, 3, 5)

# Each concept: words that, in this stand-in model, mean roughly the same thing (English words match whole words,
# Chinese ones as substrings).
CONCEPTS: dict[str, tuple[str, ...]] = {
    "beverage": ("drink", "drinks", "beverage", "tea", "coffee", "oolong", "喝", "茶", "咖啡", "饮料"),
    "books": ("book", "books", "novel", "fiction", "reading", "read", "书", "小说", "阅读"),
    "music": ("instrument", "bass", "guitar", "band", "play", "plays", "music", "乐器", "吉他", "弹", "乐队", "贝斯"),
    "exercise": ("exercise", "running", "run", "marathon", "hiking", "sport", "运动", "跑步", "爬山", "马拉松"),
    "food": ("eat", "meat", "vegetarian", "diet", "吃", "素", "肉"),
    "home": ("live", "lives", "home", "住", "上海", "shanghai", "家"),
    "job": ("job", "work", "engineer", "backend", "office", "职业", "工作", "工程师"),
    "code": ("programming", "code", "go", "postgresql", "swiftui", "编程", "代码", "写代码"),
    "kids": ("kids", "children", "daughter", "son", "孩子", "女儿", "儿子"),
    "pet": ("pet", "cat", "dog", "猫", "狗", "宠物"),
    "answers": ("answers", "answer", "format", "concise", "conclusion", "回答", "格式", "示例"),
    "language": ("language", "languages", "japanese", "chinese", "english", "语言", "外语", "日语", "英语", "中文"),
    "study": ("learning", "studying", "study", "学", "练"),
    "sleep": ("bed", "sleep", "night", "睡觉", "睡", "上床", "晚上"),
    "editor": ("editor", "code", "macbook", "ide", "编辑器", "电脑"),
    "travel": ("travel", "trip", "japan", "vacation", "旅行", "日本"),
    "spouse": ("wife", "husband", "spouse", "妻子", "丈夫"),
    "doctor": ("doctor", "pediatrician", "医生"),
    "friend": ("friend", "朋友"),
    "meeting": ("meeting", "meet", "standup", "开会", "站会", "会议"),
    "birth": ("born", "birth", "birthplace", "hometown", "出生"),
    "film": ("film", "movie", "电影"),
}


class ConceptEmbedder:
    """A deterministic stand-in for an embedding model: one dimension per concept, plus a light bag of words, so
    "What do I like to drink?" lands near "Prefers tea over coffee" and "我喜欢喝什么" near both."""

    remote = False
    min_similarity = 0.3

    def __init__(self, name: str = "concepts-v1"):
        self.id = f"test/{name}"
        self.calls = 0

    def may_embed(self, memory) -> bool:
        return True

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float]]:
        self.calls += 1
        return [self._one(t) for t in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        t = normalize(text)
        words = set(re.findall(r"[a-z0-9]+", t))
        vec = []
        for terms in CONCEPTS.values():
            hit = sum(1 for term in terms if (term in words if term.isascii() else term in t))
            vec.append(float(hit > 0))
        bag = [0.0] * 32
        for g in grams(t):
            bag[sum(map(ord, g)) % 32] += 0.15
        vec += bag
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def load_set() -> tuple[dict, list[dict]]:
    memories = json.loads((HERE / "memories.json").read_text(encoding="utf-8"))
    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["questions"]
    return memories, questions


def build_vault(path: Path) -> Vault:
    """The demo folder: the default topics with the test set's memories, ids as in memories.json."""
    memories, _ = load_set()
    v = Vault.create(path)
    for topic, rows in memories.items():
        f = v.memories_dir / f"{topic}.md"
        f.write_text(f.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
                     + "".join(f"- {content} ^{mid}\n" for mid, content in rows), encoding="utf-8")
    v.sync()
    return v


class ReplayEndpoint:
    """A fake OpenAI-compatible chat endpoint that answers expansion and rerank requests from questions.json."""

    def __init__(self, questions: list[dict]):
        self.questions = questions
        self.requests = 0

    def __call__(self, request):
        import httpx2

        self.requests += 1
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        q = next((x for x in sorted(self.questions, key=lambda x: -len(x["q"])) if x["q"] in user), None)
        if "keywords" in system and "rank" not in system:
            answer = {"keywords": q["expand"] if q else []}
        else:
            offered = re.findall(r"\[([A-Za-z0-9-]+)\] ", user)
            answer = {"ids": [mid for mid in (q["rerank"] if q else []) if mid in offered]}
        return httpx2.Response(200, json={"model": "replay", "choices": [{"message": {"content": json.dumps(answer)}}],
                                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def evaluate(vault: Vault, questions: list[dict], *, embedder=None, model: bool = False) -> dict:
    """hit@k for each k in KS, overall and per language: the share of questions with an expected id in the top k."""
    from openmymemo.recall import search

    hits = {k: {"all": 0, "en": 0, "zh": 0} for k in KS}
    counts = {"all": 0, "en": 0, "zh": 0}
    for q in questions:
        for lang in ("all", q["lang"]):
            counts[lang] += 1
        for k in KS:  # a search for k results, as an app asking for k would get (rerank runs past the limit)
            result = search(vault, None, q["q"], limit=k, model=model, embedder=embedder or None,
                            refresh_budget=None)
            if {h.memory.id for h in result.hits} & set(q["expect"]):
                for lang in ("all", q["lang"]):
                    hits[k][lang] += 1
    return {f"hit@{k}": {lang: round(hits[k][lang] / counts[lang], 3) if counts[lang] else None
                         for lang in counts} for k in KS}


def turn_on_replay(vault: Vault, questions: list[dict]) -> ReplayEndpoint:
    """The Assistant on, with a model "on this computer" that is the replay endpoint (call inside a test that
    monkeypatches, or from main, which sets the module attribute itself)."""
    import httpx2

    from openmymemo.assistant import client, ensure_connector, save_settings

    replay = ReplayEndpoint(questions)
    client.TRANSPORT = httpx2.MockTransport(replay)
    save_settings(enabled=True, kind="local", model="replay", caps={"calls": 0, "tokens": 0})
    ensure_connector(vault, regrant=True)
    return replay


def run_all(vault: Vault, questions: list[dict], local_models: Sequence = ()) -> dict[str, dict]:
    from openmymemo.assistant import save_settings

    out = {"keyword": evaluate(vault, questions), "vectors": evaluate(vault, questions, embedder=ConceptEmbedder())}
    turn_on_replay(vault, questions)
    try:
        out["expand+rerank"] = evaluate(vault, questions, model=True)
        out["vectors+rerank"] = evaluate(vault, questions, embedder=ConceptEmbedder(), model=True)
    finally:
        save_settings(enabled=False)
    for emb in local_models:
        out[f"vectors ({emb.model.id})"] = evaluate(vault, questions, embedder=emb)
    return out


def table(results: dict[str, dict]) -> str:
    lines = ["| Mode | " + " | ".join(f"hit@{k} (en / zh)" for k in KS) + " |",
             "| --- | " + " | ".join("---" for _ in KS) + " |"]
    for mode, r in results.items():
        cells = [f"{r[f'hit@{k}']['all']:.2f} ({r[f'hit@{k}']['en']:.2f} / {r[f'hit@{k}']['zh']:.2f})" for k in KS]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import os

    os.environ.setdefault("OPENMYMEMO_ASSISTANT_KEYCHAIN", "0")
    local_models = []
    if "--local" in argv:
        from openmymemo.recall import local
        from openmymemo.recall.embed import OnnxEmbedder

        local_models = [OnnxEmbedder(m) for m in local.MODELS if local.is_downloaded(m) and local.runtime_available()]
        for emb in local_models:
            emb._load()  # from this computer's models folder, before the settings folder is switched below
    with tempfile.TemporaryDirectory() as tmp:
        # The Assistant's settings go to a throwaway folder, never this computer's real ones.
        os.environ["OPENMYMEMO_CLOUD_DIR"] = str(Path(tmp) / "config")
        _, questions = load_set()
        vault = build_vault(Path(tmp) / "vault")
        print(table(run_all(vault, questions, local_models)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
