"""MyMemo Memory sync (product-design.md §20): an optional copy of this folder in the person's MyMemo Memory.

Off until the person turns it on, after its own consent, on a computer linked to OpenMyMemo Cloud (§16); nothing here
runs otherwise. The folder is the source of truth: MyMemo keeps a copy that follows it, and what MyMemo's AI features
and web apps change there comes back here as proposals, filed through ``proposals.py`` and decided by this folder's
rules. The wire format is pinned by ``tests/fixtures/cloud/sync/`` (whose README is the frame-by-frame reference);
the MyMemo service tests against the same files.

* **State** is per computer, never in the folder: ``sync.json`` next to ``cloud.json`` (mode 0600), bound to the
  link's device. It holds the consent, the picked private topics, the push and cursor counters, the version and a
  short hash of each memory last sent (so a change is noticed whatever made it: the panel, an editor, an app), the
  outcomes not yet reported, and counts for the panel. Never memory text.
* **Up** (``sync_push``): every memory outside private topics (and inside the topics the person picked), as its
  state at its version (earlier versions since the last push are not sent: their privacy then is unknown); ``set``
  for a change without a new version (a review date, pinned, kind) and to state the folder's version after MyMemo
  changed something provisionally; a ``tombstone`` when a memory leaves the sync (made private, or moved into a
  private topic that isn't picked). Topic frontmatter, groups and ``require_approval`` when they change. One push in
  flight, about 512 KB at most.
* **Down** (``sync_ops``): MyMemo's AI and web apps' proposals, filed as their ``cloud-*`` connectors with MyMemo's
  proposal id as the idempotency key (``cloud-askmemo``, ``cloud-agent``, a web app's ``cloud-<slug>``,
  ``cloud-mymemo``); the person's own edits on MyMemo's memory page, applied as ``actor: user`` edits after a version
  check, never to a memory that changed here since it went up or that MyMemo isn't sent; and, on a first merge,
  MyMemo's memories, held in memory for the preview and filed as ``cloud-mymemo`` proposals once the person starts
  syncing (a private one as a private add).
* **The session** (``Session``) runs on the bridge's event loop for one connection (``cloud.Bridge``); building a
  push and filing are done on a worker thread, under the vault's lock where they write.

Nothing here logs memory text; the activity log gets one ``sync`` entry per round that moved something, with counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .vault import NotFound, Vault, VaultError, _read_jsonl, now_iso

log = logging.getLogger("openmymemo.sync")

PROTOCOL = 1
FEATURE = "sync"
CONSENT_VERSION = "2026-10-26"
MERGE_CONNECTOR = ("cloud-mymemo", "MyMemo")
PUSH_BYTES = 512 * 1024
PUSH_OPS = 500
FILED_KEEP_DAYS = 30
APPLIED = ("applied", "accepted", "edited_accepted")
TERMINAL = ("rejected", "expired", "undone", "duplicate", "refused")
VERSIONED = ("create", "update", "move", "archive", "restore")


# What the person agrees to (the panel shows the same, web/src/i18n.ts "sync.consent"; product-design.md §20.1).
CONSENT_TEXT = """MyMemo Memory sync keeps a copy of this folder in your MyMemo Memory.
- MyMemo stores your memories outside private topics (and in the private topics you pick), their versions from now on,
  your topics' names and groups, and your approval switch. It never gets memories you marked private, other private
  topics (only their short name), evidence quotes, your apps or the activity log.
- MyMemo's AI (AskMemo, MyMemo's Agent) uses the copy when it answers you, and so do the AI apps on the web you connect
  through OpenMyMemo Cloud while this computer is off. What they change there comes back here as changes you review.
- Your folder wins: when the two disagree, MyMemo takes the folder's version.
- To delete the copy: turn sync off with "Also delete the copy in MyMemo" (`openmymemo cloud sync off --delete-copy`),
  choose the same when you unlink this computer, or delete it on MyMemo's memory page."""


class SyncError(VaultError):
    """Something the person should read (sync can't be turned on here, …)."""


# ---- the per-computer state -----------------------------------------------------------------------------------


def state_path() -> Path:
    from .cloud import cloud_dir

    return cloud_dir() / "sync.json"


@dataclass
class SyncState:
    device_id: str = ""
    vault: str = ""
    enabled: bool = False
    sync_id: str = ""  # a random id per consent: a new consent is never taken for an old one
    consent_version: str | None = None
    consent_at: str | None = None
    picked: list[str] = field(default_factory=list)  # private topics the person chose to sync
    merge: str = "pending"  # pending (MyMemo hasn't answered) | preview | confirmed | done
    seq: int = 0  # the last push made (persisted before it is sent)
    cursor: int = 0  # the last sync_ops seq filed here
    acked_cursor: int = 0  # the last cursor MyMemo acknowledged having received
    pushed: dict[str, dict] = field(default_factory=dict)  # memory id → {"v": version, "h": hash of what was sent}
    # memory id → hash of the memory right after MyMemo's own edit applied here, until a push says the same: MyMemo
    # knows that state, so its next edit on it is no conflict.
    received: dict[str, str] = field(default_factory=dict)
    topics: dict[str, str] = field(default_factory=dict)  # slug → hash of the frontmatter sent
    groups: str | None = None
    settings: str | None = None
    outcomes: list[dict] = field(default_factory=list)  # filed, not yet in an acknowledged push
    filed: dict[str, dict] = field(default_factory=dict)  # MyMemo proposal id → {"local", "status", "at"}
    forgotten: list[str] = field(default_factory=list)  # forgotten in MyMemo: archived here, never sent again
    status: dict = field(default_factory=dict)  # MyMemo's last sync_status: accepted, reason, merge
    preview: dict | None = None  # first-merge counts: cloud, here, same, similar, new
    last_sync_at: str | None = None
    skipped: int = 0  # memories MyMemo didn't store (secrets, its limit)
    off_by: str | None = None  # "mymemo": turned off on MyMemo's memory page
    pending_off: dict | None = None  # {"sync_id", "delete_copy"}: to tell MyMemo at the next connection

    def to_json(self) -> str:
        return json.dumps({"format": 1, **asdict(self)}, ensure_ascii=False, indent=1) + "\n"


