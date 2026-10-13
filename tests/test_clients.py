"""The AI app catalog and one-click connect (clients.py). Every write goes into a temporary home."""

import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import appfiles, clients, codex
from openmymemo.cli import app
from openmymemo.connectors import Connectors, Revoked
from openmymemo.snippets import command_for, stdio_command
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A Mac with nothing installed, in a temporary folder; the server and the CLI see only this."""
    home = tmp_path / "home"
    (home / "Applications").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    env = clients.Env(home=home, system="darwin", bin_dirs=(tmp_path / "bin",), app_dirs=(home / "Applications",))
    monkeypatch.setattr(clients.Env, "current", classmethod(lambda cls, home=None: env))
    monkeypatch.delenv("OPENMYMEMO_COMMAND", raising=False)
    return env


def fake_cli(env, name, script="", code=0):
    """An app's command that logs its arguments (and runs `script`, a line of Python, with them as `args`)."""
    path = env.bin_dirs[0] / name
    log = env.home / f"{name}.log"
    path.write_text(f"#!{sys.executable}\nimport json, sys\nargs = sys.argv[1:]\n"
                    f"open({str(log)!r}, 'a').write(json.dumps(args) + '\\n')\n{script}\nsys.exit({code})\n")
    path.chmod(0o755)
    return log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def entry_for(vault, app_id):
    return clients.get(app_id).entry(stdio_command(vault.root, app_id))


# ---- the command written into configs --------------------------------------------------------------------


def test_openmymemo_command_overrides_the_cli_when_it_exists(tmp_path, monkeypatch):
    launcher = tmp_path / "bin" / "openmymemo"
    monkeypatch.setenv("OPENMYMEMO_COMMAND", str(launcher))
    assert command_for(tmp_path / "v")[0] != str(launcher)  # a launcher that isn't there is ignored
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    assert command_for(tmp_path / "v") == [str(launcher), "--vault", str(tmp_path / "v")]
    assert stdio_command(tmp_path / "v", "cursor")[:4] == [str(launcher), "--vault", str(tmp_path / "v"), "mcp"]
    monkeypatch.setenv("OPENMYMEMO_COMMAND", "openmymemo")  # not absolute: ignored
    assert command_for(tmp_path / "v")[0] != "openmymemo"


# ---- JSON writers ----------------------------------------------------------------------------------------


def test_json_writer_changes_only_our_entry_keeps_a_backup_once_and_round_trips(tmp_path):
    path = tmp_path / "mcp.json"
    original = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    path.write_text(json.dumps(original, indent=4))
    path.chmod(0o600)
    entry = {"command": "/bin/omm", "args": ["mcp"]}

    assert clients.write_json_entry(path, ("mcpServers",), entry) is True
    data = json.loads(path.read_text())
    assert data == {"theme": "dark", "mcpServers": {"other": {"command": "x"}, "openmymemo": entry}}
    assert path.read_text().startswith('{\n    "theme"')  # the file's own indentation
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a file with secrets stays private
    backup = tmp_path / "mcp.json.openmymemo-backup"
    assert json.loads(backup.read_text()) == original

    assert clients.write_json_entry(path, ("mcpServers",), entry) is False  # idempotent
    assert clients.write_json_entry(path, ("mcpServers",), {**entry, "args": ["new"]}) is True
    assert json.loads(backup.read_text()) == original  # the backup is from before the first change only

    assert clients.remove_json_entry(path, ("mcpServers",)) is True
    assert json.loads(path.read_text()) == original
    assert clients.remove_json_entry(path, ("mcpServers",)) is False
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_json_writer_creates_a_missing_file_and_nested_keys(tmp_path):
    path = tmp_path / "deep" / "settings.json"
    assert clients.write_json_entry(path, ("mcp",), {"type": "local"}) is True
    assert json.loads(path.read_text()) == {"mcp": {"openmymemo": {"type": "local"}}}
    assert not (tmp_path / "deep" / "settings.json.openmymemo-backup").exists()  # nothing to back up
    assert clients.remove_json_entry(tmp_path / "missing.json", ("mcp",)) is False


def test_json_writer_refuses_comments_and_odd_shapes_and_leaves_them_alone(tmp_path):
    jsonc = tmp_path / "settings.json"
    jsonc.write_text('{\n  // my servers\n  "servers": {},\n}\n')
    with pytest.raises(clients.Refused):
        clients.write_json_entry(jsonc, ("servers",), {"command": "x"})
    with pytest.raises(clients.Refused):
        clients.remove_json_entry(jsonc, ("servers",))
    assert jsonc.read_text() == '{\n  // my servers\n  "servers": {},\n}\n'
    odd = tmp_path / "odd.json"
    odd.write_text('{"mcpServers": ["x"]}')
    with pytest.raises(clients.Refused):
        clients.write_json_entry(odd, ("mcpServers",), {"command": "x"})
    assert not list(tmp_path.glob("*.openmymemo-backup"))


