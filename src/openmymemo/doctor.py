"""``openmymemo doctor``: check the setup and say what to fix, without reading any memory's text.

Each check is a :class:`Check` with a level: ``ok``, ``info`` (worth knowing, nothing wrong), ``warn``
(something probably doesn't work as the person expects) or ``fail`` (it doesn't work). The output is meant to
be pasted into an issue, so it shows paths and counts but never memory content, connector tokens or account
details beyond what ``openmymemo cloud status`` shows.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .vault import Vault

LEVELS = ("ok", "info", "warn", "fail")


@dataclass
class Check:
    area: str
    level: str  # ok | info | warn | fail
    message: str
    fix: str | None = None


def _install_kind() -> str:
    from .snippets import _from_uvx

    if os.environ.get("OPENMYMEMO_COMMAND"):
        return "the Mac app"
    if _from_uvx():
        return "uvx"
    if "uv/tools/openmymemo" in Path(sys.prefix).as_posix():
        return "uv tool install"
    return f"Python at {sys.prefix}"


def program() -> list[Check]:
    checks = [Check("OpenMyMemo", "ok", f"{__version__} via {_install_kind()}, Python {platform.python_version()}, "
                                        f"{platform.system()} {platform.release()} ({platform.machine()})")]
    uv = shutil.which("uv")
    if uv:
        checks.append(Check("uv", "ok", f"found at {uv}"))
    else:
        checks.append(Check("uv", "warn", "not found on PATH. The Claude Code, Codex and Gemini CLI plugins and "
                                          "`uvx openmymemo` start OpenMyMemo with uv.",
                            "Install it: https://docs.astral.sh/uv/getting-started/installation/"))
    return checks


def _count(vault: Vault) -> tuple[int, int]:
    """Topic files and memory lines, parsed from the files without `Vault.sync()`, which would rewrite them
    (adding ids, migrating old files): a diagnosis must not change what it diagnoses."""
    from .mdfile import MemoryFile

    topics = memories = 0
    for path in vault._topic_paths():  # the same files, and the same types, the vault reads as topics
        mf = MemoryFile(path.read_text(encoding="utf-8"))
        if mf.frontmatter_type in (None, "topic"):
            topics += 1
            memories += len(mf.items)
    return topics, memories


def folder(root: Path) -> tuple[list[Check], Vault | None]:
    vault = Vault(root)
    if not root.exists():
        return [Check("Memory folder", "fail", f"{root} doesn't exist.",
                      "Run `openmymemo init`, or pass the folder you use with --vault.")], None
    if not vault.is_initialized():
        return [Check("Memory folder", "fail", f"{root} isn't an OpenMyMemo folder yet.",
                      "Run `openmymemo init`, or pass the folder you use with --vault.")], None
    try:
        topics, count = _count(vault)
    except (OSError, UnicodeDecodeError, ValueError):
        return [Check("Memory folder", "fail", f"the topic files in {vault.memories_dir} can't be read.",
                      "Check that they are readable UTF-8 Markdown; `openmymemo status` names the problem.")], None
    checks = [Check("Memory folder", "ok", f"{root}: {count} memories in {topics} topics")]
    for path in (vault.topics_path, vault.connectors_path, vault.meta_path, vault.settings_path):
        if path.exists() and not _parses(path):
            checks.append(Check("Memory folder", "fail", f"{path.relative_to(root)} is damaged and can't be read.",
                                "Restore it from a backup or from git if you keep the folder there."))
    if not os.access(vault.memories_dir, os.W_OK):
        checks.append(Check("Memory folder", "fail", f"{vault.memories_dir} isn't writable, so nothing can be saved.",
                            "Check the folder's permissions."))
    return checks, (None if any(c.level == "fail" for c in checks) else vault)


# The top-level shape each state file must have for the vault to read it: a mapping, with these keys (when
# present) of these types. An empty YAML file is fine (it reads as defaults); JSON must be an object.
SHAPES = {"topics.yaml": {"groups": list}, "connectors.yaml": {"connectors": list}, "settings.yaml": {},
          "meta.json": {"memories": dict}}


def _parses(path: Path) -> bool:
    import json

    import yaml

    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return False
    if data is None:
        return path.suffix != ".json"
    if not isinstance(data, dict):
        return False
    return all(data.get(key) is None or isinstance(data[key], kind) for key, kind in SHAPES[path.name].items())


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def panel(root: Path, port: int, panel_for: Callable[[Path, int], str | None]) -> list[Check]:
    url = panel_for(root, port)
    if url:
        return [Check("Panel", "ok", f"running at {url}")]
    if _port_free(port):
        return [Check("Panel", "info", "not running. It runs while an AI app with OpenMyMemo or the Mac app is open.",
                      "Start it yourself with `openmymemo serve`.")]
    return [Check("Panel", "warn", f"port {port} is in use, but not by OpenMyMemo for this folder (another program, "
                                   "or OpenMyMemo for another folder).",
                  f"Close what uses it, or run `openmymemo serve --port {port + 1}`.")]


def _entry_folder(entry: dict) -> Path | None:
    """The memory folder an app's config entry starts OpenMyMemo with: --vault, else OPENMYMEMO_HOME, else the
    default. None for an HTTP entry, which names no folder."""
    from .vault import vault_path

    cmd = entry.get("command", entry.get("cmd"))
    if not cmd:
        return None  # an HTTP entry (`url`): the server it points at decides the folder
    args = [*(cmd if isinstance(cmd, list) else [cmd]), *(entry.get("args") or [])]
    args = [a for a in args if isinstance(a, str)]
    for i, a in enumerate(args):
        if a == "--vault" and i + 1 < len(args):
            return vault_path(args[i + 1])
        if a.startswith("--vault="):
            return vault_path(a.split("=", 1)[1])
    env = entry.get("env") or entry.get("environment") or {}
    return vault_path(env.get("OPENMYMEMO_HOME") if isinstance(env, dict) else None)


def apps(vault: Vault) -> list[Check]:
    from . import clients
    from .cloud import same_folder

    env = clients.Env.current()
    checks = []
    for row in clients.catalog(vault, env):
        if row["id"] == clients.OTHER or row["method"] == "account":
            continue
        name = row["name"]
        if row["broken"]:
            checks.append(Check(name, "fail", "its settings start OpenMyMemo from a program that no longer exists "
                                              "(a moved app or a deleted environment).",
                                f"Run `openmymemo connect {row['id']}` again."))
        elif row["connected"]:
            client = clients.get(row["id"])
            entry = clients.configured(client, env) if row["via"] == "config" else None
            other = _entry_folder(entry) if entry else None
            if other is not None and not same_folder(other, vault.root):
                checks.append(Check(name, "warn", f"connected to another memory folder, {other}.",
                                    f"To share this one, run `openmymemo --vault {vault.root} connect {row['id']}`."))
            elif row["in_use"]:
                checks.append(Check(name, "ok", f"connected{' (plugin)' if row['via'] == 'plugin' else ''}, "
                                                f"last used {(row['last_seen'] or '')[:16].replace('T', ' ')} UTC"))
            else:
                checks.append(Check(name, "info", "connected, but it hasn't used your memory yet.",
                                    f"Quit {row['restart_name']} completely and open it again."))
        elif row["revoked"]:
            checks.append(Check(name, "info", "access removed.", f"`openmymemo connect {row['id']}` gives it back."))
        elif row["installed"]:
            checks.append(Check(name, "info", "installed, not connected.", f"`openmymemo connect {row['id']}`"))
    if not checks:
        checks.append(Check("AI apps", "info", "no AI apps found on this computer.",
                            "`openmymemo apps` lists the ones OpenMyMemo can connect."))
    return checks


def cloud(vault: Vault) -> list[Check]:
    from . import cloud as cloud_mod

    link = cloud_mod.load_link()
    if link is None:
        return []
    if not link.serves(vault.root):
        return [Check("MyMemo Cloud", "warn", f"linked to another folder, {link.vault}.",
                      "Run `openmymemo cloud link` from this folder to move it.")]
    return [Check("MyMemo Cloud", "ok", "linked for this folder. `openmymemo cloud status` shows the connection.")]


def _guarded(area: str, check: Callable[[], list[Check]]) -> list[Check]:
    """One check that breaks (a damaged file, an app's unreadable settings) is reported, not a traceback that
    loses the whole report. The exception's text is left out: it can quote the file."""
    try:
        return check()
    except Exception as e:
        return [Check(area, "fail", f"couldn't be checked ({type(e).__name__}).",
                      "Run `openmymemo status` and `openmymemo apps` to see the error.")]


def run(root: Path, port: int, panel_for: Callable[[Path, int], str | None]) -> list[Check]:
    checks = program()
    found = _guarded("Memory folder", lambda: folder(root)[0])
    checks += found
    checks += _guarded("Panel", lambda: panel(root, port, panel_for))
    if not any(c.level == "fail" for c in found):
        vault = Vault(root)
        checks += _guarded("AI apps", lambda: apps(vault))
        checks += _guarded("MyMemo Cloud", lambda: cloud(vault))
    return checks
