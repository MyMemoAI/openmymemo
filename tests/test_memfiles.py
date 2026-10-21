import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import memfiles, transfer
from openmymemo.cli import app
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}

CLAUDE_MD = """---
description: loaded every session
---
# Project memory

@~/.claude/team.md

## Preferences
- Prefers **concise** answers with the conclusion first.
- Uses [uv](https://docs.astral.sh/uv/) for Python,
  never pip.
- [x] Writes the tests first

Coding style:
- Two-space indentation in YAML

We deploy to Fly.io from the main branch.
Every release is tagged.

***

```bash
npm test
```

| Command | What |
|---|---|
| make | builds |

<!-- a note to self,
over two lines -->
## Gemini Added Memories
- My favourite colour is green.
"""


def write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)


def test_a_memory_file_reads_as_list_items_and_paragraphs_with_their_headings():
    items = transfer.parse("claude-code", CLAUDE_MD)
    assert [(i.hint, i.content) for i in items] == [
        ("Preferences", "Prefers concise answers with the conclusion first."),
        ("Preferences", "Uses uv for Python, never pip."),
        ("Preferences", "Writes the tests first"),
        ("Coding style", "Two-space indentation in YAML"),  # a "Label:" names the list under it…
        ("Preferences", "We deploy to Fly.io from the main branch. Every release is tagged."),  # …not what follows
        ("Gemini Added Memories", "My favourite colour is green."),
    ]


def test_the_document_parser_stays_fast_on_hostile_text():
    start = time.perf_counter()
    for text in ("[" * 200_000, "<!--" * 100_000, "**" * 200_000, "[a](" * 100_000, "- " + " " * 200_000 + "x",
                 "---\n" + "a\n" * 100_000):
        transfer.parse("file", text)
    assert time.perf_counter() - start < 2


def test_find_looks_where_each_app_keeps_its_files(tmp_path, vault):
    home = tmp_path / "home"
    write(home, {
        ".claude/CLAUDE.md": "- a", ".claude/projects/-work-app/memory/MEMORY.md": "- b",
        ".claude/projects/-work-app/memory/user_role.md": "- c", ".codex/AGENTS.md": "- d",
        ".gemini/GEMINI.md": "- e", ".gemini/settings.json": "{}", ".cursor/rules/style.mdc": "- f",
        ".codeium/windsurf/memories/global_rules.md": "- g", ".kiro/steering/product.md": "- h",
        ".github/copilot-instructions.md": "- i", "Memories.MD": "- j", "notes.md": "- not a memory file",
        "big/memory.md": "x", "memory.json": "", ".clinerules/rules.md": "- k",
    })
    write(home, {"MEMORY.txt": "a" * (memfiles.MAX_BYTES + 1)})
    found = {Path(os.path.relpath(f.path, home)).as_posix(): f.source for f in memfiles.find(home)}
    assert found == {
        ".claude/CLAUDE.md": "claude-code", ".claude/projects/-work-app/memory/MEMORY.md": "claude-code",
        ".claude/projects/-work-app/memory/user_role.md": "claude-code", ".codex/AGENTS.md": "codex",
        ".gemini/GEMINI.md": "gemini-cli", ".cursor/rules/style.mdc": "cursor",
        ".codeium/windsurf/memories/global_rules.md": "windsurf", ".kiro/steering/product.md": "kiro",
        ".github/copilot-instructions.md": "copilot", "Memories.MD": "file", ".clinerules/rules.md": "cline",
    }  # not notes.md, nothing in big/, not the empty memory.json or the oversized MEMORY.txt
    # nothing is offered from inside the vault itself
    write(vault.root, {"memories.md": "- mine"})
    assert "memories.md" in [f.name for f in memfiles.find(vault.root)]
    assert memfiles.find(vault.root, skip=vault.root) == []


def test_read_only_opens_what_find_would_offer(tmp_path):
    write(tmp_path, {"GEMINI.md": "- Likes tea", ".ssh/id_ed25519": "PRIVATE KEY", "notes.md": "- chosen by hand",
                     "latin1.md": "caf\xe9".encode("latin-1")})
    assert memfiles.read(tmp_path / "GEMINI.md") == ("gemini-cli", "- Likes tea")
    assert memfiles.read(tmp_path / "notes.md")[0] == "file"  # a Markdown file the person picked
    for bad in (tmp_path / ".ssh/id_ed25519", tmp_path / ".ssh", tmp_path / "latin1.md"):
        with pytest.raises(ValueError):
            memfiles.read(bad)