def test_json_writer_follows_a_symlink_to_the_real_file(tmp_path):
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    clients.write_json_entry(link, ("mcpServers",), {"command": "x"})
    assert link.is_symlink() and json.loads(real.read_text())["mcpServers"]["openmymemo"] == {"command": "x"}


def test_the_temporary_file_is_private_from_the_start_and_cleaned_up_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    path.chmod(0o600)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)  # what the temporary file is born with is what counts
    appfiles.replace(path, '{"a": 1}')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == '{"a": 1}'
    with pytest.raises(UnicodeEncodeError):
        appfiles.replace(path, "\ud800")  # can't be written as UTF-8
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        appfiles.replace(path, '{"b": 2}')
    assert path.read_text() == '{"a": 1}' and sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


def test_plain_json_is_read_without_stripping_comments(vault, env, monkeypatch):
    big = {"projects": {f"/p/{i}": {"history": ["x" * 200] * 5} for i in range(200)},
           "mcpServers": {"openmymemo": {"command": "x"}}}
    (env.home / ".claude.json").write_text(json.dumps(big))  # Claude Code's state can run to megabytes
    path = env.home / ".config/zed/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('// mine\n{"context_servers": {"openmymemo": {"command": "x"}}}')
    stripped = []
    real = clients._strip_jsonc
    monkeypatch.setattr(clients, "_strip_jsonc", lambda text: stripped.append(len(text)) or real(text))
    assert clients.configured(clients.get("claude-code"), env) == {"command": "x"}
    assert stripped == []  # the slow path only for files that need it
    assert clients.configured(clients.get("zed"), env) == {"command": "x"}
    assert len(stripped) == 1


def test_jsonc_is_read_for_status():
    text = '{\n  // comment, with "quotes"\n  "a": "http://x", /* block */ "b": [1, 2,],\n}'
    assert json.loads(clients._strip_jsonc(text)) == {"a": "http://x", "b": [1, 2]}


# ---- Codex's TOML ----------------------------------------------------------------------------------------

CODEX_CONFIG = '''model = "gpt-5"  # mine

[mcp_servers.github]
command = "gh-mcp"
args = ["--stdio"]

# notes about the next table
[profiles.fast]
model = "gpt-5-mini"
'''


def test_toml_append_and_remove_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CODEX_CONFIG)
    entry = {"command": "/bin/omm", "args": ["--vault", "/v w/\"q\"", "mcp"]}
    assert clients.write_toml_entry(path, entry) is True
    text = path.read_text()
    assert text.startswith(CODEX_CONFIG)  # nothing above is touched
    data = tomllib.loads(text)
    assert data["mcp_servers"]["openmymemo"] == entry and data["mcp_servers"]["github"]["command"] == "gh-mcp"
    assert clients.write_toml_entry(path, entry) is False
    assert (tmp_path / "config.toml.openmymemo-backup").read_text() == CODEX_CONFIG

    assert clients.write_toml_entry(path, {**entry, "args": ["new"]}) is True  # an older entry is replaced
    assert tomllib.loads(path.read_text())["mcp_servers"]["openmymemo"]["args"] == ["new"]
    assert path.read_text().count("[mcp_servers.openmymemo]") == 1

    assert clients.remove_toml_entry(path) is True
    assert path.read_text() == CODEX_CONFIG
    assert clients.remove_toml_entry(path) is False


def test_toml_removes_our_table_from_the_middle_and_keeps_neighbours_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.openmymemo]\ncommand = "x"\n\n[mcp_servers.openmymemo.env]\nA = "1"\n\n'
                    '# about github\n[mcp_servers.github]\ncommand = "gh"\n')
    assert clients.remove_toml_entry(path) is True
    assert path.read_text() == '# about github\n[mcp_servers.github]\ncommand = "gh"\n'


def test_toml_refuses_what_it_cant_change_safely(tmp_path):
    inline = tmp_path / "inline.toml"
    inline.write_text('mcp_servers = { github = { command = "gh" } }\n')
    with pytest.raises(clients.Refused):
        clients.write_toml_entry(inline, {"command": "x"})
    assert inline.read_text() == 'mcp_servers = { github = { command = "gh" } }\n'
    broken = tmp_path / "broken.toml"
    broken.write_text("model = \n")
    with pytest.raises(clients.Refused):
        clients.write_toml_entry(broken, {"command": "x"})
    dotted = tmp_path / "dotted.toml"
    dotted.write_text('[mcp_servers]\nopenmymemo.command = "x"\n')
    with pytest.raises(clients.Refused):
        clients.remove_toml_entry(dotted)
    assert dotted.read_text() == '[mcp_servers]\nopenmymemo.command = "x"\n'