def _lock():
    from .cloud import _link_lock

    return _link_lock()


def load_state(device_id: str | None = None) -> SyncState:
    """This computer's sync state; a fresh (off) one when there is none, it is unreadable, or it belongs to another
    link (``device_id`` given and different): a new link is a new consent."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        st = SyncState(**{k: data[k] for k in SyncState.__dataclass_fields__ if k in data})
    except FileNotFoundError:
        return SyncState(device_id=device_id or "")
    except (OSError, ValueError, TypeError) as e:
        log.warning("MyMemo Memory sync: ignoring an unreadable %s (%s)", state_path(), type(e).__name__)
        return SyncState(device_id=device_id or "")
    if device_id is not None and st.device_id != device_id:
        return SyncState(device_id=device_id, pending_off=st.pending_off if st.pending_off else None)
    return st


def save_state(st: SyncState) -> None:
    from .cloud import _write_private

    with _lock():
        _write_private(state_path(), st.to_json())


def update_state(device_id: str, fn: Callable[[SyncState], Any]) -> SyncState:
    """Read, change and write the state under the lock other processes (the CLI) take too."""
    from .cloud import _write_private

    with _lock():
        st = load_state(device_id)
        fn(st)
        _write_private(state_path(), st.to_json())
        return st


def delete_state() -> None:
    with _lock(), contextlib.suppress(FileNotFoundError):
        state_path().unlink()


def wants_sync(link: Any, vault_root: Path | str) -> bool:
    """Whether this computer advertises ``sync`` in ``hello``: on, for this link and folder."""
    if link is None:
        return False
    st = load_state(link.device_id)
    return st.enabled and bool(st.sync_id) and link.serves(Path(vault_root))


# ---- what goes up ---------------------------------------------------------------------------------------------


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _memory_hash(m: Any) -> str:
    return _hash([m.content, m.topic, m.section, m.kind, m.until, bool(m.private), bool(m.pinned), m.status,
                  m.version])


def eligible(m: Any, private_topics: set[str], picked: set[str], forgotten: set[str]) -> bool:
    """Synced: not marked private, not in a private topic the person didn't pick, not forgotten in MyMemo."""
    if m.private or (m.topic in private_topics and m.topic not in picked):
        return False
    return not (m.status == "archived" and m.id in forgotten)


def _topic_entry(t: dict, picked: set[str]) -> dict:
    hidden = t["private"] and t["slug"] not in picked
    return {"slug": t["slug"], "type": "topic", "title": None if hidden else t["title"],
            "description": None if hidden else (t.get("description") or ""), "group": t["group"],
            "private": bool(t["private"]), "tags": []}


def _groups(topics: list[dict]) -> list[dict]:
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for t in topics:
        gid = t["group"]
        if gid == "other":
            continue
        g = by_id.get(gid)
        if g is None:
            g = by_id[gid] = {"id": gid, "name": t.get("group_name") or gid, "topics": []}
            out.append(g)
        g["topics"].append(t["slug"])
    return out


@dataclass
class Commit:
    """What a push changes in the state once MyMemo acknowledges it."""

    seq: int
    pushed: dict[str, dict | None] = field(default_factory=dict)  # None: tombstoned
    topics: dict[str, str | None] = field(default_factory=dict)
    groups: str | None = None
    settings: str | None = None
    outcomes: int = 0  # how many of st.outcomes it carried
    cursor: int = 0
    filed: dict[str, str] = field(default_factory=dict)  # MyMemo proposal id → status reported
    merge_done: bool = False
    counts: dict[str, int] = field(default_factory=dict)


