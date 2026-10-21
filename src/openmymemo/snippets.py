"""Setup snippets and one-click install links for MCP clients.

Keys ending in ``_link`` are URLs the panel renders as install buttons; every other value is text to copy.
Commands use this installation's absolute path, or ``uvx openmymemo`` when it runs from uvx's cache.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

PANEL_PORT = 8787


def _from_uvx() -> bool:
    """Running from a throwaway environment of `uvx openmymemo` (uv's cache keeps those in ``archive-v0/<hash>``),
    not from `uv tool install`, a project's venv or the Mac app."""
    return Path(sys.prefix).parent.name.startswith("archive-v")


def command_for(vault_root: Path) -> list[str]:
    """The absolute command that starts this installation's CLI against this vault.

    ``OPENMYMEMO_COMMAND`` wins when it names an existing file by its absolute path: an executable that
    behaves exactly like the ``openmymemo`` CLI. The Mac app sets it to its launcher, so the configs it
    writes into other apps never point into a bundle or a venv that may move (product-design.md §15.2).
    """
    override = os.environ.get("OPENMYMEMO_COMMAND", "").strip()
    if override and Path(override).is_absolute() and Path(override).is_file():
        return [override, "--vault", str(vault_root)]
    if _from_uvx():  # its path is in uv's cache, which `uv cache clean` deletes: name uvx itself instead
        uvx, uv = shutil.which("uvx"), shutil.which("uv")
        if uvx:
            return [str(Path(uvx).resolve()), "openmymemo", "--vault", str(vault_root)]
        if uv:
            return [str(Path(uv).resolve()), "tool", "run", "openmymemo", "--vault", str(vault_root)]
    exe = shutil.which("openmymemo")
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.stem == "openmymemo" and argv0.exists():
        exe = str(argv0.resolve())
    base = [exe] if exe else [sys.executable, "-m", "openmymemo"]
    return [*base, "--vault", str(vault_root)]


def stdio_command(vault_root: Path, connector_id: str) -> list[str]:
    # --panel-port: the app's MCP process also serves the review panel when nothing else does.
    return [*command_for(vault_root), "mcp", "--connector", connector_id, "--panel-port", str(PANEL_PORT)]


def cursor_link(name: str, server: dict) -> str:
    config = base64.b64encode(json.dumps(server, separators=(",", ":")).encode()).decode()
    return f"cursor://anysphere.cursor-deeplink/mcp/install?name={name}&config={config}"


def stdio_snippets(vault_root: Path, connector_id: str) -> dict[str, str]:
    cmd = stdio_command(vault_root, connector_id)
    server = {"command": cmd[0], "args": cmd[1:]}
    json_cfg = json.dumps({"mcpServers": {"openmymemo": server}}, indent=2, ensure_ascii=False)
    home = Path.home()
    desktop = (home / "Library/Application Support/Claude/claude_desktop_config.json" if sys.platform == "darwin"
               else Path(os.environ.get("APPDATA", home)) / "Claude/claude_desktop_config.json")
    vscode = json.dumps({"name": "openmymemo", "type": "stdio", **server}, ensure_ascii=False)
    return {
        "cursor_link": cursor_link("openmymemo", server),
        "claude_code": "claude mcp add --scope user openmymemo -- " + shlex.join(cmd),
        "claude_desktop": f"// {desktop}\n{json_cfg}",
        "cursor": f"// {home / '.cursor/mcp.json'}\n{json_cfg}",
        "vscode": "code --add-mcp " + shlex.quote(vscode),
        "codex": "codex mcp add openmymemo -- " + shlex.join(cmd),
        "json": json_cfg,
    }


def http_snippets(url: str, token: str | None) -> dict[str, str]:
    bearer = token or "<your token>"
    headers = {"Authorization": f"Bearer {bearer}"}
    json_cfg = json.dumps({"mcpServers": {"openmymemo": {"type": "http", "url": url, "headers": headers}}}, indent=2)
    vscode = json.dumps({"name": "openmymemo", "type": "http", "url": url, "headers": headers})
    snippets = {
        "claude_code": f"claude mcp add --scope user --transport http openmymemo {url} "
                       f"--header {shlex.quote('Authorization: Bearer ' + bearer)}",
        "cursor": f"// {Path.home() / '.cursor/mcp.json'}\n{json_cfg}",
        "vscode": "code --add-mcp " + shlex.quote(vscode),
        "json": json_cfg,
    }
    if token:
        snippets = {"cursor_link": cursor_link("openmymemo", {"url": url, "headers": headers}), **snippets}
    return snippets
