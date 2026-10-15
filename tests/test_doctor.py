import json
import os
from pathlib import Path

from typer.testing import CliRunner

from openmymemo import doctor
from openmymemo.cli import app
from openmymemo.vault import Vault

runner = CliRunner()


def no_panel(root, port):
    return None


def by_area(checks):
    return {c.area: c for c in checks}


def test_doctor_checks_the_folder_without_printing_memories(vault):
    vault.add("health", "Allergic to penicillin")
    vault.add("work", "Leads the Atlas migration")
    result = runner.invoke(app, ["--vault", str(vault.root), "doctor", "--port", "1"])
    assert result.exit_code == 0, result.output
    assert f"{vault.root}: 2 memories in" in result.output
    assert "penicillin" not in result.output and "Atlas" not in result.output
    assert "OpenMyMemo" in result.output and "Panel" in result.output


def test_doctor_fails_for_a_folder_that_isnt_one(tmp_path):
    result = runner.invoke(app, ["--vault", str(tmp_path / "missing"), "doctor", "--port", "1"])
    assert result.exit_code == 1
    assert "doesn't exist" in result.output and "openmymemo init" in result.output
    (tmp_path / "empty").mkdir()
    checks = doctor.run(tmp_path / "empty", 1, no_panel)
    assert by_area(checks)["Memory folder"].level == "fail"


def test_doctor_reports_the_panel(vault):
    url = "http://127.0.0.1:8787"
    assert by_area(doctor.panel(vault.root, 8787, lambda root, port: url))["Panel"].message == f"running at {url}"


def write_cursor(entry: dict) -> None:
    path = Path(os.environ["HOME"]) / ".cursor/mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"openmymemo": entry}}))


def test_doctor_flags_an_app_connected_to_another_folder(vault, tmp_path):
    other = Vault.create(tmp_path / "other")
    write_cursor({"command": "/usr/bin/env", "args": ["openmymemo", "--vault", str(other.root), "mcp"]})
    cursor = by_area(doctor.apps(vault))["Cursor"]
    assert cursor.level == "warn" and str(other.root) in cursor.message
    assert f"--vault {vault.root} connect cursor" in cursor.fix
    # The same entry is fine for the folder it names.
    assert by_area(doctor.apps(other))["Cursor"].level == "info"  # connected, not used yet


def test_doctor_reads_the_folder_from_the_environment_or_the_default(vault):
    write_cursor({"command": "/usr/bin/env", "args": ["openmymemo", "mcp"], "env": {"OPENMYMEMO_HOME": str(vault.root)}})
    assert by_area(doctor.apps(vault))["Cursor"].level == "info"
    assert doctor._entry_folder({"command": "uvx", "args": ["openmymemo", "mcp"]}) == Path.home() / "OpenMyMemo"
    assert doctor._entry_folder({"command": ["x", "--vault=/a/b"]}) == Path("/a/b")


def test_doctor_flags_an_app_whose_program_is_gone(vault, tmp_path):
    write_cursor({"command": str(tmp_path / "deleted-venv/bin/openmymemo"), "args": ["--vault", str(vault.root), "mcp"]})
    cursor = by_area(doctor.apps(vault))["Cursor"]
    assert cursor.level == "fail" and cursor.fix == "Run `openmymemo connect cursor` again."
    result = runner.invoke(app, ["--vault", str(vault.root), "doctor", "--port", "1"])
    assert result.exit_code == 1 and "no longer exists" in result.output
