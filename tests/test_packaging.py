"""Plugin packaging stays consistent: versions match and every referenced file exists."""

import json
import subprocess
import tomllib
from pathlib import Path

from openmymemo import __version__

ROOT = Path(__file__).resolve().parent.parent


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def test_versions_match_everywhere():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert load(".claude-plugin/plugin.json")["version"] == version
    assert load("plugin.json")["version"] == version
    assert load("integrations/claude-desktop/manifest.json")["version"] == version
    assert load("web/package.json")["version"] == version
    assert __version__ == version


def test_referenced_plugin_files_exist():
    codex = load("plugin.json")["extensions"]["com.openai"]
    for rel in (codex["interface"]["composerIcon"], codex["interface"]["logo"],
                load(".claude-plugin/plugin.json")["hooks"]):
        assert (ROOT / rel).is_file(), rel
    claude_hook = load("integrations/claude-code/hooks/hooks.json")["hooks"]["SessionStart"][0]["hooks"][0]
    assert (ROOT / claude_hook["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")).is_file()
    for skill in ("memory", "remember", "codex-setup"):
        head = (ROOT / f"skills/{skill}/SKILL.md").read_text().split("---")[1]
        assert f"name: {skill}" in head and "description:" in head


def test_codex_gets_its_hook_from_setup_not_from_the_plugin():
    # Codex doesn't run plugin hooks; a plugin hook would also double-load memory if it ever started to.
    assert "hooks" not in load("plugin.json")["extensions"]["com.openai"]
    script = ROOT / "integrations/codex/setup.sh"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    assert (ROOT / "integrations/hooks/session-start.sh").is_file()  # what the installed hook runs


def test_no_project_mcp_json_at_repo_root():
    # Claude Code would load it as project config for everyone who opens the repo.
    assert not (ROOT / ".mcp.json").exists()