def build_push(vault: Vault, st: SyncState, *, max_bytes: int = PUSH_BYTES,
               max_ops: int = PUSH_OPS) -> tuple[dict | None, Commit | None]:
    """The next ``sync_push`` (and what it changes once acknowledged), or ``(None, None)`` when there is nothing to
    say. ``st.seq`` is not moved here; the caller persists the new seq before sending."""
    vault.sync()
    seq = st.seq + 1
    commit = Commit(seq=seq)
    frame: dict[str, Any] = {"type": "sync_push", "v": PROTOCOL, "seq": seq}
    picked = set(st.picked)
    forgotten = set(st.forgotten)
    topics = vault.topics()
    private = {t["slug"] for t in topics if t["private"]}

    # Settings, groups and the topics that changed.
    settings = {"require_approval": bool(vault.settings()["require_approval"])}
    if _hash(settings) != st.settings:
        frame["settings"], commit.settings = settings, _hash(settings)
    groups = _groups(topics)
    if _hash(groups) != st.groups:
        frame["groups"], commit.groups = groups, _hash(groups)
    changed_topics = []
    for t in topics:
        entry = _topic_entry(t, picked)
        h = _hash(entry)
        if st.topics.get(t["slug"]) != h:
            changed_topics.append(entry)
            commit.topics[t["slug"]] = h
    current = {t["slug"] for t in topics}
    for slug in st.topics:
        if slug not in current:
            changed_topics.append({"slug": slug, "deleted": True})
            commit.topics[slug] = None
    if changed_topics:
        frame["topics"] = changed_topics

    # The memories that changed since they were last sent.
    memories = vault.memories("active") + vault.memories("archived")
    present = {m.id for m in memories}
    todo: list[tuple[str, Any, dict | None]] = []  # (state | tombstone | forgotten, memory, fingerprint)
    for m in memories:
        fp = st.pushed.get(m.id)
        if eligible(m, private, picked, forgotten):
            if fp is not None and fp.get("h") == _memory_hash(m):
                continue
            if fp is None and m.status == "archived":
                continue  # archived before it was ever sent: nothing to say
            todo.append(("state", m, fp))
        elif fp is not None:
            # Forgotten in MyMemo (and archived here): MyMemo has deleted it already, nothing to send.
            todo.append(("forgotten" if m.status == "archived" and m.id in forgotten else "tombstone", m, fp))
    history: dict[str, dict[int, dict]] = {}
    if any(kind == "state" for kind, _m, _fp in todo):
        for row in _read_jsonl(vault.history_path):
            mid, version = row.get("memory_id"), row.get("version")
            if isinstance(mid, str) and isinstance(version, int):
                history.setdefault(mid, {})[version] = row
    reverse = {v.get("local"): k for k, v in st.filed.items() if v.get("local")}

    ops: list[dict] = []
    size = 0
    more = False
    tombstones = 0
    for kind, m, fp in todo:
        if kind == "forgotten":
            commit.pushed[m.id] = None
            continue
        if kind == "tombstone":
            group = [{"memory_id": m.id, "version": m.version, "op": "tombstone", "at": now_iso()}]
        else:
            group = _memory_ops(m, fp, history.get(m.id, {}), reverse)
        n = len(json.dumps(group, ensure_ascii=False).encode())
        if ops and (size + n > max_bytes or len(ops) + len(group) > max_ops):
            more = True
            break
        ops.extend(group)
        size += n
        if kind == "tombstone":
            commit.pushed[m.id] = None
            tombstones += 1
        else:
            commit.pushed[m.id] = {"v": m.version, "h": _memory_hash(m)}
    if not more:
        for mid in [mid for mid in st.pushed if mid not in present]:  # gone from meta.json altogether
            ops.append({"memory_id": mid, "version": int(st.pushed[mid].get("v") or 1), "op": "tombstone",
                        "at": now_iso()})
            commit.pushed[mid] = None
            tombstones += 1
    if ops:
        frame["ops"] = ops

    # What was filed from MyMemo, and what became of earlier proposals since.
    outcomes = list(st.outcomes) + _follow_ups(vault, st, commit)
    if outcomes:
        frame["outcomes"] = outcomes
        commit.outcomes = len(st.outcomes)
    if st.cursor:
        frame["cursor"] = commit.cursor = st.cursor

    if st.merge == "confirmed" and not more:
        after = {**{k: v for k, v in st.pushed.items()}, **commit.pushed}
        frame["merge"] = "done"
        frame["known"] = sorted(mid for mid, fp in after.items() if fp is not None)
        commit.merge_done = True
    commit.counts = {"pushed": len(ops), "tombstones": tombstones}
    if not ({"settings", "groups", "topics", "ops", "outcomes", "merge"} & set(frame)) and st.cursor <= st.acked_cursor:
        if commit.pushed:  # only fingerprints of memories MyMemo forgot: keep them out of the next look
            for mid in commit.pushed:
                st.pushed.pop(mid, None)
        return None, None
    return frame, commit


def _memory_ops(m: Any, fp: dict | None, versions: dict[int, dict], reverse: dict[str, str]) -> list[dict]:
    """A memory as operations: its state at its version. Earlier versions made since the last push are not sent
    (the protocol's ``past``): ``history.jsonl`` doesn't say whether the memory or its topic was private when each
    was written, so one of them may hold text the person kept from MyMemo."""
    def proposal(row: dict | None) -> str | None:
        pid = (row or {}).get("proposal_id")
        return reverse.get(pid, pid) if isinstance(pid, str) else None

    out: list[dict] = []
    sent = int(fp.get("v") or 0) if fp else m.version - 1
    row = versions.get(m.version)
    if fp is not None and m.version <= sent:
        op = "set"  # the same version: attributes only, or the folder restating what it has
    elif row is not None and row.get("op") in VERSIONED:
        op = row["op"]
    else:
        op = "archive" if m.status == "archived" else ("create" if m.version == 1 else "update")
    out.append({"memory_id": m.id, "version": m.version, "op": op, "content": m.content, "topic": m.topic,
                "section": m.section, "kind": m.kind, "until": m.until, "private": bool(m.private),
                "pinned": bool(m.pinned), "status": m.status, "actor": m.last_actor or "user",
                "source": m.source or "user", "at": (row or {}).get("at") or m.updated_at or now_iso(),
                "created_at": m.created_at or None, "proposal_id": proposal(row) if op != "set" else None})
    return out