def test_toml_into_a_new_file(tmp_path):
    path = tmp_path / "codex" / "config.toml"
    assert clients.write_toml_entry(path, {"command": "x", "args": []}) is True
    assert path.read_text() == '[mcp_servers.openmymemo]\ncommand = "x"\nargs = []\n'
    assert clients.remove_toml_entry(path) is True and path.read_text() == ""


def test_toml_keeps_comments_after_our_table_at_the_end_of_the_file(tmp_path):
    path = tmp_path / "config.toml"
    tail = '# [profiles.work]\n# model = "o4"\n'
    path.write_text('model = "gpt-5"\n\n[mcp_servers.openmymemo]\ncommand = "old"\n' + tail)
    assert clients.write_toml_entry(path, {"command": "new"}) is True  # replaces our table
    assert tail in path.read_text()
    assert tomllib.loads(path.read_text())["mcp_servers"]["openmymemo"] == {"command": "new"}
    assert clients.remove_toml_entry(path) is True
    assert path.read_text() == 'model = "gpt-5"\n\n' + tail


# ---- connecting apps that keep a JSON config ----------------------------------------------------------------

FILE_APPS = [c.id for c in clients.CLIENTS if c.method == "file" and c.fmt != "toml"]


def servers_in(client, env):
    data = json.loads(client.config(env).read_text())
    for k in client.key:
        data = data[k]
    return data


@pytest.mark.parametrize("app_id", FILE_APPS)
def test_connect_into_an_empty_home_then_disconnect(vault, env, app_id):
    client = clients.get(app_id)
    result = clients.connect(vault, app_id, env)
    assert result.status == "done" and result.changed and result.restart
    assert result.path == str(client.config(env))
    assert servers_in(client, env) == {"openmymemo": entry_for(vault, app_id)}
    connector = Connectors(vault).get(app_id)
    assert connector.name == client.name and connector.kind == "stdio" and connector.write == "propose"
    row = clients.app_status(vault, client, env)
    assert row["connected"] and row["via"] == "config" and row["installed"] and not row["in_use"]

    again = clients.connect(vault, app_id, env)
    assert again.status == "done" and not again.changed and not again.restart

    off = clients.disconnect(vault, app_id, env)
    assert off.status == "done" and off.changed and off.restart
    assert servers_in(client, env) == {}
    assert Connectors(vault).list() == []
    row = clients.app_status(vault, client, env)
    assert not row["connected"] and row["revoked"]


@pytest.mark.parametrize("app_id", FILE_APPS)
def test_connect_keeps_everything_else_and_backs_up_once(vault, env, app_id):
    client = clients.get(app_id)
    path = client.config(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"theme": "dark", **clients._nest(client.key, {"github": {"command": "gh", "args": ["mcp"]}})}
    path.write_text(json.dumps(original, indent=2))

    assert clients.connect(vault, app_id, env).changed
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and servers_in(client, env)["github"] == {"command": "gh", "args": ["mcp"]}
    backup = path.with_name(path.name + ".openmymemo-backup")
    assert json.loads(backup.read_text()) == original

    clients.disconnect(vault, app_id, env)
    assert json.loads(path.read_text()) == original
    clients.connect(vault, app_id, env)
    assert json.loads(backup.read_text()) == original  # still the file from before the first change


