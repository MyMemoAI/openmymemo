"""The AI apps OpenMyMemo knows: whether each one is on this computer, whether it is connected, and connecting
it with one action (product-design.md §15.1).

Each app is connected one of three ways:

* ``file``   OpenMyMemo edits the app's own MCP config at its fixed path. The file is parsed, only the
             ``openmymemo`` entry changes, it is written back atomically (keeping its permissions, through a
             symlink to the real file), and a one-time ``<file>.openmymemo-backup`` is kept before the first
             change. A file that isn't plain JSON (comments, a broken file) is never rewritten: the person gets
             the snippet to paste instead.
* ``cli``    OpenMyMemo runs the app's own ``mcp add`` command, with a timeout; on any failure the person gets
             the command to run instead. Claude Code's ``~/.claude.json`` is its live state and is never edited.
* ``manual`` a snippet to paste, plus an install link where the app has one.

An app is ``file`` or ``cli`` only once its config location and schema have been checked against its current
documentation; the URL is next to each entry. Everything that touches the computer goes through an ``Env``,
so tests point detection and every write at a temporary home.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

from . import codex
from .connectors import Connectors
from .snippets import command_for, cursor_link, stdio_command
from .vault import NotFound, Vault, VaultError

ENTRY = "openmymemo"  # the name of our server in every app's config
BACKUP_SUFFIX = ".openmymemo-backup"
CLI_TIMEOUT = 30  # seconds for an app's own `mcp add` / `mcp remove`

# Where command-line tools usually live. An app opened from the Finder (the Mac app's server) has no shell PATH.
BIN_DIRS = (".local/bin", "/opt/homebrew/bin", "/usr/local/bin", ".npm-global/bin", ".bun/bin", ".volta/bin",
            "Library/pnpm", ".local/share/pnpm", ".claude/local", ".opencode/bin", ".amp/bin",
            ".nvm/versions/node/*/bin", ".cargo/bin")


class Refused(VaultError):
    """A config file OpenMyMemo won't rewrite (comments, a broken file, an unexpected shape)."""


# ---- where to look -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    """The computer as the catalog sees it: a home folder, the OS, where commands and app bundles live."""

    home: Path
    system: str = sys.platform
    bin_dirs: tuple[Path, ...] = ()
    app_dirs: tuple[Path, ...] = ()
    app_data: Path | None = None  # Windows %APPDATA%; derived from `home` when None
    config_home: Path | None = None  # $XDG_CONFIG_HOME; ~/.config when None
    codex_dir: Path | None = None  # $CODEX_HOME; ~/.codex when None

    @classmethod
    def current(cls, home: Path | None = None) -> Env:
        home = (home or Path.home()).expanduser()
        dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        for d in BIN_DIRS:
            found = sorted(glob.glob(str(home / d))) if "*" in d else [str(home / d)]
            dirs += [Path(p) for p in reversed(found)]  # the newest nvm node first
        apps = (Path("/Applications"), home / "Applications") if sys.platform == "darwin" else ()
        env = os.environ
        return cls(home=home, system=sys.platform, bin_dirs=tuple(dict.fromkeys(dirs)), app_dirs=apps,
                   app_data=Path(env["APPDATA"]) if env.get("APPDATA") else None,
                   config_home=Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else None,
                   codex_dir=Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else None)

    @property
    def mac(self) -> bool:
        return self.system == "darwin"

    @property
    def windows(self) -> bool:
        return self.system == "win32"

    @property
    def appdata(self) -> Path:
        """Per-user application data: ~/Library/Application Support, %APPDATA% or ~/.config."""
        if self.mac:
            return self.home / "Library/Application Support"
        if self.windows:
            return self.app_data or self.home / "AppData/Roaming"
        return self.config

    @property
    def config(self) -> Path:
        """XDG config home, which several CLIs use on macOS too."""
        return self.config_home or self.home / ".config"

    @property
    def codex_home(self) -> Path:
        return self.codex_dir or self.home / ".codex"

    def which(self, name: str, extra: tuple[Path, ...] = ()) -> Path | None:
        path = os.pathsep.join(str(d) for d in (*self.bin_dirs, *extra))
        found = shutil.which(name, path=path) if path else None
        return Path(found) if found else None

    def bundle(self, name: str) -> Path | None:
        for d in self.app_dirs:
            hits = sorted(d.glob(name)) if any(ch in name for ch in "*?[") else [d / name]
            for p in hits:
                if p.is_dir():
                    return p
        return None

    def child_env(self, exe: Path) -> dict[str, str]:
        """Environment for an app's own CLI: our search path (node scripts need `node`) and this home."""
        path = os.pathsep.join(dict.fromkeys([str(exe.parent), *(str(d) for d in self.bin_dirs)]))
        return {**os.environ, "PATH": path, "HOME": str(self.home)}


# ---- the catalog ---------------------------------------------------------------------------------------


def plain(cmd: list[str]) -> dict:
    return {"command": cmd[0], "args": cmd[1:]}


