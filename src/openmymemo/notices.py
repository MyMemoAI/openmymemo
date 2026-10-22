"""Which notifications are due (product-design.md §18.4): the daily brief, a waiting reminder and safety alerts.

The desktop app asks for what's due on its own schedule, shows it, and says it was sent; ``.openmymemo/notify.json``
remembers what was, so each notification goes out once however many processes ask. Texts carry counts and app
names only, never memory text, so nothing personal shows on a locked screen.

* ``brief``: once a day, from the set time on (09:00 by default), when yesterday wasn't quiet. If the computer was
  off at that time it comes when the app next asks that day; a missed day is skipped, never piled up.
* ``waiting``: a change has waited more than WAIT_DAYS days for a decision; at most once a week.
* ``safety``: a secret pattern outside private topics, or an app able to read private topics, that wasn't there
  before; once per issue. The first time a folder is asked, what is already there is only noted (Home shows it).

The switches and the brief's time are the app's settings, per computer; they arrive here as arguments.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

from . import brief, score
from .proposals import Proposals
from .vault import Vault, _atomic_write, _read_json

KINDS = ("brief", "waiting", "safety")
WAIT_DAYS = 3
ALERTED = ("secret:", "app-private:")  # the safety issues worth interrupting for


def _path(vault: Vault):
    return vault.state_dir / "notify.json"


def _state(vault: Vault) -> dict:
    state = _read_json(_path(vault), {})
    return state if isinstance(state, dict) else {}


def _when(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when and when.tzinfo is None else when


def parse_time(value: str | None) -> time:
    try:
        return time.fromisoformat(value) if value else time(9)
    except ValueError:
        return time(9)


def due(vault: Vault, *, kinds: tuple[str, ...] = KINDS, brief_at: time = time(9),
        now: datetime | None = None) -> list[dict]:
    """The notifications due now, each ``{"id", "kind", "title", "body"}``; none is marked sent here."""
    now = now or datetime.now(UTC)
    local = now.astimezone()
    state = _state(vault)
    out = []

    if "brief" in kinds and local.time() >= brief_at:
        day = local.date() - timedelta(days=1)
        if state.get("brief") != day.isoformat():
            b = brief.build(vault, day, now=now)
            if not b["quiet"]:
                out.append({"id": f"brief:{day.isoformat()}", "kind": "brief",
                            "title": "Yesterday in your memory", "body": brief.counts_line(b)})

    if "waiting" in kinds:
        week = "{}-W{:02d}".format(*local.isocalendar()[:2])
        if state.get("waiting") != week:
            cutoff = now - timedelta(days=WAIT_DAYS)
            old = sum(1 for p in Proposals(vault).list("pending")
                      if (t := _when(p.created_at)) and t < cutoff)
            if old:
                out.append({"id": f"waiting:{week}", "kind": "waiting", "title": "Changes are waiting for you",
                            "body": f"{old} {'change has' if old == 1 else 'changes have'} waited more than "
                                    f"{WAIT_DAYS} days for your decision."})

    if "safety" in kinds:
        current = [i for i in score.compute(vault, now=now)["issues"]
                   if i["id"].startswith(ALERTED) and not i["dismissed"]]
        if "safety" not in state:  # the first time: note what is already there; Home shows it
            _seed(vault, [i["id"] for i in current])
        else:
            new = [i for i in current if i["id"] not in set(state["safety"])]
            if new:
                body = _safety_line(new[0]) if len(new) == 1 else f"{len(new)} new things to check on Home."
                out.append({"id": "safety:" + ",".join(sorted(i["id"] for i in new)), "kind": "safety",
                            "title": "Memory safety", "body": body})
    return out


def _safety_line(issue: dict) -> str:
    """A safety alert's text: the kind of secret or the app's name, never a topic title (it may be personal)."""
    if issue["id"].startswith("secret:"):
        kind = issue["id"].rsplit(":", 1)[-1]
        return f"A memory looks like it contains {score.KIND_NAMES.get(kind, 'something sensitive')}."
    return issue["message"]  # "<app> can read your private topics."


def sent(vault: Vault, notice_id: str) -> None:
    """Remember that a notification went out, so it isn't sent again."""
    kind, _, rest = notice_id.partition(":")
    if kind not in KINDS:
        raise ValueError(f"Unknown notification: {notice_id}")
    with vault.lock():  # read and write together, so two acknowledgements at once both stay
        state = _state(vault)
        if kind == "safety":
            state["safety"] = sorted(set(state.get("safety") or []) | set(rest.split(",")))
        else:
            state[kind] = rest
        _save(vault, state)


def _seed(vault: Vault, ids: list[str]) -> None:
    with vault.lock():
        state = _state(vault)
        if "safety" not in state:  # another request may have seeded or acknowledged meanwhile
            state["safety"] = ids
            _save(vault, state)


def _save(vault: Vault, state: dict) -> None:
    _atomic_write(_path(vault), json.dumps(state, indent=2, ensure_ascii=False) + "\n")
