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


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_doctor_changes_nothing_in_the_folder(vault):
    vault.add("work", "Leads the Atlas migration")
    with (vault.memories_dir / "work.md").open("a", encoding="utf-8") as f:
        f.write("- Written in an editor, no id yet\n")  # Vault.sync() would add an id and record it
    before = snapshot(vault.root)
    checks = doctor.run(vault.root, 1, no_panel)
    assert snapshot(vault.root) == before
    assert "2 memories" in by_area(checks)["Memory folder"].message


def test_doctor_reports_damaged_files_instead_of_crashing(vault):
    vault.topics_path.write_text("groups: [")
    (vault.state_dir / "meta.json").write_text("{not json")
    result = runner.invoke(app, ["--vault", str(vault.root), "doctor", "--port", "1"])
    assert result.exit_code == 1
    assert "topics.yaml is damaged" in result.output and "meta.json is damaged" in result.output
    assert "groups: [" not in result.output and "not json" not in result.output


def test_doctor_reports_a_check_that_breaks(vault, monkeypatch):
    def broken(_vault):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(doctor, "apps", broken)
    checks = by_area(doctor.run(vault.root, 1, no_panel))
    assert checks["AI apps"].level == "fail" and "secret detail" not in checks["AI apps"].message


def test_doctor_doesnt_guess_the_folder_of_an_http_entry(tmp_path):
    custom = Vault.create(tmp_path / "custom")
    write_cursor({"url": "http://127.0.0.1:8787/mcp", "headers": {"Authorization": "Bearer x"}})
    assert doctor._entry_folder({"url": "http://127.0.0.1:8787/mcp"}) is None
    assert by_area(doctor.apps(custom))["Cursor"].level != "warn"


def test_doctor_rejects_state_files_the_vault_cant_use(vault):
    for path, text in ((vault.state_dir / "meta.json", "null"), (vault.state_dir / "meta.json", '{"memories": []}'),
                       (vault.topics_path, "- a list"), (vault.connectors_path, "connectors: {}")):
        good = path.read_text()
        path.write_text(text)
        assert by_area(doctor.run(vault.root, 1, no_panel))["Memory folder"].level == "fail", (path.name, text)
        path.write_text(good)
    vault.settings_path.write_text("")  # an empty YAML file reads as the defaults
    assert all(c.level != "fail" for c in doctor.folder(vault.root)[0])


def test_doctor_counts_what_the_vault_reads_as_topics(vault):
    vault.add("work", "Leads the Atlas migration")
    (vault.memories_dir / "notes.md").write_text("---\ntype: document\ntitle: Notes\n---\n\n- Not a memory item\n")
    (vault.memories_dir / "log.md").write_text("- Reserved name\n")
    (vault.memories_dir / "Bad Name.md").write_text("- Not a slug\n")
    topics, memories = len(vault.topics()), len(vault.memories())
    assert by_area(doctor.folder(vault.root)[0])["Memory folder"].message.endswith(
        f"{memories} memories in {topics} topics")
