"""Write latest.json, the update manifest the desktop app checks (desktop/src-tauri/src/updates.rs).

    python3 scripts/update_manifest.py dist 0.3.0

Reads the signed update files the release builds left in `dist` (each with its `.sig` from Tauri's updater
key): OpenMyMemo-<version>-arm64.app.tar.gz and -x86_64.app.tar.gz for Macs, OpenMyMemo-<version>-x64-setup.exe
for Windows. Platforms without a signed file are left out, and without any, no manifest is written. The notes
are the version's section of CHANGELOG.md.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD = "https://github.com/MyMemoAI/openmymemo/releases/download/v{version}/{name}"
PLATFORMS = {
    "darwin-aarch64": "OpenMyMemo-{version}-arm64.app.tar.gz",
    "darwin-x86_64": "OpenMyMemo-{version}-x86_64.app.tar.gz",
    "windows-x86_64": "OpenMyMemo-{version}-x64-setup.exe",
}


def notes(changelog: str, version: str) -> str:
    """The changelog's section for `version`, without its heading."""
    match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    return match.group(1).strip() if match else ""


def manifest(dist: Path, version: str, changelog: str, now: datetime) -> dict | None:
    platforms = {}
    for platform, pattern in PLATFORMS.items():
        name = pattern.format(version=version)
        sig = dist / f"{name}.sig"
        if (dist / name).is_file() and sig.is_file():
            platforms[platform] = {"signature": sig.read_text().strip(), "url": DOWNLOAD.format(version=version, name=name)}
    if not platforms:
        return None
    return {"version": version, "notes": notes(changelog, version),
            "pub_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "platforms": platforms}


def main() -> None:
    dist, version = Path(sys.argv[1]), sys.argv[2]
    data = manifest(dist, version, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), datetime.now(UTC))
    if data is None:
        print("::warning::No signed update files: no latest.json, so the apps won't be offered this version.")
        return
    (dist / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"latest.json for {', '.join(data['platforms'])}")


if __name__ == "__main__":
    main()
