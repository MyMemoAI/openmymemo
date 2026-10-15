"""Build the Claude Desktop extension: dist/openmymemo-<version>.mcpb

    uv run python scripts/build_mcpb.py

Uses the MCPB "uv" server type: Claude Desktop installs Python and the dependencies from pyproject.toml
itself, so the bundle only carries our source (no compiled wheels). Requires Node (npx) for the mcpb CLI.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"


def main() -> Path:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((ROOT / "integrations/claude-desktop/manifest.json").read_text())
    if manifest["version"] != version:
        sys.exit(f"manifest.json version {manifest['version']} != pyproject version {version}")
    if not (ROOT / "src/openmymemo/static/index.html").exists():
        sys.exit("The panel isn't built. Run `pnpm build` in web/ first.")

    out = ROOT / "dist" / f"openmymemo-{version}.mcpb"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "openmymemo"
        stage.mkdir()
        # pyproject.toml references README.md and LICENSE, so the build inside the bundle needs them; and the
        # privacy policy (README's section links to it).
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "PRIVACY.md"):
            shutil.copy2(ROOT / name, stage / name)
        shutil.copytree(ROOT / "src/openmymemo", stage / "src/openmymemo",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "integrations/claude-desktop/manifest.json", stage / "manifest.json")
        shutil.copy2(ROOT / "integrations/claude-desktop/.mcpbignore", stage / ".mcpbignore")
        shutil.copy2(ROOT / "integrations/icons/icon-512.png", stage / "icon.png")
        npx = shutil.which("npx") or sys.exit("npx not found; install Node.js")
        subprocess.run([npx, "-y", MCPB_CLI, "validate", str(stage / "manifest.json")], check=True)
        subprocess.run([npx, "-y", MCPB_CLI, "pack", str(stage), str(out)], check=True)
    print(f"Built {out}")
    return out


if __name__ == "__main__":
    main()
