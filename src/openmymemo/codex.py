"""Codex: install the session-start hook that loads the user's memory into every new Codex session, and the
end-of-turn (``Stop``) hook that hands the session to the OpenMyMemo Assistant (product-design.md §17.7; it does
nothing unless the Assistant's session feature is on and the person allowed it for Codex).

Codex doesn't run hooks that plugins ship (as of 0.162 its ``plugin_hooks`` feature is marked removed), so
the hook goes into the user's own ``$CODEX_HOME/hooks.json``. Codex trusts a hook by a hash of its content,
so the entry we write never changes across plugin upgrades: in plugin mode the command finds the newest
installed plugin version when it runs instead of naming one. The user trusts it once, either in the ChatGPT
desktop app (Settings → Coding → Hooks) or with ``/hooks`` in the Codex CLI; both read the same config.
"""

from __future__ import annotations

import json
import os
import shlex
import tomllib
from pathlib import Path

from .appfiles import backup, replace
from .vault import VaultError

STATUS_MESSAGE = "Loading your OpenMyMemo memory"
MATCHER = "startup|clear|compact"
PLUGIN_CACHE = ("plugins", "cache", "openmymemo", "openmymemo")

# Resolved when the hook runs, so it keeps working, and stays trusted, after the plugin is upgraded.
# GUI-launched apps don't get ~/.local/bin on PATH, which is where uv usually lives. Never fails a session.
PLUGIN_COMMAND = (
    'r=$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/openmymemo/openmymemo/*/ 2>/dev/null'
    ' | sort -V | tail -n 1); '
    '[ -n "$r" ] && PATH="$HOME/.local/bin:$PATH" sh "${r}integrations/hooks/session-start.sh" "$r" codex; '
    "exit 0"
)


END_STATUS_MESSAGE = "Handing the session to OpenMyMemo"
# The same for the Stop hook; an older installed plugin without session-end.sh is skipped quietly.
PLUGIN_END_COMMAND = (
    'r=$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/openmymemo/openmymemo/*/ 2>/dev/null'
    ' | sort -V | tail -n 1); '
    '[ -n "$r" ] && [ -f "${r}integrations/hooks/session-end.sh" ] && PATH="$HOME/.local/bin:$PATH" '
    'sh "${r}integrations/hooks/session-end.sh" "$r" codex >/dev/null 2>&1; '
    "exit 0"
)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def hooks_path() -> Path:
    return codex_home() / "hooks.json"


def plugin_installed() -> bool:
    cache = codex_home().joinpath(*PLUGIN_CACHE)
    return cache.is_dir() and any(p.is_dir() for p in cache.iterdir())


def hook_command(cli: list[str] | None = None) -> str:
    """The plugin-mode command, or, given the CLI's argv (with ``--vault``), one that runs that CLI."""
    if cli is None:
        return PLUGIN_COMMAND
    run = shlex.join([*cli, "context", "--hook", "--connector", "codex"])
    return f"{run} 2>/dev/null; exit 0"


def end_command(cli: list[str] | None = None) -> str:
    """The Stop hook's command: plugin mode, or one that runs the given CLI. Its stdin (the hook's JSON) goes
    through; it prints nothing and never fails."""
    if cli is None:
        return PLUGIN_END_COMMAND
    run = shlex.join([*cli, "session-end", "--hook", "--connector", "codex"])
    return f"{run} >/dev/null 2>&1; exit 0"


def end_entry(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 15, "statusMessage": END_STATUS_MESSAGE}


def is_ours_end(handler: object) -> bool:
    return (isinstance(handler, dict) and handler.get("statusMessage") == END_STATUS_MESSAGE
            and "openmymemo" in str(handler.get("command", "")))


def hook_entry(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 30, "statusMessage": STATUS_MESSAGE,
            "additionalContextLimit": 12000}


def is_ours(handler: object) -> bool:
    return (isinstance(handler, dict) and handler.get("statusMessage") == STATUS_MESSAGE
            and "openmymemo" in str(handler.get("command", "")))


def _load(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise VaultError(f"{path} isn't valid JSON ({e}). Fix it, then run this again.") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise VaultError(f"{path} doesn't look like a Codex hooks file. Fix it, then run this again.")
    return data


def _save(path: Path, before: dict, after: dict) -> bool:
    if after == before:
        return False  # an unchanged file keeps its trust
    backup(path)
    replace(path, json.dumps(after, indent=2, ensure_ascii=False) + "\n")
    return True


def _place(data: dict, path: Path, event: str, entry: dict, mine, matcher: str | None) -> None:
    """Put ``entry`` in ``hooks.<event>``: in place of an existing one of ours (the first), dropping duplicates."""
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    if not isinstance(groups, list):
        raise VaultError(f"{path}: hooks.{event} should be a list. Fix it, then run this again.")
    placed = False
    for group in groups:
        handlers = group.get("hooks", []) if isinstance(group, dict) else []
        if not any(mine(h) for h in handlers):
            continue
        if matcher and all(mine(h) for h in handlers):
            group["matcher"] = matcher
        kept = []
        for h in handlers:
            if not mine(h):
                kept.append(h)
            elif not placed:
                kept.append(entry)
                placed = True
        group["hooks"] = kept
    data["hooks"][event] = [g for g in groups if not isinstance(g, dict) or g.get("hooks")]
    if not placed:
        data["hooks"][event].append({"matcher": matcher, "hooks": [entry]} if matcher else {"hooks": [entry]})


def install(command: str, path: Path | None = None, end_command: str | None = None) -> bool:
    """Add our SessionStart hook (and, given ``end_command``, our Stop hook), or update them where they already
    are. Returns whether the file changed.

    Other hooks are left as they are. An existing OpenMyMemo entry is replaced in place, so neighbouring
    hooks keep their positions (Codex keys trust on them); duplicates of ours are dropped.
    """
    path = path or hooks_path()
    before = _load(path)
    data = json.loads(json.dumps(before))
    _place(data, path, "SessionStart", hook_entry(command), is_ours, MATCHER)
    if end_command is not None:
        _place(data, path, "Stop", end_entry(end_command), is_ours_end, None)
    return _save(path, before, data)


def remove(path: Path | None = None) -> bool:
    """Take our hooks out again, leaving everything else. Returns whether the file changed."""
    path = path or hooks_path()
    before = _load(path)
    data = json.loads(json.dumps(before))
    hooks = data.get("hooks", {})
    for event, mine in (("SessionStart", is_ours), ("Stop", is_ours_end)):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict):
                group["hooks"] = [h for h in group.get("hooks", []) if not mine(h)]
        groups = [g for g in groups if not isinstance(g, dict) or g.get("hooks")]
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    return _save(path, before, data)


def hooks_in_config_toml() -> bool:
    """Whether ``config.toml`` also defines an OpenMyMemo hook (we don't edit TOML; we only warn)."""
    try:
        config = tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return False
    events = {k: v for k, v in config.get("hooks", {}).items() if k != "state"}
    return "openmymemo" in json.dumps(events)
