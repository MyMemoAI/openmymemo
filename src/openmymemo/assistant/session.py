"""Session-end extraction (product-design.md §17.7, "After a session").

The Claude Code plugin (``SessionEnd``) and the Codex hook (``Stop``, installed by ``openmymemo setup codex``) run
``openmymemo session-end --hook --connector <app>`` with the hook's JSON on stdin. ``hook()`` decides quickly and
never fails the host app: it prints nothing, returns, and leaves the slow part to a detached background process
(``openmymemo session-end --worker``), so the hook returns in well under a second.

It does something only when all of these hold:

* the Assistant is on and its ``session`` feature is on (off by default);
* the person allowed it for that app. Consent is per app and per computer, in ``assistant.json`` next to the
  Assistant's other settings (``session_apps``): transcripts are files on this computer, and so is the hook. The
  first time an app's session ends with the feature on, the app is recorded as ``asked`` and nothing is read; the
  panel (Settings → Assistant, and a note on the Changes page) and ``openmymemo assistant sessions`` ask the person.
  Nothing is read retroactively once they allow it;
* the transcript is where the app keeps them (``~/.claude`` or ``$CLAUDE_CONFIG_DIR``, ``$CODEX_HOME``) in the home
  folder, a ``.jsonl`` file within the size cap.

The worker reads the transcript locally (never stored, never copied), takes the messages since the last run for that
session (``session-state.json`` next to ``assistant.json`` keeps only counts and times per session, for 30 days),
leaves out what may not be sent (``chats.chunks``), sends at most ``MAX_CHARS`` of the newest of it to the model with
what the app already proposed during the session, and files each durable fact the app didn't save as the Assistant's
proposal (``Assistant.propose``) with the person's quoted words and date as evidence. Sensitive candidates are
dropped: the Assistant never writes into a private topic.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..vault import Vault, VaultError
from . import chats
from .core import Assistant, Data
from .guard import contains_any
from .settings import SESSION_APPS, _cloud_dir, ask_session_consent

log = logging.getLogger("openmymemo.assistant")

MAX_STDIN = 64_000
MAX_CHARS = 48_000  # of transcript text sent per run: the newest, in chunks of chats.CHUNK_CHARS
MIN_NEW_USER_MESSAGES = {"Stop": 3}  # Codex's Stop runs after every turn: wait for a few new messages
STATE_DAYS = 30
SOURCE = {"claude-code": "claude-code-sessions", "codex": "codex-sessions"}
APP_NAMES = {"claude-code": "Claude Code", "codex": "Codex"}


def state_path() -> Path:
    return _cloud_dir() / "session-state.json"


def transcript_roots(app: str) -> list[Path]:
    """Where ``app`` keeps its transcripts; a transcript path from a hook must be inside one of these."""
    home = Path.home()
    if app == "claude-code":
        roots = [home / ".claude"]
        if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
            roots.append(Path(os.environ["CLAUDE_CONFIG_DIR"].strip()).expanduser())
        return roots
    if app == "codex":
        from ..codex import codex_home

        return [codex_home()]
    return []


def safe_transcript(app: str, path: str) -> Path | None:
    """The transcript path a hook gave, if it is a ``.jsonl`` file in the app's own folder inside the home
    folder (resolved, so ``..`` and links can't point elsewhere); None otherwise."""
    if not isinstance(path, str) or not path.strip() or len(path) > 4096:
        return None
    real = os.path.normpath(os.path.realpath(os.path.expanduser(path.strip())))
    home = os.path.realpath(Path.home())
    if not real.startswith(home) or real[len(home):len(home) + 1] not in ("", os.sep):
        return None
    if not real.endswith(".jsonl") or not os.path.isfile(real):
        return None
    for root in transcript_roots(app):
        base = os.path.realpath(root)
        if real.startswith(base + os.sep):
            return Path(real)
    return None


def find_codex_transcript(session_id: str) -> Path | None:
    """Codex's session file for an id, when its hook didn't give a path: ``sessions/YYYY/MM/DD/rollout-…-<id>.jsonl``."""
    if not session_id or not all(c.isalnum() or c == "-" for c in session_id) or len(session_id) > 100:
        return None
    from ..codex import codex_home

    found = sorted((codex_home() / "sessions").glob(f"*/*/*/rollout-*{session_id}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def parse_hook_input(text: str) -> dict[str, Any] | None:
    """The hook's JSON object, or None."""
    if not text or len(text) > MAX_STDIN:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---- state ----------------------------------------------------------------------------------------------------


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    from ..cloud import _write_private

    cutoff = (datetime.now(UTC) - timedelta(days=STATE_DAYS)).isoformat()
    kept = {k: v for k, v in state.items() if isinstance(v, dict) and str(v.get("at", "")) >= cutoff}
    _write_private(state_path(), json.dumps(kept, indent=1) + "\n")


@contextlib.contextmanager
def _session_lock():
    """One worker at a time (Codex's Stop hook can fire again while the last one runs): the next one waits, then
    sees how far the last one got."""
    from ..vault import _flock

    folder = _cloud_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(folder / ".session.lock", "a+") as fh:
        _flock(fh, True)
        try:
            yield
        finally:
            _flock(fh, False)


# ---- the hook -------------------------------------------------------------------------------------------------


def hook(vault_root: Path, app: str, stdin_text: str, *, spawn: bool = True) -> str:
    """What ``session-end --hook`` does; returns a short outcome for tests and logs (never printed). Never raises."""
    try:
        return _hook(vault_root, app, stdin_text, spawn=spawn)
    except Exception as e:  # noqa: BLE001 - a hook must never break the host app
        log.debug("session-end hook: %s", type(e).__name__)
        return "error"


def _hook(vault_root: Path, app: str, stdin_text: str, *, spawn: bool) -> str:
    if app not in SESSION_APPS:
        return "unknown-app"
    data = parse_hook_input(stdin_text)
    if data is None:
        return "no-input"
    if data.get("stop_hook_active"):
        return "stop-active"  # a Stop hook running because of a Stop hook
    vault = Vault(vault_root)
    if not vault.is_initialized():
        return "no-vault"
    assistant = Assistant.from_settings(vault)
    if not assistant.available("session"):
        return "off"
    consent = ask_session_consent(app)
    if consent != "allowed":
        return consent or "not-asked"
    session_id = str(data.get("session_id") or "")[:200]
    path = safe_transcript(app, str(data.get("transcript_path") or ""))
    if path is None and app == "codex":
        path = find_codex_transcript(session_id)
    if path is None:
        return "no-transcript"
    event = str(data.get("hook_event_name") or "SessionEnd")[:40]
    args = ["--vault", str(vault_root), "session-end", "--worker", "--connector", app, "--transcript", str(path),
            "--session", session_id or path.stem, "--event", event]
    if not spawn:
        return "spawn:" + json.dumps(args)
    subprocess.Popen([sys.executable, "-m", "openmymemo", *args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True, cwd=str(Path.home()))
    return "spawned"


# ---- the worker -----------------------------------------------------------------------------------------------


def work(vault: Vault, app: str, transcript: Path, session_id: str, event: str = "SessionEnd",
         assistant: Assistant | None = None) -> dict[str, Any]:
    """Read one transcript and file what's worth keeping. Returns counts (never any text). Never raises."""
    try:
        return _work(vault, app, transcript, session_id, event, assistant)
    except Exception as e:  # noqa: BLE001 - background work: nothing to report to
        log.warning("session-end: failed (%s)", type(e).__name__)
        return {"result": "error"}


def _work(vault: Vault, app: str, transcript: Path, session_id: str, event: str,
          assistant: Assistant | None) -> dict[str, Any]:
    assistant = assistant or Assistant.from_settings(vault)
    if app not in SESSION_APPS or not assistant.available("session"):
        return {"result": "off"}
    if assistant.settings.session_apps.get(app) != "allowed":
        return {"result": "no-consent"}
    path = safe_transcript(app, str(transcript))
    if path is None:
        return {"result": "no-transcript"}
    key = f"{app}:{session_id}"[:240]
    with _session_lock():
        # The lock may have waited for another worker: the settings and the consent are read again.
        assistant = Assistant.from_settings(vault)
        if not assistant.available("session"):
            return {"result": "off"}
        if assistant.settings.session_apps.get(app) != "allowed":
            return {"result": "no-consent"}
        conv = chats.read_transcript(SOURCE[app], path)
        if conv is None:
            return {"result": "empty"}
        state = _load_state()
        seen = int((state.get(key) or {}).get("messages") or 0)
        new = conv.messages[seen:] if seen <= len(conv.messages) else conv.messages
        users = sum(1 for m in new if m.role == "user")
        if users < MIN_NEW_USER_MESSAGES.get(event, 1):
            return {"result": "too-little", "new_user_messages": users}
        conv.messages = new
        filed = _extract_and_file(vault, assistant, app, conv)
        if filed["result"] == "stopped" or (filed["calls"] and filed["failed"] == filed["calls"]):
            return filed  # the model couldn't be reached: the next run tries these messages again
        state[key] = {"messages": seen + len(new), "at": datetime.now(UTC).isoformat(timespec="seconds")}
        _save_state(state)
        return filed


def _already_proposed(vault: Vault, assistant: Assistant, app: str, since: str | None) -> list[str]:
    """What ``app`` (and the Assistant, for this session) proposed since the session began, that may be sent."""
    from ..proposals import Proposals

    g = assistant.guard()
    if g is None:
        return []
    withheld = g.withheld()[2]
    out = []
    for p in Proposals(vault).list():
        if p.connector_id not in (app, "assistant") or (since and p.created_at < since):
            continue
        text = p.final_content or p.content
        topic = p.final_topic or p.topic
        if text and topic and g.topic_ok(topic) and not contains_any(text, withheld):
            out.append(text)
    return out[:100]


def _extract_and_file(vault: Vault, assistant: Assistant, app: str, conv: chats.Conversation) -> dict[str, Any]:
    started = next((m.at for m in conv.messages if m.at), None)
    proposed = _already_proposed(vault, assistant, app, started)
    if not assistant.screen("session", chats.original_text(conv)):
        return {"result": "refused", "calls": 0, "failed": 0}  # it holds something that may not be sent
    all_chunks = chats.chunks([conv], forbidden=chats.forbidden_texts(assistant))
    # chunks() puts the newest part first: within the cap, a long session's latest part is what is read.
    todo, size = [], 0
    for ch in all_chunks:
        if size + ch.chars > MAX_CHARS and todo:
            break
        todo.append(ch)
        size += ch.chars
    extra = [Data("\n".join(f"- {chats.mask_secrets(t)}" for t in proposed), label="already saved")] if proposed else []
    topic_list = chats.targets(vault, assistant)
    found: list[chats.Candidate] = []
    calls = failed = 0
    planned = chats.destination(assistant)
    stopped = False
    for ch in todo:
        current = chats.live_assistant(assistant, "session", planned)  # re-read before every call
        if current is None or current.settings.session_apps.get(app) != "allowed":
            stopped = True
            break
        got = chats.extract(current, "session", ch, topic_list, extra=extra)
        calls += 1
        if got is None:
            failed += 1
            continue
        found.extend(c for c in got if not c.sensitive)  # the Assistant never writes into a private topic
    if not stopped:  # once more right before filing: a change during the last call counts too
        current = chats.live_assistant(assistant, "session", planned)
        stopped = current is None or current.settings.session_apps.get(app) != "allowed"
        assistant = current or assistant
    if stopped:  # turned off, consent withdrawn or the endpoint changed meanwhile: nothing is filed
        return {"result": "stopped", "calls": calls, "failed": failed}
    filed = {"applied": 0, "pending": 0, "duplicate": 0, "refused": 0}
    name = APP_NAMES.get(app, app)
    for c in chats.unique(found, against=proposed, at=0.6):
        key = "session-" + _digest(f"{app}|{conv.id}|{c.content}")
        try:
            out = assistant.propose("add", content=c.content, topic=c.topic, kind=c.kind,
                                    reason=f"Said in a {name} session; {name} didn't save it.",
                                    evidence=c.evidence, idempotency_key=key)
        except VaultError:  # removed, read-only, a blocked topic, …
            filed["refused"] += 1
            continue
        status = out.get("status")
        filed[status if status in filed else "pending"] += 1
    return {"result": "ok", "calls": calls, "failed": failed, **filed}


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:20]


def consent_rows(settings_apps: Mapping[str, str]) -> list[dict[str, Any]]:
    return [{"app": a, "name": APP_NAMES[a], "answer": settings_apps.get(a)} for a in SESSION_APPS]