@dataclass(frozen=True)
class Client:
    id: str  # also the connector id
    name: str
    method: str  # file | cli | manual
    docs: str  # the documentation this entry was checked against
    site: str | None = None  # where to get the app
    bundles: tuple[str, ...] = ()  # macOS app bundles (globs allowed)
    commands: tuple[str, ...] = ()  # executables
    bundle_bins: tuple[str, ...] = ()  # folders inside app bundles that hold its CLI
    dirs: tuple[str, ...] = ()  # folders under home (globs allowed) that mean the app is set up here
    config: Callable[[Env], Path] | None = None  # its MCP config file, also read for status
    fmt: str = "json"  # how to read `config`: json | jsonc | toml | yaml
    key: tuple[str, ...] = ("mcpServers",)  # where servers live in it
    entry: Callable[[list[str]], dict] = plain
    where: str = ""  # manual: where in the app to paste the snippet
    link: Callable[[list[str]], tuple[str, str]] | None = None  # (label, url) of a one-click install link
    snippet: Callable[[list[str]], str] | None = None  # what to paste, when it isn't the config entry
    add_args: Callable[[list[str]], list[str]] | None = None  # cli: the app's own `mcp add`
    remove_args: tuple[str, ...] = ()  # cli: the app's own `mcp remove`
    plugin: Callable[[Env], str | None] | None = None  # names our plugin when it is installed there
    plugin_where: str = ""  # where the person removes that plugin
    session: bool = False  # a CLI: "start a new session" rather than "restart the app"
    restart_as: str = ""  # what to restart, when it isn't `name`
    note: str = ""


OTHER = "other"  # the generic entry: any other MCP app, added by name


def display_name(app_id: str) -> str | None:
    """The app's name for a connector id, so a connector registered on first use isn't called `claude-desktop`."""
    return next((c.name for c in CLIENTS if c.id == app_id and c.id != OTHER), None)


def get(app_id: str) -> Client:
    for c in CLIENTS:
        if c.id == app_id:
            return c
    raise NotFound(f"Unknown app {app_id}")


