"""Stage what the desktop app bundles: the Python core and the `uv` binary that runs it.

    python3 scripts/stage_desktop.py      (Tauri runs this before `tauri dev` and `tauri build`)

Writes desktop/src-tauri/core/ (pyproject.toml, uv.lock, README.md, LICENSE and src/openmymemo, the same
files as the .mcpb) and desktop/src-tauri/binaries/uv-<target triple>, copied from the `uv` on PATH. On
Windows it also builds desktop/launcher, the `openmymemo` program the app installs for AI apps, into
binaries/openmymemo-launcher-<target triple>.exe. All of it is build output and not committed. Runs on the
system Python 3.9 on a Mac, so no tomllib.
"""

import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"
TRIPLES = {("Darwin", "arm64"): "aarch64-apple-darwin", ("Darwin", "x86_64"): "x86_64-apple-darwin",
           ("Windows", "AMD64"): "x86_64-pc-windows-msvc"}


def main() -> None:
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    conf = json.loads((TAURI / "tauri.conf.json").read_text())
    if conf["version"] != version:
        sys.exit(f"tauri.conf.json version {conf['version']} != pyproject version {version}")
    if not (ROOT / "src/openmymemo/static/index.html").exists():
        sys.exit("The panel isn't built. Run `pnpm build` in web/ first.")

    core = TAURI / "core"
    shutil.rmtree(core, ignore_errors=True)
    core.mkdir(parents=True)
    # pyproject.toml references README.md and LICENSE, so building the package needs them.
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, core / name)
    # The app installs the package non-editable, and uv reinstalls it when pyproject.toml's modification
    # time changes (or the version does): stamp it with the staging time so each build's code is installed.
    (core / "pyproject.toml").touch()
    shutil.copytree(ROOT / "src/openmymemo", core / "src/openmymemo",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    machine = (platform.system(), platform.machine())
    triple = TRIPLES.get(machine) or sys.exit(f"Unsupported system {machine[0]} on {machine[1]}")
    exe = ".exe" if machine[0] == "Windows" else ""
    uv = shutil.which("uv") or sys.exit("uv not found on PATH; install it from https://docs.astral.sh/uv/")
    binaries = TAURI / "binaries"
    binaries.mkdir(exist_ok=True)
    shutil.copy2(uv, binaries / f"uv-{triple}{exe}")
    if exe:
        launcher = ROOT / "desktop" / "launcher"
        subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path", str(launcher / "Cargo.toml")],
                       check=True)
        shutil.copy2(launcher / "target/release/openmymemo-launcher.exe",
                     binaries / f"openmymemo-launcher-{triple}.exe")
    print(f"Staged the core {version} and {uv} for {triple}")


if __name__ == "__main__":
    main()
