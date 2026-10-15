"""Plugin packaging stays consistent: versions match and every referenced file exists."""

import json
import re
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
    assert load("gemini-extension.json")["version"] == version
    assert load("integrations/claude-desktop/manifest.json")["version"] == version
    assert load("web/package.json")["version"] == version
    assert load("desktop/package.json")["version"] == version
    assert load("desktop/src-tauri/tauri.conf.json")["version"] == version
    assert tomllib.loads((ROOT / "desktop/src-tauri/Cargo.toml").read_text())["package"]["version"] == version
    assert __version__ == version


def test_mac_app_grants_exactly_the_commands_the_panel_calls():
    # The panel is a remote origin to Tauri: each command needs the ACL manifest and the capability. The
    # bundled setup page (desktop/splash) calls some too.
    pages = (ROOT / "web/src/desktop.ts").read_text() + (ROOT / "desktop/splash/index.html").read_text()
    panel = set(re.findall(r'invoke\("([a-z_]+)"', pages))
    build = (ROOT / "desktop/src-tauri/build.rs").read_text()
    declared = set(re.findall(r'"([a-z_]+)"', re.search(r"commands\(&\[(.*?)\]\)", build, re.S).group(1)))
    caps = load("desktop/src-tauri/capabilities/default.json")
    granted = {p[len("allow-"):].replace("-", "_") for p in caps["permissions"] if p.startswith("allow-")}
    assert panel and panel == declared == granted
    assert caps["remote"]["urls"] == ["http://127.0.0.1:*"]


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


def test_claude_desktop_extension_installs_with_nothing_to_fill_in():
    manifest = load("integrations/claude-desktop/manifest.json")
    fields = manifest["user_config"].values()
    assert not any(f.get("required") for f in fields)
    # Claude Desktop shows defaults verbatim, so a "${HOME}" default reads as text the user must change.
    assert not any("${" in str(f.get("default", "")) for f in fields)
    config = manifest["server"]["mcp_config"]
    assert config["env"]["OPENMYMEMO_HOME"] == "${user_config.vault}" and "--vault" not in config["args"]


def test_the_privacy_policy_is_where_directories_look_for_it():
    """Claude's directory rejects a local server without a "Privacy Policy" section in its README and HTTPS links
    in the manifest's privacy_policies; both lead to PRIVACY.md."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^## Privacy Policy$", readme, re.M)
    policies = load("integrations/claude-desktop/manifest.json")["privacy_policies"]
    assert policies == ["https://github.com/MyMemoAI/openmymemo/blob/main/PRIVACY.md"]
    assert policies[0] in readme and (ROOT / "PRIVACY.md").is_file()


def test_gemini_extension_runs_the_plugin_server_and_carries_the_instructions():
    from openmymemo.mcp_server import INSTRUCTIONS

    ext = load("gemini-extension.json")
    claude = load(".claude-plugin/plugin.json")["mcpServers"]["openmymemo"]["args"]
    args = ext["mcpServers"]["openmymemo"]["args"]
    assert args == [a.replace("${CLAUDE_PLUGIN_ROOT}", "${extensionPath}").replace("claude-code", "gemini-cli")
                    for a in claude]
    # Gemini CLI loads this file into every session; it repeats the server's instructions for clients that
    # don't show those to the model.
    context = (ROOT / ext["contextFileName"]).read_text()
    assert " ".join(INSTRUCTIONS.split()) in " ".join(context.split())
    # A GEMINI.md at the root would also load for anyone running Gemini CLI inside this repository.
    assert not (ROOT / "GEMINI.md").exists()