# ---- reading and writing config files --------------------------------------------------------------------


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas → JSON. Only used to read status, never to write."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_config(client: Client, env: Env) -> dict | None:
    """The app's config as data, read leniently (comments allowed); None when missing or unreadable."""
    if client.config is None:
        return None
    try:
        text = client.config(env).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        if client.fmt == "toml":
            data = tomllib.loads(text)
        elif client.fmt == "yaml":
            data = yaml.safe_load(text)
        else:
            data = json.loads(_strip_jsonc(text)) if text.strip() else {}
    except (ValueError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _servers(data: dict | None, key: tuple[str, ...]) -> dict | None:
    node = data
    for k in key:
        node = node.get(k) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def configured(client: Client, env: Env) -> dict | None:
    """Our entry in the app's config, if it has one."""
    entry = (_servers(read_config(client, env), client.key) or {}).get(ENTRY)
    return entry if isinstance(entry, dict) else None


def _indent(text: str) -> int | str:
    for line in text.splitlines()[1:]:
        body = line.lstrip(" \t")
        if body and len(body) < len(line):
            ws = line[: len(line) - len(body)]
            return "\t" if ws.startswith("\t") else len(ws)
    return 2


def _replace(path: Path, text: str) -> None:
    """Atomically replace a config file, keeping its permissions; a symlink keeps pointing at the real file."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = None
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, target)


def _backup(path: Path) -> None:
    """Keep the file as it was before OpenMyMemo first changed it, once."""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not backup.exists():
        shutil.copy2(path.resolve(), backup)


def _load_json(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    except (OSError, UnicodeDecodeError) as e:
        raise Refused(f"OpenMyMemo can't read {path} ({e}).") from None
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise Refused(f"{path} has comments or isn't plain JSON, so OpenMyMemo won't rewrite it.") from None
    if not isinstance(data, dict):
        raise Refused(f"{path} doesn't look like a settings file, so OpenMyMemo won't rewrite it.")
    return data, text


def _node(data: dict, key: tuple[str, ...], path: Path, create: bool) -> dict | None:
    node = data
    for k in key:
        if create and (k not in node or node[k] == []):  # Cursor ships `"mcpServers": []`
            node[k] = {}
        node = node.get(k)
        if node is None or node == []:
            return None
        if not isinstance(node, dict):
            raise Refused(f"{path}: \"{k}\" isn't what OpenMyMemo expects there, so it won't rewrite the file.")
    return node


def write_json_entry(path: Path, key: tuple[str, ...], entry: dict) -> bool:
    """Set our server entry, changing nothing else. Returns whether the file changed."""
    data, text = _load_json(path)
    servers = _node(data, key, path, create=True)
    assert servers is not None
    if servers.get(ENTRY) == entry:
        return False
    servers[ENTRY] = entry
    _backup(path)
    _replace(path, json.dumps(data, indent=_indent(text), ensure_ascii=False) + "\n")
    return True


def remove_json_entry(path: Path, key: tuple[str, ...]) -> bool:
    """Take our server entry out, changing nothing else. Returns whether the file changed."""
    if not path.exists():
        return False
    data, text = _load_json(path)
    servers = _node(data, key, path, create=False)
    if servers is None or ENTRY not in servers:
        return False
    del servers[ENTRY]
    _backup(path)
    _replace(path, json.dumps(data, indent=_indent(text), ensure_ascii=False) + "\n")
    return True


# Codex keeps its settings in TOML. Python can read TOML but not write it, so we append our own table as text
# and take exactly that table out again, and check by parsing that nothing else changed.

_OUR_TABLE = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:openmymemo|"openmymemo")\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$')
_ANY_TABLE = re.compile(r"^\s*\[")


def _toml_value(v: object) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {_toml_value(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def toml_table(entry: dict) -> str:
    return "[mcp_servers.openmymemo]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in entry.items())


def _parse_toml(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"{path} isn't valid TOML ({e}), so OpenMyMemo won't change it.") from None


def _without_ours(data: dict) -> dict:
    data = json.loads(json.dumps(data, default=str))
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(ENTRY, None)
        if not servers:
            data.pop("mcp_servers")
    return data


def _cut_toml_table(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _OUR_TABLE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        end = i + 1
        while end < len(lines) and (not _ANY_TABLE.match(lines[end]) or _OUR_TABLE.match(lines[end])):
            end += 1
        # Comments after our last key belong to what follows (the next table, or notes at the end of the file);
        # the blank lines before them go with our table.
        k = end
        while k > i + 1 and (not lines[k - 1].strip() or lines[k - 1].lstrip().startswith("#")):
            k -= 1
        end = next((j for j in range(k, end) if lines[j].strip()), end)
        while out and not out[-1].strip() and (end >= len(lines) or not lines[end].strip()):
            out.pop()  # don't leave a double blank line where the table was
        i = end
    result = "".join(out)
    return result.rstrip("\n") + "\n" if result.strip() else ""


def write_toml_entry(path: Path, entry: dict) -> bool:
    """Append our ``[mcp_servers.openmymemo]`` table (replacing an older one). Returns whether the file changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    before = _parse_toml(path, text)
    current = (before.get("mcp_servers") or {}).get(ENTRY) if isinstance(before.get("mcp_servers"), dict) else None
    if current == entry:
        return False
    base = _cut_toml_table(text) if current is not None else text
    if _without_ours(_parse_toml(path, base)) != _without_ours(before):
        raise Refused(f"OpenMyMemo couldn't safely update the openmymemo entry in {path}.")
    sep = "" if not base.strip() else ("\n" if base.endswith("\n") else "\n\n")
    new = base + sep + toml_table(entry)
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        raise Refused(f"OpenMyMemo couldn't add its entry to {path} without changing its meaning.") from None
    if (after.get("mcp_servers") or {}).get(ENTRY) != entry or _without_ours(after) != _without_ours(before):
        raise Refused(f"OpenMyMemo couldn't add its entry to {path} without changing its meaning.")
    _backup(path)
    _replace(path, new)
    return True


def remove_toml_entry(path: Path) -> bool:
    """Take exactly our table out again. Returns whether the file changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    before = _parse_toml(path, text)
    servers = before.get("mcp_servers")
    if not isinstance(servers, dict) or ENTRY not in servers:
        return False
    new = _cut_toml_table(text)
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        after = None
    if after is None or ENTRY in (after.get("mcp_servers") or {}) or _without_ours(after) != _without_ours(before):
        raise Refused(f"OpenMyMemo couldn't take its entry out of {path} by itself.")
    _backup(path)
    _replace(path, new)
    return True


# ---- status --------------------------------------------------------------------------------------------


def _bundle_bins(client: Client, env: Env) -> tuple[Path, ...]:
    return tuple(d / rel for d in env.app_dirs for rel in client.bundle_bins)


def find_command(client: Client, env: Env) -> Path | None:
    for name in client.commands:
        if exe := env.which(name, _bundle_bins(client, env)):
            return exe
    return None


def installed(client: Client, env: Env) -> bool:
    if any(env.bundle(b) for b in client.bundles) or find_command(client, env):
        return True
    return any(glob.glob(str(env.home / d)) for d in client.dirs)


def _command_of(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k in ("command", "args", "cmd")}


def _gone(entry: dict) -> bool:
    """The entry runs a program by an absolute path that no longer exists (a moved app or a deleted venv)."""
    cmd = entry.get("command", entry.get("cmd"))
    exe = cmd[0] if isinstance(cmd, list) and cmd else cmd
    return isinstance(exe, str) and Path(exe).is_absolute() and not Path(exe).exists()


def _last_seen(vault: Vault) -> dict[str, str]:
    seen: dict[str, str] = {}
    for row in vault.activity(limit=10**9):  # newest first
        seen.setdefault(row.get("connector", ""), row.get("at"))
    return seen


def app_status(vault: Vault, client: Client, env: Env, known: dict | None = None,
               seen: dict[str, str] | None = None) -> dict:
    """One app as the panel and `openmymemo apps` show it."""
    known = known if known is not None else {c.id: c for c in Connectors(vault)._load()}
    seen = seen if seen is not None else _last_seen(vault)
    connector = known.get(client.id)
    active = connector is not None and not connector.revoked_at
    entry = configured(client, env)
    plugin = client.plugin(env) if client.plugin else None
    # Apps whose settings we can't read count as connected once their connector exists.
    via = "plugin" if plugin else "config" if entry else "connector" if active and client.config is None else None
    return {
        "id": client.id, "name": client.name, "method": client.method, "docs": client.docs, "site": client.site,
        "note": client.note or None, "session": client.session, "restart_name": client.restart_as or client.name,
        "installed": installed(client, env) or bool(via),
        "connected": bool(via) and (active or connector is None),
        "via": via, "plugin": plugin,
        "broken": bool(entry) and not plugin and _gone(entry),
        "revoked": bool(connector and connector.revoked_at),
        "in_use": active and client.id in seen,
        "last_seen": seen.get(client.id) if active else None,
        "has_connector": active,
    }


def catalog(vault: Vault, env: Env | None = None) -> list[dict]:
    env = env or Env.current()
    known = {c.id: c for c in Connectors(vault)._load()}
    seen = _last_seen(vault)
    return [app_status(vault, c, env, known, seen) for c in CLIENTS]


# ---- connect and disconnect ------------------------------------------------------------------------------


@dataclass
class Result:
    """What happened, for the panel and the CLI. `status`: done | manual (the person finishes it) | error."""

    status: str
    message: str
    changed: bool = False
    restart: bool = False  # the app has to be restarted (or a new session started) to pick it up
    path: str | None = None  # the file that was changed, or where the snippet goes
    snippet: str | None = None
    link: dict | None = None  # {"label", "url"}: a one-click install link
    steps: list[str] = field(default_factory=list)  # anything else the person has to do, in order

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "changed": self.changed, "restart": self.restart,
                "path": self.path, "snippet": self.snippet, "link": self.link, "steps": self.steps}


def _nest(key: tuple[str, ...], value: dict) -> dict:
    for k in reversed(key):
        value = {k: value}
    return value


def snippet(client: Client, cmd: list[str]) -> str:
    """What to paste into the app's config (or run), using this installation's absolute command."""
    if client.snippet:
        return client.snippet(cmd)
    entry = client.entry(cmd)
    if client.fmt == "toml":
        return toml_table(entry)
    if client.fmt == "yaml":
        return yaml.safe_dump(_nest(client.key, {ENTRY: entry}), sort_keys=False, allow_unicode=True)
    return json.dumps(_nest(client.key, {ENTRY: entry}), indent=2, ensure_ascii=False)


def _command(vault: Vault, client: Client) -> list[str]:
    # Apps added by hand get a connector id of the person's choosing; "my-app" is the placeholder.
    return stdio_command(vault.root, "my-app" if client.id == OTHER else client.id)


def setup(vault: Vault, app_id: str, env: Env | None = None) -> dict:
    """Instructions for doing it by hand: the snippet, where it goes, an install link, the app's docs."""
    client = get(app_id)
    env = env or Env.current()
    cmd = _command(vault, client)
    label_url = client.link(cmd) if client.link else None
    return {"id": client.id, "name": client.name, "snippet": snippet(client, cmd),
            "path": str(client.config(env)) if client.config and client.method != "cli" else None,
            "where": client.where or None, "docs": client.docs,
            "link": {"label": label_url[0], "url": label_url[1]} if label_url else None}


def _manual(vault: Vault, client: Client, env: Env, why: str) -> Result:
    s = setup(vault, client.id, env)
    return Result("manual", why, path=s["path"], snippet=s["snippet"], link=s["link"])


def _restart(client: Client) -> str:
    what = client.restart_as or client.name
    return f"Start a new {what} session to finish." if client.session else f"Restart {what} to finish."


def _grant(vault: Vault, client: Client, *, only_revoked: bool = False) -> None:
    """Create the app's connector with default permissions, or re-grant a revoked one; keep an existing one."""
    cons = Connectors(vault)
    current = next((c for c in cons._load() if c.id == client.id), None)
    if (current and not current.revoked_at) or (current is None and only_revoked):
        return
    cons.create(client.name, "stdio", connector_id=client.id)


def _run(exe: Path, args: list[str], env: Env) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=CLI_TIMEOUT,
                              stdin=subprocess.DEVNULL, env=env.child_env(exe))
    except (OSError, subprocess.SubprocessError):
        return None