@pytest.mark.parametrize("app_id", FILE_APPS)
def test_a_file_with_comments_gets_the_snippet_instead(vault, env, app_id):
    client = clients.get(app_id)
    path = client.config(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "// my settings\n" + json.dumps(clients._nest(client.key, {"github": {"command": "gh"}}))
    path.write_text(text)
    result = clients.connect(vault, app_id, env)
    assert result.status == "manual" and "comments" in result.message
    assert json.loads(result.snippet) == clients._nest(client.key, {"openmymemo": entry_for(vault, app_id)})
    assert result.path == str(path)
    assert path.read_text() == text and not path.with_name(path.name + ".openmymemo-backup").exists()
    assert Connectors(vault).list() == []  # nothing is connected until the person pastes it
    # an entry pasted by hand into a commented file still counts as connected
    path.write_text("// mine\n" + json.dumps(clients._nest(client.key, {"openmymemo": entry_for(vault, app_id)})))
    assert clients.app_status(vault, client, env)["connected"]
    off = clients.disconnect(vault, app_id, env)
    assert off.status == "manual" and "comments" in off.message


def test_connect_regrants_a_revoked_connector_and_keeps_an_existing_one(vault, env):
    cons = Connectors(vault)
    cons.create("Cursor", "stdio", connector_id="cursor")
    cons.update("cursor", write="auto")
    clients.connect(vault, "cursor", env)
    assert cons.get("cursor").write == "auto"  # kept as it was
    cons.remove("cursor")
    assert clients.app_status(vault, clients.get("cursor"), env)["connected"] is False  # entry there, access revoked
    clients.connect(vault, "cursor", env)
    assert cons.get("cursor").write == "propose"  # an explicit re-grant, with default permissions

    cons.remove("cursor")
    path = clients.get("zed").config(env)
    path.parent.mkdir(parents=True)
    path.write_text("// comments\n{}")
    cons.create("Zed", "stdio", connector_id="zed")
    cons.remove("zed")
    assert clients.connect(vault, "zed", env).status == "manual"
    assert cons.get("zed")  # pasting the snippet by hand must work, so a revoked app is re-granted


def test_cursor_ships_an_empty_list_of_servers(vault, env):
    path = env.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": []}')
    assert clients.disconnect(vault, "cursor", env).status == "done"
    assert clients.connect(vault, "cursor", env).changed
    assert json.loads(path.read_text()) == {"mcpServers": {"openmymemo": entry_for(vault, "cursor")}}


def test_a_null_list_of_servers_is_filled_in(vault, env, tmp_path):
    path = env.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": null, "theme": "dark"}')
    assert clients.disconnect(vault, "cursor", env).status == "done"
    assert clients.connect(vault, "cursor", env).changed
    assert json.loads(path.read_text()) == {"mcpServers": {"openmymemo": entry_for(vault, "cursor")}, "theme": "dark"}
    nested = tmp_path / "amp.json"
    nested.write_text('{"a": null}')
    assert clients.write_json_entry(nested, ("a", "b"), {"command": "x"}) is True
    assert json.loads(nested.read_text()) == {"a": {"b": {"openmymemo": {"command": "x"}}}}


def test_reconnecting_keeps_the_environment_the_person_gave_our_server(vault, env):
    path = env.home / ".cursor/mcp.json"
    path.parent.mkdir()
    proxy = {"HTTPS_PROXY": "http://proxy:3128"}
    path.write_text(json.dumps({"mcpServers": {"openmymemo": {"command": "/old/omm", "args": [], "env": proxy}}}))
    assert clients.connect(vault, "cursor", env).changed
    assert json.loads(path.read_text())["mcpServers"]["openmymemo"] == {**entry_for(vault, "cursor"), "env": proxy}
    assert not clients.connect(vault, "cursor", env).changed

    config = env.home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.openmymemo]\ncommand = "/old/omm"\n\n[mcp_servers.openmymemo.env]\nA = "1"\n\n'
                      '[mcp_servers.github]\ncommand = "gh"\n')
    assert clients.connect(vault, "codex", env).changed
    data = tomllib.loads(config.read_text())
    assert data["mcp_servers"]["openmymemo"] == {**entry_for(vault, "codex"), "env": {"A": "1"}}
    assert data["mcp_servers"]["github"] == {"command": "gh"}
    assert not clients.connect(vault, "codex", env).changed
    clients.disconnect(vault, "codex", env)
    assert tomllib.loads(config.read_text()) == {"mcp_servers": {"github": {"command": "gh"}}}


def test_an_unwritable_config_is_a_plain_error(vault, env):
    path = env.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.parent.chmod(0o500)
    try:
        if os.access(path.parent, os.W_OK):
            pytest.skip("running as a user who can write anywhere")
        result = clients.connect(vault, "cursor", env)
    finally:
        path.parent.chmod(0o700)
    assert result.status == "error" and "couldn't change Cursor's settings" in result.message
    assert path.read_text() == "{}"


def test_claude_desktop_with_the_extension_is_connected_without_a_config_entry(vault, env):
    support = env.home / "Library/Application Support/Claude"
    support.mkdir(parents=True)
    (support / "extensions-installations.json").write_text(json.dumps(
        {"extensions": {"local.mcpb.mymemo.openmymemo": {"id": "local.mcpb.mymemo.openmymemo",
                                                         "manifest": {"name": "openmymemo"}}}}))
    row = clients.app_status(vault, clients.get("claude-desktop"), env)
    assert row["connected"] and row["via"] == "plugin" and row["plugin"] == "the OpenMyMemo extension"
    result = clients.connect(vault, "claude-desktop", env)
    assert result.status == "done" and "extension" in result.message and not result.changed
    assert not (support / "claude_desktop_config.json").exists()  # no second copy of the server
    assert Connectors(vault).get("claude-desktop").name == "Claude Desktop"
    off = clients.disconnect(vault, "claude-desktop", env)
    assert off.status == "done" and any("Settings → Extensions" in s for s in off.steps)
    assert Connectors(vault).list() == []


def test_disconnecting_an_app_before_its_first_call_still_turns_it_away(vault, env):
    support = env.home / "Library/Application Support/Claude"
    support.mkdir(parents=True)
    (support / "extensions-installations.json").write_text(json.dumps(
        {"extensions": {"local.mcpb.mymemo.openmymemo": {"id": "local.mcpb.mymemo.openmymemo",
                                                         "manifest": {"name": "openmymemo"}}}}))
    assert clients.app_status(vault, clients.get("claude-desktop"), env)["connected"]  # no connector yet
    clients.disconnect(vault, "claude-desktop", env)
    with pytest.raises(Revoked):
        Connectors(vault).ensure("claude-desktop")
    row = clients.app_status(vault, clients.get("claude-desktop"), env)
    assert row["revoked"] and not row["connected"]
    assert clients.connect(vault, "claude-desktop", env).status == "done"  # connecting grants it again
    assert Connectors(vault).ensure("claude-desktop").name == "Claude Desktop"


