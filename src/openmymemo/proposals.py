"""Proposals: how an AI changes the user's memory.

Every AI write is a proposal. What happens to it is decided when it arrives (docs/product-design.md §5.5):

* a connector with ``write: none`` is refused, and so is a topic the user blocked for that connector;
* anything in a private topic (or a memory marked private) waits for the user, whatever else is set;
* ``write: auto`` applies everything else;
* ``write: propose`` with the vault's "require approval" switch on: everything waits;
* ``write: propose`` with the switch off (the default): additions apply, and so do updates and archives
  of memories an AI wrote last; changes to **protected** memories wait. A memory is protected when its
  latest version was written by the person (``last_actor`` is ``user``, ``file`` or ``import:*``).

Why a proposal waits is kept in ``hold``: ``approval``, ``protected``, ``private``, ``sensitive`` (the Assistant's
check found it looks sensitive), or ``review`` when the proposer asked for the person's decision even though the
rules would have applied it (the Assistant's tidy-up does for "possibly outdated" archives, product-design.md §17.6).

With the OpenMyMemo Assistant's ``check`` feature on, an app's proposal gets a second look before it is filed
(``propose(..., check=True)``, ``assistant/checks.py``, product-design.md §17.7): within a short time budget, the
model may turn an ``add`` into an ``update`` of a memory it repeats or contradicts, pick a topic the app didn't give,
or make a sensitive one wait (``hold: sensitive``, or a private topic). It can only make a proposal more cautious,
never apply something the table above says must wait; it is recorded as ``checked_by`` and ``check_note``. When the
check is off, slow or fails, the proposal is filed exactly as without it.

Lifecycle::

    propose ──(applies now)──▶ applied ─┬─keep──▶ applied, reviewed
       │                                ├─edit──▶ edited_accepted ──undo──▶ undone
       │                                └─undo──▶ undone
       ├──(waits: hold)──▶ pending ─┬─accept──▶ accepted | edited_accepted ──undo──▶ undone
       │                            ├─reject──▶ rejected
       │                            └─(30 days)──▶ expired
       └──(already known)──▶ duplicate (nothing is stored)

Applied proposals form the user's digest until they have seen them (``reviewed_at``); keeping, editing
or undoing one marks it seen. Editing an applied change rewrites the memory as the user's, so it becomes
protected. A change can be undone for as long as it is still the memory's latest version
(``applied_version``); after that the memory's version history takes over. Like a rejection, an undo
can also stop the app proposing to that topic (``block_topic``).

A proposal never blocks the AI: ``propose`` returns immediately with the proposal's status.
Before a proposal is stored it is checked against existing memories: an exact duplicate is not stored,
and an ``add`` that closely resembles an existing memory becomes an ``update`` of it — so the user
reviews a diff instead of collecting near-duplicates, and the rules above apply to that memory.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from .connectors import Connector, Connectors
from .textutil import KINDS, classify_topic, looks_contradictory, similarity
from .topics import IMPORTED_GROUP
from .vault import Conflict, Memory, NotFound, Vault, VaultError, _atomic_write, now_iso, parse_until

OPS = ("add", "update", "archive")
HOLDS = ("approval", "protected", "private", "review")  # what a proposer may ask for (`sensitive` is the check's)
UNDOABLE = ("applied", "accepted", "edited_accepted")
PENDING_TTL = timedelta(days=30)
DUPLICATE_AT = 0.92
SIMILAR_AT = 0.6

HOLD_MESSAGES = {
    "protected": "Waiting for the user: they wrote this memory themselves.",
    "private": "Waiting for the user: it is in a private topic.",
    "approval": "Waiting for the user: they review every change first.",
    "sensitive": "Waiting for the user: it looks sensitive (health, money or credentials).",
    "review": "Waiting for the user: this change is only made when they agree.",
}
APPLIED_MESSAGE = "Applied. The user will see it in their OpenMyMemo digest and can undo it."
CHANGED_SINCE = "The memory has changed since this proposal; use its version history instead"


class PermissionDenied(VaultError):
    pass


def batch_label(batch_id: str, n: int) -> str:
    """How a batch is named in the digest, ``openmymemo review`` and log.md: "Tidy-up, 2026-10-25: 6 changes"
    (a tidy-up's id is ``tidy-YYYY-MM-DD-xxxx``)."""
    changes = f"{n} change" + ("" if n == 1 else "s")
    if batch_id.startswith("tidy-") and len(batch_id) >= 15:
        return f"Tidy-up, {batch_id[5:15]}: {changes}"
    return f"{batch_id}: {changes}"


def is_protected(m: Memory) -> bool:
    """True when the memory's latest version was written by the person, so an AI may not change it silently."""
    actor = m.last_actor or "user"
    return actor in ("user", "file") or actor.startswith("import:")


@dataclass
class Proposal:
    id: str
    connector_id: str
    connector_name: str
    op: str
    content: str | None
    topic: str | None
    memory_id: str | None = None
    kind: str | None = None
    reason: str | None = None
    evidence: str | None = None
    idempotency_key: str | None = None
    created_at: str = ""
    queue_id: str | None = None  # filed from MyMemo Cloud's offline queue: its receipt's id
    status: str = "pending"
    hold: str | None = None  # why it waits: approval | protected | private | sensitive (None: applied at once)
    decided_at: str | None = None
    reviewed_at: str | None = None  # when the user saw an applied change; None = still in the digest
    final_content: str | None = None
    final_topic: str | None = None
    applied_memory_id: str | None = None
    applied_version: int | None = None  # the memory's version right after this change; undo needs it current
    undo_until: str | None = None  # format 1 only; read but never set or enforced
    before_content: str | None = None
    before_topic: str | None = None
    original_op: str | None = None
    similarity: float | None = None
    conflict: bool = False
    topic_confidence: float | None = None
    checked_by: str | None = None  # "assistant" when the OpenMyMemo Assistant checked it before it was filed
    check_note: str | None = None  # what that check found or changed, for the person
    until: str | None = None  # a review date this proposal sets (`until` of MEMORY_FORMAT.md §3.5)
    before_until: str | None = None  # the memory's `until` before an update that sets one, for undo
    batch_id: str | None = None  # proposals filed together by one run, e.g. a tidy-up: grouped in the digest

    def to_dict(self) -> dict:
        return asdict(self)


_last_ms = 0


def _new_id() -> str:
    """``p`` + milliseconds in hex + 4 random hex digits: file names sort by creation time."""
    global _last_ms
    _last_ms = max(int(time.time() * 1000), _last_ms + 1)  # strictly increasing within a process
    return f"p{_last_ms:x}{secrets.token_hex(2)}"


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Proposals:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.connectors = Connectors(vault)

    # ---- storage -------------------------------------------------------------------------------

    def _path(self, pid: str):
        if not pid.isalnum():
            raise NotFound(f"No proposal {pid}")
        return self.vault.proposals_dir / f"{pid}.json"

    def _save(self, p: Proposal) -> None:
        _atomic_write(self._path(p.id), json.dumps(p.to_dict(), ensure_ascii=False, indent=1) + "\n")

    def get(self, pid: str) -> Proposal:
        try:
            data = json.loads(self._path(pid).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotFound(f"No proposal {pid}") from None
        p = Proposal(**{k: v for k, v in data.items() if k in Proposal.__dataclass_fields__})
        if "hold" not in data and p.status == "pending":
            p.hold = "approval"  # format 1: every proposal waited for approval
        return p

    def list(self, status: str | None = None, connector_id: str | None = None) -> list[Proposal]:
        """Proposals, newest first."""
        self.vault.proposals_dir.mkdir(parents=True, exist_ok=True)
        out = []
        now = datetime.now(UTC)
        for path in sorted(self.vault.proposals_dir.glob("*.json"), reverse=True):
            try:
                p = self.get(path.stem)
            except (NotFound, json.JSONDecodeError, TypeError):
                continue
            if p.status == "pending" and _parse(p.created_at) and now - _parse(p.created_at) > PENDING_TTL:
                p.status, p.decided_at = "expired", now_iso()
                self._save(p)
            if status and p.status != status:
                continue
            if connector_id and p.connector_id != connector_id:
                continue
            out.append(p)
        return out

    def pending_count(self) -> int:
        return len(self.list("pending"))

    # ---- the digest ----------------------------------------------------------------------------

    def unreviewed(self) -> list[Proposal]:
        """The digest: changes that applied without the user, which they haven't looked at yet (newest first)."""
        return [p for p in self.list("applied") if not p.reviewed_at]

    def unreviewed_count(self) -> int:
        return len(self.unreviewed())

    def mark_reviewed(self, pid: str) -> Proposal:
        with self.vault.lock():
            p = self.get(pid)
            if p.status != "applied":
                raise VaultError(f"Proposal {pid} is {p.status}; only applied changes are reviewed")
            if not p.reviewed_at:
                p.reviewed_at = now_iso()
                self._save(p)
            return p

    def mark_all_reviewed(self) -> int:
        with self.vault.lock():
            todo = self.unreviewed()
            now = now_iso()
            for p in todo:
                p.reviewed_at = now
                self._save(p)
            return len(todo)

    # ---- batches (one run's proposals, e.g. a tidy-up) -----------------------------------------

    def batch(self, batch_id: str) -> list[Proposal]:
        """The proposals filed with this ``batch_id``, newest first."""
        return [p for p in self.list() if p.batch_id == batch_id]

    def keep_batch(self, batch_id: str) -> int:
        """"Keep all": every applied change of the batch still in the digest is marked seen. Returns how many."""
        with self.vault.lock():
            todo = [p for p in self.batch(batch_id) if p.status == "applied" and not p.reviewed_at]
            now = now_iso()
            for p in todo:
                p.reviewed_at = now
                self._save(p)
            return len(todo)

    def undo_batch(self, batch_id: str) -> dict:
        """"Undo all": undoes every change of the batch that applied without the person and is still unreviewed,
        newest first. A change something else has changed since is left alone (its version history takes over)
        and only marked seen. Returns ``{"undone", "kept"}`` counts."""
        undone = kept = 0
        for p in self.batch(batch_id):
            if p.status != "applied" or p.reviewed_at:
                continue
            try:
                self.undo(p.id)
                undone += 1
            except VaultError:
                self.mark_reviewed(p.id)
                kept += 1
        return {"undone": undone, "kept": kept}

    # ---- proposing -----------------------------------------------------------------------------

    def propose(self, connector: Connector, op: str, *, content: str | None = None, memory_id: str | None = None,
                topic: str | None = None, kind: str | None = None, reason: str | None = None,
                evidence: str | None = None, idempotency_key: str | None = None,
                created_at: str | None = None, queue_id: str | None = None, until: str | None = None,
                hold: str | None = None, batch_id: str | None = None,
                expected: Mapping[str, tuple[int, str | None]] | None = None, check: bool = False) -> dict:
        """`created_at`: when the AI proposed it, if that was earlier, and `queue_id`: the receipt it got then
        (both from MyMemo Cloud's offline queue). `until`: a review date to set (``YYYY-MM`` or ``YYYY-MM-DD``).
        `hold`: make it wait for the person with this reason even where the rules would apply it (a reason the
        rules give, such as ``private``, still wins). `batch_id`: groups the proposals of one run.
        `expected`: memory id → (version, until) as the proposer last saw them; checked under the lock this
        proposal is filed and applied under, and Conflict is raised when any of them is no longer active at
        that version and review date, so a suggestion made from older text never lands on newer text.
        `check`: give the OpenMyMemo Assistant a look first, when it is on for that (an app's own
        `memory_propose`; imports and the Assistant's own proposals aren't checked). The check never lifts a
        hold the rules or the proposer set."""
        v = self.vault
        if connector.write == "none":
            raise PermissionDenied("This connector is read-only. The user can allow proposals in the OpenMyMemo panel.")
        if op not in OPS:
            raise VaultError(f"op must be one of {', '.join(OPS)}")
        if kind is not None and kind not in KINDS:
            raise VaultError(f"kind must be one of {', '.join(KINDS)}")
        content = " ".join(content.split()) if content else None
        if op in ("add", "update") and not content:
            raise VaultError(f"content is required for {op}")
        if content and len(content) > 1000:
            raise VaultError("A memory must be a single statement of at most 1000 characters")
        if until is not None and (op == "archive" or parse_until(until) is None):
            raise VaultError("until must be a date, YYYY-MM or YYYY-MM-DD, on an add or update")
        if hold is not None and hold not in HOLDS:
            raise VaultError(f"hold must be one of {', '.join(HOLDS)}")

        verdict = None
        if check and not (idempotency_key and self._known_key(connector.id, idempotency_key)):
            from .assistant.checks import check_proposal  # the Assistant imports this module

            verdict = check_proposal(v, connector, op, content=content, memory_id=memory_id, topic=topic)

        with v.lock():
            if idempotency_key:
                for p in self.list(connector_id=connector.id):
                    if p.idempotency_key == idempotency_key:
                        if queue_id and not p.queue_id:  # the live retry came first: keep the queued receipt too
                            p.queue_id = queue_id
                            self._save(p)
                        return self._result(p, deduplicated=True)

            private = v.private_topics()
            topics = v.topics()
            for mid, (version, until_seen) in (expected or {}).items():
                try:
                    seen = v.get(mid)
                except NotFound:
                    raise Conflict(f"Memory {mid} is gone") from None
                if seen.status != "active" or seen.version != version or seen.until != until_seen:
                    raise Conflict(f"Memory {mid} has changed since this was suggested")
            args = dict(op=op, content=content, memory_id=memory_id, topic=topic, kind=kind, reason=reason,
                        evidence=evidence, idempotency_key=idempotency_key, created_at=created_at,
                        queue_id=queue_id, until=until, batch_id=batch_id)
            # The rules first, exactly as without a check: the baseline the check may only make stricter.
            routed = self._route(connector, private, topics, verdict=None, **args)
            if isinstance(routed, dict):
                v.log_activity(connector.id, "propose", op=op, memory_ids=[routed["memory_id"]], result="duplicate")
                return routed
            p, target = routed
            p.hold = self._hold(connector, p, target, private) or hold
            if verdict is not None:
                p, target = self._checked(connector, p, target, verdict, private, topics, args)
                p.hold = p.hold or hold
            if p.hold is None:
                self._apply(p, actor=f"connector:{connector.id}")
                p.status, p.decided_at = "applied", now_iso()
            self._save(p)
            self._batch_logged(p)
            v.log_activity(connector.id, "propose", op=p.op, proposal_id=p.id,
                           memory_ids=[x for x in [p.memory_id or p.applied_memory_id] if x], result=p.status)
            return self._result(p, private_topics=private)

    def _batch_logged(self, p: Proposal) -> None:
        """log.md groups a batch's changes by the proposal's ``batch_id``, but it was rendered while the change was
        written, before the proposal file existed: render it again."""
        if p.batch_id and p.applied_memory_id:
            self.vault._render_log_md()

    def _checked(self, connector: Connector, base: Proposal, base_target: Memory | None, verdict,
                 private: set[str], topics: list[dict], args: dict) -> tuple[Proposal, Memory | None]:
        """The proposal with the Assistant's check applied, never less restrictive than ``base`` (the rules alone):
        it keeps the base's private topic and its hold, and falls back to the base when the check's version would be
        refused. Recorded on the proposal either way."""
        try:
            routed = self._route(connector, private, topics, verdict=verdict, **args)
        except VaultError:
            routed = None
        if routed is None or isinstance(routed, dict) or (base.topic in private and routed[0].topic not in private):
            self._record_check(connector, base, base_target, verdict, private, topics, apply=False)
            return base, base_target
        p, target = routed
        p.id = base.id
        self._record_check(connector, p, target, verdict, private, topics)
        p.hold = self._hold(connector, p, target, private) or base.hold
        return p, target

    def _route(self, connector: Connector, private: set[str], topics: list[dict], *, verdict, op: str,
               content: str | None, memory_id: str | None, topic: str | None, kind: str | None, reason: str | None,
               evidence: str | None, idempotency_key: str | None, created_at: str | None, queue_id: str | None,
               until: str | None = None, batch_id: str | None = None) -> tuple[Proposal, Memory | None] | dict:
        """Where a proposal goes (its op, topic, target), without saving anything: a Proposal and its target, or a
        ``duplicate`` result. ``verdict``: the Assistant's check, or None for the rules alone. Raises
        PermissionDenied for a blocked topic, VaultError for a bad target."""
        v = self.vault
        slugs = {t["slug"] for t in topics}
        target = None
        if op in ("update", "archive"):
            if not memory_id:
                raise VaultError(f"memory_id is required for {op}")
            try:
                target = v.get(memory_id)
            except NotFound:
                raise VaultError(f"No memory with id {memory_id}") from None
            if target.status != "active" or not connector.can_read_topic(target.topic, private) or (
                    target.private and not connector.include_private):
                raise VaultError(f"No memory with id {memory_id}")

        confidence = None
        if topic and topic not in slugs:
            topic = next((t["slug"] for t in topics if t["title"] == topic), None) or None
        if op == "add" and not topic:
            topic, confidence = classify_topic(content, [t for t in topics if t["group"] != IMPORTED_GROUP])
            topic = topic or ("identity" if "identity" in slugs else next(iter(slugs)))
            # The check may name another topic only in place of an ordinary one the rules picked: never instead of
            # a private one, and never to get around a topic the person blocked for this app.
            if (verdict is not None and verdict.topic in slugs and verdict.topic not in private
                    and verdict.topic not in connector.blocked_topics and topic not in private
                    and topic not in connector.blocked_topics):
                topic, confidence = verdict.topic, None
        if op == "update" and not topic:
            topic = target.topic
        if op == "archive":
            topic = target.topic

        if topic in connector.blocked_topics:
            raise PermissionDenied(f"The user does not accept proposals about '{topic}' from this app.")

        p = Proposal(id=_new_id(), connector_id=connector.id, connector_name=connector.name, op=op,
                     content=content, topic=topic, memory_id=memory_id, kind=kind, reason=reason,
                     evidence=(evidence or None) and evidence[:2000], idempotency_key=idempotency_key,
                     created_at=created_at or now_iso(), queue_id=queue_id, topic_confidence=confidence,
                     until=until, batch_id=batch_id)
        if target:
            p.before_content, p.before_topic = target.content, target.topic
            if until is not None:
                p.before_until = target.until

        if op == "add":
            readable = [m for m in v.memories()
                        if connector.can_read_topic(m.topic, private) and (connector.include_private or not m.private)]
            best, best_sim = None, 0.0
            for m in readable:
                s = similarity(content, m.content)
                if s > best_sim:
                    best, best_sim = m, s
            if best and best_sim >= DUPLICATE_AT:
                return {"status": "duplicate", "memory_id": best.id,
                        "message": "The user already has this memory; nothing to do."}
            if best and best_sim >= SIMILAR_AT:
                # An update of that memory from here on, so its protection applies too.
                target = best
                p.original_op, p.op, p.memory_id = "add", "update", best.id
                p.before_content, p.before_topic = best.content, best.topic
                if until is not None:
                    p.before_until = best.until
                p.topic = best.topic if not topic or confidence is not None else topic
                p.similarity = round(best_sim, 3)
                p.conflict = looks_contradictory(content, best.content)
            elif verdict is not None and verdict.target:
                found = self._readable(connector, verdict.target, private)
                if found is not None and found.topic not in connector.blocked_topics:
                    # The model saw what word overlap missed: an update of that memory, so §5.5 applies to it.
                    target = found
                    p.original_op, p.op, p.memory_id = "add", "update", found.id
                    p.before_content, p.before_topic = found.content, found.topic
                    if until is not None:
                        p.before_until = found.until
                    p.topic = found.topic if p.topic not in private else p.topic
                    p.conflict = bool(verdict.contradicts) or looks_contradictory(content, found.content)
        elif op == "update" and target:
            if (similarity(content, target.content) >= 0.999 and topic == target.topic
                    and (until is None or until == target.until)):
                return {"status": "duplicate", "memory_id": target.id,
                        "message": "The memory already says exactly this; nothing to do."}
            p.conflict = looks_contradictory(content, target.content)
        if target is not None and target.topic in connector.blocked_topics:  # e.g. an add that became an update
            raise PermissionDenied(f"The user does not accept proposals about '{target.topic}' from this app.")
        return p, target

    def _hold(self, connector: Connector, p: Proposal, target: Memory | None, private: set[str]) -> str | None:
        """Why this proposal must wait for the user, or None when it applies now (the table in §5.5)."""
        if p.topic in private or (target is not None and (target.topic in private or target.private)):
            return "private"  # nothing writes into a private topic without the user, not even `auto`
        if p.hold == "sensitive":
            return "sensitive"  # the Assistant's check flagged it: it waits, whatever the writing mode
        if connector.write == "auto":
            return None
        if self.vault.settings()["require_approval"]:
            return "approval"
        if p.op in ("update", "archive") and target is not None and is_protected(target):
            return "protected"
        return None

    def _known_key(self, connector_id: str, key: str) -> bool:
        return any(p.idempotency_key == key for p in self.list(connector_id=connector_id))

    def _readable(self, connector: Connector, memory_id: str, private: set[str]) -> Memory | None:
        try:
            m = self.vault.get(memory_id)
        except NotFound:
            return None
        if m.status != "active" or not connector.can_read_topic(m.topic, private) or (
                m.private and not connector.include_private):
            return None
        return m

    def _record_check(self, connector: Connector, p: Proposal, target: Memory | None, verdict, private: set[str],
                      topics: list[dict], apply: bool = True) -> None:
        """Apply what the Assistant's check may change (only ever more cautious) and record it on the proposal.
        ``apply=False``: the rules' version is kept; only the sensitivity flag (which only adds a wait) applies."""
        from .assistant.checks import describe, private_route

        routed = None
        held = False
        if not apply:
            from dataclasses import replace

            verdict = replace(verdict, topic=None, duplicate_of=None, contradicts=None)
        if not apply and verdict.sensitive and p.topic not in private:
            p.hold = p.hold or "sensitive"
            held = True
        elif verdict.sensitive and p.topic not in private:
            if p.op == "add" and target is None:
                routed = private_route(self.vault, connector, p.content or "", verdict.sensitive)
            if routed:
                p.topic, p.topic_confidence = routed, None  # waits there: hold "private"
            else:
                p.hold = held = "sensitive"
        p.checked_by = "assistant"
        p.check_note = describe(verdict, converted=p.original_op == "add" and p.similarity is None,
                                routed=routed, held=bool(held), topic_titles={t["slug"]: t["title"] for t in topics})

    def _result(self, p: Proposal, deduplicated: bool = False, private_topics: set[str] | None = None) -> dict:
        out = {"proposal_id": p.id, "status": p.status, "op": p.op, "topic": p.topic, "hold": p.hold}
        if p.checked_by:
            out["checked_by"] = p.checked_by
        if p.memory_id or p.applied_memory_id:
            out["memory_id"] = p.memory_id or p.applied_memory_id
        if p.original_op:
            out["note"] = f"Converted to an update of {p.memory_id}, which says something similar."
        if p.status == "pending":
            out["message"] = HOLD_MESSAGES.get(p.hold or "approval", HOLD_MESSAGES["approval"])
            if p.hold == "private":
                private = self.vault.private_topics() if private_topics is None else private_topics
                if p.topic not in private and p.before_topic not in private:
                    out["message"] = "Waiting for the user: the memory is marked private."
        elif p.status == "applied":
            out["message"] = APPLIED_MESSAGE
        else:
            out["message"] = f"This proposal is {p.status.replace('_', ' ')}."
        if deduplicated:
            out["deduplicated"] = True
        return out

    # ---- deciding ------------------------------------------------------------------------------

    def _apply(self, p: Proposal, *, actor: str, content: str | None = None, topic: str | None = None,
               kind: str | None = None, as_new: bool = False) -> None:
        v = self.vault
        content = content or p.content
        topic = topic or p.topic
        op = "add" if as_new else p.op
        if op == "add":
            m = v.add(topic, content, kind=kind or p.kind, source=f"connector:{p.connector_id}", actor=actor,
                      evidence=p.evidence, proposal_id=p.id, until=p.until)
        elif op == "update":
            m = v.update(p.memory_id, content=content, topic=topic, kind=kind or p.kind, until=p.until, actor=actor,
                         proposal_id=p.id)
        elif op == "archive":
            m = v.archive(p.memory_id, actor=actor, proposal_id=p.id)
        else:
            raise VaultError(f"Unknown op {op}")
        p.applied_memory_id, p.applied_version = m.id, m.version
        p.final_content, p.final_topic = content, topic
        if as_new:
            p.op = "add"

    def accept(self, pid: str, *, content: str | None = None, topic: str | None = None, kind: str | None = None,
               as_new: bool = False) -> Proposal:
        with self.vault.lock():
            p = self.get(pid)
            if p.status != "pending":
                raise VaultError(f"Proposal {pid} is {p.status}, not pending")
            edited = bool((content and content != p.content) or (topic and topic != p.topic) or as_new)
            self._apply(p, actor="user", content=content, topic=topic, kind=kind, as_new=as_new)
            p.status = "edited_accepted" if edited else "accepted"
            p.decided_at = now_iso()
            self._save(p)
            self._batch_logged(p)
            return p

    def reject(self, pid: str, *, block_topic: bool = False) -> Proposal:
        with self.vault.lock():
            p = self.get(pid)
            if p.status != "pending":
                raise VaultError(f"Proposal {pid} is {p.status}, not pending")
            p.status, p.decided_at = "rejected", now_iso()
            self._save(p)
            if block_topic and p.topic:
                self.connectors.block_topic(p.connector_id, p.topic)
            return p

    # ---- undo and edit after the fact ----------------------------------------------------------

    def _applied_version(self, p: Proposal, history: list[dict] | None = None) -> int | None:
        if p.applied_version is not None:
            return p.applied_version
        # Format 1 didn't record it: the latest version this proposal wrote, from history.
        rows = history if history is not None else self.vault.history(p.applied_memory_id, limit=1_000_000)
        versions = [int(r.get("version", 0)) for r in rows
                    if r.get("proposal_id") == p.id and r.get("memory_id") == p.applied_memory_id]
        return max(versions) if versions else None

    def can_undo(self, p: Proposal, *, versions: dict[str, int] | None = None,
                 history: list[dict] | None = None, untils: dict[str, str | None] | None = None) -> bool:
        """True while this change is still the memory's latest version. A change that set a review date
        (``until``) also needs that date to be the memory's current one: changing a date makes no new version, so
        undo would otherwise reset a date set since."""
        if p.status not in UNDOABLE or not p.applied_memory_id:
            return False
        sets_until = p.op == "update" and p.until is not None
        if versions is not None and (untils is not None or not sets_until):
            current = versions.get(p.applied_memory_id)
            current_until = (untils or {}).get(p.applied_memory_id)
        else:
            try:
                m = self.vault.get(p.applied_memory_id)
                current, current_until = m.version, m.until
            except NotFound:
                current, current_until = None, None
        if sets_until and current_until != p.until:
            return False
        return current is not None and current == self._applied_version(p, history)

    def as_dict(self, p: Proposal) -> dict:
        """A proposal for the panel API: its stored fields plus whether it can still be undone."""
        return self.as_dicts([p])[0]

    def as_dicts(self, items: list[Proposal]) -> list[dict]:
        if not items:
            return []
        versions = None
        untils = None
        history = None
        if any(p.status in UNDOABLE for p in items):
            mems = self.vault.memories("active") + self.vault.memories("archived")
            versions = {m.id: m.version for m in mems}
            untils = {m.id: m.until for m in mems}
            if any(p.status in UNDOABLE and p.applied_version is None for p in items):
                history = self.vault.history(limit=1_000_000)
        return [{**p.to_dict(), "can_undo": self.can_undo(p, versions=versions, history=history, untils=untils)} for p in items]

    def undo(self, pid: str, *, block_topic: bool = False) -> Proposal:
        """Revert an applied or accepted change; with ``block_topic`` this app may no longer propose to the topic."""
        v = self.vault
        with v.lock():
            p = self.get(pid)
            if p.status not in UNDOABLE:
                raise VaultError(f"Proposal {pid} is {p.status}; only applied or accepted proposals can be undone")
            if not self.can_undo(p):
                raise VaultError(CHANGED_SINCE)
            mid = p.applied_memory_id
            if p.op == "add":
                v.archive(mid, actor="user", proposal_id=p.id)
            elif p.op == "update":
                v.update(mid, content=p.before_content, topic=p.before_topic, actor="user", proposal_id=p.id,
                         until=None if p.until is None else (p.before_until or ""))
            elif p.op == "archive":
                v.restore(mid, actor="user", proposal_id=p.id)
            now = now_iso()
            if p.status == "applied" and not p.reviewed_at:
                p.reviewed_at = now
            p.status, p.decided_at = "undone", now
            self._save(p)
            if block_topic and p.topic:
                try:
                    self.connectors.block_topic(p.connector_id, p.topic)
                except NotFound:
                    pass  # the app was removed since; it can't propose anything anyway
            return p

    def edit_applied(self, pid: str, content: str, topic: str | None = None) -> Proposal:
        """The user rewrites a change that applied without them; the memory becomes theirs (protected)."""
        v = self.vault
        content = " ".join((content or "").split())
        if not content:
            raise VaultError("Memory content is empty")
        if len(content) > 1000:
            raise VaultError("A memory must be a single statement of at most 1000 characters")
        with v.lock():
            p = self.get(pid)
            if p.status not in UNDOABLE:
                raise VaultError(f"Proposal {pid} is {p.status}; only applied or accepted changes can be edited")
            if p.op == "archive":
                raise VaultError("An archived memory has no text to edit; undo the archive instead")
            if not self.can_undo(p):
                raise VaultError("The memory has changed since this proposal; edit the memory itself instead")
            current = v.get(p.applied_memory_id)
            now = now_iso()
            if content != current.content or (topic and topic != current.topic):
                m = v.update(p.applied_memory_id, content=content, topic=topic or None, actor="user", proposal_id=p.id)
                p.final_content, p.final_topic, p.applied_version = m.content, m.topic, m.version
                p.status, p.decided_at = "edited_accepted", now
            p.reviewed_at = p.reviewed_at or now  # unchanged text counts as "keep"
            self._save(p)
            return p
