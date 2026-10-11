"""Stage what the Mac app bundles: the Python core and the `uv` binary that runs it.

    python3 scripts/stage_desktop.py      (Tauri runs this before `tauri dev` and `tauri build`)

Writes desktop/src-tauri/core/ (pyproject.toml, uv.lock, README.md, LICENSE and src/openmymemo, the same
files as the .mcpb) and desktop/src-tauri/binaries/uv-<target triple>, copied from the `uv` on PATH.
Both are build output and not committed. Runs on the system Python 3.9, so no tomllib.
"""

import json
import platform
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"
TRIPLES = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}


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
    shutil.copytree(ROOT / "src/openmymemo", core / "src/openmymemo",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    triple = TRIPLES.get(platform.machine()) or sys.exit(f"Unsupported machine {platform.machine()}")
    uv = shutil.which("uv") or sys.exit("uv not found on PATH; install it from https://docs.astral.sh/uv/")
    binaries = TAURI / "binaries"
    binaries.mkdir(exist_ok=True)
    shutil.copy2(uv, binaries / f"uv-{triple}")
    print(f"Staged the core {version} and {uv} for {triple}")


if __name__ == "__main__":
    main()