def _follow_ups(vault: Vault, st: SyncState, commit: Commit) -> list[dict]:
    """Proposals from MyMemo decided here since their outcome went up (the person accepted one that waited, or undid
    one): their new status goes up with ``seq: null``."""
    from .proposals import Proposals

    out = []
    props = Proposals(vault)
    for pid, entry in list(st.filed.items())[:200]:
        last = entry.get("status")
        if last in TERMINAL or not entry.get("local"):
            continue
        try:
            p = props.get(entry["local"])
        except (NotFound, ValueError, TypeError, OSError):
            continue
        if p.status != last:
            out.append({"seq": None, "proposal_id": pid, "status": p.status,
                        "memory_id": p.applied_memory_id or p.memory_id, "hold": p.hold if p.status == "pending"
                        else None, "code": None})
            commit.filed[pid] = p.status
    return out


def apply_ack(st: SyncState, commit: Commit, ack: dict) -> None:
    """MyMemo applied the push ``commit`` describes."""
    for mid, fp in commit.pushed.items():
        if fp is None:
            st.pushed.pop(mid, None)
        else:
            st.pushed[mid] = fp
        if fp is None or st.received.get(mid) == fp.get("h"):
            st.received.pop(mid, None)
    for slug, h in commit.topics.items():
        if h is None:
            st.topics.pop(slug, None)
        else:
            st.topics[slug] = h
    if commit.groups is not None:
        st.groups = commit.groups
    if commit.settings is not None:
        st.settings = commit.settings
    st.outcomes = st.outcomes[commit.outcomes:]
    for pid, status in commit.filed.items():
        if pid in st.filed:
            st.filed[pid]["status"] = status
    _prune_filed(st)
    if commit.merge_done:
        st.merge = "done"
    st.acked_cursor = max(st.acked_cursor, commit.cursor)
    skipped = ack.get("skipped") if isinstance(ack.get("skipped"), list) else []
    st.skipped = len(skipped) if commit.merge_done else st.skipped + len(skipped)
    st.last_sync_at = now_iso()
    st.forgotten = [mid for mid in st.forgotten if mid not in commit.pushed or commit.pushed[mid] is not None]


def _prune_filed(st: SyncState) -> None:
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=FILED_KEEP_DAYS)).isoformat(timespec="seconds")
    st.filed = {pid: e for pid, e in st.filed.items()
                if e.get("status") not in TERMINAL and (e.get("status") == "pending" or (e.get("at") or "") >= cutoff)}


# ---- what comes down ------------------------------------------------------------------------------------------


def _outcome_of(result: dict) -> tuple[str, str | None, str | None]:
    """(status, memory_id, hold) of a ``Proposals.propose`` answer."""
    status = result.get("status") or "refused"
    return status, result.get("memory_id"), result.get("hold") if status == "pending" else None


def _code(e: Exception) -> str:
    from .connectors import Revoked
    from .proposals import PermissionDenied

    if isinstance(e, Revoked):
        return "revoked"
    if isinstance(e, PermissionDenied):
        return "read_only" if "read-only" in str(e) else "topic_blocked"
    if isinstance(e, NotFound):
        return "not_found"
    return "invalid"


def _connector(vault: Vault, cid: str, name: str):
    from .connectors import Connectors, Revoked

    if not isinstance(cid, str) or not cid.startswith("cloud-") or len(cid) > 64:
        raise VaultError("Not a MyMemo connector")
    c = Connectors(vault).ensure(cid, " ".join(str(name or cid).split())[:60], kind="cloud")
    if c.kind != "cloud":  # an app of the person's own with this id: MyMemo never acts as it
        raise Revoked("The person revoked this app's access in OpenMyMemo.")
    return c


def file_ops(vault: Vault, st: SyncState, ops: list[dict]) -> tuple[list[dict], set[str]]:
    """File MyMemo's ops after ``st.cursor``, in order; returns their outcomes and the memories whose state MyMemo
    should be told again (``set``) because what it has may differ from the folder now."""
    outcomes: list[dict] = []
    touched: set[str] = set()
    for op in ops:
        seq = op.get("seq")
        if not isinstance(seq, int) or seq <= st.cursor:
            continue
        try:
            outcome = _file_one(vault, st, op, touched)
        except VaultError as e:
            outcome = {"seq": seq, "status": "refused", "code": _code(e)}
            for key in ("proposal_id", "edit_id"):
                if isinstance(op.get(key), str):
                    outcome[key] = op[key]
            if op.get("from") == "merge":
                outcome["merge_id"] = op.get("memory_id")
        outcome.setdefault("memory_id", None)
        outcome.setdefault("code", None)
        outcomes.append(outcome)
        st.cursor = seq
    return outcomes, touched