def test_disconnecting_a_pasted_config_before_its_first_call_still_turns_it_away(vault, env):
    path = env.home / ".config/zed/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('// mine\n{"context_servers": {"openmymemo": {"command": "/x/omm", "args": []}}}\n')
    clients.disconnect(vault, "zed", env)
    with pytest.raises(Revoked):
        Connectors(vault).ensure("zed")


# ---- Claude Code: its own command -------------------------------------------------------------------------

CLAUDE_WRITES = (
    "import pathlib\n"
    "p = pathlib.Path.home() / '.claude.json'\n"
    "data = json.loads(p.read_text()) if p.exists() else {}\n"
    "if args[:2] == ['mcp', 'add']:\n"
    "    cmd = args[args.index('--') + 1:]\n"
    "    data.setdefault('mcpServers', {})['openmymemo'] = {'type': 'stdio', 'command': cmd[0], 'args': cmd[1:], 'env': {}}\n"
    "if args[:2] == ['mcp', 'remove']:\n"
    "    data.get('mcpServers', {}).pop('openmymemo', None)\n"
    "if args[:2] == ['mcp', 'add-json']:\n"
    "    data.setdefault('mcpServers', {})[args[4]] = json.loads(args[5])\n"
    "p.write_text(json.dumps(data))\n"
)


def test_claude_code_connects_through_its_own_command(vault, env):
    claude_json = env.home / ".claude.json"
    claude_json.write_text(json.dumps({"projects": {"/x": {}}, "mcpServers": {"github": {"command": "gh"}}}))
    log = fake_cli(env, "claude", CLAUDE_WRITES)
    cmd = stdio_command(vault.root, "claude-code")

    result = clients.connect(vault, "claude-code", env)
    assert result.status == "done" and result.restart and "new Claude Code session" in result.message
    assert calls(log) == [["mcp", "add", "--scope", "user", "--transport", "stdio", "openmymemo", "--", *cmd]]
    row = clients.app_status(vault, clients.get("claude-code"), env)
    assert row["connected"] and row["via"] == "config" and row["installed"]
    assert Connectors(vault).get("claude-code").name == "Claude Code"

    assert clients.connect(vault, "claude-code", env).changed is False  # already there: the command isn't run
    assert len(calls(log)) == 1

    off = clients.disconnect(vault, "claude-code", env)
    assert off.status == "done" and off.changed
    assert calls(log)[-1] == ["mcp", "remove", "openmymemo", "--scope", "user"]
    assert json.loads(claude_json.read_text())["mcpServers"] == {"github": {"command": "gh"}}


def test_claude_code_replaces_an_older_entry_and_falls_back_to_the_command(vault, env):
    (env.home / ".claude.json").write_text(json.dumps({"mcpServers": {"openmymemo": {"command": "/old/omm"}}}))
    log = fake_cli(env, "claude", CLAUDE_WRITES)
    assert clients.connect(vault, "claude-code", env).status == "done"
    assert [c[:2] for c in calls(log)] == [["mcp", "remove"], ["mcp", "add"]]

    log.unlink()
    fake_cli(env, "claude", "print('Error: something went wrong', file=sys.stderr)", code=1)
    (env.home / ".claude.json").write_text("{}")
    failed = clients.connect(vault, "claude-code", env)
    assert failed.status == "manual" and "something went wrong" in failed.message
    assert failed.snippet.startswith("claude mcp add --scope user --transport stdio openmymemo -- ")


def test_claude_code_reconnect_keeps_the_servers_environment(vault, env):
    old = {"type": "stdio", "command": "/old/omm", "args": [], "env": {"HTTPS_PROXY": "http://proxy:3128"}}
    claude_json = env.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"openmymemo": old}}))
    log = fake_cli(env, "claude", CLAUDE_WRITES)
    assert clients.connect(vault, "claude-code", env).status == "done"
    add = calls(log)[-1]
    assert add[:2] == ["mcp", "add"] and add[add.index("--env") + 1] == "HTTPS_PROXY=http://proxy:3128"
    assert add.index("--env") < add.index("openmymemo") < add.index("--")


