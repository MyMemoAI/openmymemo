import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from openmymemo import codex
from openmymemo.cli import app
from openmymemo.vault import VaultError

runner = CliRunner()

FOREIGN_START = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hello"}]}
FOREIGN_TOOL = {"matcher": "shell", "hooks": [{"type": "command", "command": "./check.sh"}]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path / "codex"


def fake_plugin(home, *versions):
    for v in versions:
        script = home / "plugins/cache/openmymemo/openmymemo" / v / "integrations/hooks/session-start.sh"
        script.parent.mkdir(parents=True)
        script.write_text('echo "$1 $2"\n')


def run_hook(home, command):
    env = {**os.environ, "CODEX_HOME": str(home)}
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env)


def ours(data):
    return [h for g in data["hooks"]["SessionStart"] for h in g["hooks"] if codex.is_ours(h)]


def test_hook_runs_the_newest_installed_plugin_and_never_fails(home):
    fake_plugin(home, "0.9.0", "0.10.0")
    out = run_hook(home, codex.hook_command())
    assert out.returncode == 0
    assert out.stdout.strip() == f"{home}/plugins/cache/openmymemo/openmymemo/0.10.0/ codex"

    empty = run_hook(home.parent / "no-codex", codex.hook_command())
    assert empty.returncode == 0 and empty.stdout == ""


def test_install_keeps_other_hooks_and_is_idempotent(home):
    path = home / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"SessionStart": [FOREIGN_START], "PreToolUse": [FOREIGN_TOOL]}}))

    assert codex.install(codex.hook_command(), path) is True
    data = json.loads(path.read_text())
    assert data["hooks"]["PreToolUse"] == [FOREIGN_TOOL]
    assert data["hooks"]["SessionStart"][0] == FOREIGN_START
    assert data["hooks"]["SessionStart"][1] == {"matcher": codex.MATCHER, "hooks": [codex.hook_entry(codex.hook_command())]}

    written = path.read_text()
    assert codex.install(codex.hook_command(), path) is False  # unchanged, so Codex's trust survives
    assert path.read_text() == written


def test_install_replaces_an_older_entry_in_place_and_drops_duplicates(home):
    path = home / "hooks.json"
    path.parent.mkdir()
    old = {"type": "command", "command": "sh /x/openmymemo/0.2.0/integrations/hooks/session-start.sh /x codex",
           "statusMessage": codex.STATUS_MESSAGE}
    path.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [old]}, FOREIGN_START, {"matcher": "startup", "hooks": [old]}]}}))

    codex.install(codex.hook_command(), path)
    groups = json.loads(path.read_text())["hooks"]["SessionStart"]
    assert groups[0] == {"matcher": codex.MATCHER, "hooks": [codex.hook_entry(codex.hook_command())]}
    assert groups[1:] == [FOREIGN_START]


def test_remove_takes_out_only_ours(home):
    path = home / "hooks.json"
    codex.install(codex.hook_command(), path)
    data = json.loads(path.read_text())
    data["hooks"]["SessionStart"].insert(0, FOREIGN_START)
    path.write_text(json.dumps(data))

    assert codex.remove(path) is True
    assert json.loads(path.read_text())["hooks"]["SessionStart"] == [FOREIGN_START]
    assert codex.remove(path) is False


def test_invalid_hooks_file_is_left_alone(home):
    path = home / "hooks.json"
    path.parent.mkdir()
    path.write_text("{ not json")
    with pytest.raises(VaultError):
        codex.install(codex.hook_command(), path)
    assert path.read_text() == "{ not json"

    out = runner.invoke(app, ["setup", "codex"])
    reason = "needs a POSIX shell" if os.name == "nt" else "isn't valid JSON"
    assert out.exit_code == 1 and reason in out.output
    assert path.read_text() == "{ not json"


@pytest.mark.skipif(os.name == "nt", reason="the Codex hook command needs a POSIX shell")
def test_cli_uses_the_plugin_when_installed_and_this_install_otherwise(home, tmp_path):
    vault_dir = tmp_path / "vault"
    out = runner.invoke(app, ["--vault", str(vault_dir), "setup", "codex"])
    assert out.exit_code == 0, out.output
    assert "/hooks" in out.output
    [entry] = ours(json.loads((home / "hooks.json").read_text()))
    assert f"--vault {vault_dir.resolve()}" in entry["command"] and "--connector codex" in entry["command"]

    fake_plugin(home, "0.2.1")
    out = runner.invoke(app, ["setup", "codex"])
    assert out.exit_code == 0, out.output
    [entry] = ours(json.loads((home / "hooks.json").read_text()))
    assert entry["command"] == codex.hook_command()

    out = runner.invoke(app, ["setup", "codex", "--remove"])
    assert out.exit_code == 0 and "Removed" in out.output
    assert json.loads((home / "hooks.json").read_text())["hooks"] == {}
