"""What the panel's Home page shows (product-design.md §18.1), worked out from files that already exist.

Counts per group, growth over 30 days, where memories came from, how each connected app used memory in the
last 7 days, what waits for the person, reads and writes per day, and the Memory safety score with its top
issues. Read-only: it changes nothing except ``score.record``'s one line a day. Private topics show as counts
only, and no memory text is returned.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta

from . import score
from .connectors import Connectors
from .proposals import Proposals
from .vault import Vault, parse_until

READS = ("context", "search", "get", "recall")
WRITES = ("propose",)
GROWTH_DAYS = 30
ACTIVITY_DAYS = 7
REVIEW_SOON = timedelta(days=14)  # the same window as a memory's ``review_due``


def _day(value: str | None) -> date | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if when is None:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone().date()


def source_of(source: str) -> str:
    """``you``, ``apps`` or ``imports``: who a memory came from."""
    if source.startswith("connector:"):
        return "apps"
    return "imports" if source.startswith("import:") else "you"


def build(vault: Vault, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    today = now.astimezone().date()
    topics = vault.topics()
    private = vault.private_topics()
    mems = vault.memories()

    groups: list[dict] = []
    for t in topics:
        if not groups or groups[-1]["id"] != t["group"]:
            groups.append({"id": t["group"], "name": t["group_name"], "count": 0})
        groups[-1]["count"] += t["count"]

    days = [today - timedelta(days=n) for n in range(GROWTH_DAYS - 1, -1, -1)]
    created = Counter(_day(m.created_at) for m in mems)
    total = sum(n for d, n in created.items() if d is None or d < days[0])
    growth = []
    for d in days:
        total += created[d]
        growth.append({"date": d.isoformat(), "total": total})

    props = Proposals(vault).list()
    waiting = {
        "pending": sum(1 for p in props if p.status == "pending"),
        "unreviewed": sum(1 for p in props if p.status == "applied" and not p.reviewed_at),
        "review_due": sum(1 for m in mems if (until := parse_until(m.until)) and until <= today + REVIEW_SOON),
    }

    week = [today - timedelta(days=n) for n in range(ACTIVITY_DAYS - 1, -1, -1)]
    per_day = {d: {"reads": 0, "writes": 0} for d in week}
    per_app: dict[str, Counter] = {}
    for row in vault.activity(limit=100_000):
        d = _day(row.get("at"))
        if d not in per_day:
            continue
        kind = "reads" if row.get("action") in READS else "writes" if row.get("action") in WRITES else None
        if kind:
            per_day[d][kind] += 1
            per_app.setdefault(row.get("connector", ""), Counter())[kind] += 1

    apps = []
    for c in Connectors(vault).list():
        last = next(iter(vault.activity(c.id, limit=1)), None)
        used = per_app.get(c.id, Counter())
        apps.append({"id": c.id, "name": c.name, "kind": c.kind, "last_seen": last["at"] if last else None,
                     "reads": used["reads"], "writes": used["writes"],
                     "read_topics": c.read_topics, "include_private": c.include_private, "write": c.write})
    apps.sort(key=lambda a: a["last_seen"] or "", reverse=True)

    safety = score.compute(vault, now=now)
    score.record(vault, safety, now=now)
    return {
        "first_run": not mems and not apps,
        "safety": {"score": safety["score"], "level": safety["level"], "week_change": safety["week_change"],
                   "top": [i for i in safety["issues"] if not i["dismissed"]][:3]},
        "waiting": waiting,
        "memories": {
            "total": len(mems),
            "private": sum(1 for m in mems if m.private or m.topic in private),
            "groups": groups,
            "growth": growth,
            "sources": dict(Counter(source_of(m.source) for m in mems)),
        },
        "apps": apps,
        "activity": [{"date": d.isoformat(), **per_day[d]} for d in week],
    }
