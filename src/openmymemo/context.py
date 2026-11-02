"""Compile what an AI sees: the memory context served by ``memory_context`` and written to MEMORY.md.

Also renders ``log.md``, the human-readable change log (MEMORY_FORMAT.md §4.2).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import TYPE_CHECKING

import yaml

from .mdfile import FENCE_RE
from .textutil import search_score
from .topics import CONTEXT_PRIORITY, IMPORT_SOURCE_NAMES, IMPORTED_GROUP

if TYPE_CHECKING:
    from .connectors import Connector
    from .vault import Document, Memory, Vault

KIND_ORDER = {"instruction": 0, "preference": 1, "fact": 2, "ongoing": 3, "event": 4}

HEADER = (
    "# About the user (OpenMyMemo)\n\n"
    "The user keeps this memory in a local folder they own and chose to share it with you. "
    "Treat it as true unless the conversation says otherwise. Ids are in brackets.\n"
    "To add, correct or retire a memory, call `memory_propose`; the user sees every change and can undo it.\n"
)


DOC_NOTE = ("Documents are shown whole under `## Title [doc:<slug>]`. To change one, call `memory_propose` with "
            'op "update", its `doc:<slug>` as `memory_id` and the complete new text as `content`.\n')
DOC_MIN_SHOWN = 300  # a document cut shorter than this is only named
HEADING_RE = re.compile(r"^(#{1,6})(?=[ \t]|$)")


def readable_documents(vault: Vault, connector: Connector | None) -> list[Document]:
    """The documents this connector may read (``None`` = default scope: every document that isn't private). A
    document's slug decides access like a topic's (MEMORY_FORMAT.md §3.6)."""
    private = vault.private_topics()
    return [d for d in vault.documents()
            if (connector.can_read_topic(d.slug, private) if connector else not d.private)]


def demote_headings(text: str) -> str:
    """A document's own headings two levels down, so they sit under its ``##`` heading; code is left alone."""
    out, fence = [], None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if m:
            fence = m.group(1) if fence is None else (None if m.group(1) == fence else fence)
        elif fence is None and (h := HEADING_RE.match(line)):
            line = "#" * min(6, len(h.group(1)) + 2) + line[len(h.group(1)):]
        out.append(line)
    return "\n".join(out)


def _document_block(d: Document, room: int | None) -> str | None:
    """A document under its heading, cut at a line to fit ``room`` characters (None: no limit); None when not even
    ``DOC_MIN_SHOWN`` characters of it fit."""
    head = f"## {d.title} [{d.id}]\n\n"
    body = demote_headings(d.content)
    block = f"{head}{body}\n"
    if room is None or len(block) <= room:
        return block
    note = ("\n\n_(Cut here to stay within the budget: {n} more characters. `memory_get` with `"
            + d.id + "` returns the whole document.)_\n")
    fit = room - len(head) - len(note.format(n=len(body)))
    if fit < DOC_MIN_SHOWN:
        return None
    cut = body[:fit]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    cut = cut.rstrip()
    return head + cut + note.format(n=len(body) - len(cut))


def readable(vault: Vault, connector: Connector | None) -> tuple[list[dict], list[Memory]]:
    """Topics and active memories this connector may read (``None`` = default scope: everything non-private)."""
    private_topics = vault.private_topics()
    topics = [t for t in vault.topics()
              if (connector.can_read_topic(t["slug"], private_topics) if connector else not t["private"])]
    allowed = {t["slug"] for t in topics}
    include_private = connector.include_private if connector else False
    mems = [m for m in vault.memories() if m.topic in allowed and (include_private or not m.private)]
    return topics, mems


def _ordered_topics(topics: list[dict]) -> list[dict]:
    prio = {slug: i for i, slug in enumerate(CONTEXT_PRIORITY)}
    return sorted(topics, key=lambda t: (prio.get(t["slug"], len(prio)), topics.index(t)))


def _line(m: Memory) -> str:
    extra = f" (until {m.until})" if m.until else ""
    return f"- {m.content}{extra} [{m.id}]"


def compile_context(vault: Vault, connector: Connector | None = None, *, topics: list[str] | None = None,
                    max_chars: int | None = 8000, include_topic_index: bool = True) -> dict:
    all_topics, mems = readable(vault, connector)
    shown_topics = [t for t in all_topics if not topics or t["slug"] in topics]
    by_topic: dict[str, list[Memory]] = {}
    for m in mems:
        by_topic.setdefault(m.topic, []).append(m)
    docs = [d for d in readable_documents(vault, connector) if d.content and (not topics or d.slug in topics)]

    parts = [HEADER + (DOC_NOTE if docs else "")]
    if include_topic_index:
        blocked = set(connector.blocked_topics) if connector else set()
        idx = [f"- `{t['slug']}` {t['title']}" + (f": {t['description']}" if t.get("description") else "")
               for t in vault.topics() if t["slug"] not in blocked and t["group"] != IMPORTED_GROUP]
        parts.append("Topics to file proposals under (you may not see the memories in every topic):\n" + "\n".join(idx) + "\n")

    used = sum(len(p) + 1 for p in parts)
    shown, omitted = [], 0
    # Documents first (product-design.md §23.3): within a budget, at most half of what's left when there are
    # memories to show too; one that doesn't fit is cut at a line, or named at the end.
    unshown_docs: list[Document] = []
    if docs:
        has_memories = any(by_topic.get(t["slug"]) for t in shown_topics)
        limit = None if not max_chars else used + max(0, max_chars - used) // (2 if has_memories else 1)
        for d in docs:
            block = _document_block(d, None if limit is None else limit - used - 1)
            if block is None:
                unshown_docs.append(d)
                continue
            parts.append(block)
            used += len(block) + 1
            shown.append(d.id)
    for t in _ordered_topics(shown_topics):
        items = by_topic.get(t["slug"], [])
        if not items:
            continue
        items = sorted(items, key=lambda m: (not m.pinned, KIND_ORDER.get(m.kind, 9)))
        block = [f"## {t['title']} ({t['slug']})"]
        section = None
        for m in items:
            if m.section != section and m.section:
                block.append(f"### {m.section}")
            section = m.section
            line = _line(m)
            if max_chars and used + sum(len(b) + 1 for b in block) + len(line) + 1 > max_chars:
                omitted += 1
                continue
            block.append(line)
            shown.append(m.id)
        if len(block) > 1 and any(b.startswith("- ") for b in block):
            text = "\n".join(block) + "\n"
            parts.append(text)
            used += len(text) + 1
    if unshown_docs:
        names = ", ".join(f"{d.title} [{d.id}]" for d in unshown_docs)
        parts.append(f"_Documents not shown to stay within the budget: {names}. `memory_get` returns them._\n")
    if not shown:
        parts.append("_No memories shared yet._\n")
    if omitted:
        parts.append(f"_{omitted} more memories not shown to stay within budget; use `memory_search` to find them._\n")
    text = "\n".join(parts)
    return {"text": text, "memory_ids": shown, "omitted": omitted, "chars": len(text),
            "approx_tokens": approx_tokens(text)}


def approx_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "㐀" <= ch <= "鿿")
    return int(cjk * 0.7 + (len(text) - cjk) / 4) + 1