def test_claude_code_keeps_the_old_entry_when_the_new_one_cant_be_added(vault, env):
    old = {"type": "stdio", "command": "/old/omm", "args": ["mcp"], "env": {"A": "1"}}
    claude_json = env.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"openmymemo": old}}))
    fails_to_add = CLAUDE_WRITES.replace("if args[:2] == ['mcp', 'add']:", "if args[:2] == ['mcp', 'add']:\n"
                                         "    print('Error: boom', file=sys.stderr); sys.exit(1)\nif False:")
    log = fake_cli(env, "claude", fails_to_add)
    result = clients.connect(vault, "claude-code", env)
    assert result.status == "manual" and "boom" in result.message
    assert "--env A=1" in result.snippet
    assert [c[:2] for c in calls(log)] == [["mcp", "remove"], ["mcp", "add"], ["mcp", "add-json"]]
    assert json.loads(claude_json.read_text())["mcpServers"]["openmymemo"] == old


def test_claude_code_without_its_command_gets_the_command_to_run(vault, env):
    result = clients.connect(vault, "claude-code", env)
    assert result.status == "manual" and "couldn't find the claude command" in result.message
    assert result.snippet == "claude mcp add --scope user --transport stdio openmymemo -- " + " ".join(
        __import__("shlex").quote(a) for a in stdio_command(vault.root, "claude-code"))
    assert not (env.home / ".claude.json").exists()  # never written by OpenMyMemo


def test_claude_code_plugin_counts_as_connected(vault, env):
    plugins = env.home / ".claude/plugins/installed_plugins.json"
    plugins.parent.mkdir(parents=True)
    plugins.write_text(json.dumps({"version": 2, "plugins": {"openmymemo@openmymemo": [
        {"scope": "user", "installPath": "/x", "version": "0.2.3"}]}}))
    row = clients.app_status(vault, clients.get("claude-code"), env)
    assert row["via"] == "plugin" and row["connected"]
    (env.home / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {"openmymemo@openmymemo": False}}))
    assert clients.app_status(vault, clients.get("claude-code"), env)["via"] is None
    plugins.write_text(json.dumps({"version": 2, "plugins": {"openmymemo@openmymemo": [
        {"scope": "project", "projectPath": "/p"}]}}))
    (env.home / ".claude/settings.json").unlink()
    assert clients.app_status(vault, clients.get("claude-code"), env)["via"] is None  # only in one project


# ---- Codex: TOML and the session hook ----------------------------------------------------------------------


def test_codex_connect_appends_the_table_and_installs_the_hook(vault, env):
    config = env.home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(CODEX_CONFIG)
    result = clients.connect(vault, "codex", env)
    assert result.status == "done" and result.changed
    assert "Restart Codex or the ChatGPT app" in result.message
    assert any("/hooks" in s and "Settings → Coding → Hooks" in s for s in result.steps)
    data = tomllib.loads(config.read_text())
    assert data["mcp_servers"]["openmymemo"] == entry_for(vault, "codex") and "github" in data["mcp_servers"]
    assert data["mcp_servers"]["openmymemo"]["startup_timeout_sec"] == 60  # room for the Mac app's first install
    hooks = json.loads((env.home / ".codex/hooks.json").read_text())
    handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert codex.is_ours(handler)
    assert handler["command"] == codex.hook_command(command_for(vault.root))
    assert clients.app_status(vault, clients.get("codex"), env)["connected"]

    again = clients.connect(vault, "codex", env)
    assert not again.changed and again.steps == []

    off = clients.disconnect(vault, "codex", env)
    assert off.status == "done" and off.changed
    assert config.read_text() == CODEX_CONFIG
    assert json.loads((env.home / ".codex/hooks.json").read_text())["hooks"] == {}


def test_codex_hooks_file_is_written_like_any_other_apps_file(vault, env, tmp_path):
    real = tmp_path / "dotfiles/hooks.json"
    real.parent.mkdir()
    original = {"hooks": {"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": "x"}]}]}}
    real.write_text(json.dumps(original))
    real.chmod(0o600)
    hooks = env.home / ".codex/hooks.json"
    hooks.parent.mkdir()
    hooks.symlink_to(real)

    clients.connect(vault, "codex", env)
    assert hooks.is_symlink() and codex.is_ours(json.loads(real.read_text())["hooks"]["SessionStart"][0]["hooks"][0])
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert json.loads((env.home / ".codex/hooks.json.openmymemo-backup").read_text()) == original
    clients.disconnect(vault, "codex", env)
    assert hooks.is_symlink() and json.loads(real.read_text())["hooks"] == original["hooks"]