def _file_one(vault: Vault, st: SyncState, op: dict, touched: set[str]) -> dict:
    from .proposals import Proposals

    seq, kind = op["seq"], op.get("from")
    if kind in ("app", "merge"):
        if kind == "merge":
            mem = op.get("memory") or {}
            connector = _connector(vault, *MERGE_CONNECTOR)
            args = dict(content=mem.get("content"), topic=mem.get("topic"), kind=mem.get("kind"),
                        until=mem.get("until") or None, idempotency_key=f"merge:{op.get('memory_id')}",
                        new_memory_id=op.get("memory_id"), created_at=op.get("created_at"),
                        private=bool(mem.get("private")))
            name, key = "add", {"merge_id": op.get("memory_id")}
        else:
            c = op.get("connector") or {}
            connector = _connector(vault, c.get("id"), c.get("name"))
            args = dict(content=op.get("content"), memory_id=op.get("memory_id"), topic=op.get("topic"),
                        kind=op.get("kind"), reason=op.get("reason"), evidence=op.get("evidence"),
                        until=op.get("until") or None, idempotency_key=op.get("proposal_id"),
                        created_at=op.get("created_at"), new_memory_id=op.get("new_memory_id"),
                        hold="review" if op.get("hold") == "review" else None, check=True)
            name, key = op.get("op"), {"proposal_id": op.get("proposal_id")}
        result = Proposals(vault).propose(connector, name, **args)
        status, memory_id, hold = _outcome_of(result)
        if isinstance(memory_id, str) and name != "add":
            touched.add(memory_id)
        if kind == "app" and result.get("proposal_id") and status not in TERMINAL:
            st.filed[op["proposal_id"]] = {"local": result["proposal_id"], "status": status, "at": now_iso()}
        return {"seq": seq, **key, "status": status, "memory_id": memory_id, "hold": hold}
    if kind != "user":
        raise VaultError("Unknown op")
    edit = {"seq": seq, "edit_id": op.get("edit_id")}
    what = op.get("op")
    if what == "put":
        status, memory_id = _put(vault, st, op, touched)
        return {**edit, "status": status, "memory_id": memory_id}
    if what == "forget":
        mid = op.get("memory_id")
        try:
            m = vault.get(mid)
        except NotFound:
            return {**edit, "status": "missing", "memory_id": mid}
        if m.status == "active":
            vault.archive(mid, actor="user")
        if mid not in st.forgotten:
            st.forgotten.append(mid)
        return {**edit, "status": "applied", "memory_id": mid}
    if what == "topic":
        t = op.get("topic") or {}
        slug = t.get("slug")
        st.topics.pop(slug, None)  # say what the folder has, whatever happens
        if t.get("deleted"):
            return {**edit, "status": "refused", "code": "unsupported"}
        try:
            vault.topic(slug)
            vault.update_topic(slug, title=t.get("title") or None, description=t.get("description"),
                               private=bool(t.get("private")))
        except NotFound:
            vault.add_topic(slug, t.get("title") or slug, t.get("description") or None, group=t.get("group"),
                            private=bool(t.get("private")))
        return {**edit, "status": "applied"}
    if what == "settings":
        s = op.get("settings") or {}
        st.settings = None
        if isinstance(s.get("require_approval"), bool):
            vault.update_settings(require_approval=s["require_approval"])
        return {**edit, "status": "applied"}
    if what == "decide":
        return _decide(vault, st, op, edit, touched)
    raise VaultError("Unknown edit")


def _same(m: Any, want: dict) -> bool:
    return (m.content == want.get("content") and m.topic == want.get("topic") and m.section == want.get("section")
            and m.kind == want.get("kind") and m.until == want.get("until") and bool(m.private) == bool(
                want.get("private")) and bool(m.pinned) == bool(want.get("pinned"))
            and m.status == want.get("status", "active"))


def _put(vault: Vault, st: SyncState, op: dict, touched: set[str]) -> tuple[str, str | None]:
    """The person's own edit on MyMemo's memory page, as their own edit here: applied when the memory is still at
    the version the edit was made on and, once it has gone up, unchanged here since in what a version doesn't count
    (private, pinned, kind…): it is still as last pushed, or as MyMemo's own last edit left it (``received``), so
    edits made one after another on MyMemo all apply; else a conflict and the folder's memory stays as it is. A
    memory MyMemo isn't sent (marked private, in a private topic not picked) is never changed from there."""
    mid, base, want = op.get("memory_id"), op.get("base_version"), op.get("memory") or {}
    with vault.lock():
        try:
            m = vault.get(mid)
        except NotFound:
            m = None
        if m is None:
            if base is not None:
                return "missing", mid
            if want.get("status", "active") != "active":
                return "refused", mid
            vault.add(want.get("topic"), want.get("content") or "", kind=want.get("kind"),
                      section=want.get("section"), source="user", actor="user", private=bool(want.get("private")),
                      until=want.get("until") or None, pinned=bool(want.get("pinned")), memory_id=mid)
            return "applied", mid
        touched.add(m.id)
        fp = st.pushed.get(mid)
        if _same(m, want):
            if fp is not None:
                st.received[mid] = _memory_hash(m)
            return "applied", mid
        if base is None or m.version != base:
            return "conflict", mid
        if not eligible(m, vault.private_topics(), set(st.picked), set(st.forgotten)):
            return "conflict", mid  # made private here (no new version): MyMemo's edit never undoes that
        if fp is not None and _memory_hash(m) not in (fp.get("h"), st.received.get(mid)):
            # Changed here since it went up (or since MyMemo's last edit applied), if only in what a version doesn't
            # count.
            return "conflict", mid
        if want.get("status") == "archived":
            if m.status == "active":
                vault.archive(mid, actor="user", expected_version=base)
        else:
            if m.status == "archived":
                vault.restore(mid, actor="user")
            vault.update(mid, content=want.get("content"), topic=want.get("topic"), section=want.get("section"),
                         kind=want.get("kind"), pinned=bool(want.get("pinned")), private=bool(want.get("private")),
                         until=want.get("until") or "", actor="user")
        if fp is not None:
            st.received[mid] = _memory_hash(vault.get(mid))
        return "applied", mid