def _failure(proc: subprocess.CompletedProcess | None) -> str:
    if proc is None:
        return "it didn't finish in time or couldn't start"
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1][:300] if lines else f"it exited with code {proc.returncode}"


def _cli_connect(vault: Vault, client: Client, env: Env, cmd: list[str]) -> Result:
    assert client.add_args is not None
    command = shlex.join([client.commands[0], *client.add_args(cmd)])
    current = configured(client, env)
    if current is not None and _command_of(current) == _command_of(client.entry(cmd)):
        return Result("done", f"{client.name} already has OpenMyMemo.")
    exe = find_command(client, env)
    if exe is None:
        return _manual(vault, client, env, f"OpenMyMemo couldn't find the {client.commands[0]} command. "
                                           "Run this in a terminal instead:")
    if current is not None:  # an older entry: the app's own `add` refuses to overwrite it
        _run(exe, list(client.remove_args), env)
    proc = _run(exe, client.add_args(cmd), env)
    if proc is None or proc.returncode != 0:
        return _manual(vault, client, env, f"`{command}` didn't work ({_failure(proc)}). "
                                           "Run it in a terminal instead:")
    return Result("done", f"Added OpenMyMemo to {client.name}.", changed=True, restart=True)


def _cli_disconnect(client: Client, env: Env) -> Result:
    if configured(client, env) is None:
        return Result("done", f"Disconnected {client.name}.")
    command = shlex.join([client.commands[0], *client.remove_args])
    exe = find_command(client, env)
    proc = _run(exe, list(client.remove_args), env) if exe else None
    if proc is None or proc.returncode != 0:
        why = f"didn't work ({_failure(proc)})" if exe else "needs the app's command, which OpenMyMemo couldn't find"
        return Result("manual", f"Removing it from {client.name} {why}. Run this in a terminal:", snippet=command)
    return Result("done", f"Disconnected {client.name}.", changed=True)


