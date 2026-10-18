"""The vault: a folder of Markdown topic files plus a few plain-file sidecars.

Layout::

    <root>/
      memories/<topic>.md        the memories themselves (source of truth for content)
      MEMORY.md                  generated "what an AI sees" summary, overwritten on change
      log.md                     generated change log, newest day first (MEMORY_FORMAT.md §4.2)
      connectors.yaml            who may read / propose, and how
      .openmymemo/
        settings.yaml            vault-wide switches (e.g. whether AI changes need approval)
        topics.yaml              group names and sidebar order (a topic's group and privacy live in its frontmatter)
        meta.json                per-memory metadata keyed by id (kind, source, timestamps, version …)
        history.jsonl            append-only version history of every memory
        activity.jsonl           append-only log of what each connector read or proposed
        proposals/<id>.json      change proposals from AI connectors

Every durable fact lives in a plain file. There is no database to corrupt or rebuild. The format is
specified in docs/MEMORY_FORMAT.md; format-1 vaults (OpenMyMemo v0.1) are migrated on first open.

Users can edit ``memories/*.md`` in any editor at any time. ``sync()`` reconciles the files with
``meta.json`` (new lines, edited lines, deleted lines, lines moved between files) and records those
changes in history with actor ``file``. All writes happen under an inter-process lock and re-read the
file right before modifying it, so the stdio MCP server, the web panel and the user's editor can
coexist.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import string
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from .mdfile import MemoryFile, new_topic_file, render_item
from .textutil import KINDS, classify_kind
from .topics import DEFAULT_PRIVATE, DEFAULT_TOPICS, REVIEW_MONTHS, default_group_of, default_topics_config

STATE_DIR = ".openmymemo"
META_FORMAT = 2
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
RESERVED_FILES = {"index", "log"}  # the Open Knowledge Format's names: never topics
_ID_ALPHABET = string.ascii_lowercase + string.digits


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class Conflict(VaultError):
    """The memory isn't at the version the caller last saw: someone changed it meanwhile."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_until(value: str | None) -> date | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            y, m = map(int, value.split("-"))
            return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        return date.fromisoformat(value)
    except ValueError:
        return None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "topic"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@dataclass
class Memory:
    id: str
    content: str
    topic: str
    section: str | None = None
    kind: str = "fact"
    source: str = "user"
    last_actor: str = "user"  # who wrote the latest version; user / file / import:* make it protected
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    status: str = "active"
    pinned: bool = False
    private: bool = False  # item-level flag from the file
    until: str | None = None
    evidence: str | None = None
    archived_at: str | None = None

    def to_dict(self, topic_private: bool = False) -> dict:
        d = asdict(self)
        d["effective_private"] = self.private or topic_private
        until = parse_until(self.until)
        d["review_due"] = bool(until and until <= date.today() + timedelta(days=14))
        return d


def _check_version(memory_id: str, e: dict, expected: int | None) -> None:
    """Checked under the lock after re-reading the files, so an edit made in the meantime is never overwritten."""
    if expected is not None and int(e.get("version", 1)) != expected:
        raise Conflict(f"Memory {memory_id} was changed meanwhile (now version {e.get('version', 1)})")


