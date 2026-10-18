"""What the Assistant may send: the check every request passes before it leaves the computer (product-design.md
§17.2, §17.3).

A memory may be sent when its topic is inside the ``assistant`` connector's read scope and it isn't private (a
private topic, or marked private), except that a model on this computer may get the private topics the person
opted in. Everything else is *withheld*, and that covers more than the memories in the topic files: archived
memories, earlier versions in ``history.jsonl``, and the text of proposals (pending or decided) that concern a
withheld memory or topic.

``Guard.refusal(blocks)`` refuses a request when any data block

* declares a topic that may not be sent, or a memory id that is unknown or withheld;
* contains withheld text, in any form: each block is normalized (Unicode NFKC, case folded, everything that isn't a
  letter or digit removed, so spacing, punctuation, Markdown, line breaks and full-width forms don't matter) and
  checked for any run of a withheld memory's normalized text of ``WINDOW`` characters (``WINDOW_CJK`` for text that
  is mostly Chinese, Japanese or Korean), so partial quotes are caught too. A withheld memory shorter than the window
  is matched whole.

A false alarm costs nothing but the rule-based fallback, so the check errs on the side of refusing.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from ..connectors import Connector
from ..vault import Memory, Vault

if TYPE_CHECKING:
    from .core import Data
    from .settings import Settings

# Long enough that everyday phrases ("prefers not to") don't match, short enough to catch a few words quoted.
WINDOW = 16  # letters or digits: about three words
WINDOW_CJK = 6  # characters of Chinese, Japanese or Korean text
MIN_WHOLE = 6  # a withheld memory shorter than its window is matched whole from this length (3 for CJK)
MIN_WHOLE_CJK = 3


def normalize(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F)


def _window(norm: str) -> tuple[int, int]:
    cjk = sum(1 for ch in norm if _cjk(ch))
    return (WINDOW_CJK, MIN_WHOLE_CJK) if cjk * 3 >= len(norm) else (WINDOW, MIN_WHOLE)


class Guard:
    """The send rules for one request: built from the vault, the connector and the settings as they are now."""

    def __init__(self, vault: Vault, connector: Connector, settings: Settings):
        self.vault = vault
        self.connector = connector
        self.settings = settings
        self.private = vault.private_topics()
        self.topics = {t["slug"] for t in vault.topics()}

    # ---- the rules -------------------------------------------------------------------------------------------

    def topic_ok(self, topic: str | None) -> bool:
        """Memories in this topic may be sent: in the read scope, and not private unless opted in for a local model."""
        if not topic:
            return False
        c = self.connector
        if not (c.read_topics == "all" or topic in c.read_topics):
            return False
        if topic in self.private:
            return self.settings.local and topic in self.settings.private_topics
        return True

    def memory_ok(self, m: Memory) -> bool:
        if not self.topic_ok(m.topic):
            return False
        if m.private and m.topic not in self.private:  # marked private in an ordinary topic
            return self.settings.local and m.topic in self.settings.private_topics
        return True

    def choice_ok(self, topic: dict) -> bool:
        """A topic the model may be told about and suggest: in the read scope and not private."""
        return not topic["private"] and self.topic_ok(topic["slug"])

    # ---- what is withheld ----------------------------------------------------------------------------------------

    def _all_memories(self) -> dict[str, Memory]:
        out = {m.id: m for m in self.vault.memories("archived")}
        out.update({m.id: m for m in self.vault.memories()})
        return out

    def withheld(self) -> tuple[dict[str, Memory], set[str], list[str]]:
        """All memories by id, the ids that may be sent, and the texts that may not."""
        from ..proposals import Proposals

        memories = self._all_memories()
        ok_ids = {mid for mid, m in memories.items() if self.memory_ok(m)}  # archived ones too: they aren't secret
        texts: list[str] = [m.content for mid, m in memories.items() if mid not in ok_ids]
        for row in self.vault.history(limit=10**9):
            mid = row.get("memory_id")
            if mid not in ok_ids or not self.topic_ok(row.get("topic")):
                texts.append(str(row.get("content") or ""))
        for p in Proposals(self.vault).list():
            topics = [p.topic, p.final_topic, p.before_topic]
            ids = [p.memory_id, p.applied_memory_id]
            if any(t and not self.topic_ok(t) for t in topics) or any(i and i not in ok_ids for i in ids):
                texts.extend(t for t in (p.content, p.final_content, p.before_content) if t)
        # A memory moved out of a private topic was private once; its current wording is the person's to share.
        allowed = {normalize(memories[mid].content) for mid in ok_ids}
        return memories, ok_ids, [t for t in texts if normalize(t) not in allowed]

    # ---- the check ---------------------------------------------------------------------------------------------

    def refusal(self, blocks: Sequence[Data], instructions: str = "") -> str | None:
        """Why this request may not be sent (``topic``, ``memory`` or ``content``), or None. The instructions and the
        blocks' labels are searched as well as their text: everything that goes out."""
        for b in blocks:
            if b.topic is not None and (b.topic not in self.topics or not self.topic_ok(b.topic)):
                return "topic"
        memories, ok_ids, texts = self.withheld()
        for b in blocks:
            if any(mid not in ok_ids for mid in b.memory_ids):
                return "memory"
        everything = "\n".join([instructions, *(f"{b.label}\n{b.text}" for b in blocks)])
        return "content" if contains_any(everything, texts) else None

    def sendable(self, memories: Iterable[Memory]) -> list[Memory]:
        return [m for m in memories if m.status == "active" and self.memory_ok(m)]


def contains_any(text: str, withheld: Iterable[str]) -> bool:
    """Whether ``text`` contains a run of any withheld text (see the module docstring)."""
    hay = normalize(text)
    if not hay:
        return False
    grams: dict[int, set[str]] = {}

    def has(piece: str) -> bool:
        n = len(piece)
        if n not in grams:
            grams[n] = {hay[i:i + n] for i in range(len(hay) - n + 1)}
        return piece in grams[n]

    for t in withheld:
        s = normalize(t)
        if not s:
            continue
        w, minimum = _window(s)
        if len(s) < w:
            if len(s) >= minimum and s in hay:
                return True
            continue
        if any(has(s[i:i + w]) for i in range(len(s) - w + 1)):
            return True
    return False