def render_memory_md(vault: Vault) -> str:
    result = compile_context(vault, None, max_chars=None, include_topic_index=False)
    note = ("<!-- Generated by OpenMyMemo: this is what an AI with the default (non-private) scope sees.\n"
            "     Edits here are overwritten. Edit memories/*.md or use the panel instead. -->\n\n")
    return note + result["text"]


# ---- log.md ----------------------------------------------------------------------------------------

LOG_LIMIT = 500  # newest lines kept in log.md; history.jsonl has everything
LOG_QUOTE_CHARS = 160
LOG_HEADER = ("<!-- Generated by OpenMyMemo from .openmymemo/history.jsonl: who changed what, newest first.\n"
              "     Edits here are overwritten. Programs should read history.jsonl instead of parsing this. -->\n\n"
              "# Change log\n")
_UNDO_STATUSES = ("undone", "applied", "accepted", "edited_accepted")


def _connector_names(vault: Vault) -> dict[str, str]:
    """Every connector's display name, removed ones included (their changes stay in the log)."""
    try:
        data = yaml.safe_load(vault.connectors_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    rows = data.get("connectors") if isinstance(data, dict) else None
    return {str(c["id"]): str(c.get("name") or c["id"]) for c in rows or [] if isinstance(c, dict) and c.get("id")}


def actor_name(actor: str | None, connectors: dict[str, str]) -> str:
    """How log.md names whoever wrote a version: You, an import, or the connector's name."""
    actor = actor or "user"
    if actor == "user":
        return "You"
    if actor == "file":
        return "You (in an editor)"
    if actor.startswith("import:"):
        source = actor[7:]
        return f"Import from {IMPORT_SOURCE_NAMES.get(source, source)}"
    if actor.startswith("connector:"):
        return connectors.get(actor[10:], actor[10:])
    return actor


def _is_undo(rec: dict, p: dict | None) -> bool:
    """True when this history record is the user undoing proposal ``p``.

    Recognised by its shape as well as by ``status: undone``: the record is written, and log.md rendered, just
    before the proposal itself is saved as undone.
    """
    if not p or (rec.get("actor") or "user") != "user" or p.get("status") not in _UNDO_STATUSES:
        return False
    op = rec.get("op")
    if p.get("op") == "add":
        return op == "archive"
    if p.get("op") == "archive":
        return op == "restore"
    if p.get("op") == "update":
        return (op in ("update", "move") and rec.get("content") == p.get("before_content")
                and rec.get("topic") == p.get("before_topic"))
    return False


def render_log_md(vault: Vault) -> str:
    """The change log (MEMORY_FORMAT.md §4.2): ``## YYYY-MM-DD`` in local time, newest day first, one line per
    history record, newest first. The text of private memories is left out, as in MEMORY.md. A batch's changes
    (a tidy-up) are listed under one line per day: "- Tidy-up, 2026-10-25: 6 changes", then indented."""
    from .proposals import batch_label
    from .vault import is_document_id

    titles = {t["slug"]: t["title"] for t in vault.topics()}
    documents = vault.documents() + vault.documents("archived")
    titles.update({d.slug: d.title for d in documents})
    private_topics = vault.private_topics()
    current = {m.id: m for m in vault.memories("active") + vault.memories("archived") + [d.memory for d in documents]}
    connectors = _connector_names(vault)
    proposals: dict[str, dict | None] = {}

    def proposal(pid: str) -> dict | None:
        if pid not in proposals:
            try:
                proposals[pid] = (json.loads((vault.proposals_dir / f"{pid}.json").read_text(encoding="utf-8"))
                                  if pid.isalnum() else None)
            except (OSError, ValueError):
                proposals[pid] = None
        return proposals[pid]

    def title(slug: str | None) -> str:
        return titles.get(slug or "", slug or "?")

    def quote(text: str | None) -> str:
        text = " ".join((text or "").split())
        if len(text) > LOG_QUOTE_CHARS:
            text = text[:LOG_QUOTE_CHARS - 1].rstrip() + "…"
        return f'"{text}"'

    def is_private(mid: str, topic: str | None) -> bool:
        m = current.get(mid)
        return topic in private_topics or (m is not None and (m.private or m.topic in private_topics))

    def line(rec: dict, prev: dict | None) -> str:
        mid = str(rec.get("memory_id", ""))
        op, topic = rec.get("op"), rec.get("topic")
        who = f"**{actor_name(rec.get('actor'), connectors)}**"
        ref = f"(^{mid})"
        pid = rec.get("proposal_id")
        p = proposal(str(pid)) if pid else None
        if is_document_id(mid):  # named, never quoted: a document is a long text (product-design.md §23.5)
            name, ref = f"the document {title(topic)}", f"({mid})"
            if p is not None and _is_undo(rec, p):
                app = p.get("connector_name") or p.get("connector_id") or "an app"
                return f"- {who} undid {app}'s change to {name} {ref}"
            verb = {"create": "added", "archive": "archived", "restore": "restored"}.get(str(op), "edited")
            return f"- {who} {verb} {name} {ref}"
        if p is not None and _is_undo(rec, p):
            app = p.get("connector_name") or p.get("connector_id") or "an app"
            return f"- {who} undid {app}'s change to {title(p.get('final_topic') or p.get('topic') or topic)} {ref}"
        hidden = is_private(mid, topic) or (prev is not None and is_private(mid, prev.get("topic")))
        text = quote(rec.get("content"))
        if op == "create":
            return (f"- {who} added a private memory to {title(topic)} {ref}" if hidden
                    else f"- {who} added to {title(topic)}: {text} {ref}")
        if op == "archive":
            return (f"- {who} archived a private memory from {title(topic)} {ref}" if hidden
                    else f"- {who} archived from {title(topic)}: {text} {ref}")
        if op == "restore":
            return (f"- {who} restored a private memory to {title(topic)} {ref}" if hidden
                    else f"- {who} restored to {title(topic)}: {text} {ref}")
        if op == "move" or (prev is not None and prev.get("content") == rec.get("content")):
            what = "a private memory" if hidden else text
            where = f"from {title(prev.get('topic'))} to {title(topic)}" if prev else f"to {title(topic)}"
            return f"- {who} moved {what} {where} {ref}"
        moved = prev is not None and prev.get("topic") != topic
        where = f"{title(prev.get('topic'))} → {title(topic)}" if prev is not None and moved else title(topic)
        if hidden:
            return f"- {who} edited a private memory in {where} {ref}"
        if prev is None:
            return f"- {who} edited {where}: {text} {ref}"
        return f"- {who} edited {where}: {quote(prev.get('content'))} → {text} {ref}"

    def batch_of(rec: dict) -> str | None:
        """The batch (e.g. a tidy-up) of the proposal this change applied; undoing one isn't part of it."""
        pid = rec.get("proposal_id")
        p = proposal(str(pid)) if pid else None
        if p is None or not p.get("batch_id") or _is_undo(rec, p):
            return None
        return str(p["batch_id"])

    previous: dict[str, dict] = {}
    entries: list[tuple[str, str, str | None]] = []  # (local date, line, batch), oldest first
    for rec in vault.history(limit=sys.maxsize)[::-1]:
        mid = str(rec.get("memory_id", ""))
        try:
            day = datetime.fromisoformat(str(rec.get("at"))).astimezone().date().isoformat()
        except ValueError:
            day = str(rec.get("at") or "")[:10] or "unknown date"
        entries.append((day, line(rec, previous.get(mid)), batch_of(rec)))
        previous[mid] = rec

    out = [LOG_HEADER]
    if not entries:
        out.append("\n_Nothing has changed yet._\n")
    day = None
    shown = entries[-LOG_LIMIT:][::-1]
    grouped: set[tuple[str, str]] = set()
    for d, text, batch in shown:
        if d != day:
            out.append(f"\n## {d}\n")
            day = d
        if batch is None:
            out.append(text + "\n")
        elif (d, batch) not in grouped:  # a batch's changes that day, under one line
            grouped.add((d, batch))
            lines = [t for dd, t, b in shown if dd == d and b == batch]
            out.append(f"- {batch_label(batch, len(lines))}\n")
            out.extend(f"  {t}\n" for t in lines)
    if len(entries) > LOG_LIMIT:
        out.append(f"\n_{len(entries) - LOG_LIMIT} older entries are in .openmymemo/history.jsonl._\n")
    return "".join(out)


SNIPPET_CHARS = 240


def snippet(text: str, query: str) -> str:
    """About ``SNIPPET_CHARS`` of ``text`` around the first word of ``query`` found in it (else its start), on one
    line."""
    flat = " ".join(text.split())
    lower = flat.lower()
    at = min((i for w in query.lower().split() if len(w) > 1 and (i := lower.find(w)) >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    piece = flat[start:start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(flat) else "")


def search_documents(vault: Vault, connector: Connector | None, query: str, *, topics: list[str] | None = None,
                     limit: int = 5) -> list[tuple[float, Document, str]]:
    """Documents this connector may read whose words match ``query``, best first, each with a snippet."""
    out = []
    for d in readable_documents(vault, connector):
        if topics and d.slug not in topics:
            continue
        score = max(search_score(query, d.content), search_score(query, d.title))
        if score >= 0.34:
            out.append((score, d, snippet(d.content, query)))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:limit]


def search(vault: Vault, connector: Connector | None, query: str, *, topics: list[str] | None = None,
           limit: int = 10) -> list[tuple[float, Memory]]:
    topic_rows, mems = readable(vault, connector)
    titles = {t["slug"]: t["title"] for t in topic_rows}
    scored = []
    for m in mems:
        if topics and m.topic not in topics:
            continue
        score = search_score(query, m.content)
        score = max(score, 0.5 * search_score(query, f"{m.topic} {titles.get(m.topic, '')} {m.section or ''}"))
        if score >= 0.34:
            scored.append((score, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]