CODEX_TRUST = (f'Trust "{codex.STATUS_MESSAGE}" once: in the ChatGPT app under Settings → Coding → Hooks, or in a '
               "terminal: run `codex`, type /hooks and trust it under SessionStart. Either place covers both.")


def _codex_hook(vault: Vault, env: Env, result: Result, plugin: bool) -> None:
    """Codex loads memory at session start through a user-level hook (codex.py); install it next to the entry."""
    if env.windows:
        result.steps.append("Memory at session start needs a POSIX shell, so it isn't available on Windows yet; "
                            "Codex can still read your memory when it asks for it.")
        return
    try:
        changed = codex.install(codex.hook_command(None if plugin else command_for(vault.root)),
                                env.codex_home / "hooks.json")
    except VaultError as e:
        result.steps.append(f"The session hook wasn't added: {e}")
        return
    if changed:
        result.changed = True
        result.steps.append(CODEX_TRUST)


def connect(vault: Vault, app_id: str, env: Env | None = None) -> Result:
    """Connect an app: write its config (or run its own command), grant its connector, say what's left to do."""
    client = get(app_id)
    env = env or Env.current()
    cmd = _command(vault, client)
    if client.id == OTHER:
        return Result("manual", "Give the app a name of your own: `openmymemo connector add \"App name\"` (add --http "
                                "for an HTTP config with a token), or \"Add another app\" in the panel. Or paste this "
                                "into any app that runs MCP servers, with my-app changed to a short name for it:",
                      snippet=snippet(client, cmd))
    plugin = client.plugin(env) if client.plugin else None
    try:
        if plugin:
            result = Result("done", f"{client.name} is connected through {plugin}.")
        elif client.method == "file":
            assert client.config is not None
            path = client.config(env)
            changed = (write_toml_entry(path, client.entry(cmd)) if client.fmt == "toml"
                       else write_json_entry(path, client.key, client.entry(cmd)))
            result = Result("done", f"Added OpenMyMemo to {path}." if changed
                            else f"{client.name} already has OpenMyMemo.", changed=changed, path=str(path))
        elif client.method == "cli":
            result = _cli_connect(vault, client, env, cmd)
        else:
            where = client.where or (str(client.config(env)) if client.config else f"{client.name}'s MCP settings")
            result = _manual(vault, client, env, f"Paste this into {where}.")
    except Refused as e:
        result = _manual(vault, client, env, f"{e} Paste this in yourself instead:")
    except OSError as e:
        return Result("error", f"OpenMyMemo couldn't change {client.name}'s settings: {e.strerror or e}.",
                      path=e.filename and str(e.filename))
    if client.id == "codex" and result.status == "done":
        _codex_hook(vault, env, result, bool(plugin))
    if result.status == "done":
        _grant(vault, client)
        if result.changed:
            result.restart = True
            result.message += " " + _restart(client)
    else:
        _grant(vault, client, only_revoked=True)  # the pasted config must work; its first call registers it
        result.steps.append(_restart(client).replace("to finish", "afterwards"))
    return result


def disconnect(vault: Vault, app_id: str, env: Env | None = None) -> Result:
    """Take our entry out of the app's config (and Codex's hook), and revoke its connector."""
    client = get(app_id)
    env = env or Env.current()
    if client.id == OTHER:
        return Result("manual", "Apps added by name are removed one by one: `openmymemo connector remove <id>`, "
                                "or Remove in the panel.")
    result = Result("done", f"Disconnected {client.name}.")
    try:
        if client.method == "file" and client.config:
            path = client.config(env)
            result.changed = remove_toml_entry(path) if client.fmt == "toml" else remove_json_entry(path, client.key)
            result.path = str(path)
        elif client.method == "cli":
            result = _cli_disconnect(client, env)
        elif client.config and configured(client, env) is not None:
            path = client.config(env)
            result = Result("manual", f"Remove the \"{ENTRY}\" entry from {path} yourself.", path=str(path))
    except Refused as e:
        result = Result("manual", f"{e} Remove the \"{ENTRY}\" entry yourself.", path=result.path)
    except OSError as e:
        return Result("error", f"OpenMyMemo couldn't change {client.name}'s settings: {e.strerror or e}.",
                      path=e.filename and str(e.filename))
    if client.id == "codex" and not env.windows:
        try:
            result.changed = codex.remove(env.codex_home / "hooks.json") or result.changed
        except VaultError as e:
            result.steps.append(f"The session hook is still there: {e}")
    if plugin := (client.plugin(env) if client.plugin else None):
        where = f" ({client.plugin_where})" if client.plugin_where else ""
        result.steps.append(f"{client.name} still has {plugin}. Remove it there{where}; until then it asks for "
                            "your memory and is turned away.")
    try:
        Connectors(vault).remove(client.id)
    except NotFound:
        pass
    if result.changed:
        result.restart = True
        result.message += " " + _restart(client)
    return result


# ---- the catalog -----------------------------------------------------------------------------------------
# Checked against each app's documentation on 2026-10-12 (the URL above each entry). How our plugins show up
# on disk isn't documented anywhere; those checks were read from the apps themselves and fail closed.


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _claude_extension(env: Env) -> str | None:
    """The .mcpb extension in Claude Desktop: listed in extensions-installations.json under an id like
    ``local.mcpb.mymemo.openmymemo`` (Claude.app 2.31226; undocumented)."""
    exts = _json_file(env.appdata / "Claude/extensions-installations.json").get("extensions")
    for key, ext in (exts.items() if isinstance(exts, dict) else []):
        manifest = ext.get("manifest") if isinstance(ext, dict) else None
        if key.endswith(".openmymemo") or (isinstance(manifest, dict) and manifest.get("name") == ENTRY):
            return "the OpenMyMemo extension"
    return None