def test_panel_finds_previews_and_imports_a_memory_file(tmp_path, vault, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() and ~
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    write(tmp_path, {"proj/AGENTS.md": "# Conventions\n- Runs ruff before every commit\n", ".ssh/id_rsa": "KEY"})
    listed = api.get("/api/import/files", params={"path": str(tmp_path / "proj")}, headers=H).json()
    assert [(f["name"], f["source"], f["app"]) for f in listed["files"]] == [("AGENTS.md", "codex", "Codex")]
    preview = api.post("/api/import/file", json={"path": listed["files"][0]["path"]}, headers=H).json()
    assert preview["source"] == "codex" and [i["content"] for i in preview["items"]] == ["Runs ruff before every commit"]
    assert preview["items"][0]["hint"] == "Conventions"
    done = api.post("/api/import/commit", json={"source": "codex", "items": preview["items"]}, headers=H).json()
    assert len(done["memory_ids"]) == 1
    assert {t["slug"]: t["title"] for t in vault.topics()}["from-codex"] == "From Codex"
    # a file the browser read: its name says what it is
    picked = api.post("/api/import/file", json={"name": "GEMINI.md", "text": "- Lives in Lisbon"}, headers=H).json()
    assert picked["source"] == "gemini-cli" and picked["items"][0]["content"] == "Lives in Lisbon"
    # never an arbitrary file, a missing folder, or without the panel's header
    assert api.post("/api/import/file", json={"path": str(tmp_path / ".ssh/id_rsa")}, headers=H).status_code == 400
    assert api.get("/api/import/files", params={"path": str(tmp_path / "nope")}, headers=H).status_code == 400
    assert api.get("/api/import/files", params={"path": str(tmp_path)}).status_code == 403
    # only inside the home folder, symlinks included
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    write(outside, {"AGENTS.md": "- not yours"})
    (tmp_path / "link").symlink_to(outside)
    sibling = tmp_path.parent / f"{tmp_path.name}2"  # starts with the home folder's path, but isn't in it
    write(sibling, {"AGENTS.md": "- not yours either"})
    for path in (str(outside), str(outside / "AGENTS.md"), str(tmp_path / "link"), str(tmp_path / ".." / outside.name),
                 str(sibling)):
        assert api.get("/api/import/files", params={"path": path}, headers=H).status_code == 400, path
    assert api.post("/api/import/file", json={"path": str(tmp_path / "link/AGENTS.md")}, headers=H).status_code == 400
    assert api.get("/api/import/files", headers=H).json()["path"] == os.path.realpath(tmp_path)  # empty: the home folder
    # a link inside home to a file inside home still reads as the app whose folder it's in
    write(tmp_path, {"dotfiles/style.mdc": "- Tabs, never spaces"})
    (tmp_path / ".cursor/rules").mkdir(parents=True)
    (tmp_path / ".cursor/rules/style.mdc").symlink_to(tmp_path / "dotfiles/style.mdc")
    for path in (tmp_path, tmp_path / ".cursor/rules/style.mdc"):
        listed = [f for f in api.get("/api/import/files", params={"path": str(path)}, headers=H).json()["files"]
                  if f["name"] == "style.mdc"]
        assert [(f["path"], f["source"]) for f in listed] == [(str(tmp_path / ".cursor/rules/style.mdc"), "cursor")]
        preview = api.post("/api/import/file", json={"path": listed[0]["path"]}, headers=H).json()
        assert preview["source"] == "cursor" and preview["items"][0]["content"] == "Tabs, never spaces"


def test_cli_imports_a_memory_file_by_its_name(tmp_path, vault):
    write(tmp_path, {".claude/CLAUDE.md": "- Prefers tea over coffee\n- Works at Acme as CTO\n"})
    res = CliRunner().invoke(app, ["--vault", str(vault.root), "import", "auto", str(tmp_path / ".claude/CLAUDE.md"),
                                   "--yes"])
    assert res.exit_code == 0, res.output
    assert "reads as Claude Code" in res.output and "from-claude-code" in res.output
    assert CliRunner().invoke(app, ["--vault", str(vault.root), "import", "auto"]).exit_code == 2


def test_a_file_reads_as_the_same_app_whether_found_or_opened(tmp_path):
    # one file for every place `find` looks, including generic names in an app's own folder
    files = {".claude/CLAUDE.md": "claude-code", ".claude/projects/-app/memory/MEMORY.md": "claude-code",
             ".claude/projects/-app/memory/memories.md": "claude-code", "memory/MEMORY.md": "file",
             ".codex/AGENTS.md": "codex", ".gemini/GEMINI.md": "gemini-cli", ".cursor/rules/a.mdc": "cursor",
             ".cursor/rules/memory.md": "cursor", ".codeium/windsurf/memories/global_rules.md": "windsurf",
             ".windsurf/rules/memory.md": "windsurf", ".clinerules/memory.md": "cline", "memory-bank/a.md": "cline",
             "Documents/Cline/Rules/style.md": "cline", ".github/copilot-instructions.md": "copilot",
             ".github/instructions/py.instructions.md": "copilot", ".kiro/steering/memory.md": "kiro",
             "memory.md": "file", "AGENTS.md": "codex"}
    write(tmp_path, {rel: "- something to remember" for rel in files})
    found = {Path(os.path.relpath(f.path, tmp_path)).as_posix(): f.source for f in memfiles.find(tmp_path)}
    assert found == files
    assert all(memfiles.read(tmp_path / rel)[0] == source for rel, source in files.items())
    assert all(memfiles.find(tmp_path / rel)[0].source == source for rel, source in files.items())


def test_code_blocks_end_only_at_a_matching_fence():
    def parse(text):
        return [i.content for i in transfer.parse("file", text)]

    # a longer fence holds a shorter one; a fence of the other character doesn't close it
    assert parse("Before paragraph.\n\n````markdown\n```sh\nrm -rf build\n```\n````\n\nAfter paragraph.") == [
        "Before paragraph.", "After paragraph."]
    assert parse("One thing.\n~~~\n```\nnot this\n~~~~\nTwo things.") == ["One thing.", "Two things."]
    assert parse("One thing.\n```\ncode\n``` text after it\nstill code\n```\nTwo things.") == ["One thing.", "Two things."]
    assert parse("One thing.\n```\nnever closed\n- not a memory") == ["One thing."]
