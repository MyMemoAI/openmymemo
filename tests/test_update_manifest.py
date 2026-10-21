import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_manifest.py"
spec = importlib.util.spec_from_file_location("update_manifest", SCRIPT)
update_manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update_manifest)

CHANGELOG = """# Changelog

## 0.3.0 — 2026-10-22

- **Updates in the app.** It asks before installing.

## 0.2.9 — 2026-10-19

- Older.
"""


def test_signed_files_become_the_manifest_and_unsigned_ones_are_left_out(tmp_path):
    for name in ("OpenMyMemo-0.3.0-arm64.app.tar.gz", "OpenMyMemo-0.3.0-x64-setup.exe", "OpenMyMemo-0.3.0-x86_64.app.tar.gz"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "OpenMyMemo-0.3.0-arm64.app.tar.gz.sig").write_text("c2lnLWFybQ==\n")
    (tmp_path / "OpenMyMemo-0.3.0-x64-setup.exe.sig").write_text("c2lnLXdpbg==")
    data = update_manifest.manifest(tmp_path, "0.3.0", CHANGELOG, datetime(2026, 10, 22, 9, 30, tzinfo=UTC))
    assert data == {
        "version": "0.3.0",
        "notes": "- **Updates in the app.** It asks before installing.",
        "pub_date": "2026-10-22T09:30:00Z",
        "platforms": {
            "darwin-aarch64": {"signature": "c2lnLWFybQ==",
                               "url": "https://github.com/MyMemoAI/openmymemo/releases/download/v0.3.0/OpenMyMemo-0.3.0-arm64.app.tar.gz"},
            "windows-x86_64": {"signature": "c2lnLXdpbg==",
                               "url": "https://github.com/MyMemoAI/openmymemo/releases/download/v0.3.0/OpenMyMemo-0.3.0-x64-setup.exe"},
        },
    }
    assert update_manifest.notes(CHANGELOG, "0.2.9") == "- Older."
    assert update_manifest.notes(CHANGELOG, "9.9.9") == ""


def test_without_signed_files_no_manifest_is_written(tmp_path):
    (tmp_path / "OpenMyMemo-0.3.0-x64-setup.exe").write_bytes(b"x")
    out = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "0.3.0"], capture_output=True, text=True, check=True)
    assert "No signed update files" in out.stdout and not (tmp_path / "latest.json").exists()
    (tmp_path / "OpenMyMemo-0.3.0-x64-setup.exe.sig").write_text("c2ln")
    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), "0.3.0"], capture_output=True, text=True, check=True)
    assert list(json.loads((tmp_path / "latest.json").read_text())["platforms"]) == ["windows-x86_64"]