def _claude_code_plugin(env: Env) -> str | None:
    """The Claude Code plugin: an install record in ~/.claude/plugins/installed_plugins.json that the user's
    settings don't turn off (https://code.claude.com/docs/en/plugins/loading.md)."""
    plugins = _json_file(env.home / ".claude/plugins/installed_plugins.json").get("plugins")
    enabled = _json_file(env.home / ".claude/settings.json").get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    for key, installs in (plugins.items() if isinstance(plugins, dict) else []):
        if key.split("@")[0] != ENTRY or enabled.get(key) is False:
            continue
        rows = installs if isinstance(installs, list) else [installs]  # format 2: a list per scope; 1: one record
        if any(not isinstance(r, dict) or r.get("scope", "user") in ("user", "managed") for r in rows):
            return "the OpenMyMemo plugin"
    return None


def _codex_plugin(env: Env) -> str | None:
    cache = env.codex_home.joinpath(*codex.PLUGIN_CACHE)
    found = cache.is_dir() and any(p.is_dir() for p in cache.iterdir())
    return "the OpenMyMemo plugin" if found else None


def _json_or_jsonc(path: Path) -> Path:
    """`settings.json`, or the `.jsonc` the person keeps instead."""
    jsonc = path.with_suffix(".jsonc")
    return jsonc if jsonc.exists() and not path.exists() else path


def _no_spaces(cmd: list[str]) -> dict:
    """Trae can't run a command whose path has a space (the Mac app's launcher lives in "Application Support")."""
    if " " not in cmd[0]:
        return plain(cmd)
    return {"command": "/bin/sh", "args": ["-c", 'exec "$0" "$@"', *cmd]}


def _typed(cmd: list[str]) -> dict:
    return {"type": "stdio", "command": cmd[0], "args": cmd[1:]}


def _b64(entry: dict, url: bool = False) -> str:
    raw = json.dumps(entry, separators=(",", ":")).encode()
    return (base64.urlsafe_b64encode(raw) if url else base64.b64encode(raw)).decode()


