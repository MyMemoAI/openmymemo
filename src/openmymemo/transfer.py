"""Import memories from other AI assistants and export them back out.

Supported import sources:

* ``chatgpt``  — text copied from ChatGPT's Settings → Personalization → Manage memories, or the list
  ChatGPT prints when asked to dump its memory.
* ``claude``   — Claude's memory as shown in Settings, or the ``[date] – memory`` list its export prompt produces.
* ``gemini`` / ``text`` — any list, one memory per line.
* ``markdown`` — list items; headings are kept as hints.
* ``json``     — a list of strings or objects (``content``/``memory``/``text``), including Mem0 exports and
  OpenMyMemo's own export (whose ``topic`` is honoured when that topic exists).

Imports always go through a preview: parse → check duplicates → commit. Nothing is classified into a topic at
import time: memories land in a topic of their own, ``from-<source>`` in the "Imported" group, in the source's
order and with its dates. Sorting them into the regular topics is a separate step: ``sort_plan`` suggests a
topic for each memory with the rule-based classifier and ``sort_commit`` moves the ones the user picked.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .textutil import KINDS, classify_kind, classify_topic, similarity
from .topics import IMPORT_SOURCE_NAMES, IMPORTED_GROUP, import_topic_slug
from .vault import NotFound, Vault, VaultError, now_iso

SOURCES = ("chatgpt", "claude", "gemini", "text", "markdown", "json")
EXPORT_FORMATS = ("markdown", "claude", "chatgpt", "json")
SORT_SURE_AT = 0.5  # suggestions at or above this confidence are applied by `sort --yes`

# UI chrome that ends up in a copy-paste of a memory settings page.
_NOISE = {
    "saved memories", "manage memories", "memory", "memories", "delete", "delete all", "search memories",
    "memory is full", "reference saved memories", "reference chat history", "chatgpt", "claude", "close",
    "管理记忆", "已保存的记忆", "记忆", "删除", "全部删除", "搜索记忆", "记忆已满", "关闭", "保存的记忆",
}
_BULLET = re.compile(r"^\s*(?:[-*+•·▪●◦]|\d+[.)、]|\(\d+\))\s+")
_DATED = re.compile(r"^\s*\[?(\d{4}-\d{2}(?:-\d{2})?)\]?\s*[–—:\-]+\s*(.+)$")
_HEADING = re.compile(r"^\s*(?:#{1,6}\s+(.+?)\s*#*|\*\*(.+?)\*\*:?)\s*$")


@dataclass
class ImportItem:
    content: str
    topic: str | None = None  # an existing topic the source names explicitly; None lands in from-<source>
    kind: str | None = None
    created_at: str | None = None
    hint: str | None = None  # the heading or category the item sat under in the source
    duplicate_of: str | None = None
    similar_to: str | None = None
    similar_content: str | None = None
    include: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(line: str) -> str:
    line = _BULLET.sub("", line).strip()
    line = re.sub(r"\s*\^[a-z0-9-]+$", "", line)
    return line.strip(" \t\"“”")


def parse(source: str, text: str) -> list[ImportItem]:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if source == "json" or (text[:1] in "[{" and source in ("text", "chatgpt", "claude")):
        try:
            return _parse_json(json.loads(text))
        except json.JSONDecodeError:
            if source == "json":
                raise
    items: list[ImportItem] = []
    hint = None
    in_fence = False
    for raw in text.split("\n"):
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        h = _HEADING.match(raw)
        if h and not _BULLET.match(raw):
            hint = (h.group(1) or h.group(2)).strip()
            continue
        line = raw.strip()
        if not line or line.lower().strip(":：") in _NOISE or len(line) < 3:
            continue
        if source == "markdown" and not _BULLET.match(raw):
            continue
        created = None
        m = _DATED.match(_BULLET.sub("", line))
        if m:
            created, line = m.group(1), m.group(2)
        content = _clean(line)
        if len(content) < 3 or content.lower() in _NOISE:
            continue
        items.append(ImportItem(content=content, created_at=created, hint=hint))
    # The same memory pasted twice is one memory.
    unique: list[ImportItem] = []
    for it in items:
        if not any(similarity(it.content, u.content) >= 0.95 for u in unique):
            unique.append(it)
    return unique


def _parse_json(data) -> list[ImportItem]:
    if isinstance(data, dict):
        for key in ("memories", "results", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    items = []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, str):
            items.append(ImportItem(content=" ".join(row.split())))
        elif isinstance(row, dict):
            content = row.get("content") or row.get("memory") or row.get("text") or row.get("value")
            if not content:
                continue
            cats = row.get("categories")
            hint = row.get("topic") or (cats[0] if isinstance(cats, list) and cats else None)
            items.append(ImportItem(content=" ".join(str(content).split()), topic=row.get("topic"), kind=row.get("kind"),
                                    created_at=row.get("created_at"), hint=hint))
    return items


def plan(vault: Vault, items: list[ImportItem]) -> list[ImportItem]:
    """Guess each item's kind and flag duplicates of memories already in the vault.

    No topic is guessed: an item keeps its topic only when the source names a topic that exists (OpenMyMemo's
    own JSON export does); everything else goes to the source's ``from-<source>`` topic on commit.
    """
    slugs = {t["slug"] for t in vault.topics()}
    existing = vault.memories()
    for it in items:
        if not (isinstance(it.topic, str) and it.topic in slugs):
            it.topic = None
        if it.kind not in KINDS:
            it.kind = classify_kind(it.content)
        best, best_sim = None, 0.0
        for m in existing:
            s = similarity(it.content, m.content)
            if s > best_sim:
                best, best_sim = m, s
        if best and best_sim >= 0.92:
            it.duplicate_of, it.include = best.id, False
        elif best and best_sim >= 0.6:
            it.similar_to, it.similar_content = best.id, best.content
            it.include = best_sim < 0.8  # a near-copy of something you have: opt in rather than out
    return items


def _created_at(value) -> str | None:
    """A source's date as an ISO timestamp; ``YYYY-MM`` and ``YYYY-MM-DD`` become midnight UTC."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        text += "-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    if not re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat(timespec="seconds")


