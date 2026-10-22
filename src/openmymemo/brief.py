"""The daily brief (product-design.md §18.3): what happened in memory on one calendar day, built by rules.

Built from the activity log, the proposals, the Memory safety history and memory metadata. Each part is left out
when it has nothing to say, and ``quiet`` tells whether the day had anything to report (the notification is
skipped on quiet days, §18.4). Memories in private topics, or marked private, count but never show their text.
The resurfaced memory ("From your memory") is never private and doesn't make a day worth reporting.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from . import score
from .connectors import Connectors
from .overview import READS, local_day
from .proposals import Proposals
from .vault import NotFound, Vault, parse_until

APPLIED = ("applied", "accepted", "edited_accepted")
RESURFACE_AFTER = timedelta(days=30)  # a memory unchanged this long can come back as "From your memory"
RESURFACE_POOL = 20                   # picked among this many of the longest-unchanged memories
TOP_TOPICS = 3


def yesterday(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone().date() - timedelta(days=1)


def build(vault: Vault, day: date | None = None, *, now: datetime | None = None) -> dict:
    """The brief for ``day`` (local time; yesterday by default)."""
    now = now or datetime.now(UTC)
    day = day or yesterday(now)
    private_topics = vault.private_topics()
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    names = {c.id: c.name for c in Connectors(vault).list()}

    def is_private(memory_id: str | None, topic: str | None) -> bool:
        if topic in private_topics:
            return True
        try:
            return bool(memory_id) and vault.get(memory_id).private
        except NotFound:
            return False

    # 1. What AI apps learned: changes that applied that day, by app.
    learned: dict[str, dict] = {}
    props = Proposals(vault).list()
    for p in props:
        if p.status not in APPLIED or local_day(p.decided_at) != day:
            continue
        app = learned.setdefault(p.connector_id, {"app": p.connector_name, "changes": [], "private": 0})
        topic = p.final_topic or p.topic
        if is_private(p.applied_memory_id or p.memory_id, topic):
            app["private"] += 1
        else:
            app["changes"].append({"op": p.op, "memory_id": p.applied_memory_id or p.memory_id,
                                   "content": p.final_content or p.content, "topic": topic})

    # 2. What waits now.
    mems = vault.memories()
    today = now.astimezone().date()
    waiting = {
        "pending": sum(1 for p in props if p.status == "pending"),
        "review_due": sum(1 for m in mems if (until := parse_until(m.until)) and until <= today + timedelta(days=14)),
    }

    # 3. Who used memory that day: reads per app and the topics read most.
    topic_of = {m.id: m.topic for m in mems}
    reads: Counter = Counter()
    topics_read: dict[str, Counter] = defaultdict(Counter)
    for row in vault.activity(limit=100_000):
        if row.get("action") not in READS or local_day(row.get("at")) != day:
            continue
        who = row.get("connector", "")
        reads[who] += 1
        for mid in row.get("memory_ids") or []:
            if (topic := topic_of.get(mid)) and topic not in private_topics:
                topics_read[who][titles.get(topic, topic)] += 1
    used = [{"app": names.get(who, who), "reads": n, "topics": [t for t, _ in topics_read[who].most_common(TOP_TOPICS)]}
            for who, n in reads.most_common()]

    # 4. Memory safety: the change since the day before, from score.jsonl.
    lines = {r.get("date"): r.get("score") for r in score.history(vault, days=400)}
    before, after = lines.get((day - timedelta(days=1)).isoformat()), lines.get(day.isoformat())
    safety = ({"score": after, "change": after - before}
              if isinstance(before, int) and isinstance(after, int) and after != before else None)

    learned_rows = [a for a in learned.values() if a["changes"] or a["private"]]
    return {
        "date": day.isoformat(),
        "quiet": not learned_rows and not used and safety is None,
        "learned": learned_rows,
        "waiting": waiting,
        "used": used,
        "safety": safety,
        "resurface": _resurface(vault, mems, day, now, private_topics),
    }


def _resurface(vault: Vault, mems: list, day: date, now: datetime, private_topics: set[str]) -> dict | None:
    """One non-private memory unchanged for a while, the same one all day."""
    cutoff = (now - RESURFACE_AFTER).isoformat()
    old = sorted((m for m in mems if not (m.private or m.topic in private_topics) and m.updated_at
                  and m.updated_at < cutoff), key=lambda m: m.updated_at)[:RESURFACE_POOL]
    if not old:
        return None
    pick = old[int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16) % len(old)]
    return {"memory_id": pick.id, "content": pick.content, "topic": pick.topic, "updated_at": pick.updated_at}


def counts_line(brief: dict) -> str:
    """The notification's text: app names and counts only, never memory text (§18.4)."""
    parts = []
    for a in brief["learned"]:
        n = len(a["changes"]) + a["private"]
        parts.append(f"{a['app']} saved {n} {'memory' if n == 1 else 'memories'}")
    for u in brief["used"]:
        if not any(p.startswith(u["app"] + " ") for p in parts):
            parts.append(f"{u['app']} read {u['reads']}")
    text = ", ".join(parts) + "." if parts else "A quiet day."
    if (n := brief["waiting"]["pending"]):
        text += f" {n} {'change is' if n == 1 else 'changes are'} waiting for you."
    if brief["safety"]:
        text += f" Memory safety {brief['safety']['change']:+d}, now {brief['safety']['score']}."
    return text
