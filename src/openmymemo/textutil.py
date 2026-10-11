"""Small text helpers: normalization, similarity, search scoring and rule-based classification.

Everything here is deterministic and dependency-free so the vault works without any model configured.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .topics import CLASSIFY_WEIGHT, STRONG_KEYWORDS, TOPIC_BY_SLUG

KINDS = ("fact", "preference", "ongoing", "instruction", "event")

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(text.split())


def grams(text: str) -> set[str]:
    """Word tokens for Latin text plus character bigrams for CJK runs (and short single chars)."""
    text = normalize(text)
    out: set[str] = set(_WORD.findall(text))
    cjk = "".join(ch if _CJK.match(ch) else " " for ch in text)
    for run in cjk.split():
        if len(run) == 1:
            out.add(run)
        out.update(run[i : i + 2] for i in range(len(run) - 1))
    return out


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    ga, gb = grams(na), grams(nb)
    jaccard = len(ga & gb) / len(ga | gb) if ga and gb else 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return max(jaccard, ratio * 0.9 + jaccard * 0.1)


_NEGATIONS = ("不", "没", "别", "无", "非", "never", "not ", "n't", "no longer", "stopped", "quit")


def looks_contradictory(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return any(neg in na for neg in _NEGATIONS) != any(neg in nb for neg in _NEGATIONS)


def search_score(query: str, text: str) -> float:
    q, t = normalize(query), normalize(text)
    if not q:
        return 0.0
    score = 1.0 if q in t else 0.0
    qg = grams(q)
    if qg:
        tg = grams(t)
        score += len(qg & tg) / len(qg)
    return score


# ---- classification ------------------------------------------------------------------------------

_KIND_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("instruction", ("请", "回答时", "回复时", "不要", "别", "务必", "总是", "永远", "用中文", "用英文",
                     "always ", "never ", "please ", "when you", "don't ", "do not ", "make sure", "prefer that you",
                     "respond", "reply in")),
    ("preference", ("喜欢", "不喜欢", "讨厌", "偏好", "更喜欢", "爱", "习惯用", "倾向",
                    "prefer", "like", "dislike", "love", "hate", "enjoy", "favorite", "favourite", "can't stand")),
    ("ongoing", ("正在", "目前", "最近", "在做", "在学", "准备", "计划", "打算", "这周", "这个月", "今年",
                 "currently", "working on", "right now", "these days", "planning", "plan to", "this week",
                 "this month", "this year", "preparing", "learning", "building")),
    ("event", ("曾经", "去年", "那年", "毕业于", "出生于", "搬到", "结婚", "发生", "年在",
               "graduated", "was born", "moved to", "married", "last year", "in 19", "in 20", "happened", "used to")),
]


def classify_kind(text: str) -> str:
    t = normalize(text)
    for kind, cues in _KIND_RULES:
        if any(cue in t for cue in cues):
            return kind
    return "fact"


_STOP = {"what", "with", "that", "this", "from", "your", "about", "goes", "here", "they", "them", "have", "into"}


def classify_topic(text: str, topics: list[dict], hint: str | None = None) -> tuple[str | None, float]:
    """Pick the best topic for a memory. ``topics`` are ``{"slug", "title", "description"}`` dicts.

    Returns ``(slug, confidence)``; slug is None when nothing matched.
    """
    t = normalize(text)
    if hint:
        h = normalize(hint)
        for topic in topics:
            if h in (topic["slug"], normalize(topic.get("title") or "")):
                return topic["slug"], 1.0
    best, best_score = None, 0.0
    for topic in topics:
        keywords: list[str] = []
        default = TOPIC_BY_SLUG.get(topic["slug"])
        if default:
            keywords.extend(default.keywords)
        elif topic.get("description"):
            # Custom topics have no curated keywords; use the meaningful words of their description.
            words = re.split(r"[,，、;；/\s—-]+", normalize(topic["description"]))
            keywords.extend(w for w in words if (len(w) >= 2 and _CJK.match(w)) or (len(w) >= 4 and w not in _STOP))
        if topic.get("title"):
            keywords.append(normalize(topic["title"]))
        score = 0.0
        for kw in keywords:
            kw = kw.lower()
            if kw and kw in t:
                score += 1.0 + min(len(kw), 8) / 8
        score += sum(3.0 for kw in STRONG_KEYWORDS.get(topic["slug"], ()) if kw in t)
        if hint and topic.get("title") and normalize(topic["title"]) in normalize(hint):
            score += 2
        score *= CLASSIFY_WEIGHT.get(topic["slug"], 1.0)
        if score > best_score:
            best, best_score = topic["slug"], score
    if best is None:
        return None, 0.0
    return best, min(1.0, best_score / 3)
