"""The Memory safety score (product-design.md §18.2): 0 to 100, by rules, on this computer.

Five areas, each with its points; every issue costs points within its area, and an area never goes below 0.
Each issue names a memory, an app or a setting and the fix the panel offers, never a memory's text, so the
result can be shown, logged and kept anywhere. Dismissed issues cost nothing and stay listed.

State lives in the folder's ``.openmymemo/`` as plain files: ``score.json`` (dismissals, when memory was last exported in full) and
``score.jsonl`` (one line a day: the date, the score and each area's points). Deleting them resets the history
and changes no memory.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import secrets
from .connectors import Connectors
from .proposals import Proposals
from .textutil import grams, similarity
from .vault import Vault, _append_jsonl, _atomic_write, _read_json, _read_jsonl, now_iso, parse_until

log = logging.getLogger(__name__)

AREAS = (  # id, name, points
    ("sensitive", "Sensitive information", 25),
    ("apps", "App access", 25),
    ("review", "Review habits", 20),
    ("upkeep", "Memory upkeep", 15),
    ("backup", "Backup", 15),
)
LEVELS = ((90, "Excellent"), (70, "Good"), (50, "Needs attention"), (0, "At risk"))
STALE_DAYS = 7      # a change waiting longer than this hurts review habits
UNUSED_DAYS = 30    # an app with access it hasn't used for this long
BACKUP_DAYS = 30    # how recent a commit or an export must be
LONG_CHARS = 600    # a memory longer than this is hard to read at a glance
DUPLICATE_AT = 0.6  # the similarity at which an AI app's addition becomes an update (§5.5)
MAX_COMPARED = 3000  # above this many memories, skip the pairwise duplicate check
KIND_NAMES = {"key": "an API key or token", "password": "a password", "card": "a payment card number",
              "id_number": "an ID number"}


@dataclass
class Issue:
    id: str
    area: str
    points: int
    message: str
    fix: str                   # what the panel offers: private_topics, memory, app, changes, memories, export
    target: str | None = None  # the memory or app id the fix opens
    dismissed: bool = False


def level_of(score: int) -> str:
    return next(name for floor, name in LEVELS if score >= floor)


def _state_path(vault: Vault) -> Path:
    return vault.state_dir / "score.json"


def _history_path(vault: Vault) -> Path:
    return vault.state_dir / "score.jsonl"


def _state(vault: Vault) -> dict:
    state = _read_json(_state_path(vault), {})
    return state if isinstance(state, dict) else {}


def _save_state(vault: Vault, state: dict) -> None:
    _atomic_write(_state_path(vault), json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def _when(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when and when.tzinfo is None else when


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---- the five areas ------------------------------------------------------------------------------------

def _sensitive(vault: Vault) -> list[Issue]:
    issues = []
    if not vault.private_topics():
        issues.append(Issue("no-private-topics", "sensitive", 10,
                            "No topic is private, so every app that reads your memory can read all of it.",
                            "private_topics"))
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    for f in secrets.scan(vault):
        issues.append(Issue(f"secret:{f.memory_id}:{f.kind}", "sensitive", 5,
                            f"A memory in {titles.get(f.topic, f.topic)} looks like it contains "
                            f"{KIND_NAMES[f.kind]}.", "memory", f.memory_id))
    return issues


def _apps(vault: Vault, now: datetime) -> list[Issue]:
    issues = []
    for c in Connectors(vault).list():
        if c.kind == "assistant":  # its private topics are opted in one by one in Settings → Assistant (§17.2)
            continue
        if c.include_private:
            issues.append(Issue(f"app-private:{c.id}", "apps", 8, f"{c.name} can read your private topics.",
                                "app", c.id))
        if c.write == "auto":
            issues.append(Issue(f"app-auto:{c.id}", "apps", 6,
                                f"{c.name} changes your memory without anything waiting for you, even what you "
                                "wrote yourself.", "app", c.id))
        last = next(iter(vault.activity(c.id, limit=1)), None)
        since = _when(last["at"] if last else c.created_at)
        if since and now - since > timedelta(days=UNUSED_DAYS):
            issues.append(Issue(f"app-unused:{c.id}", "apps", 3,
                                f"{c.name} hasn't used your memory in {UNUSED_DAYS} days but can still read it.",
                                "app", c.id))
    try:
        from . import clients

        for row in clients.catalog(vault):
            if row["broken"]:
                issues.append(Issue(f"app-broken:{row['id']}", "apps", 4,
                                    f"{row['name']}'s settings start OpenMyMemo from a program that no longer "
                                    "exists.", "app", row["id"]))
    except Exception:  # the app catalog reads other apps' files; a surprise there must not hide the score
        log.warning("Couldn't check the AI apps' settings for the safety score", exc_info=True)
    return issues


def _review(vault: Vault, now: datetime) -> list[Issue]:
    props = Proposals(vault).list()
    cutoff = now - timedelta(days=STALE_DAYS)

    def old(p) -> bool:
        created = _when(p.created_at)
        return bool(created and created < cutoff)

    pending = sum(1 for p in props if p.status == "pending" and old(p))
    unreviewed = sum(1 for p in props if p.status == "applied" and not p.reviewed_at and old(p))
    issues = []
    if pending:
        issues.append(Issue("review-pending", "review", 12,
                            f"{_plural(pending, 'change')} AI apps proposed {'has' if pending == 1 else 'have'} "
                            f"waited for you more than {STALE_DAYS} days.", "changes"))
    if unreviewed:
        issues.append(Issue("review-unreviewed", "review", 8,
                            f"{_plural(unreviewed, 'change')} AI apps made more than {STALE_DAYS} days ago "
                            f"{'is' if unreviewed == 1 else 'are'} still unseen.", "changes"))
    return issues


def _duplicates(memories: list) -> int:
    if len(memories) > MAX_COMPARED:
        return 0
    gs = [grams(m.content) for m in memories]
    found = 0
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if gs[i] & gs[j] and similarity(memories[i].content, memories[j].content) >= DUPLICATE_AT:
                found += 1
    return found


def _upkeep(vault: Vault, today: date) -> list[Issue]:
    mems = vault.memories()
    issues = []
    overdue = sum(1 for m in mems if (until := parse_until(m.until)) and until < today)
    if overdue:
        what = "is past the date to check it" if overdue == 1 else "are past the date to check them"
        issues.append(Issue("upkeep-overdue", "upkeep", 5, f"{_plural(overdue, 'memory', 'memories')} {what}.",
                            "memories"))
    pairs = _duplicates(mems)
    if pairs:
        issues.append(Issue("upkeep-duplicates", "upkeep", 5,
                            f"{_plural(pairs, 'pair')} of memories look like duplicates.", "memories"))
    long = sum(1 for m in mems if len(m.content) > LONG_CHARS)
    if long:
        issues.append(Issue("upkeep-long", "upkeep", 5,
                            f"{_plural(long, 'memory', 'memories')} {'is' if long == 1 else 'are'} too long to "
                            "read at a glance.", "memories"))
    return issues


def _git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_backup(vault: Vault, now: datetime) -> bool:
    """A commit touching ``memories/`` in the last BACKUP_DAYS whose tree holds every memory file there is now.

    Files edited since that commit still count (the commit restores them); a file only staged, untracked or
    ignored doesn't, since no commit has a copy of it.
    """
    where = vault.memories_dir
    last = (_git(where, "log", "-1", "--format=%ct", "--", ".") or "").strip()
    if not last.isdigit() or now - datetime.fromtimestamp(int(last), UTC) > timedelta(days=BACKUP_DAYS):
        return False
    tree = _git(where, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", ".")
    if tree is None:
        return False
    committed = set(tree.split("\0"))
    return all(p.relative_to(where).as_posix() in committed for p in where.rglob("*.md"))


def _backup(vault: Vault, now: datetime) -> list[Issue]:
    if not vault.memories() and not vault.memories("archived"):
        return []  # nothing to lose yet
    exported = _when(_state(vault).get("last_full_export"))
    if exported and now - exported <= timedelta(days=BACKUP_DAYS):
        return []
    if git_backup(vault, now):
        return []
    return [Issue("backup", "backup", 15,
                  f"There's no copy of your memory from the last {BACKUP_DAYS} days: no git commit with every "
                  "memory file and no full export.", "export")]


# ---- the score ---------------------------------------------------------------------------------------

def compute(vault: Vault, *, now: datetime | None = None) -> dict:
    """The score, its level, each area's points and every issue, the costliest first; dismissed ones last."""
    now = now or datetime.now(UTC)
    today = now.astimezone().date()
    dismissed = set(_state(vault).get("dismissed") or [])
    issues = (_sensitive(vault) + _apps(vault, now) + _review(vault, now) + _upkeep(vault, today)
              + _backup(vault, now))
    for i in issues:
        i.dismissed = i.id in dismissed
    areas = []
    for area, name, points in AREAS:
        lost = sum(i.points for i in issues if i.area == area and not i.dismissed)
        areas.append({"id": area, "name": name, "max": points, "points": max(0, points - lost)})
    score = sum(a["points"] for a in areas)
    issues.sort(key=lambda i: (i.dismissed, -i.points))
    return {"score": score, "level": level_of(score), "areas": areas, "issues": [asdict(i) for i in issues],
            "week_change": _week_change(vault, score, today)}


