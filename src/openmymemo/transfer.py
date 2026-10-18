"""Import memories from other AI assistants and export them back out.

Supported import sources:

* ``chatgpt``  — text copied from ChatGPT's Settings → Personalization → Manage memories, or the list
  ChatGPT prints when asked to dump its memory.
* ``claude``   — Claude's memory as shown in Settings, or the ``[date] – memory`` list its export prompt produces.
* ``gemini`` / ``text`` — any list, one memory per line.
* ``markdown`` — list items; headings are kept as hints.
* ``json``     — a list of strings or objects (``content``/``memory``/``text``), including Mem0 exports and
  OpenMyMemo's own export (whose ``topic`` is honoured when that topic exists).
* ``claude-code``, ``codex``, ``gemini-cli``, ``cursor``, ``windsurf``, ``cline``, ``copilot``, ``kiro``, ``file``
  — a memory or instructions file another AI app keeps on disk (CLAUDE.md, AGENTS.md, GEMINI.md, Cursor rules,
  memory.md, …; ``memfiles`` finds them). Each list item or paragraph is one memory.

Imports always go through a preview: parse → check duplicates → commit. With the OpenMyMemo Assistant on for imports,
``assistant.features.import_preview`` splits the text with the model instead and cross-checks it against ``parse``. Nothing is classified into a topic at
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

DOCUMENT_SOURCES = ("claude-code", "codex", "gemini-cli", "cursor", "windsurf", "cline", "copilot", "kiro", "file")
SOURCES = ("chatgpt", "claude", "gemini", "text", "markdown", "json", *DOCUMENT_SOURCES)
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
# The # form takes everything after the first space and _heading_text trims it: a lazy `\s+(.+?)\s*#*\s*$`
# backtracks over every split of a long run of spaces (CodeQL py/polynomial-redos).
_HEADING = re.compile(r"^\s*(?:#{1,6}\s(.+)|\*\*(.+?)\*\*:?\s*)$")


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
    by: str | None = None  # who split it: "assistant" or "rules" when the Assistant was on for imports, else None
    flag: str | None = None  # "dropped": the Assistant left this line out, the rules would have kept it

    def to_dict(self) -> dict:
        return asdict(self)


def _heading_text(text: str) -> str:
    """A # heading's text without the spaces around it and the closing #s, keeping at least one character."""
    text = text.lstrip() or text[-1:]
    return text.rstrip().rstrip("#").rstrip() or text[:1]


def _clean(line: str) -> str:
    line = _BULLET.sub("", line).strip()
    # A trailing block id (`^abc`), then the space before it. `\s*\^…$` would rescan a long run of spaces from
    # every position, so a pasted line full of spaces took seconds (CodeQL py/polynomial-redos).
    line = re.sub(r"\^[a-z0-9-]+$", "", line).rstrip()
    return line.strip(" \t\"“”")


def parse(source: str, text: str) -> list[ImportItem]:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if source in DOCUMENT_SOURCES:
        return _unique(_parse_document(text))
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
            hint = (_heading_text(h.group(1)) if h.group(1) else h.group(2)).strip()
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
    return _unique(items)


def _unique(items: list[ImportItem]) -> list[ImportItem]:
    """The same memory pasted twice is one memory."""
    unique: list[ImportItem] = []
    for it in items:
        if not any(similarity(it.content, u.content) >= 0.95 for u in unique):
            unique.append(it)
    return unique


_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.DOTALL)
# Negated classes that exclude their own delimiters keep these linear on a line full of brackets.
_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\([^()\n]*\)")  # [text](url) → text; an image keeps its alt text
_CHECKBOX = re.compile(r"^\[[ xX]\][ \t]+")
_RULE = re.compile(r"(?:[-*_][ \t]*){3,}")  # a thematic break: ---, * * *
_LABEL_MAX = 80  # "Preferences:" over a list is the list's heading, not a memory
_LINE_MAX = 2000  # a longer "memory" is a pasted blob, not something to remember


def _strip_comments(text: str) -> str:
    """Drop ``<!-- … -->`` (an unclosed one runs to the end), with ``find`` rather than a lazy regex, which
    rescans the rest of the text from every unclosed ``<!--``."""
    out, pos = [], 0
    while (start := text.find("<!--", pos)) != -1:
        out.append(text[pos:start])
        end = text.find("-->", start + 4)
        pos = len(text) if end == -1 else end + 3
    out.append(text[pos:])
    return "".join(out)


def _parse_document(text: str) -> list[ImportItem]:
    """A memory or instructions file: every list item, and every paragraph, is one memory. Headings are kept
    as hints, and so is a short "Label:" line over a list, for that list. Front matter, code blocks, comments,
    tables and Claude Code's ``@file`` imports are skipped."""
    text = _strip_comments(_FRONTMATTER.sub("", text, count=1))
    items: list[ImportItem] = []
    heading: str | None = None
    label: str | None = None  # a "Label:" line; it names the list after it, until a paragraph or heading
    current: list[str] = []  # the lines of the item being read
    bullet = False  # whether that item is a list item
    fence = ""  # the open code fence (``` or ~~~~, …): only the same character, as many or more, closes it

    def flush() -> None:
        nonlocal label
        if not current:
            return
        line = " ".join(" ".join(current).split())
        current.clear()
        if len(line) > _LINE_MAX:
            return
        line = _LINK.sub(r"\1", _CHECKBOX.sub("", line)).replace("**", "")
        if line.endswith((":", "：")) and len(line) <= _LABEL_MAX:
            label = line.rstrip(":：").strip() or None
            return
        if not bullet:
            label = None
        content = _clean(line)
        if len(content) >= 3 and content.lower() not in _NOISE:
            items.append(ImportItem(content=content, hint=label or heading))

    for raw in text.split("\n"):
        stripped = raw.strip()
        if fence:
            run = len(stripped) - len(stripped.lstrip(fence[0]))
            if run >= len(fence) and not stripped[run:].strip():
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            flush()
            fence = stripped[: len(stripped) - len(stripped.lstrip(stripped[0]))]
            continue
        if not stripped or stripped.startswith("|") or re.fullmatch(r"@\S+", stripped) or _RULE.fullmatch(stripped):
            flush()
            continue
        h = _HEADING.match(raw)
        if h and not _BULLET.match(raw):
            flush()
            heading, label = (_heading_text(h.group(1)) if h.group(1) else h.group(2)).strip() or heading, None
            continue
        if _BULLET.match(raw):
            flush()
            bullet = True
            current.append(_BULLET.sub("", raw))
        else:
            if not current:
                bullet = False
            current.append(stripped.lstrip("> ") if stripped.startswith(">") else stripped)
    flush()
    return items


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