CLIENTS: list[Client] = [
    # https://modelcontextprotocol.io/docs/develop/connect-local-servers (config path and schema)
    # https://claude.com/docs/connectors/building/mcpb.md (the .mcpb extension)
    Client("claude-desktop", "Claude Desktop", "file",
           docs="https://modelcontextprotocol.io/docs/develop/connect-local-servers",
           site="https://claude.ai/download", bundles=("Claude.app",),
           dirs=("Library/Application Support/Claude", "AppData/Roaming/Claude"),
           config=lambda e: e.appdata / "Claude/claude_desktop_config.json",
           plugin=_claude_extension, plugin_where="Settings → Extensions"),
    # https://code.claude.com/docs/en/mcp — `claude mcp add --scope user`; ~/.claude.json is its live state,
    # read here for status but only ever changed through its own command.
    Client("claude-code", "Claude Code", "cli", docs="https://code.claude.com/docs/en/mcp",
           site="https://code.claude.com/docs/en/setup", commands=("claude",), dirs=(".claude",),
           config=lambda e: e.home / ".claude.json", entry=_typed,
           add_args=lambda cmd: ["mcp", "add", "--scope", "user", "--transport", "stdio", ENTRY, "--", *cmd],
           remove_args=("mcp", "remove", ENTRY, "--scope", "user"),
           snippet=lambda cmd: shlex.join(["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", ENTRY,
                                           "--", *cmd]),
           plugin=_claude_code_plugin, plugin_where="/plugin in Claude Code", session=True),
    # https://learn.chatgpt.com/docs/extend/mcp?surface=cli and /docs/config-file/config-reference:
    # [mcp_servers.<name>] in $CODEX_HOME/config.toml, shared by the Codex CLI, the IDE extension and the
    # ChatGPT desktop app (bundle id com.openai.codex, which carries the CLI inside).
    Client("codex", "Codex", "file", docs="https://learn.chatgpt.com/docs/extend/mcp?surface=cli",
           site="https://chatgpt.com/download", commands=("codex",),
           bundle_bins=("ChatGPT.app/Contents/Resources/codex-cli/bin",), dirs=(".codex",),
           config=lambda e: e.codex_home / "config.toml", fmt="toml", key=("mcp_servers",),
           # Codex gives a server 10 s to start; the Mac app's launcher may first have to install it.
           entry=lambda cmd: {**plain(cmd), "startup_timeout_sec": 60},
           plugin=_codex_plugin, plugin_where="Plugins in Codex", restart_as="Codex or the ChatGPT app",
           note="also the ChatGPT app"),
    # https://cursor.com/docs/mcp and https://cursor.com/docs/mcp/install-links
    Client("cursor", "Cursor", "file", docs="https://cursor.com/docs/mcp", site="https://cursor.com/download",
           bundles=("Cursor.app",), commands=("cursor",), dirs=(".cursor",),
           config=lambda e: e.home / ".cursor/mcp.json",
           link=lambda cmd: ("Add to Cursor", cursor_link(ENTRY, plain(cmd)))),
    # https://code.visualstudio.com/docs/copilot/customization/mcp-servers and /reference/mcp-configuration:
    # the user profile's mcp.json (JSONC; a file with comments is left alone), `servers`, vscode:mcp/install.
    Client("vscode", "VS Code", "file", docs="https://code.visualstudio.com/docs/copilot/customization/mcp-servers",
           site="https://code.visualstudio.com/download", bundles=("Visual Studio Code.app",), commands=("code",),
           bundle_bins=("Visual Studio Code.app/Contents/Resources/app/bin",),
           dirs=("Library/Application Support/Code/User", ".vscode"),
           config=lambda e: e.appdata / "Code/User/mcp.json", fmt="jsonc", key=("servers",), entry=_typed,
           link=lambda cmd: ("Install in VS Code",
                             "vscode:mcp/install?" + quote(json.dumps({"name": ENTRY, **_typed(cmd)}))),
           note="Copilot agent mode"),
    # https://docs.devin.ai/desktop/cascade/mcp — Windsurf became Devin Desktop; its pages disagree on the
    # config path (~/.config/devin/mcp_config.json there, ~/.codeium/... in the FAQ), so it is pasted by hand.
    Client("windsurf", "Windsurf", "manual", docs="https://docs.devin.ai/desktop/cascade/mcp",
           site="https://windsurf.com", bundles=("Windsurf.app", "Devin.app"),
           dirs=(".codeium/windsurf", ".config/devin"), config=lambda e: e.config / "devin/mcp_config.json",
           where="Cascade's MCP settings (View raw config)", note="Devin Desktop"),
    # https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md (~/.gemini/settings.json)
    Client("gemini-cli", "Gemini CLI", "file",
           docs="https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md",
           site="https://github.com/google-gemini/gemini-cli", commands=("gemini",), dirs=(".gemini/settings.json",),
           config=lambda e: e.home / ".gemini/settings.json", fmt="jsonc", session=True),
    # https://zed.dev/docs/ai/mcp: `context_servers` with a plain command in settings.json (JSONC, usually
    # with comments, which OpenMyMemo never rewrites: then it's the snippet).
    Client("zed", "Zed", "file", docs="https://zed.dev/docs/ai/mcp", site="https://zed.dev/download",
           bundles=("Zed.app", "Zed Preview.app"), commands=("zed",), dirs=(".config/zed",),
           config=lambda e: (e.appdata / "Zed" if e.windows else e.config / "zed") / "settings.json", fmt="jsonc",
           key=("context_servers",)),
    # https://docs.cline.bot/mcp/adding-and-configuring-servers: ~/.cline/data/settings/cline_mcp_settings.json,
    # shared by the extension, the CLI and the JetBrains plugin since Cline 4.0 (CHANGELOG 4.0.0).
    Client("cline", "Cline", "file", docs="https://docs.cline.bot/mcp/adding-and-configuring-servers",
           site="https://cline.bot", commands=("cline",),
           dirs=(".cline", ".vscode/extensions/saoudrizwan.claude-dev-*"),
           config=lambda e: e.home / ".cline/data/settings/cline_mcp_settings.json"),
    # https://lmstudio.ai/docs/app/mcp (Cursor's notation, the deeplink) and https://lmstudio.ai/blog/mcp (the path)
    Client("lm-studio", "LM Studio", "file", docs="https://lmstudio.ai/docs/app/mcp", site="https://lmstudio.ai",
           bundles=("LM Studio.app",), commands=("lms",), dirs=(".lmstudio",),
           config=lambda e: e.home / ".lmstudio/mcp.json",
           link=lambda cmd: ("Add to LM Studio",
                             f"lmstudio://add_mcp?name={ENTRY}&config={quote(_b64(plain(cmd)))}")),
    # https://kiro.dev/docs/mcp/configuration (~/.kiro/settings/mcp.json) and https://kiro.dev/docs/mcp/servers
    Client("kiro", "Kiro", "file", docs="https://kiro.dev/docs/mcp/configuration", site="https://kiro.dev",
           bundles=("Kiro.app",), commands=("kiro", "kiro-cli"), dirs=(".kiro",),
           config=lambda e: e.home / ".kiro/settings/mcp.json",
           link=lambda cmd: ("Add to Kiro", f"https://kiro.dev/launch/mcp/add?name={ENTRY}"
                                            f"&config={quote(json.dumps(plain(cmd)))}")),
    # https://opencode.ai/docs/mcp-servers/ and /docs/config/: `mcp` in ~/.config/opencode/opencode.json (or
    # opencode.jsonc), a local server's command as one list.
    Client("opencode", "opencode", "file", docs="https://opencode.ai/docs/mcp-servers/", site="https://opencode.ai",
           bundles=("OpenCode.app",), commands=("opencode",), dirs=(".config/opencode",),
           config=lambda e: _json_or_jsonc(e.config / "opencode/opencode.json"), fmt="jsonc", key=("mcp",),
           entry=lambda cmd: {"type": "local", "command": cmd, "enabled": True}, session=True),
    # https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
    Client("copilot-cli", "GitHub Copilot CLI", "file",
           docs="https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers",
           site="https://github.com/features/copilot/cli", commands=("copilot",),
           dirs=(".copilot/mcp-config.json",), config=lambda e: e.home / ".copilot/mcp-config.json",
           entry=lambda cmd: {"type": "local", "command": cmd[0], "args": cmd[1:], "tools": ["*"]}, session=True),
    # https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/ (~/.qwen/settings.json, comments allowed)
    Client("qwen-code", "Qwen Code", "file", docs="https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/",
           site="https://github.com/QwenLM/qwen-code", commands=("qwen",), dirs=(".qwen",),
           config=lambda e: e.home / ".qwen/settings.json", fmt="jsonc", session=True),
    # https://ampcode.com/docs/customize/mcp and /docs/cli/settings: the flat key "amp.mcpServers".
    Client("amp", "Amp", "file", docs="https://ampcode.com/docs/customize/mcp", site="https://ampcode.com",
           commands=("amp",), dirs=(".config/amp",), config=lambda e: _json_or_jsonc(e.config / "amp/settings.json"),
           fmt="jsonc", key=("amp.mcpServers",), session=True),
    # https://goose-docs.ai/docs/guides/config-files: `extensions` in ~/.config/goose/config.yaml. OpenMyMemo
    # doesn't rewrite YAML (comments would be lost), and Goose's install link only runs a fixed list of commands.
    Client("goose", "Goose", "manual", docs="https://goose-docs.ai/docs/guides/config-files",
           site="https://goose-docs.ai", bundles=("Goose.app",), commands=("goose",), dirs=(".config/goose",),
           config=lambda e: (e.appdata / "Block/goose/config" if e.windows else e.config / "goose") / "config.yaml",
           fmt="yaml", key=("extensions",),
           entry=lambda cmd: {"type": "stdio", "name": ENTRY, "enabled": True, "cmd": cmd[0], "args": cmd[1:],
                              "timeout": 300}),
    # https://docs.trae.ai/ide/add-mcp-servers and /ide/mcp-server-install-links: no documented global file,
    # an official install link; the command may not contain spaces, so such a path runs through sh.
    Client("trae", "Trae", "manual", docs="https://docs.trae.ai/ide/add-mcp-servers", site="https://www.trae.ai",
           bundles=("Trae.app",), dirs=(".trae",), entry=lambda cmd: _no_spaces(cmd),
           where="Trae's MCP settings (Add → Configure Manually)",
           link=lambda cmd: ("Add to Trae", "trae://trae.ai-ide/mcp-import?type=stdio&name=" + ENTRY
                                            + "&config=" + quote(_b64(_no_spaces(cmd))))),
    # https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md: servers live in the app's database. The
    # install link is from its source (src/main/services/protocol/handlers/mcpInstall.ts, v2.1.6).
    Client("cherry-studio", "Cherry Studio", "manual", docs="https://docs.cherryai.com.cn/advanced-basic/extensions/mcp.md",
           site="https://www.cherry-ai.com", bundles=("Cherry Studio.app",),
           where="Settings → MCP → MCP Servers → Add → Import from JSON",
           link=lambda cmd: ("Add to Cherry Studio", "cherrystudio://mcp/install?servers="
                             + quote(_b64({"mcpServers": {ENTRY: {"type": "stdio", **plain(cmd)}}}, url=True)))),
    # https://www.jetbrains.com/help/ai-assistant/mcp.html: pasted into the IDE's settings; no file on disk.
    Client("jetbrains", "JetBrains AI Assistant", "manual", docs="https://www.jetbrains.com/help/ai-assistant/mcp.html",
           site="https://www.jetbrains.com/ai-assistant/",
           bundles=("IntelliJ IDEA*.app", "PyCharm*.app", "WebStorm.app", "GoLand.app", "PhpStorm.app", "RubyMine.app",
                    "CLion.app", "Rider.app", "RustRover.app", "DataGrip.app"),
           dirs=("Library/Application Support/JetBrains",),
           where="Settings | Tools | AI Assistant | Model Context Protocol (MCP) → Add (Global)"),
    # https://docs.warp.dev/agents/capabilities/mcp/ — the file-based ~/.warp/.mcp.json comes from Warp's source
    # (crates/warp_core/src/paths.rs) and Warp may hold outside edits for approval, so it is pasted by hand.
    Client("warp", "Warp", "manual", docs="https://docs.warp.dev/agents/capabilities/mcp/", site="https://www.warp.dev",
           bundles=("Warp.app",), dirs=(".warp",), config=lambda e: e.home / ".warp/.mcp.json",
           where="Warp's MCP servers page (Settings → MCP Servers → Add)",
           link=lambda cmd: ("Open Warp's MCP settings", "warp://settings/mcp")),
    # https://antigravity.google/docs/mcp — Google's codelabs name ~/.gemini/config/mcp_config.json, while
    # existing installs also have ~/.gemini/antigravity/mcp_config.json; pasted by hand until that settles.
    Client("antigravity", "Antigravity", "manual", docs="https://antigravity.google/docs/mcp",
           site="https://antigravity.google", bundles=("Antigravity.app",),
           dirs=(".gemini/antigravity",),
           where="the agent panel: … → MCP Servers → Manage MCP Servers → View raw config"),
]

CLIENTS.append(
    Client(OTHER, "Other MCP app", "manual", docs="https://modelcontextprotocol.io/docs/develop/connect-local-servers",
           snippet=lambda cmd: json.dumps({"mcpServers": {ENTRY: plain(cmd)}}, indent=2, ensure_ascii=False)))