def import_topic(vault: Vault, source: str) -> tuple[str, str]:
    """Slug and title of the topic imports from ``source`` land in (created on the first import)."""
    slug = import_topic_slug(source)
    title = next((t["title"] for t in vault.topics() if t["slug"] == slug), f"From {IMPORT_SOURCE_NAMES[source]}")
    return slug, title


def commit(vault: Vault, items: list[ImportItem], source: str) -> list[str]:
    """Save the included items in the order given: into the topic an item names if it exists, otherwise into
    ``from-<source>`` (created on the first import from that source). Returns the new memory ids."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    ids: list[str] = []
    with vault.lock():
        slugs = {t["slug"] for t in vault.topics()}
        inbox: str | None = None
        try:
            for it in items:
                if not it.include:
                    continue
                topic = it.topic if isinstance(it.topic, str) and it.topic in slugs else None
                if topic is None:
                    topic = inbox = inbox or vault.ensure_import_topic(source)
                m = vault.add(topic, it.content, kind=it.kind if it.kind in KINDS else None,
                              source=f"import:{source}", actor=f"import:{source}", created_at=_created_at(it.created_at))
                ids.append(m.id)
        finally:
            vault.log_activity("user", "import", source=source, memory_ids=ids)
    return ids


# ---- sorting imported memories into topics ---------------------------------------------------------

def sort_plan(vault: Vault, topic: str | None = None) -> list[dict]:
    """Suggest a topic for every memory in the imported topics (or only in ``topic``). Moves nothing.

    Suggestions come from the rule-based classifier over the topics outside the Imported group; ``topic`` is
    None when nothing matched.
    """
    topics = vault.topics()
    imported = [t["slug"] for t in topics if t["group"] == IMPORTED_GROUP]
    if topic is not None:
        if topic not in imported:
            vault.topic(topic)  # an unknown topic is NotFound
            raise VaultError(f"{topic} isn't an imported topic; sort files the memories in the Imported group")
        imported = [topic]
    targets = [t for t in topics if t["group"] != IMPORTED_GROUP]
    rows = []
    for m in vault.memories():
        if m.topic in imported:
            slug, confidence = classify_topic(m.content, targets)
            rows.append({"memory_id": m.id, "content": m.content, "from_topic": m.topic, "topic": slug,
                         "confidence": round(confidence, 2)})
    return rows


def sort_commit(vault: Vault, moves: list[tuple[str, str]]) -> int:
    """Move each listed memory to its topic. Returns how many moved; archived ones and ones already there don't."""
    with vault.lock():
        slugs = {t["slug"] for t in vault.topics()}
        for memory_id, topic in moves:  # check everything before moving anything
            if topic not in slugs:
                raise NotFound(f"Unknown topic: {topic}")
            vault.get(memory_id)
        moved = 0
        for memory_id, topic in moves:
            m = vault.get(memory_id)
            if m.status != "active" or m.topic == topic:
                continue
            vault.update(memory_id, topic=topic, actor="user")
            moved += 1
    return moved


def export(vault: Vault, fmt: str = "markdown", include_private: bool = False) -> str:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(EXPORT_FORMATS)}")
    topics = {t["slug"]: t for t in vault.topics()}
    mems = [m for m in vault.memories() if include_private or not vault.is_private(m)]
    if fmt == "json":
        return json.dumps({"format": "openmymemo/1", "exported_at": now_iso(),
                           "topics": list(topics.values()),
                           "memories": [m.to_dict(topics.get(m.topic, {}).get("private", False)) for m in mems]},
                          ensure_ascii=False, indent=2) + "\n"
    if fmt == "claude":
        return "\n".join(f"[{(m.created_at or now_iso())[:10]}] – {m.content}" for m in mems) + "\n"
    if fmt == "chatgpt":
        head = "Please remember the following about me (from my OpenMyMemo memory). Save each item to memory:"
        return head + "\n\n" + "\n".join(f"- {m.content}" for m in mems) + "\n"
    lines = []
    current = None
    for m in mems:
        if m.topic != current:
            current = m.topic
            title = topics.get(m.topic, {}).get("title", m.topic)
            lines.append(("\n" if lines else "") + f"## {title}\n")
        lines.append(f"- {m.content}")
    return "\n".join(lines) + "\n"