def test_codex_with_the_plugin_only_gets_the_hook(vault, env):
    (env.home / ".codex/plugins/cache/openmymemo/openmymemo/0.2.3").mkdir(parents=True)
    result = clients.connect(vault, "codex", env)
    assert result.status == "done" and "plugin" in result.message
    assert not (env.home / ".codex/config.toml").exists()
    handler = json.loads((env.home / ".codex/hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler["command"] == codex.hook_command()  # the plugin-mode command
    off = clients.disconnect(vault, "codex", env)
    assert any("Plugins in Codex" in s for s in off.steps)


def test_codex_config_it_cant_change_safely_gets_the_snippet(vault, env):
    config = env.home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('mcp_servers = { github = { command = "gh" } }\n')
    result = clients.connect(vault, "codex", env)
    assert result.status == "manual" and result.snippet.startswith("[mcp_servers.openmymemo]")
    assert config.read_text() == 'mcp_servers = { github = { command = "gh" } }\n'
    assert not (env.home / ".codex/hooks.json").exists()


def test_codex_config_that_isnt_utf8_gets_the_snippet(vault, env):
    config = env.home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_bytes(b'model = "\xff"\n')
    result = clients.connect(vault, "codex", env)
    assert result.status == "manual" and result.snippet.startswith("[mcp_servers.openmymemo]")
    assert clients.disconnect(vault, "codex", env).status == "manual"
    assert config.read_bytes() == b'model = "\xff"\n'


@pytest.mark.parametrize("broken", ["latin-1", "directory"])
def test_a_broken_codex_hooks_file_doesnt_stop_connecting_or_disconnecting(vault, env, broken):
    hooks = env.home / ".codex/hooks.json"
    hooks.parent.mkdir()
    if broken == "directory":
        hooks.mkdir()
    else:
        hooks.write_bytes(b'{"hooks": {}, "note": "\xff"}')
    result = clients.connect(vault, "codex", env)
    assert result.status == "done" and any("session hook" in s for s in result.steps)
    assert "openmymemo" in tomllib.loads((env.home / ".codex/config.toml").read_text())["mcp_servers"]
    assert Connectors(vault).get("codex")  # granted, though the hook couldn't be added

    off = clients.disconnect(vault, "codex", env)
    assert off.status == "done" and any("session hook" in s for s in off.steps)
    assert Connectors(vault).list() == []  # revoked, though the hook couldn't be checked
    assert "mcp_servers" not in tomllib.loads((env.home / ".codex/config.toml").read_text())


# ---- detection ---------------------------------------------------------------------------------------------


def test_detection_finds_bundles_commands_in_bundles_and_config_folders(vault, env):
    apps = env.app_dirs[0]
    rows = {r["id"]: r for r in clients.catalog(vault, env)}
    assert not any(r["installed"] or r["connected"] for r in rows.values())
    assert rows["cursor"]["method"] == "file" and rows["cursor"]["restart_name"] == "Cursor"

    (apps / "Cursor.app").mkdir()
    (apps / "PyCharm CE.app").mkdir()
    code = apps / "Visual Studio Code.app/Contents/Resources/app/bin/code"
    codex_cli = apps / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    for exe in (code, codex_cli):
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    (env.home / ".gemini").mkdir()
    (env.home / ".gemini/settings.json").write_text("{}")
    fake_cli(env, "opencode")
    rows = {r["id"]: r for r in clients.catalog(vault, env)}
    found = {i for i, r in rows.items() if r["installed"]}
    assert found == {"cursor", "jetbrains", "vscode", "codex", "gemini-cli", "opencode"}
    assert clients.find_command(clients.get("vscode"), env) == code


def test_env_current_looks_beyond_the_shell_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".nvm/versions/node/v22.0.0/bin").mkdir(parents=True)
    env = clients.Env.current(tmp_path)
    dirs = [str(d) for d in env.bin_dirs]
    assert dirs[0] == "/usr/bin"
    for d in (tmp_path / ".local/bin", Path("/opt/homebrew/bin"), tmp_path / ".bun/bin",
              tmp_path / ".nvm/versions/node/v22.0.0/bin"):
        assert str(d) in dirs
    assert env.codex_home == tmp_path / ".codex"


def test_manual_apps_get_a_snippet_and_their_install_link(vault, env):
    goose = clients.connect(vault, "goose", env)
    assert goose.status == "manual" and goose.path.endswith(".config/goose/config.yaml")
    import yaml

    entry = yaml.safe_load(goose.snippet)["extensions"]["openmymemo"]
    assert entry["cmd"] == stdio_command(vault.root, "goose")[0] and entry["enabled"] is True
    assert Connectors(vault).list() == []

    trae = clients.connect(vault, "trae", env)
    assert trae.link["url"].startswith("trae://trae.ai-ide/mcp-import?type=stdio&name=openmymemo&config=")
    assert clients._no_spaces(["/Library/Application Support/x/openmymemo", "mcp"]) == {
        "command": "/bin/sh", "args": ["-c", 'exec "$0" "$@"', "/Library/Application Support/x/openmymemo", "mcp"]}

    cursor = clients.setup(vault, "cursor", env)
    assert cursor["link"]["url"].startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=openmymemo&config=")
    vscode = clients.setup(vault, "vscode", env)
    assert vscode["link"]["url"].startswith("vscode:mcp/install?%7B%22name%22%3A%20%22openmymemo%22")
    assert json.loads(vscode["snippet"])["servers"]["openmymemo"]["type"] == "stdio"

    other = clients.connect(vault, "other", env)
    assert other.status == "manual" and "--connector" in other.snippet and "my-app" in other.snippet
    with pytest.raises(clients.NotFound):
        clients.connect(vault, "nope", env)


def test_in_use_comes_from_activity(vault, env):
    clients.connect(vault, "cursor", env)
    row = clients.app_status(vault, clients.get("cursor"), env)
    assert not row["in_use"] and row["last_seen"] is None
    vault.log_activity("cursor", "context", memory_ids=[], chars=0)
    row = clients.app_status(vault, clients.get("cursor"), env)
    assert row["in_use"] and row["last_seen"]


def test_an_entry_whose_program_is_gone_is_broken(vault, env):
    path = env.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"openmymemo": {"command": "/gone/openmymemo", "args": []}}}))
    assert clients.app_status(vault, clients.get("cursor"), env)["broken"]
    clients.connect(vault, "cursor", env)
    assert not clients.app_status(vault, clients.get("cursor"), env)["broken"]


