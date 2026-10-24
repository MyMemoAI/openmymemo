"""Tell OpenMyMemo (product-design.md §19): the person says what's new, the Assistant turns it into changes.

``preview`` asks the model which memories to add, update or archive, given what the person typed, the topics and up
to ``MAX_RELATED`` memories search finds for it. What may be sent follows §17: memories in private topics, or marked
private, never go to a model outside this computer (``private_skipped`` counts the likely matches left out), and
key and token shapes in the typed text are masked first. The answer is checked: updates and archives may only name
memories that were sent, topics must be ones the model was offered, content must be one memory's worth. Without the
Assistant (off, signed out, no plan, a failure) the text is kept, by rules, as new memories filed by today's
classifier, so nothing typed is lost.

``apply`` saves the changes the person kept, as their own edits (``actor="user"``), each update and archive checked
against the version the preview showed. Nothing is written by ``preview``.
"""

from __future__ import annotations

import logging
import re

from ..textutil import KINDS, classify_topic, search_score
from ..topics import IMPORTED_GROUP
from ..vault import Conflict, NotFound, Vault, VaultError
from .chats import mask_secrets
from .core import Assistant, Data

log = logging.getLogger("openmymemo.assistant")

MAX_TEXT = 4000
MAX_RELATED = 20
PRIVATE_NEAR = 5  # a private memory among this many best matches counts as a likely match left out
MAX_CHANGES = 20
REASON_MAX = 300
PROFILE = ("identity", "lifestyle", "how-to-work-with-me", "people", "work")  # filled in when search finds little
OPS = ("add", "update", "archive")
NO_ASSISTANT = "Turn on the Assistant to also update what you already have."

TELL_INSTRUCTIONS = """Someone tells OpenMyMemo, their memory folder, something about themselves in their own words. \
Turn it into changes to their memories.

- A memory is one durable fact about the person, a preference, or how they want AI assistants to work with them, \
written as a single standalone statement in the language they used. Keep names, numbers and dates exactly as given. \
Don't invent or infer anything.
- Update an existing memory, by its id, when what they said replaces what it says (a move, a new job, a changed \
preference); give its full new text. Archive one only when they say it's no longer true and nothing replaces it. \
Otherwise add a new memory, with the topic it belongs in from the list, by slug, or null when none fits, and its kind.
- Never change a memory they didn't talk about. Leave out passing remarks that aren't worth remembering, and say in \
one short sentence in "skipped" what you left out, or null.
- For each change, give a short reason the person can read."""

TELL_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array", "maxItems": MAX_CHANGES,
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "memory_id": {"type": ["string", "null"], "maxLength": 64},
                    "content": {"type": ["string", "null"], "maxLength": 1000},
                    "topic": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"]},
                    "reason": {"type": "string", "maxLength": REASON_MAX},
                },
                "required": ["op", "memory_id", "content", "topic", "kind", "reason"],
                "additionalProperties": False,
            },
        },
        "skipped": {"type": ["string", "null"], "maxLength": REASON_MAX},
    },
    "required": ["changes", "skipped"],
    "additionalProperties": False,
}


class TellError(ValueError):
    """What the person typed can't be used; the message is for them."""


