"""Tidy up (product-design.md §17.6): one batch of proposals that keeps memory tidy, filed by the Assistant.

``run(vault, …)`` looks at the folder in chunks and asks the model, per chunk, for:

* **merges** of near-duplicates. Only pairs the similarity code (``textutil.similarity``) found are candidates; the
  model confirms them and writes the merged text. The earlier memory is updated, the other archived.
* **contradictions**: the later memory (by when it was saved) is proposed as an update of the earlier one, so the
  earlier text is the "before"; the later one is then archived as a copy.
* **possibly outdated** memories: an archive that always waits for the person (``hold: review``), whatever the
  Assistant's writing mode.
* **``until`` suggestions** for time-bound memories without one.
* **rewrites** of memories that are too long or only make sense in their original context.

Rules on top of the Assistant's own (``core.py``):

* Private topics and private memories are never looked at, with any model: the Assistant can't write there anyway.
* Changes to memories the person wrote always wait (``hold: protected``), even with the writing mode ``auto``.
  A merge's or contradiction's second step (archiving the copy) waits whenever its first step waits.
* Not twice: every suggestion has a fingerprint (its kind and the ids and text of the memories it is about), kept
  in the proposal's ``idempotency_key`` as ``tidy:<fingerprint>``. Anything the Assistant suggested before —
  rejected, undone, still waiting or expired — isn't suggested again while those memories say the same.
* Cost: a run makes at most ``max_calls`` calls (one per chunk), and stops at the Assistant's monthly limits with a
  message. ``dry_run`` makes no calls and files nothing: it reports what would be sent, the near-duplicate
  candidates and the estimated calls and tokens.
* When the model fails, the run stops and says why; nothing is filed for the parts it didn't answer.
* What the model saw is what is changed: each proposal carries the version and review date of every memory the
  suggestion is about, as sent, and ``proposals.py`` checks them under the lock it writes under. A memory another
  app or the person changed or archived during the call is left alone (``changed`` in the result).
* A pass over the folder can take several runs: the memories a run has looked at are kept per folder in
  ``assistant.json`` (``tidy_done``: a short hash of each memory's id and text), and the next run starts with
  the parts not looked at yet. A memory whose text changed counts as not looked at. Once every part has been
  looked at, the next run starts a new pass.

The schedule (§17.10 question 1) is per computer, in ``assistant.json``: ``tidy_schedule`` (off, daily, weekly,
monthly) and ``tidy_last_run`` per folder. ``Scheduler`` runs inside whichever long-running process serves the panel
(``openmymemo serve``, the Mac app, a plugin's panel); when no panel runs, nothing runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..proposals import PermissionDenied, Proposals, batch_label, is_protected
from ..textutil import grams, normalize, similarity
from ..vault import Conflict, Memory, NotFound, Vault, VaultError, parse_until
from . import settings as settings_mod
from .core import CONNECTOR_ID, Assistant, Data

log = logging.getLogger("openmymemo.assistant")

KINDS = ("merge", "contradiction", "outdated", "until", "rewrite")
PAIR_AT = 0.5  # candidate near-duplicates (§5.5 turns an add into an update at 0.6)
MAX_PAIRS = 300
CHUNK_MEMORIES = 60
CHUNK_CHARS = 10_000
MAX_CALLS = 20  # per run, by default
MAX_TOKENS = 4096  # per answer
OUTPUT_ESTIMATE = 800  # tokens a typical answer takes, for the estimate
CHARS_PER_TOKEN = 3  # rough; Chinese text takes more tokens per character, English fewer
SCHEDULES = {"off": None, "daily": 1, "weekly": 7, "monthly": 30}
CHECK_EVERY = 3600.0  # seconds between the scheduler's checks
FIRST_CHECK = 120.0  # seconds after the panel starts

INSTRUCTIONS = """You help someone keep their memory folder tidy. The folder holds short statements about the \
person that AI assistants read. Look at the memories given and suggest only clear improvements; suggest nothing \
when nothing is clearly wrong. Each memory is one line: [id] (topic; saved YYYY-MM-DD; until …; written by the \
person or by an AI app) text.