@dataclass
class _Lock:
    rlock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    fh: object | None = None


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.memories_dir = self.root / "memories"
        self.state_dir = self.root / STATE_DIR
        self.meta_path = self.state_dir / "meta.json"
        self.topics_path = self.state_dir / "topics.yaml"
        self.settings_path = self.state_dir / "settings.yaml"
        self.history_path = self.state_dir / "history.jsonl"
        self.activity_path = self.state_dir / "activity.jsonl"
        self.proposals_dir = self.state_dir / "proposals"
        self.connectors_path = self.root / "connectors.yaml"
        self.memory_md_path = self.root / "MEMORY.md"
        self.log_md_path = self.root / "log.md"
        self._lock = _Lock()
        self._sig: tuple | None = None
        self._syncing = False
        self._meta: dict = {"format": 1, "memories": {}}
        self._topics_cfg: dict = {}
        self._headers: dict[str, dict] = {}  # topic -> title, description, group, private, sections (from the file)
        self._order: dict[str, list[str]] = {}  # topic -> ids in file order
        self._render_pending = False

    # ---- lifecycle -----------------------------------------------------------------------------

    @classmethod
    def create(cls, root: str | Path) -> Vault:
        vault = cls(root)
        with vault.lock():
            if vault.is_initialized():
                raise VaultError(f"{vault.root} is already an OpenMyMemo vault")
            vault._initialize()
        return vault

    @classmethod
    def open_or_create(cls, root: str | Path) -> Vault:
        """Open a vault, creating it first if the folder is missing or empty (used by plugins on first run).

        Apps often start at the same moment (Claude Desktop runs two copies of the server), so the check
        and the creation happen under the vault lock: a second app waits, then finds the vault ready.
        """
        vault = cls(root)
        if vault.is_initialized():
            return vault
        with vault.lock():  # creates .openmymemo/, which the emptiness check ignores like any dot entry
            if vault.is_initialized():
                return vault
            if any(not p.name.startswith(".") for p in vault.root.iterdir()):
                raise VaultError(f"{vault.root} isn't empty and isn't an OpenMyMemo vault. Choose another folder.")
            vault._initialize()
        return vault

    def _initialize(self) -> None:
        """Write a new vault's files. Callers hold the lock (on this same instance: flock isn't re-entrant)."""
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        for topic in DEFAULT_TOPICS:
            path = self.memories_dir / f"{topic.slug}.md"
            if not path.exists():
                _atomic_write(path, new_topic_file(topic.title, topic.description, group=default_group_of(topic.slug),
                                                   private=topic.slug in DEFAULT_PRIVATE))
        _atomic_write(self.meta_path, json.dumps({"format": META_FORMAT, "memories": {}}, indent=1))
        _atomic_write(self.settings_path, yaml.safe_dump({"format": 1, **self.DEFAULT_SETTINGS, "onboarded": False},
                                                         sort_keys=False))
        if not self.connectors_path.exists():
            _atomic_write(self.connectors_path, yaml.safe_dump({"format": 1, "connectors": []}, sort_keys=False))
        (self.state_dir / ".gitignore").write_text(".lock\n*.tmp\n", encoding="utf-8")
        # Last: is_initialized() checks for this file, so an app that looks without the lock never sees half a vault.
        _atomic_write(self.topics_path, yaml.safe_dump(default_topics_config(), allow_unicode=True, sort_keys=False))
        self.sync()

    def is_initialized(self) -> bool:
        return self.topics_path.exists() and self.memories_dir.is_dir()

    def ensure(self) -> Vault:
        if not self.is_initialized():
            raise VaultError(f"No OpenMyMemo vault at {self.root}. Run `openmymemo init` first.")
        return self

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Re-entrant lock shared by threads in this process and by other processes via flock."""
        with self._lock.rlock:
            if self._lock.depth == 0:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                fh = open(self.state_dir / ".lock", "a+")  # noqa: SIM115
                _flock(fh, True)
                self._lock.fh = fh
            self._lock.depth += 1
            try:
                yield
            finally:
                self._lock.depth -= 1
                if self._lock.depth == 0 and self._lock.fh is not None:
                    _flock(self._lock.fh, False)
                    self._lock.fh.close()
                    self._lock.fh = None

    # ---- sync ----------------------------------------------------------------------------------

    def _signature(self) -> tuple:
        parts = []
        for path in [self.meta_path, self.topics_path, *sorted(self._topic_paths())]:
            try:
                st = path.stat()
                parts.append((path.name, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                parts.append((path.name, None, None))
        return tuple(parts)

    def _topic_paths(self) -> list[Path]:
        """Every file that may be a topic: a valid slug, not a reserved name. Type is checked when parsed."""
        if not self.memories_dir.is_dir():
            return []
        return [p for p in self.memories_dir.glob("*.md") if SLUG_RE.match(p.stem) and p.stem not in RESERVED_FILES]

    def sync(self) -> None:
        self.ensure()
        with self.lock():
            if self._syncing or (self._sig is not None and self._sig == self._signature()):
                return
            self._syncing = True
            try:
                changed = self._reconcile()
                self._sig = self._signature()
            finally:
                self._syncing = False
            if changed or self._render_pending or not self.memory_md_path.exists() or not self.log_md_path.exists():
                self._render_pending = False
                self._render_memory_md()
                self._render_log_md()

    def _new_id(self, used: set[str]) -> Callable[[], str]:
        def gen() -> str:
            while True:
                candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(5))
                if candidate not in used and not candidate.isdigit():
                    used.add(candidate)
                    return candidate
        return gen

    def _reconcile(self) -> bool:
        cfg = yaml.safe_load(self.topics_path.read_text(encoding="utf-8")) or {}
        original_cfg = copy.deepcopy(cfg)
        legacy_private = set(cfg.get("private") or [])  # format 1 kept privacy here
        cfg_group_of = {slug: g.get("id") for g in cfg.get("groups") or [] for slug in g.get("topics") or []}
        meta = _read_json(self.meta_path, {"format": 1, "memories": {}})
        entries: dict[str, dict] = meta.setdefault("memories", {})
        changed = int(meta.get("format", 1)) < META_FORMAT and self._migrate_meta(meta)
        used = set(entries)
        files: dict[str, MemoryFile] = {}
        headers: dict[str, dict] = {}
        for path in sorted(self._topic_paths()):
            slug = path.stem
            mf = MemoryFile(path.read_text(encoding="utf-8"))
            if mf.migrate_header(group=cfg_group_of.get(slug), private=slug in legacy_private):
                _atomic_write(path, mf.render())  # format 1 header → frontmatter, once
            if mf.frontmatter_type not in (None, "topic"):
                continue  # documents and unknown types are left untouched and not shown (MEMORY_FORMAT.md §3.2)
            used.update(i.id for i in mf.items if i.id)
            files[slug] = mf
            headers[slug] = {"title": mf.title, "description": mf.description, "group": mf.group,
                             "private": mf.private, "sections": [sec for sec, _ in mf.sections]}

        gen = self._new_id(used)
        seen: set[str] = set()
        for slug, mf in files.items():
            dirty = bool(mf.assign_ids(gen))
            for item in list(mf.items):  # ids duplicated across files (e.g. a line copied to another topic)
                if item.id in seen:
                    mf.lines[item.line_no] = render_item(item.content, gen(), item.attrs, item.marker)
                    dirty = True
            if dirty:
                mf._reparse()
            seen.update(i.id for i in mf.items)
            if dirty:
                _atomic_write(self.memories_dir / f"{slug}.md", mf.render())

        now = now_iso()
        present: set[str] = set()
        self._order = {}
        for slug, mf in files.items():
            self._order[slug] = []
            for item in mf.items:
                present.add(item.id)
                self._order[slug].append(item.id)
                attrs = item.attrs
                e = entries.get(item.id)
                if e is None:
                    entries[item.id] = {
                        "content": item.content, "topic": slug, "section": item.section,
                        "kind": classify_kind(item.content), "source": "user", "last_actor": "file",
                        "created_at": now, "updated_at": now, "version": 1, "status": "active",
                    }
                    self._history(item.id, 1, "create", item.content, slug, "file")
                    changed = True
                    continue
                op = None
                if e.get("status") == "archived":
                    op = "restore"
                elif e.get("content") != item.content:
                    op = "update"
                elif e.get("topic") != slug or e.get("section") != item.section:
                    op = "move"
                if op:
                    e.update(content=item.content, topic=slug, section=item.section, status="active",
                             updated_at=now, version=int(e.get("version", 1)) + 1, last_actor="file")
                    e.pop("archived_at", None)
                    self._history(item.id, e["version"], op, item.content, slug, "file")
                    changed = True
                for key in ("until", "private"):
                    if e.get(key) != attrs.get(key):
                        if attrs.get(key) is None:
                            e.pop(key, None)
                        else:
                            e[key] = attrs[key]
                        changed = True
        for mid, e in entries.items():
            if e.get("status", "active") == "active" and mid not in present:
                e.update(status="archived", archived_at=now, updated_at=now,
                         version=int(e.get("version", 1)) + 1, last_actor="file")
                self._history(mid, e["version"], "archive", e.get("content", ""), e.get("topic", ""), "file")
                changed = True

        # topics.yaml only orders topics; each file's frontmatter says which group it is in. Newcomers are
        # appended to their group in name order (files were visited sorted), stale or moved slugs drop out.
        groups = [g for g in (cfg.get("groups") or []) if isinstance(g, dict) and g.get("id")]
        by_id = {g["id"]: g for g in groups}
        for g in groups:
            g["topics"] = [t for t in (g.get("topics") or []) if t in headers and headers[t]["group"] == g["id"]]
        for slug, h in headers.items():
            gid = h["group"]
            if not gid:
                continue
            g = by_id.get(gid)
            if g is None:
                g = {"id": gid, "name": gid.replace("-", " ").capitalize(), "topics": []}
                groups.append(g)
                by_id[gid] = g
            if slug not in g["topics"]:
                g["topics"].append(slug)
        new_cfg = {"format": 2, "groups": groups,
                   **{k: v for k, v in cfg.items() if k not in ("format", "groups", "private")}}
        if new_cfg != original_cfg:
            _atomic_write(self.topics_path, yaml.safe_dump(new_cfg, allow_unicode=True, sort_keys=False))
        self._topics_cfg = new_cfg
        if headers != self._headers:
            self._render_pending = True  # titles and privacy show up in MEMORY.md
        self._headers = headers
        self._meta = meta
        if changed or not self.meta_path.exists():
            self._write_meta()
        return changed

    def _migrate_meta(self, meta: dict) -> bool:
        """Format 1 → 2: remember who wrote each memory last, and keep this vault's approval behaviour."""
        latest: dict[str, str] = {}
        for rec in _read_jsonl(self.history_path):
            latest[str(rec.get("memory_id", ""))] = str(rec.get("actor") or "user")
        for mid, e in meta.get("memories", {}).items():
            e.setdefault("last_actor", latest.get(mid, "user"))
        meta["format"] = META_FORMAT
        # v0.1 made every AI change wait; an existing vault keeps that until the user turns it off.
        try:
            settings = yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            settings = {}
        except yaml.YAMLError:
            settings = None  # not ours to rewrite
        if isinstance(settings, dict) and "require_approval" not in settings:
            _atomic_write(self.settings_path, yaml.safe_dump({"format": 1, **settings, "require_approval": True},
                                                             sort_keys=False))
        return True

    def _write_meta(self) -> None:
        _atomic_write(self.meta_path, json.dumps(self._meta, ensure_ascii=False, indent=1) + "\n")

    def _render_memory_md(self) -> None:
        from .context import render_memory_md

        text = render_memory_md(self)
        try:
            if self.memory_md_path.read_text(encoding="utf-8") == text:
                return  # don't touch the mtime for sync tools and git when nothing changed
        except OSError:
            pass
        _atomic_write(self.memory_md_path, text)

    def _render_log_md(self) -> None:
        from .context import render_log_md

        text = render_log_md(self)
        try:
            if self.log_md_path.read_text(encoding="utf-8") == text:
                return  # as with MEMORY.md: leave the mtime alone when nothing changed
        except OSError:
            pass
        _atomic_write(self.log_md_path, text)

    def _after_write(self) -> None:
        self._write_meta()
        self._sig = self._signature()
        self._render_memory_md()
        self._render_log_md()

    # ---- history & activity --------------------------------------------------------------------

    def _history(self, memory_id: str, version: int, op: str, content: str, topic: str, actor: str,
                 proposal_id: str | None = None) -> None:
        rec = {"at": now_iso(), "memory_id": memory_id, "version": version, "op": op,
               "content": content, "topic": topic, "actor": actor}
        if proposal_id:
            rec["proposal_id"] = proposal_id
        _append_jsonl(self.history_path, rec)

    def history(self, memory_id: str | None = None, limit: int = 200) -> list[dict]:
        rows = _read_jsonl(self.history_path)
        if memory_id:
            rows = [r for r in rows if r.get("memory_id") == memory_id]
        return rows[-limit:][::-1]

    def log_activity(self, connector_id: str, action: str, **fields) -> None:
        rec = {"at": now_iso(), "connector": connector_id, "action": action}
        rec.update({k: v for k, v in fields.items() if v is not None})
        _append_jsonl(self.activity_path, rec)

    def activity(self, connector_id: str | None = None, limit: int = 200) -> list[dict]:
        rows = _read_jsonl(self.activity_path)
        if connector_id:
            rows = [r for r in rows if r.get("connector") == connector_id]
        return rows[-limit:][::-1]

    # ---- settings ------------------------------------------------------------------------------

    DEFAULT_SETTINGS = {"require_approval": False}  # new vaults: AI changes apply and show up for review
    # `onboarded` (the panel's first-run guide) is written as false into new vaults. Without the key, a folder
    # counts as onboarded once it has been used, so people upgrading never see the guide (MEMORY_FORMAT.md §6.1).
    SETTINGS = (*DEFAULT_SETTINGS, "onboarded")

    def _stored_settings(self) -> dict:
        try:
            data = yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _used(self) -> bool:
        """Any memory, archived ones included, or any connector, revoked ones included."""
        if self._meta["memories"]:
            return True
        try:
            data = yaml.safe_load(self.connectors_path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return False
        return isinstance(data, dict) and bool(data.get("connectors"))

    def settings(self) -> dict:
        if self.is_initialized():
            self.sync()  # a format-1 vault gets its settings.yaml written during migration; read it after that
        data = self._stored_settings()
        out = {k: data.get(k, v) for k, v in self.DEFAULT_SETTINGS.items()}
        onboarded = data.get("onboarded")
        out["onboarded"] = onboarded if isinstance(onboarded, bool) else self._used()
        return out

    def update_settings(self, **changes) -> dict:
        unknown = set(changes) - set(self.SETTINGS)
        if unknown:
            raise VaultError(f"Unknown setting: {', '.join(sorted(unknown))}")
        with self.lock():
            stored = self._stored_settings()
            current = self.settings()
            changed = {k: bool(v) for k, v in changes.items() if v is not None}
            current.update(changed)
            # Keys this version doesn't know are kept (MEMORY_FORMAT.md §6.1). A computed `onboarded` isn't
            # written out along with another change: it stays computed until someone sets it.
            stored.update({**{k: current[k] for k in self.DEFAULT_SETTINGS}, **changed})
            _atomic_write(self.settings_path, yaml.safe_dump({"format": 1, **stored}, sort_keys=False))
            return current

    # ---- topics --------------------------------------------------------------------------------

    def private_topics(self) -> set[str]:
        self.sync()
        return {slug for slug, h in self._headers.items() if h["private"]}

    def topics(self) -> list[dict]:
        self.sync()
        counts: dict[str, int] = {}
        for e in self._meta["memories"].values():
            if e.get("status", "active") == "active":
                counts[e["topic"]] = counts.get(e["topic"], 0) + 1
        groups = self._topics_cfg.get("groups") or []
        names = {g["id"]: g.get("name") or g["id"] for g in groups}
        order = [(g["id"], list(g.get("topics") or [])) for g in groups]
        order.append(("other", sorted(slug for slug, h in self._headers.items() if not h["group"])))
        out, placed = [], set()
        for gid, slugs in order:
            for slug in slugs:
                h = self._headers.get(slug)
                if h is None or slug in placed:
                    continue
                placed.add(slug)
                out.append({
                    "slug": slug, "title": h["title"] or slug, "description": h["description"],
                    "group": gid, "group_name": names.get(gid) or ("Other" if gid == "other" else gid),
                    "private": h["private"], "count": counts.get(slug, 0), "sections": list(h["sections"]),
                })
        return out

    def topic(self, slug: str) -> dict:
        for t in self.topics():
            if t["slug"] == slug:
                return t
        raise NotFound(f"Unknown topic: {slug}")

    def add_topic(self, slug: str, title: str, description: str | None = None, group: str | None = None,
                  private: bool = False) -> dict:
        slug = slug.strip().lower()
        if not SLUG_RE.match(slug) or slug in RESERVED_FILES:
            raise VaultError("Topic slug must be lowercase letters, digits and dashes")
        with self.lock():
            self.sync()
            path = self.memories_dir / f"{slug}.md"
            if path.exists():
                raise VaultError(f"Topic {slug} already exists")
            gid = None
            if group:
                groups = self._topics_cfg.setdefault("groups", [])
                target = next((g for g in groups if g["id"] in (group, slugify(group))), None)
                if target is None:  # a new group: keep the name the user typed
                    target = {"id": slugify(group), "name": group, "topics": []}
                    groups.append(target)
                    _atomic_write(self.topics_path, yaml.safe_dump(self._topics_cfg, allow_unicode=True, sort_keys=False))
                gid = target["id"]
            _atomic_write(path, new_topic_file(title, description, group=gid, private=private))
            self._sig = None
            self.sync()  # files the topic under its group in topics.yaml
        return self.topic(slug)

    def ensure_import_topic(self, source: str) -> str:
        """The ``from-<source>`` topic an import lands in, created on the first import from that source."""
        from .topics import IMPORT_SOURCE_NAMES, IMPORTED_GROUP_NAME, import_topic_slug

        name = IMPORT_SOURCE_NAMES.get(source)
        if name is None:
            raise VaultError(f"Unknown import source: {source}")
        slug = import_topic_slug(source)
        with self.lock():
            self.sync()
            if slug not in self._headers:
                self.add_topic(slug, f"From {name}",
                               f"Imported from {name} on {date.today().isoformat()}, in the original order. "
                               "Sort these into topics when you like.",
                               group=IMPORTED_GROUP_NAME)  # "Imported": id `imported`, shown as "Imported"
        return slug

    def update_topic(self, slug: str, *, title: str | None = None, description: str | None = None,
                     private: bool | None = None) -> dict:
        with self.lock():
            self.sync()
            path = self.memories_dir / f"{slug}.md"
            if slug not in self._headers or not path.exists():
                raise NotFound(f"Unknown topic: {slug}")
            if title is None and description is None and private is None:
                return self.topic(slug)
            mf = MemoryFile(path.read_text(encoding="utf-8"))
            if mf.frontmatter_broken:
                raise VaultError(f"The frontmatter of {path.name} isn't valid YAML; fix it in an editor first")
            fm = dict(mf.frontmatter or {})
            fm.setdefault("type", "topic")
            if title is not None:
                fm["title"] = title
            if description is not None:
                fm["description"] = description
                if not description:
                    fm.pop("description")
            if private is not None:
                fm["private"] = True
                if not private:
                    fm.pop("private")
            mf.set_frontmatter(fm)
            _atomic_write(path, mf.render())
            self._sig = None
            self.sync()
            self._render_memory_md()
            self._render_log_md()
        return self.topic(slug)

    # ---- reading memories ----------------------------------------------------------------------

    def _memory(self, mid: str, e: dict) -> Memory:
        return Memory(
            id=mid, content=e.get("content", ""), topic=e.get("topic", ""), section=e.get("section"),
            kind=e.get("kind", "fact"), source=e.get("source", "user"), last_actor=e.get("last_actor", "user"),
            created_at=e.get("created_at", ""),
            updated_at=e.get("updated_at", ""), version=int(e.get("version", 1)), status=e.get("status", "active"),
            pinned=bool(e.get("pinned")), private=bool(e.get("private")), until=e.get("until"),
            evidence=e.get("evidence"), archived_at=e.get("archived_at"),
        )

    def memories(self, status: str = "active") -> list[Memory]:
        self.sync()
        entries = self._meta["memories"]
        if status == "active":
            out = []
            for t in self.topics():
                out.extend(self._memory(mid, entries[mid]) for mid in self._order.get(t["slug"], [])
                           if mid in entries and entries[mid].get("status", "active") == "active")
            return out
        rows = [self._memory(mid, e) for mid, e in entries.items() if e.get("status", "active") == status]
        return sorted(rows, key=lambda m: m.archived_at or m.updated_at, reverse=True)

    def get(self, memory_id: str) -> Memory:
        self.sync()
        e = self._meta["memories"].get(memory_id)
        if e is None:
            raise NotFound(f"No memory with id {memory_id}")
        return self._memory(memory_id, e)

    def is_private(self, m: Memory) -> bool:
        return m.private or m.topic in self.private_topics()

    # ---- writing memories ----------------------------------------------------------------------

    def _edit_file(self, slug: str, fn: Callable[[MemoryFile], None]) -> None:
        path = self.memories_dir / f"{slug}.md"
        if not path.exists():
            raise NotFound(f"Unknown topic: {slug}")
        mf = MemoryFile(path.read_text(encoding="utf-8"))
        fn(mf)
        text = mf.render()
        _atomic_write(path, text)
        # Writes refresh the signature without re-reading the files, so keep the file order in step here.
        self._order[slug] = [item.id for item in MemoryFile(text).items]

    def add(self, topic: str, content: str, *, kind: str | None = None, section: str | None = None,
            source: str = "user", actor: str = "user", private: bool = False, until: str | None = None,
            evidence: str | None = None, pinned: bool = False, proposal_id: str | None = None,
            created_at: str | None = None) -> Memory:
        content = " ".join(content.split())
        if not content:
            raise VaultError("Memory content is empty")
        if len(content) > 1000:
            raise VaultError("A memory must be a single statement of at most 1000 characters")
        if kind is not None and kind not in KINDS:
            raise VaultError(f"kind must be one of {', '.join(KINDS)}")
        with self.lock():
            self.sync()
            if topic not in self._headers:
                raise NotFound(f"Unknown topic: {topic}")
            if until is None and topic in REVIEW_MONTHS and source != "user":
                d = date.today() + timedelta(days=30 * REVIEW_MONTHS[topic])
                until = f"{d.year:04d}-{d.month:02d}"
            mid = self._new_id(set(self._meta["memories"]))()
            attrs: dict = {}
            if until:
                attrs["until"] = until
            if private:
                attrs["private"] = True
            self._edit_file(topic, lambda mf: mf.add(content, mid, attrs, section))
            now = now_iso()
            entry = {"content": content, "topic": topic, "section": section, "kind": kind or classify_kind(content),
                     "source": source, "last_actor": actor, "created_at": created_at or now, "updated_at": now,
                     "version": 1, "status": "active"}
            entry.update({k: v for k, v in attrs.items()})
            if evidence:
                entry["evidence"] = evidence[:2000]
            if pinned:
                entry["pinned"] = True
            self._meta["memories"][mid] = entry
            self._history(mid, 1, "create", content, topic, actor, proposal_id)
            self._after_write()
            return self._memory(mid, entry)

    def update(self, memory_id: str, *, content: str | None = None, topic: str | None = None,
               kind: str | None = None, pinned: bool | None = None, private: bool | None = None,
               until: str | None = None, actor: str = "user", proposal_id: str | None = None,
               expected_version: int | None = None) -> Memory:
        with self.lock():
            self.sync()
            e = self._meta["memories"].get(memory_id)
            if e is None:
                raise NotFound(f"No memory with id {memory_id}")
            _check_version(memory_id, e, expected_version)
            if e.get("status") == "archived":
                raise VaultError(f"Memory {memory_id} is archived; restore it first")
            if kind is not None and kind not in KINDS:
                raise VaultError(f"kind must be one of {', '.join(KINDS)}")
            new_content = " ".join(content.split()) if content is not None else e["content"]
            if not new_content:
                raise VaultError("Memory content is empty")
            new_topic = topic or e["topic"]
            attrs: dict = {}
            new_until = e.get("until") if until is None else (until or None)
            new_private = bool(e.get("private")) if private is None else private
            if new_until:
                attrs["until"] = new_until
            if new_private:
                attrs["private"] = True
            file_changed = (new_content != e["content"] or new_topic != e["topic"]
                            or new_until != e.get("until") or new_private != bool(e.get("private")))
            if new_topic != e["topic"]:
                if new_topic not in self._headers:
                    raise NotFound(f"Unknown topic: {new_topic}")
                self._edit_file(e["topic"], lambda mf: mf.remove(memory_id))
                self._edit_file(new_topic, lambda mf: mf.add(new_content, memory_id, attrs))
                e["section"] = None
            elif file_changed:
                def edit(mf: MemoryFile) -> None:
                    item = mf.find(memory_id)
                    if item is None:
                        mf.add(new_content, memory_id, attrs, e.get("section"))
                    else:
                        mf.set_line(item, new_content, attrs)
                self._edit_file(e["topic"], edit)
            op = "move" if new_topic != e["topic"] and new_content == e["content"] else "update"
            content_changed = new_content != e["content"] or new_topic != e["topic"]
            e.update(content=new_content, topic=new_topic, updated_at=now_iso())
            for key, value in (("until", new_until), ("private", new_private or None)):
                if value is None:
                    e.pop(key, None)
                else:
                    e[key] = value
            if kind is not None:
                e["kind"] = kind
            if pinned:
                e["pinned"] = True
            elif pinned is not None:
                e.pop("pinned", None)
            if content_changed:
                e["version"] = int(e.get("version", 1)) + 1
                e["last_actor"] = actor
                self._history(memory_id, e["version"], op, new_content, new_topic, actor, proposal_id)
            self._after_write()
            return self._memory(memory_id, e)

    def archive(self, memory_id: str, *, actor: str = "user", proposal_id: str | None = None,
                expected_version: int | None = None) -> Memory:
        with self.lock():
            self.sync()
            e = self._meta["memories"].get(memory_id)
            if e is None:
                raise NotFound(f"No memory with id {memory_id}")
            _check_version(memory_id, e, expected_version)
            if e.get("status") == "archived":
                return self._memory(memory_id, e)
            self._edit_file(e["topic"], lambda mf: mf.remove(memory_id))
            now = now_iso()
            e.update(status="archived", archived_at=now, updated_at=now, version=int(e.get("version", 1)) + 1,
                     last_actor=actor)
            self._history(memory_id, e["version"], "archive", e["content"], e["topic"], actor, proposal_id)
            self._after_write()
            return self._memory(memory_id, e)

    def restore(self, memory_id: str, *, actor: str = "user", proposal_id: str | None = None) -> Memory:
        with self.lock():
            self.sync()
            e = self._meta["memories"].get(memory_id)
            if e is None:
                raise NotFound(f"No memory with id {memory_id}")
            if e.get("status", "active") == "active":
                return self._memory(memory_id, e)
            if not self._headers:
                raise VaultError("There is no topic to restore into")
            topic = e["topic"] if e["topic"] in self._headers else (
                "identity" if "identity" in self._headers else next(iter(self._headers)))
            attrs = {k: e[k] for k in ("until", "private") if e.get(k)}
            self._edit_file(topic, lambda mf: mf.add(e["content"], memory_id, attrs, e.get("section")))
            e.update(status="active", topic=topic, updated_at=now_iso(), version=int(e.get("version", 1)) + 1,
                     last_actor=actor)
            e.pop("archived_at", None)
            self._history(memory_id, e["version"], "restore", e["content"], topic, actor, proposal_id)
            self._after_write()
            return self._memory(memory_id, e)

    def rollback(self, memory_id: str, version: int, *, actor: str = "user") -> Memory:
        target = next((h for h in self.history(memory_id, limit=10_000) if h["version"] == version), None)
        if target is None:
            raise NotFound(f"Memory {memory_id} has no version {version}")
        with self.lock():
            m = self.get(memory_id)
            if m.status == "archived":
                self.restore(memory_id, actor=actor)
            return self.update(memory_id, content=target["content"], topic=target["topic"], actor=actor)


# ---- helpers ---------------------------------------------------------------------------------------

def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _flock(fh, acquire: bool) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


def vault_path(value: str | Path | None) -> Path:
    """The vault folder for a ``--vault`` / ``OPENMYMEMO_HOME`` value; blank means the default ~/OpenMyMemo.

    App launchers hand these over from settings fields the user may leave empty, and the MCPB spec doesn't
    say what an empty optional field becomes, so an empty string and an unsubstituted ``${…}`` placeholder
    both count as "not set".
    """
    text = str(value).strip() if value is not None else ""
    return Path(text).expanduser() if text and "${" not in text else Path.home() / "OpenMyMemo"


def default_vault_path() -> Path:
    return vault_path(os.environ.get("OPENMYMEMO_HOME"))