def _one_line(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _regular_topics(vault: Vault) -> list[dict]:
    return [t for t in vault.topics() if t["group"] != IMPORTED_GROUP] or vault.topics()


def _file(content: str, regular: list[dict]) -> str:
    """The topic today's rules pick for a new memory (the first regular topic when nothing matches)."""
    return classify_topic(content, regular)[0] or regular[0]["slug"]


def preview(vault: Vault, text: str, *, assistant: Assistant | None = None) -> dict:
    """``{"by": "assistant" | "rules", "changes", "skipped", "private_skipped", "note"}``. Each change is
    ``{"op", "memory_id", "content", "topic", "kind", "reason", "current", "version"}``; ``current`` and ``version``
    are the memory as it is now, for an update or archive."""
    text = (text or "").strip()
    if not text:
        raise TellError("Say what's new first.")
    if len(text) > MAX_TEXT:
        raise TellError(f"That's more than {MAX_TEXT:,} characters. Import long text from the Import page.")
    assistant = assistant or Assistant.from_settings(vault)
    reason = assistant.unavailable_reason("tell")
    if reason is None:
        found = _ask(vault, assistant, text)
        if found is not None:
            return found
        reason = assistant.last_error
    return _by_rules(vault, text, NO_ASSISTANT if assistant.connector() is None or not assistant.settings.enabled
                     else reason or NO_ASSISTANT)


def _related(vault: Vault, assistant: Assistant, text: str) -> tuple[list, int]:
    """The memories the model may need, best first, and how many likely matches were left out as private: search's
    hits (by meaning too when an embedder is in use), then any memory sharing words with what was said, then, while
    there's room, the newest memories in the topic today's rules pick and in the profile topics (``PROFILE``), where
    what the person says about themselves usually lives ("I moved" shares no word with "Lives in Lyon")."""
    from ..recall import search

    try:
        hits = [h.memory for h in search(vault, None, text, limit=MAX_RELATED, everything=True).hits]
    except Exception:  # noqa: BLE001 - search is a help here: without it, the model can still add
        log.warning("Tell: search failed", exc_info=True)
        hits = []
    scored = sorted(((search_score(text, m.content), m) for m in vault.memories() if m.status == "active"),
                    key=lambda x: -x[0])
    ranked, seen = [], set()
    regular = _regular_topics(vault)
    guess = classify_topic(text, regular)[0]
    near = sorted((m for m in vault.memories() if m.topic == guess or m.topic in PROFILE),
                  key=lambda m: m.updated_at or "", reverse=True)  # newest first, then by topic (a stable sort)
    near.sort(key=lambda m: (m.topic != guess, PROFILE.index(m.topic) if m.topic in PROFILE else 0))
    matches = [m for m in hits + [m for score, m in scored if score > 0] if m.status == "active"]
    for m in matches + near:
        if m.status == "active" and m.id not in seen:
            seen.add(m.id)
            ranked.append(m)
    sendable = [m for m in ranked if assistant.may_send(m)][:MAX_RELATED]
    best = list(dict.fromkeys(m.id for m in matches))[:PRIVATE_NEAR]  # real matches only, not the profile fill
    by_id = {m.id: m for m in matches}
    held_back = sum(1 for mid in best if not assistant.may_send(by_id[mid]))
    return sendable, held_back


def _ask(vault: Vault, assistant: Assistant, text: str) -> dict | None:
    if not assistant.screen("tell", text):
        return None  # it holds text that may not be sent (a private memory's words): rules
    said = mask_secrets(text)
    related, private_skipped = _related(vault, assistant, said)
    topics = assistant.topic_choices()
    blocks = [Data(said, label="what they said")]
    if topics:
        blocks.append(Data("\n".join(f"- {t['slug']}: {t['title']}" for t in topics), label="topics"))
    blocks += assistant.memory_data(related, label="memory")
    answer = assistant.json_call("tell", TELL_INSTRUCTIONS, blocks, TELL_SCHEMA, max_tokens=4096, task="tell")
    if answer is None:
        return None
    sent = {m.id: m for m in related if assistant.may_send(m)}
    slugs = {t["slug"] for t in topics}
    regular = _regular_topics(vault)
    changes, touched = [], set()
    for c in answer.get("changes") or []:
        op = c.get("op")
        kind = c.get("kind") if c.get("kind") in KINDS else None
        topic = c.get("topic") if c.get("topic") in slugs else None
        content = _one_line(c.get("content"), 1000)
        row = {"op": op, "memory_id": None, "content": None, "topic": None, "kind": kind,
               "reason": _one_line(c.get("reason"), REASON_MAX), "current": None, "version": None}
        if op == "add":
            if len(content) < 3:
                continue
            row.update(content=content, topic=topic or _file(content, regular))
        else:
            m = sent.get(c.get("memory_id") or "")
            if m is None or m.id in touched or (op == "update" and (len(content) < 3 or content == m.content)):
                continue  # only memories that were sent, each once, and an update that changes something
            touched.add(m.id)
            row.update(memory_id=m.id, current=m.content, version=m.version,
                       topic=topic if op == "update" and topic and topic != m.topic else None,
                       content=content if op == "update" else None, kind=kind if op == "update" else None)
        changes.append(row)
    return {"by": "assistant", "changes": changes, "skipped": _one_line(answer.get("skipped"), REASON_MAX) or None,
            "private_skipped": private_skipped, "note": None}


def _by_rules(vault: Vault, text: str, note: str | None) -> dict:
    """What was typed, kept as new memories: one per paragraph (one memory is at most 1,000 characters)."""
    regular = _regular_topics(vault)
    parts = [_one_line(p, 10_000) for p in re.split(r"\n\s*\n", text)]
    changes = []
    for part in parts:
        while part:
            cut = part if len(part) <= 1000 else part[:1000].rsplit(" ", 1)[0] or part[:1000]
            part = part[len(cut):].strip()
            changes.append({"op": "add", "memory_id": None, "content": cut, "kind": None, "reason": "",
                            "topic": _file(cut, regular), "current": None,
                            "version": None})
    return {"by": "rules", "changes": changes, "skipped": None, "private_skipped": 0, "note": note}


def apply(vault: Vault, changes: list[dict]) -> dict:
    """Save the changes the person kept, as their own edits. ``{"added", "updated", "archived", "ids", "errors"}``;
    an update or archive whose memory moved on since the preview is refused (``errors``), the others are saved.
    Every change is saved: a batch bigger than any preview can make (the rules make at most one change per character
    typed) is refused whole, before anything is written."""
    if len(changes) > MAX_TEXT:
        raise TellError("Too many changes at once. Preview again.")
    out: dict = {"added": 0, "updated": 0, "archived": 0, "ids": [], "errors": []}
    for c in changes:
        op = c.get("op")
        try:
            if op == "add":
                m = vault.add(c.get("topic") or "", _one_line(c.get("content"), 1001), kind=c.get("kind") or None,
                              source="user", actor="user")
                out["added"] += 1
            elif op == "update":
                m = vault.update(str(c.get("memory_id") or ""), content=_one_line(c.get("content"), 1001),
                                 topic=c.get("topic") or None, kind=c.get("kind") or None, actor="user",
                                 expected_version=c.get("version"))
                out["updated"] += 1
            elif op == "archive":
                m = vault.archive(str(c.get("memory_id") or ""), actor="user", expected_version=c.get("version"))
                out["archived"] += 1
            else:
                out["errors"].append({"change": c, "error": "Unknown change."})
                continue
        except Conflict:
            out["errors"].append({"change": c, "error": "This memory changed since the preview. Preview again."})
            continue
        except (NotFound, VaultError) as e:
            out["errors"].append({"change": c, "error": str(e)})
            continue
        out["ids"].append(m.id)
    return out