# ---- the panel API -----------------------------------------------------------------------------------------


@pytest.fixture
def api(vault, env):
    return TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")


def test_api_lists_connects_and_disconnects_apps(vault, env, api):
    assert api.get("/api/clients").status_code == 403  # the guard applies
    assert api.post("/api/clients/cursor/connect").status_code == 403
    Connectors(vault).create("My Tool", "stdio")
    data = api.get("/api/clients", headers=H).json()
    assert data["system"] == "darwin" and [c["id"] for c in data["custom"]] == ["my-tool"]
    cursor = next(a for a in data["apps"] if a["id"] == "cursor")
    assert cursor["connected"] is False and cursor["connector"] is None

    r = api.post("/api/clients/cursor/connect", headers=H).json()
    assert r["result"]["status"] == "done" and r["result"]["restart"]
    assert r["app"]["connected"] and r["app"]["connector"]["id"] == "cursor"
    assert r["app"]["connector"]["last_seen"] is None and "token_hash" not in r["app"]["connector"]
    assert (env.home / ".cursor/mcp.json").exists()

    setup = api.get("/api/clients/zed/setup", headers=H).json()
    assert setup["path"].endswith(".config/zed/settings.json") and "context_servers" in setup["snippet"]

    r = api.post("/api/clients/cursor/disconnect", headers=H).json()
    assert r["result"]["status"] == "done" and not r["app"]["connected"] and r["app"]["revoked"]
    assert api.get("/api/connectors", headers=H).json()[0]["id"] == "my-tool"

    for path in ("/api/clients/nope/connect", "/api/clients/nope/disconnect"):
        assert api.post(path, headers=H).status_code == 404
    assert api.get("/api/clients/nope/setup", headers=H).status_code == 404


def test_api_keeps_an_app_added_by_hand_as_other_manageable(vault, env, api):
    created = api.post("/api/connectors", headers=H, json={"name": "Other", "kind": "stdio"}).json()
    assert created["connector"]["id"] == "other"
    data = api.get("/api/clients", headers=H).json()
    assert [c["id"] for c in data["custom"]] == ["other"]
    assert api.delete("/api/connectors/other", headers=H).json() == {"ok": True}
    assert api.get("/api/clients", headers=H).json()["custom"] == []


# ---- the CLI -----------------------------------------------------------------------------------------------


def test_cli_apps_connect_and_disconnect(vault, env):
    (env.app_dirs[0] / "Cursor.app").mkdir()
    base = ["--vault", str(vault.root)]
    out = runner.invoke(app, [*base, "apps"])
    assert out.exit_code == 0
    first = out.stdout.splitlines()[1]
    assert first.startswith("Cursor (cursor)") and " yes " in first

    out = runner.invoke(app, [*base, "connect", "cursor"])
    assert out.exit_code == 0 and "Restart Cursor to finish" in out.stdout
    assert json.loads((env.home / ".cursor/mcp.json").read_text())["mcpServers"]["openmymemo"]

    out = runner.invoke(app, [*base, "connect", "goose"])
    assert out.exit_code == 0 and "extensions:" in out.stdout and "Restart Goose afterwards" in out.stdout

    out = runner.invoke(app, [*base, "disconnect", "cursor"])
    assert out.exit_code == 0 and "Disconnected Cursor" in out.stdout
    assert json.loads((env.home / ".cursor/mcp.json").read_text())["mcpServers"] == {}

    out = runner.invoke(app, [*base, "connect", "nope"])
    assert out.exit_code == 2 and "openmymemo apps" in out.stderr

    out = runner.invoke(app, [*base, "connector", "list"])  # the old commands still work
    assert out.exit_code == 0 and out.stdout == ""


def test_cli_connect_creates_a_missing_vault(tmp_path, env):
    folder = tmp_path / "fresh"
    out = runner.invoke(app, ["--vault", str(folder), "connect", "cursor"])
    assert out.exit_code == 0 and (folder / "memories").is_dir()