def _decide(vault: Vault, st: SyncState, op: dict, edit: dict, touched: set[str]) -> dict:
    from .proposals import Proposals

    pid = op.get("proposal_id")
    props = Proposals(vault)
    local = (st.filed.get(pid) or {}).get("local")
    if local is None:
        local = next((p.id for p in props.list() if p.idempotency_key == pid and p.connector_id.startswith("cloud-")),
                     None)
    out = {**edit, "proposal_id": pid}
    if local is None:
        return {**out, "status": "refused", "code": "not_found"}
    decision = op.get("decision")
    code = None
    try:
        if decision == "accept":
            props.accept(local, content=op.get("content") or None, topic=op.get("topic") or None)
        elif decision == "reject":
            props.reject(local)
        elif decision == "undo":
            props.undo(local)
        else:
            code = "invalid"
    except VaultError:
        code = "not_" + ("pending" if decision != "undo" else "undoable")
    p = props.get(local)
    for mid in (p.applied_memory_id, p.memory_id):
        if mid:
            touched.add(mid)
    if pid in st.filed:
        st.filed[pid]["status"] = p.status
    return {**out, "status": p.status, "memory_id": p.applied_memory_id,
            "hold": p.hold if p.status == "pending" else None, "code": code}


def merge_preview(vault: Vault, st: SyncState, ops: list[dict]) -> dict:
    """Counts for the first merge's preview: MyMemo's memories (already here, similar, new) and this folder's."""
    from .textutil import similarity

    vault.sync()
    mine = vault.memories("active")
    private = vault.private_topics()
    picked, forgotten = set(st.picked), set(st.forgotten)
    here = sum(1 for m in mine if eligible(m, private, picked, forgotten))
    same = similar = new = 0
    for op in ops:
        if op.get("from") != "merge":
            continue
        text = str((op.get("memory") or {}).get("content") or "")
        best = max((similarity(text, m.content) for m in mine), default=0.0)
        if best >= 0.92 or any(m.id == op.get("memory_id") for m in mine):
            same += 1
        elif best >= 0.6:
            similar += 1
        else:
            new += 1
    return {"cloud": same + similar + new, "here": here, "same": same, "similar": similar, "new": new}


# ---- the session on one bridge connection ---------------------------------------------------------------------