def _week_change(vault: Vault, score: int, today: date) -> int | None:
    week_ago = (today - timedelta(days=7)).isoformat()
    before = [r for r in _read_jsonl(_history_path(vault)) if isinstance(r.get("score"), int)
              and str(r.get("date", "")) <= week_ago]
    return score - before[-1]["score"] if before else None


def record(vault: Vault, result: dict, *, now: datetime | None = None) -> None:
    """Add today's line to ``score.jsonl`` once a day: the date, the score and each area's points."""
    today = (now or datetime.now(UTC)).astimezone().date().isoformat()
    rows = _read_jsonl(_history_path(vault))
    if rows and rows[-1].get("date") == today:
        return
    _append_jsonl(_history_path(vault), {"date": today, "score": result["score"],
                                         "areas": {a["id"]: a["points"] for a in result["areas"]}})


def history(vault: Vault, days: int = 90) -> list[dict]:
    return _read_jsonl(_history_path(vault))[-days:]


def dismiss(vault: Vault, issue_id: str, *, restore: bool = False) -> None:
    """Stop counting an issue, or count it again. Dismissing ``backup`` is "I back up another way"."""
    with vault.lock():
        state = _state(vault)
        dismissed = [d for d in state.get("dismissed") or [] if d != issue_id]
        if not restore:
            dismissed.append(issue_id)
        state["dismissed"] = dismissed
        _save_state(vault, state)


def note_export(vault: Vault, *, full: bool) -> None:
    """Remember a full export (private memories included) for the Backup area; a partial one is no backup."""
    if not full:
        return
    try:
        with vault.lock():
            state = _state(vault)
            state["last_full_export"] = now_iso()
            _save_state(vault, state)
    except OSError:
        log.warning("Couldn't note the export for the safety score", exc_info=True)