- merges: pairs from the "possible duplicates" list that say the same thing. Give both ids and one merged \
statement that keeps every detail of both. Only pairs from that list.
- contradictions: two memories that can't both be true now, e.g. "lives in Shanghai" and "moved to Lisbon in \
2026-09". Give both ids.
- outdated: memories that were probably true when saved but likely aren't any more, judged from today's date, \
e.g. "looking for a job" saved months ago. The person will be asked whether it is still true.
- until: memories about something time-bound (a trip, a project, a temporary situation) that have no until date. \
Give the date after which it should be reviewed, YYYY-MM or YYYY-MM-DD, after today.
- rewrites: memories that are too long or only make sense in their original context ("this project", "the \
above"). Give a single standalone statement that keeps names, numbers and dates exactly. Never add facts.

Write in the language of the memory. Give a short reason for each suggestion, for the person. Each memory in at \
most one suggestion."""

_ID = {"type": "string", "minLength": 1, "maxLength": 64}
_REASON = {"type": "string", "minLength": 1, "maxLength": 300}
_PAIR = {"type": "array", "items": _ID, "minItems": 2, "maxItems": 2}


def _list(props: dict) -> dict:
    return {"type": "array", "maxItems": 100, "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}


SCHEMA = {
    "type": "object",
    "properties": {
        "merges": _list({"ids": _PAIR, "content": {"type": "string", "minLength": 3, "maxLength": 1000},
                         "reason": _REASON}),
        "contradictions": _list({"ids": _PAIR, "reason": _REASON}),
        "outdated": _list({"id": _ID, "reason": _REASON}),
        "until": _list({"id": _ID, "until": {"type": "string", "minLength": 7, "maxLength": 10}, "reason": _REASON}),
        "rewrites": _list({"id": _ID, "content": {"type": "string", "minLength": 3, "maxLength": 1000},
                           "reason": _REASON}),
    },
    "required": ["merges", "contradictions", "outdated", "until", "rewrites"],
    "additionalProperties": False,
}


@dataclass
class Item:
    """One suggestion and what became of it (``filed``: the proposals' results, in order)."""

    kind: str
    memory_ids: tuple[str, ...]
    reason: str
    content: str | None = None
    until: str | None = None
    fingerprint: str = ""
    before: list[str] = field(default_factory=list)  # the memories' text, for showing it
    filed: list[dict] = field(default_factory=list)
    error: str | None = None
    changed: bool = False  # a memory it is about changed while the model looked: nothing (more) was filed

    def to_dict(self) -> dict:
        d = asdict(self)
        d["memory_ids"] = list(self.memory_ids)
        return d


@dataclass
class Result:
    ok: bool
    message: str
    dry_run: bool = False
    batch_id: str | None = None
    memories: int = 0  # looked at (or would be)
    left_out: int = 0  # private, outside the Assistant's read scope or in a topic blocked for it
    chunks: int = 0
    calls: int = 0
    estimate: dict[str, int] = field(default_factory=lambda: {"calls": 0, "tokens": 0})
    candidates: list[list[str]] = field(default_factory=list)  # near-duplicate pairs from the similarity code
    items: list[Item] = field(default_factory=list)
    applied: int = 0
    waiting: int = 0
    seen_before: int = 0  # suggestions skipped because the Assistant made them before
    changed: int = 0  # suggestions not filed because a memory changed while the model looked at it
    remaining: int = 0  # parts of this pass still to look at after this run
    stopped: str | None = None  # unavailable | busy | limit | cap | error

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [i.to_dict() for i in self.items]
        return d


# ---- helpers -------------------------------------------------------------------------------------------------


def fingerprint(kind: str, memories: Sequence[Memory]) -> str:
    """Stable for a suggestion of this kind about these memories as they read now (not the model's wording)."""
    text = kind + "\n" + "\n".join(sorted(f"{m.id}:{normalize(m.content)}" for m in memories))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def suggested_before(vault: Vault) -> set[str]:
    """Fingerprints of every tidy-up suggestion the Assistant filed before, whatever happened to it."""
    out = set()
    for p in Proposals(vault).list(connector_id=CONNECTOR_ID):
        key = p.idempotency_key or ""
        if key.startswith("tidy:"):
            out.add(key.split(":")[1])
    return out


def candidate_pairs(memories: Sequence[Memory]) -> list[tuple[str, str]]:
    """Pairs of memories that look like near-duplicates (``textutil.similarity`` ≥ PAIR_AT), most similar first."""
    gs = [grams(m.content) for m in memories]
    found = []
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if not gs[i] & gs[j]:
                continue
            s = similarity(memories[i].content, memories[j].content)
            if s >= PAIR_AT:
                found.append((s, memories[i].id, memories[j].id))
    found.sort(key=lambda x: -x[0])
    return [(a, b) for _, a, b in found[:MAX_PAIRS]]


def _chunks(memories: Sequence[Memory], pairs: Sequence[tuple[str, str]]) -> list[list[Memory]]:
    """Memories in folder order, packed into chunks; the two memories of a candidate pair go into the same chunk."""
    parent = {m.id: m.id for m in memories}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[root(a)] = root(b)
    groups: dict[str, list[Memory]] = {}
    for m in memories:
        groups.setdefault(root(m.id), []).append(m)
    out: list[list[Memory]] = []
    current: list[Memory] = []
    size = 0
    placed = set()
    for m in memories:
        r = root(m.id)
        if r in placed:
            continue
        placed.add(r)
        group = groups[r]
        gsize = sum(len(x.content) + 60 for x in group)
        if current and (len(current) + len(group) > CHUNK_MEMORIES or size + gsize > CHUNK_CHARS):
            out.append(current)
            current, size = [], 0
        current.extend(group)
        size += gsize
    if current:
        out.append(current)
    return out


def _line(m: Memory) -> str:
    who = "written by the person" if is_protected(m) else "written by an AI app"
    until = f"; until {m.until}" if m.until else ""
    return f"[{m.id}] ({m.topic}; saved {(m.created_at or '')[:10] or 'unknown'}{until}; {who}) {m.content}"


def _blocks(assistant: Assistant, chunk: Sequence[Memory], pairs: Sequence[tuple[str, str]]) -> list[Data]:
    """Today's date, the memories (through ``memory_data``, which leaves out anything that may not be sent, one
    declared block each) and the candidate pairs among the memories that are sent (ids only)."""
    memories = assistant.memory_data(chunk, label="memory", render=_line)
    ids = {mid for b in memories for mid in b.memory_ids}
    inside = [f"{a} ~ {b}" for a, b in pairs if a in ids and b in ids]
    return [Data(date.today().isoformat(), label="today"), *memories,
            Data("\n".join(inside) or "(none)", label="possible duplicates")]


def _estimate(chunks: Sequence[Sequence[Memory]], pairs: Sequence[tuple[str, str]]) -> int:
    total = 0
    for chunk in chunks:
        chars = len(INSTRUCTIONS) + 1500 + sum(len(_line(m)) + 60 for m in chunk) + 30 * len(pairs) // max(len(chunks), 1)
        total += chars // CHARS_PER_TOKEN + OUTPUT_ESTIMATE
    return total


def _later(a: Memory, b: Memory) -> tuple[Memory, Memory]:
    """(earlier, later) by when they were saved; the model's order when that ties."""
    return (b, a) if (b.created_at or "") < (a.created_at or "") else (a, b)


def memory_hash(m: Memory) -> str:
    """A memory as the model saw it, for the pass checkpoint: its id and text."""
    return hashlib.sha256(f"{m.id}:{normalize(m.content)}".encode()).hexdigest()[:12]


def _done(vault: Vault) -> set[str]:
    raw = settings_mod._read_raw()
    done = raw.get("tidy_done") if isinstance(raw.get("tidy_done"), dict) else {}
    rows = done.get(str(vault.root))
    return {x for x in rows if isinstance(x, str)} if isinstance(rows, list) else set()


def _save_done(vault: Vault, add: set[str] = frozenset(), drop: set[str] = frozenset(),
               keep: set[str] | None = None) -> None:
    """Change this folder's checkpoint: add and drop hashes; ``keep`` limits it to hashes of current memories."""
    with settings_mod._lock():
        raw = settings_mod._read_raw()
        done = raw.get("tidy_done") if isinstance(raw.get("tidy_done"), dict) else {}
        rows = done.get(str(vault.root))
        current = {x for x in rows if isinstance(x, str)} if isinstance(rows, list) else set()
        current = (current - drop) | add
        if keep is not None:
            current &= keep
        done[str(vault.root)] = sorted(current)
        raw["tidy_done"] = done
        settings_mod._write(raw)


def batch_id_for(day: date | None = None) -> str:
    return f"tidy-{(day or date.today()).isoformat()}-{secrets.token_hex(2)}"


@contextlib.contextmanager
def _run_lock(vault: Vault) -> Iterator[bool]:
    """One tidy-up per folder at a time on this computer (the panel's button, the CLI and the schedule). Yields
    False when another run holds it. POSIX only; on Windows runs aren't serialised."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield True
        return
    folder = settings_mod._cloud_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(str(vault.root).encode("utf-8")).hexdigest()[:12]
    with open(folder / f".tidy-{name}.lock", "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---- the run -------------------------------------------------------------------------------------------------


def _parse(answer: dict, chunk: Sequence[Memory], pairs: set[frozenset[str]], claimed: set[str]) -> list[Item]:
    """The model's answer as suggestions, keeping only what refers to this chunk's memories and makes sense."""
    by_id = {m.id: m for m in chunk}
    today = date.today()
    out: list[Item] = []

    def free(*ids: str) -> bool:
        return len(set(ids)) == len(ids) and all(i in by_id and i not in claimed for i in ids)

    def take(item: Item, mems: Sequence[Memory]) -> None:
        item.fingerprint = fingerprint(item.kind, mems)
        item.before = [m.content for m in mems]
        claimed.update(item.memory_ids)
        out.append(item)

    for s in answer.get("merges", []):
        a, b = s["ids"]
        if not free(a, b) or frozenset((a, b)) not in pairs:
            continue
        earlier, later = _later(by_id[a], by_id[b])
        content = " ".join(s["content"].split())
        take(Item("merge", (earlier.id, later.id), s["reason"].strip(), content=content), [earlier, later])
    for s in answer.get("contradictions", []):
        a, b = s["ids"]
        if not free(a, b):
            continue
        earlier, later = _later(by_id[a], by_id[b])
        take(Item("contradiction", (earlier.id, later.id), s["reason"].strip(), content=later.content),
             [earlier, later])
    for s in answer.get("outdated", []):
        if free(s["id"]):
            take(Item("outdated", (s["id"],), s["reason"].strip()), [by_id[s["id"]]])
    for s in answer.get("rewrites", []):
        m = by_id.get(s["id"])
        content = " ".join(s["content"].split())
        if m is not None and free(m.id) and similarity(content, m.content) < 0.999:
            take(Item("rewrite", (m.id,), s["reason"].strip(), content=content), [m])
    for s in answer.get("until", []):
        m = by_id.get(s["id"])
        until = s["until"].strip()
        d = parse_until(until)
        if m is not None and free(m.id) and not m.until and d is not None and d > today:
            take(Item("until", (m.id,), s["reason"].strip(), until=until), [m])
    return out


CHANGED = "A memory it is about changed while the Assistant looked at it, so this wasn't suggested."


def _still_valid(assistant: Assistant, ids: Sequence[str], mems: dict[str, Memory]) -> bool:
    """Every memory a suggestion is made from, checked again now (call under the vault lock): still active, exactly
    as it was sent, not private and not in a private topic (a topic may have become private since), inside the
    Assistant's read scope and not in a topic blocked for it. Derived text (a merge, a rewrite) is only filed when
    all of them pass, so it can't carry text from a memory that has since become private or out of scope."""
    vault = assistant.vault
    connector = assistant.connector()
    if connector is None or connector.write == "none":
        return False
    guard = assistant.guard(connector)
    private = vault.private_topics()
    for mid in ids:
        try:
            cur = vault.get(mid)
        except NotFound:
            return False
        seen = mems.get(mid)
        if (seen is None or cur.status != "active" or cur.version != seen.version or cur.until != seen.until
                or cur.content != seen.content or cur.private or cur.topic in private
                or cur.topic in connector.blocked_topics or guard is None or not guard.memory_ok(cur)):
            return False
    return True


def _file(assistant: Assistant, item: Item, batch_id: str, mems: dict[str, Memory]) -> None:
    """File one suggestion as the Assistant's proposals (§17.6 holds on top of §5.5). ``mems`` is the snapshot the
    model was sent. The whole suggestion is one transaction under the vault lock: every memory it is made from is
    checked again first (``_still_valid``), every proposal expects the memories it depends on to be exactly as
    checked, and when the second step of a merge or contradiction can't be filed, the first is taken back."""
    key = f"tidy:{item.fingerprint}"
    vault = assistant.vault

    def seen(*ids: str) -> dict[str, tuple[int, str | None]]:
        return {i: (mems[i].version, mems[i].until) for i in ids}

    def propose(op: str, memory_id: str, *, step: int = 1, hold: str | None = None, reason: str | None = None,
                expected: dict[str, tuple[int, str | None]] | None = None, **kw: Any) -> dict:
        target = mems[memory_id]
        hold = hold or ("protected" if is_protected(target) else None)
        res = assistant.propose(op, memory_id=memory_id, reason=reason or item.reason, hold=hold, batch_id=batch_id,
                                idempotency_key=key if step == 1 else f"{key}:{step}",
                                expected=expected if expected is not None else seen(memory_id), **kw)
        item.filed.append(res)
        return res

    def take_back(res: dict) -> None:
        """Undo (applied) or reject (waiting) a first step whose second step couldn't be filed."""
        pid = res.get("proposal_id")
        if not pid:
            return
        props = Proposals(vault)
        try:
            if res.get("status") == "applied":
                props.undo(pid)
                res["status"] = "undone"
            elif res.get("status") == "pending":
                props.reject(pid)
                res["status"] = "rejected"
        except VaultError:  # changed again since (only possible from this same process): its history has both
            pass

    with vault.lock():
        if not _still_valid(assistant, item.memory_ids, mems):
            item.changed, item.error = True, CHANGED
            return
        try:
            if item.kind in ("merge", "contradiction"):
                keep, drop = item.memory_ids
                skip_update = similarity(item.content or "", mems[keep].content) >= 0.999
                if skip_update:
                    first = {"status": "duplicate"}  # the earlier memory already says it all: only archive the copy
                    after = seen(keep)
                else:  # both memories as the model saw them: the new text is made from the two
                    first = propose("update", keep, content=item.content, expected=seen(keep, drop))
                    after = seen(keep)
                    if first.get("status") == "applied":  # keep exactly as the first step left it
                        now = vault.get(keep)
                        after = {keep: (Proposals(vault).get(first["proposal_id"]).applied_version or now.version,
                                        now.until)}
                if first.get("status") in ("applied", "pending", "duplicate"):
                    waits = first["status"] == "pending"
                    note = "merged into" if item.kind == "merge" else "now said by"
                    try:
                        propose("archive", drop, step=2, hold="review" if waits else None,
                                expected={**after, **seen(drop)},
                                reason=f"{item.reason} (This copy is {note} memory {keep}"
                                       + (", once you accept that change.)" if waits else ".)"))
                    except VaultError:
                        take_back(first)
                        raise
            elif item.kind == "outdated":
                propose("archive", item.memory_ids[0], hold="review")
            elif item.kind == "until":
                m = mems[item.memory_ids[0]]
                propose("update", m.id, content=m.content, until=item.until)
            elif item.kind == "rewrite":
                propose("update", item.memory_ids[0], content=item.content)
        except Conflict:
            item.changed, item.error = True, CHANGED
        except (PermissionDenied, VaultError) as e:
            item.error = str(e)


def run(vault: Vault, *, topic: str | None = None, dry_run: bool = False, max_calls: int = MAX_CALLS,
        assistant: Assistant | None = None) -> Result:
    """One tidy-up. Never raises for model trouble: the result says what happened (``ok``, ``message``,
    ``stopped``)."""
    assistant = assistant or Assistant.from_settings(vault)
    reason = assistant.unavailable_reason("tidy")
    connector = assistant.connector()
    private = vault.private_topics()
    if topic is not None and topic not in {t["slug"] for t in vault.topics()}:
        return Result(False, f"No topic {topic}.", dry_run=dry_run, stopped="unavailable")
    everything = [m for m in vault.memories() if topic is None or m.topic == topic]
    mems = [m for m in everything if not (m.private or m.topic in private)
            and (connector is None or (assistant.may_send(m, private, connector)
                                       and m.topic not in connector.blocked_topics))]
    pairs = candidate_pairs(mems)
    all_chunks = _chunks(mems, pairs)
    # Continue the current pass: the parts with a memory not looked at yet (or changed since); a new pass when none.
    done = _done(vault)
    chunks = [c for c in all_chunks if not all(memory_hash(m) in done for m in c)]
    new_pass = not chunks
    if new_pass:
        chunks = all_chunks
    result = Result(True, "", dry_run=dry_run, memories=len(mems), left_out=len(everything) - len(mems),
                    chunks=len(chunks), candidates=[list(p) for p in pairs],
                    estimate={"calls": min(len(chunks), max_calls), "tokens": _estimate(chunks[:max_calls], pairs)})
    if not mems:
        result.message = "Nothing to tidy."
        return result
    if dry_run:
        more = (f" One run makes at most {max_calls} calls, so it would look at {max_calls} of the {len(chunks)} "
                "parts; the next run continues with the rest." if len(chunks) > max_calls else "")
        if len(chunks) < len(all_chunks):
            more += f" {len(all_chunks) - len(chunks)} parts were looked at already in this pass."
        off = f" {reason} Turn it on in Settings → Assistant to run it." if reason else ""
        result.message = (f"A tidy-up would send {len(mems)} memories in {result.estimate['calls']} calls, about "
                          f"{result.estimate['tokens']:,} tokens, and nothing is filed until it runs.{more}{off}")
        return result
    if reason:
        result.ok, result.stopped = False, "unavailable"
        result.message = f"Tidy up needs the OpenMyMemo Assistant: {reason}"
        return result

    with _run_lock(vault) as got:
        if not got:
            result.ok, result.stopped = False, "busy"
            result.message = "A tidy-up of this folder is already running."
            return result
        _mark_run(vault)
        scope = {memory_hash(m) for m in mems}
        current = {memory_hash(m) for m in vault.memories()}
        # Prune hashes of memories that changed or went; a new pass forgets what this scope looked at before.
        _save_done(vault, drop=scope if new_pass else set(), keep=current)
        _work(vault, assistant, result, chunks, pairs, max_calls)
    return result


def _work(vault: Vault, assistant: Assistant, result: Result, chunks: list[list[Memory]],
          pairs: list[tuple[str, str]], max_calls: int) -> None:
    pair_set = {frozenset(p) for p in pairs}
    seen = suggested_before(vault)
    claimed: set[str] = set()
    found: list[Item] = []
    stop_note = ""
    for i, chunk in enumerate(chunks):
        if result.calls >= max_calls:
            result.stopped = "limit"
            result.remaining = len(chunks) - i
            stop_note = (f"Stopped after {max_calls} calls, the limit for one tidy-up; {len(chunks) - i} of "
                         f"{len(chunks)} parts weren't looked at. The next run continues with them.")
            break
        blocks = _blocks(assistant, chunk, pairs)
        sent_ids = {mid for b in blocks for mid in b.memory_ids}
        shown = [m for m in chunk if m.id in sent_ids]  # suggestions only about what the model was shown
        if not shown:
            _save_done(vault, add={memory_hash(m) for m in chunk})
            continue
        answer = assistant.json_call("tidy", INSTRUCTIONS, blocks, SCHEMA, max_tokens=MAX_TOKENS)
        result.calls += 1
        if answer is None:
            why = assistant.last_error or "The model didn't answer."
            result.stopped = "cap" if "month's limit" in why else "error"
            left = result.remaining = len(chunks) - i
            stop_note = (f"Stopped: {why} {left} of {len(chunks)} parts weren't looked at"
                         + ("; it continues once the limit resets or is raised in Settings → Assistant."
                            if result.stopped == "cap" else "."))
            break
        for item in _parse(answer, shown, pair_set, claimed):
            if item.fingerprint in seen:
                result.seen_before += 1
            else:
                found.append(item)
        _save_done(vault, add={memory_hash(m) for m in chunk})  # looked at, whatever it found
    result.items = found
    if found and assistant.connector() is not None and assistant.connector().write == "none":
        result.message = (f"{len(found)} suggestions; nothing was filed because the Assistant may only read "
                          "(its writing mode is “none”).")
        if stop_note:
            result.message += " " + stop_note
        result.ok = result.stopped is None
        return
    if found:
        result.batch_id = batch_id_for()
        sent = {m.id: m for chunk in chunks for m in chunk}  # the memories as the model saw them
        for item in found:
            _file(assistant, item, result.batch_id, sent)
        result.changed = sum(1 for item in found if item.changed)
    filed = [r for item in found for r in item.filed]
    result.applied = sum(1 for r in filed if r.get("status") == "applied")
    result.waiting = sum(1 for r in filed if r.get("status") == "pending")
    n = result.applied + result.waiting
    if n:
        result.message = (f"{batch_label(result.batch_id or '', n)}: {result.applied} applied, {result.waiting} "
                          "waiting for you.")
    elif result.calls and result.stopped is None:
        result.message = "Everything looks tidy: no suggestions."
    else:
        result.message = "No changes."
    if result.seen_before:
        result.message += f" Skipped {result.seen_before} suggested before."
    if result.changed:
        result.message += (f" Left out {result.changed} about memories that changed while the Assistant looked at "
                           "them.")
    if stop_note:
        result.message += " " + stop_note
    if result.stopped and not n:
        result.message = stop_note + " Nothing was changed."
    result.ok = result.stopped is None


# ---- the schedule (per computer, assistant.json) --------------------------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def schedule(vault: Vault, now: datetime | None = None) -> dict[str, Any]:
    """``{"schedule", "every_days", "last_run", "next_run", "due"}`` for this folder on this computer."""
    raw = settings_mod._read_raw()
    name = raw.get("tidy_schedule") if raw.get("tidy_schedule") in SCHEDULES else "off"
    runs = raw.get("tidy_last_run") if isinstance(raw.get("tidy_last_run"), dict) else {}
    last = _parse_time(runs.get(str(vault.root)))
    days = SCHEDULES[name]
    now = now or datetime.now(UTC)
    next_run = None if days is None else (last + timedelta(days=days) if last else now)
    return {"schedule": name, "every_days": days, "last_run": last.isoformat() if last else None,
            "next_run": next_run.isoformat() if next_run else None,
            "due": next_run is not None and next_run <= now}


def set_schedule(name: str) -> None:
    if name not in SCHEDULES:
        raise settings_mod.SettingsError(f"The schedule must be one of {', '.join(SCHEDULES)}.")
    with settings_mod._lock():
        raw = settings_mod._read_raw()
        raw["tidy_schedule"] = name
        settings_mod._write(raw)


def _mark_run(vault: Vault, now: datetime | None = None) -> None:
    with settings_mod._lock():
        raw = settings_mod._read_raw()
        runs = raw.get("tidy_last_run") if isinstance(raw.get("tidy_last_run"), dict) else {}
        runs[str(vault.root)] = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        raw["tidy_last_run"] = runs
        settings_mod._write(raw)


def run_if_due(vault: Vault, now: datetime | None = None) -> Result | None:
    """Run a tidy-up when the schedule is on, the Assistant is on for tidy-up and the last run is old enough;
    otherwise None."""
    if not schedule(vault, now)["due"]:
        return None
    assistant = Assistant.from_settings(vault)
    if not assistant.available("tidy"):
        return None
    result = run(vault, assistant=assistant)
    if result.stopped:
        log.info("Scheduled tidy-up: %s", result.stopped)
    return result


class Scheduler:
    """Checks every ``interval`` seconds, in a background thread of the panel's process, whether a tidy-up is due."""

    def __init__(self, vault: Vault, interval: float = CHECK_EVERY, first: float = FIRST_CHECK):
        self.vault = vault
        self.interval = interval
        self.first = first
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="openmymemo-tidy", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        wait = self.first
        while not self._stop.wait(wait):
            try:
                run_if_due(self.vault)
            except Exception as e:  # noqa: BLE001 - the panel must keep running
                log.warning("Scheduled tidy-up failed (%s)", type(e).__name__)
            wait = self.interval