class Session:
    """MyMemo Memory sync on one bridge connection: ``sync_hello``, then pushes and filing until the connection ends
    or sync is turned off. ``send`` writes a frame on the connection. ``off_only``: just tell MyMemo that sync was
    turned off here (``st.pending_off``) and stop."""

    TICK = 2.0  # how often the folder is looked at for changes while connected
    ACK_WAIT = 60.0  # a push not acknowledged by then is sent again (same seq; MyMemo applies it once)

    def __init__(self, vault: Vault, link: Any, send: Callable[[dict], Awaitable[None]], *, off_only: bool = False,
                 tick: float | None = None):
        self.vault = vault
        self.link = link
        self.send = send
        self.off_only = off_only
        self.tick = self.TICK if tick is None else tick
        self.frames: asyncio.Queue[dict] = asyncio.Queue()
        self.sync_id: str | None = None
        self.accepted = False
        self.held: dict[int, dict] = {}  # ops received during a first merge's preview (memory only)
        self.held_complete = False
        self.pending: tuple[Commit, dict] | None = None
        self.sent_at = 0.0
        self.received = 0  # ops filed since the last activity entry
        self.done = asyncio.Event()

    def feed(self, frame: dict) -> None:
        self.frames.put_nowait(frame)

    def poke(self) -> None:
        """Look at the folder (and the state) now instead of at the next tick."""
        self.frames.put_nowait({"type": "_poke"})

    def _state(self) -> SyncState:
        return load_state(self.link.device_id)

    def _update(self, fn: Callable[[SyncState], Any]) -> SyncState:
        return update_state(self.link.device_id, fn)

    async def run(self) -> None:
        try:
            st = self._state()
            if self.off_only or st.pending_off:
                off = st.pending_off or {}
                if off.get("sync_id"):
                    await self.send({"type": "sync_off", "v": PROTOCOL, "sync_id": off["sync_id"],
                                     "delete_copy": bool(off.get("delete_copy"))})
                    await self._await_off()
                if self.off_only or not st.enabled:
                    return
                st = self._state()
            if not st.enabled or not st.sync_id:
                return
            self.sync_id = st.sync_id
            await self.send({"type": "sync_hello", "v": PROTOCOL, "sync_id": st.sync_id})
            while not self.done.is_set():
                try:
                    frame = await asyncio.wait_for(self.frames.get(), timeout=self.tick)
                except TimeoutError:
                    frame = None
                if frame is not None and frame.get("type") != "_poke":
                    await self._on_frame(frame)
                if not self.done.is_set():
                    await self._step()
        finally:
            self.done.set()

    async def _await_off(self) -> None:
        with contextlib.suppress(TimeoutError):
            while True:
                frame = await asyncio.wait_for(self.frames.get(), timeout=15)
                if frame.get("type") == "sync_status" and frame.get("reason") == "off":
                    def clear(st: SyncState) -> None:
                        st.pending_off = None
                    await asyncio.to_thread(self._update, clear)
                    return

    # -- frames from MyMemo --

    async def _on_frame(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "sync_status":
            await self._on_status(frame)
        elif kind == "sync_ops" and self.accepted:
            await self._on_ops(frame)
        elif kind == "sync_ack" and self.pending is not None and frame.get("seq") == self.pending[0].seq:
            commit, _ = self.pending
            self.pending = None

            def acked(st: SyncState) -> None:
                apply_ack(st, commit, frame)
            await asyncio.to_thread(self._update, acked)
            pushed, tombstones = commit.counts.get("pushed", 0), commit.counts.get("tombstones", 0)
            if pushed or tombstones or self.received:
                from .clients import CLOUD

                received, self.received = self.received, 0
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self.vault.log_activity, CLOUD, "sync", pushed=pushed, received=received,
                                            tombstones=tombstones, result="ok")

    async def _on_status(self, frame: dict) -> None:
        accepted = frame.get("accepted") is True
        reason = frame.get("reason")
        cloud = (frame.get("cloud") or {}).get("memories") if isinstance(frame.get("cloud"), dict) else None

        def take(st: SyncState) -> None:
            st.status = {"accepted": accepted, "reason": reason, "merge": frame.get("merge"),
                         "seq": frame.get("seq"), "cursor": frame.get("cursor"), "at": now_iso()}
            if isinstance(frame.get("seq"), int):
                st.seq = max(st.seq, frame["seq"])
            if isinstance(frame.get("cursor"), int):
                st.acked_cursor = max(st.acked_cursor, frame["cursor"])
            if not accepted and reason == "off":
                st.enabled, st.off_by, st.pending_off = False, "mymemo", None
                return
            if accepted and frame.get("merge") == "needed" and st.merge not in ("preview", "confirmed"):
                # A first merge (again): everything goes up afresh once the person starts it.
                st.merge, st.preview = "preview", None
                st.pushed, st.topics, st.groups, st.settings = {}, {}, None, None
                st.outcomes, st.skipped = [], 0
            elif accepted and frame.get("merge") == "done" and st.merge in ("pending", "preview"):
                st.merge = "done"
        st = await asyncio.to_thread(self._update, take)
        self.accepted = accepted
        if not accepted:
            # Nothing goes up; the session stays to hear MyMemo out (sync turned off on its memory page).
            log.info("MyMemo Memory sync: refused (%s)", reason)
            return
        if frame.get("merge") == "needed" and cloud == 0:
            self.held_complete = True
        log.info("MyMemo Memory sync: accepted (merge %s)", st.merge)

    async def _on_ops(self, frame: dict) -> None:
        ops = [op for op in frame.get("ops") or [] if isinstance(op, dict) and isinstance(op.get("seq"), int)]
        st = self._state()
        if st.merge in ("pending", "preview"):
            for op in ops:
                if op["seq"] > st.cursor:
                    self.held[op["seq"]] = op
            if not frame.get("more"):
                self.held_complete = True
            return
        await self._file(ops, frame.get("cursor"))

    async def _file(self, ops: list[dict], cursor: Any) -> None:
        def run(st: SyncState) -> None:
            outcomes, touched = file_ops(self.vault, st, ops)
            st.outcomes.extend(outcomes)
            if isinstance(cursor, int) and cursor > st.cursor:
                st.cursor = cursor  # rows MyMemo skipped (gone since) are acknowledged too
            for mid in touched:  # MyMemo may have changed them provisionally: say what the folder has
                if mid in st.pushed:
                    st.pushed[mid] = {**st.pushed[mid], "h": ""}
            self.received += len(outcomes)
        await asyncio.to_thread(self._update, run)

    # -- the folder going up --

    async def _step(self) -> None:
        st = self._state()
        if not st.enabled or st.sync_id != self.sync_id:
            self.done.set()  # turned off or on again here: the bridge says hello again
            return
        if not self.accepted:
            return
        if st.merge == "preview":
            if self.held_complete and st.preview is None:
                held = [self.held[k] for k in sorted(self.held)]
                counts = await asyncio.to_thread(merge_preview, self.vault, st, held)

                def keep(s: SyncState) -> None:
                    s.preview = counts
                await asyncio.to_thread(self._update, keep)
            return
        if st.merge == "confirmed" and self.held:
            held = [self.held[k] for k in sorted(self.held)]
            self.held.clear()
            await self._file(held, max(op["seq"] for op in held))
            st = self._state()
        if st.merge == "pending":
            return
        if self.pending is not None:
            if time.monotonic() - self.sent_at >= self.ACK_WAIT:
                self.sent_at = time.monotonic()
                await self.send(self.pending[1])
            return
        frame, commit = await asyncio.to_thread(build_push, self.vault, st)
        if frame is None or commit is None:
            return

        def bump(s: SyncState) -> None:
            s.seq = max(s.seq, commit.seq)
        await asyncio.to_thread(self._update, bump)
        self.pending = (commit, frame)
        self.sent_at = time.monotonic()
        await self.send(frame)


# ---- turning it on and off (panel, CLI) ---------------------------------------------------------------------------


def _linked(vault: Vault):
    from .cloud import load_link

    link = load_link()
    if link is None or not link.serves(vault.root):
        raise SyncError("Link this computer to OpenMyMemo Cloud for this folder first (Connectors → OpenMyMemo Cloud).")
    return link


def _valid_picks(vault: Vault, picked: list[str] | None) -> list[str]:
    private = vault.private_topics()
    unknown = sorted(set(picked or []) - private)
    if unknown:
        raise SyncError(f"Not a private topic here: {', '.join(unknown)}")
    return sorted(set(picked or []))


def turn_on(vault: Vault, picked: list[str] | None = None, *, merge_confirmed: bool = False) -> SyncState:
    """The person agreed (consent version ``CONSENT_VERSION``): a new ``sync_id``, so MyMemo starts a first merge. With
    ``merge_confirmed`` (the CLI's ``--yes``), the merge doesn't wait for the preview."""
    link = _linked(vault)
    picks = _valid_picks(vault, picked)

    def on(st: SyncState) -> None:
        fresh = SyncState(device_id=link.device_id, vault=str(vault.root), enabled=True,
                          sync_id=secrets.token_hex(16), consent_version=CONSENT_VERSION, consent_at=now_iso(),
                          picked=picks, merge="confirmed" if merge_confirmed else "pending",
                          seq=st.seq, pending_off=st.pending_off)
        st.__dict__.update(fresh.__dict__)
    return update_state(link.device_id, on)


def turn_off(vault: Vault | None = None, *, delete_copy: bool = False) -> SyncState:
    """Sync off here; MyMemo is told (and deletes its copy with ``delete_copy``) now if connected, else at the next
    connection."""
    from .cloud import load_link

    link = load_link()
    if link is None:
        delete_state()
        return SyncState()

    def off(st: SyncState) -> None:
        if st.sync_id:
            st.pending_off = {"sync_id": st.sync_id, "delete_copy": bool(delete_copy)}
        st.enabled, st.merge, st.preview, st.off_by = False, "pending", None, None
        st.pushed, st.topics, st.groups, st.settings, st.outcomes, st.filed = {}, {}, None, None, [], {}
    return update_state(link.device_id, off)


def confirm_merge(vault: Vault) -> SyncState:
    link = _linked(vault)

    def go(st: SyncState) -> None:
        if not st.enabled:
            raise SyncError("MyMemo Memory sync is off.")
        if st.merge in ("pending", "preview"):
            st.merge = "confirmed"
    return update_state(link.device_id, go)


def set_picked(vault: Vault, picked: list[str]) -> SyncState:
    link = _linked(vault)
    picks = _valid_picks(vault, picked)

    def pick(st: SyncState) -> None:
        st.picked = picks
    return update_state(link.device_id, pick)


def status(vault: Vault, *, online: bool = False) -> dict:
    """MyMemo Memory sync for the panel and ``openmymemo cloud sync status``: counts and states, never memory text."""
    from .cloud import load_link

    link = load_link()
    private = []
    with contextlib.suppress(VaultError, OSError):
        private = [{"slug": t["slug"], "title": t["title"]} for t in vault.topics() if t["private"]]
    if link is None or not link.serves(vault.root):
        return {"available": False, "enabled": False, "state": "off", "reason": None, "merge": None, "preview": None,
                "picked": [], "private_topics": private, "last_sync_at": None, "skipped": 0, "off_by": None,
                "pending_off": False, "consent_version": CONSENT_VERSION, "consent_at": None}
    st = load_state(link.device_id)
    s = st.status or {}
    if not st.enabled:
        state = "off"
    elif s.get("accepted") is False and s.get("reason"):
        state = "refused"
    elif not online or not s:
        state = "waiting"
    elif st.merge == "preview":
        state = "preview"
    elif st.merge in ("pending", "confirmed"):
        state = "merging"
    else:
        state = "syncing"
    return {"available": True, "enabled": st.enabled, "state": state,
            "reason": s.get("reason") if st.enabled else None, "merge": st.merge if st.enabled else None,
            "preview": st.preview if st.enabled and st.merge == "preview" else None, "picked": list(st.picked),
            "private_topics": private, "last_sync_at": st.last_sync_at, "skipped": st.skipped,
            "off_by": st.off_by, "pending_off": bool(st.pending_off), "consent_version": CONSENT_VERSION,
            "consent_at": st.consent_at if st.enabled else None}
