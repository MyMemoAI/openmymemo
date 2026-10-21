import os
import shutil
import socket
from pathlib import Path

from typer.testing import CliRunner

from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.proposals import Proposals
from openmymemo.vault import Vault, vault_path

runner = CliRunner()


def test_context_hook_is_silent_and_safe(tmp_path, vault):
    missing = runner.invoke(app, ["--vault", str(tmp_path / "nope"), "context", "--hook", "--connector", "claude-code"])
    assert missing.exit_code == 0 and missing.stdout == ""

    vault.add("work", "CTO at Acme")
    ok = runner.invoke(app, ["--vault", str(vault.root), "context", "--hook", "--connector", "claude-code"])
    assert ok.exit_code == 0 and "CTO at Acme" in ok.stdout
    assert Connectors(vault).get("claude-code")  # registered on first use
    assert vault.activity("claude-code")[0]["via"] == "hook"

    Connectors(vault).remove("claude-code")
    revoked = runner.invoke(app, ["--vault", str(vault.root), "context", "--hook", "--connector", "claude-code"])
    assert revoked.exit_code == 0 and revoked.stdout == ""


def test_context_hook_starts_with_the_digest_line(vault):
    hook = ["--vault", str(vault.root), "context", "--hook", "--connector", "claude-code"]
    vault.add("work", "CTO at Acme")
    quiet = runner.invoke(app, hook)
    assert quiet.exit_code == 0 and quiet.stdout.startswith("# About the user")  # nothing unreviewed: no line

    cons = Connectors(vault)
    cursor, _ = cons.create("Cursor", "http", write="auto")
    props = Proposals(vault)
    props.propose(cursor, "add", content="Uses pnpm for JavaScript projects", topic="tools")
    props.propose(cursor, "add", content="Drinks oolong tea every afternoon", topic="lifestyle")
    r = runner.invoke(app, hook)
    assert r.exit_code == 0, r.output
    first, second = r.stdout.splitlines()[:2]
    assert first == ("OpenMyMemo: Cursor added 2 memories since you last looked. Review them at "
                     "http://127.0.0.1:8787/#/changes or with `openmymemo review`.")
    assert second == "# About the user (OpenMyMemo)" and "Uses pnpm for JavaScript projects" in r.stdout

    claude = cons.ensure("claude-desktop", "Claude Desktop")
    pid = props.propose(claude, "add", content="Is evaluating SwiftUI for the iOS app", topic="projects")["proposal_id"]
    props.propose(claude, "update", memory_id=props.get(pid).applied_memory_id,
                  content="Decided to move the iOS app to SwiftUI")
    first = runner.invoke(app, hook).stdout.splitlines()[0]
    assert first.startswith("OpenMyMemo: Cursor added 2 memories, and Claude Desktop added 1 and changed 1 since ")

    props.mark_all_reviewed()
    assert runner.invoke(app, hook).stdout.startswith("# About the user")


def _topics(vault):
    return {m.content: m.topic for m in vault.memories()}


def test_import_then_sort_interactively(tmp_path, vault):
    paste = tmp_path / "chatgpt.txt"
    paste.write_text("Saved memories\nPrefers concise answers with the conclusion first.\nDelete\nGrows tomatoes\n"
                     "Is the CTO of Acme, a note-taking startup.\nUses a MacBook Pro with VS Code and Python.\n"
                     "Has a daughter who is five years old.\n")
    r = runner.invoke(app, ["--vault", str(vault.root), "import", "chatgpt", str(paste)], input="y\n")
    assert r.exit_code == 0, r.output
    assert "Into From ChatGPT (from-chatgpt), in this order:" in r.stdout
    assert "Imported 5 memories into From ChatGPT (from-chatgpt). Run `openmymemo sort` to file them into topics." \
        in r.stdout
    assert set(_topics(vault).values()) == {"from-chatgpt"}

    script = "\n".join([
        "y",                    # concise answers → How to work with me
        "y", "n",               # tomatoes: no suggestion, so y asks again; skip
        "n",                    # CTO: skip
        "t", "nope", "lifestyle",  # MacBook: unknown slug, then a typed slug
        "q",                    # daughter: quit
    ]) + "\n"
    r = runner.invoke(app, ["--vault", str(vault.root), "sort"], input=script)
    assert r.exit_code == 0, r.output
    assert '[1/5] "Prefers concise answers with the conclusion first." → How to work with me (how-to-work-with-me)' \
        in r.stdout
    assert '[2/5] "Grows tomatoes" → no suggestion' in r.stdout
    assert "No suggestion here" in r.stdout and "Unknown topic: nope" in r.stdout
    assert "[y]es [n]o [t]opic (type a slug) [a]ll remaining [q]uit" in r.stdout
    assert r.stdout.rstrip().endswith("Moved 2 memories.")
    assert _topics(vault) == {
        "Prefers concise answers with the conclusion first.": "how-to-work-with-me",
        "Grows tomatoes": "from-chatgpt",
        "Is the CTO of Acme, a note-taking startup.": "from-chatgpt",
        "Uses a MacBook Pro with VS Code and Python.": "lifestyle",
        "Has a daughter who is five years old.": "from-chatgpt",
    }

    r = runner.invoke(app, ["--vault", str(vault.root), "sort", "--topic", "from-chatgpt"], input="a\n")
    assert r.exit_code == 0 and "[1/3]" in r.stdout and r.stdout.rstrip().endswith("Moved 2 memories.")
    assert _topics(vault)["Is the CTO of Acme, a note-taking startup."] == "work"
    assert _topics(vault)["Has a daughter who is five years old."] == "people"
    assert _topics(vault)["Grows tomatoes"] == "from-chatgpt"  # [a]ll skips rows without a suggestion

    bad = runner.invoke(app, ["--vault", str(vault.root), "sort", "--topic", "work"])
    assert bad.exit_code == 1 and "isn't an imported topic" in bad.output


def test_sort_yes_applies_only_confident_suggestions(vault):
    r = runner.invoke(app, ["--vault", str(vault.root), "import", "gemini", "-", "--yes"],
                      input="Is the CTO of Acme\nLikes jazz\nGrows tomatoes\n")
    assert r.exit_code == 0 and "Imported 3 memories into From Gemini (from-gemini)." in r.stdout
    help_text = runner.invoke(app, ["sort", "--help"]).stdout
    assert "0.5" in help_text
    r = runner.invoke(app, ["--vault", str(vault.root), "sort", "--yes"])
    assert r.exit_code == 0, r.output
    assert '"Is the CTO of Acme" → Work (work)' in r.stdout
    assert "Left 2 memories" in r.stdout and r.stdout.rstrip().endswith("Moved 1 memories.")
    assert _topics(vault) == {"Is the CTO of Acme": "work", "Likes jazz": "from-gemini",  # 0.43: not sure enough
                              "Grows tomatoes": "from-gemini"}
    assert runner.invoke(app, ["--vault", str(vault.root), "sort", "--topic", "nope"]).exit_code == 1


def test_approval_command(vault):
    shown = runner.invoke(app, ["--vault", str(vault.root), "approval"])
    assert shown.exit_code == 0 and "Approval is OFF" in shown.stdout  # the default
    assert "apply right away and show up in `openmymemo review`" in shown.stdout
    assert "memories you wrote yourself and private topics still wait for you" in shown.stdout
    on = runner.invoke(app, ["--vault", str(vault.root), "approval", "on"])
    assert on.exit_code == 0 and "Approval is ON: AI changes wait for you" in on.stdout
    assert vault.settings()["require_approval"] is True
    r = runner.invoke(app, ["--vault", str(vault.root), "approval", "off"])
    assert r.exit_code == 0 and "OFF" in r.stdout
    assert vault.settings()["require_approval"] is False
    assert runner.invoke(app, ["--vault", str(vault.root), "approval", "maybe"]).exit_code == 2


def _changes(vault):
    """Four changes that applied (oldest first: piano, ios, cat, tea) and one that waits (npm)."""
    claude = Connectors(vault).ensure("claude-desktop", "Claude Desktop")
    props = Proposals(vault)
    mine = vault.add("tools", "Uses npm")
    ai = vault.add("work", "Works on the iOS app", source="connector:claude-desktop", actor="connector:claude-desktop")
    results = {
        "piano": props.propose(claude, "add", content="Plays the piano", topic="interests"),
        "ios": props.propose(claude, "update", memory_id=ai.id, content="Leads the iOS team"),
        "cat": props.propose(claude, "add", content="Owns a cat", topic="lifestyle"),
        "tea": props.propose(claude, "add", content="Drinks oolong", topic="lifestyle"),
        "npm": props.propose(claude, "update", memory_id=mine.id, content="Uses pnpm"),
    }
    return props, mine, ai, {k: v["proposal_id"] for k, v in results.items()}


def test_status_shows_unreviewed_changes(vault):
    _changes(vault)
    r = runner.invoke(app, ["--vault", str(vault.root), "status"])
    assert r.exit_code == 0
    assert "Unreviewed changes: 4   Pending proposals: 1   Approval: off" in r.stdout


def test_review_walks_the_digest_then_what_waits(vault):
    props, mine, ai, ids = _changes(vault)
    keys = "k\nu\ne\nLeads the iOS and Android teams\nb\na\n"  # tea, cat, ios, piano (newest first), then npm
    r = runner.invoke(app, ["--vault", str(vault.root), "review"], input=keys)
    out = r.stdout
    assert r.exit_code == 0, out
    assert "Changed since you last looked: 4" in out and "Waiting for you: 1" in out
    assert '[1/4] Claude Desktop added to Lifestyle · "Drinks oolong"' in out
    assert '[2/4] Claude Desktop added to Lifestyle · "Owns a cat"' in out
    assert "[3/4] Claude Desktop changed a memory in Work" in out
    assert "  - Works on the iOS app\n  + Leads the iOS team" in out
    assert out.count("[k]eep  [u]ndo  [e]dit  [b]lock topic  [s]kip  [q]uit") == 4
    assert "[1/1] Claude Desktop wants to update · Tools" in out
    assert "waiting because you wrote this memory yourself" in out

    assert props.get(ids["tea"]).status == "applied" and props.get(ids["tea"]).reviewed_at
    cat = props.get(ids["cat"])
    assert cat.status == "undone" and vault.get(cat.applied_memory_id).status == "archived"
    assert props.get(ids["ios"]).status == "edited_accepted"
    assert vault.get(ai.id).content == "Leads the iOS and Android teams" and vault.get(ai.id).last_actor == "user"
    assert props.get(ids["piano"]).status == "undone"
    assert Connectors(vault).get("claude-desktop").blocked_topics == ["interests"]
    assert props.get(ids["npm"]).status == "accepted" and vault.get(mine.id).content == "Uses pnpm"
    assert props.unreviewed() == [] and props.pending_count() == 0

    again = runner.invoke(app, ["--vault", str(vault.root), "review"])
    assert again.exit_code == 0 and "Nothing to review" in again.stdout


def test_review_skip_quit_and_changed_memories(vault):
    props, _, _, ids = _changes(vault)
    vault.update(props.get(ids["tea"]).applied_memory_id, content="Drinks oolong tea")  # the user changed it since
    r = runner.invoke(app, ["--vault", str(vault.root), "review"], input="u\ns\nq\n")
    out = r.stdout
    assert r.exit_code == 0, out
    assert "the memory has changed since; roll it back from its history in the panel" in out
    assert "The memory has changed since this proposal" in out  # the refused undo
    assert "Waiting for you" not in out  # quit before the pending part
    assert len(props.unreviewed()) == 4 and props.pending_count() == 1


def test_blank_or_unsubstituted_vault_means_the_default(tmp_path, monkeypatch):
    # Claude Desktop passes its optional "Memory folder" field as OPENMYMEMO_HOME, which may arrive
    # empty or as a placeholder it didn't substitute.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for value in (None, "", "  ", "${user_config.vault}", "${HOME}/OpenMyMemo"):
        assert vault_path(value) == tmp_path / "OpenMyMemo", value
    assert vault_path("~/elsewhere") == tmp_path / "elsewhere"

    for env in ("", "${user_config.vault}"):
        out = runner.invoke(app, ["init"], env={"OPENMYMEMO_HOME": env})
        assert out.exit_code == 0, out.output
        assert Vault(tmp_path / "OpenMyMemo").is_initialized()
        assert not (Path.cwd() / "${user_config.vault}").exists()
        shutil.rmtree(tmp_path / "OpenMyMemo")


def test_serve_init_creates_a_missing_or_empty_vault_and_refuses_other_folders(tmp_path, monkeypatch):
    # The Mac app's first launch: `serve --init` on a folder that doesn't exist yet, or an empty one.
    from openmymemo import web

    served = []
    monkeypatch.setattr(web, "serve_panel", lambda vault, port: served.append(port))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    serve = ["serve", "--init", "--no-open", "--port", str(port)]

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_text("")
    for folder in (tmp_path / "absent", empty):
        r = runner.invoke(app, ["--vault", str(folder), *serve])
        assert r.exit_code == 0, r.output
        assert Vault(folder).is_initialized()
    r = runner.invoke(app, ["--vault", str(empty), *serve])  # opens it the second time
    assert r.exit_code == 0 and len(served) == 3

    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    r = runner.invoke(app, ["--vault", str(busy), *serve])
    assert r.exit_code == 1 and "isn't empty" in r.output
    assert not Vault(busy).is_initialized() and (busy / "notes.txt").read_text() == "mine"

    r = runner.invoke(app, ["--vault", str(tmp_path / "other"), "serve", "--no-open", "--port", str(port)])
    assert r.exit_code == 1 and "openmymemo init" in r.output  # without --init nothing is created
    assert not (tmp_path / "other").exists() and len(served) == 3


def test_a_process_from_a_mac_app_environment_holds_its_lock(tmp_path, monkeypatch):
    import os

    import pytest

    fcntl = pytest.importorskip("fcntl")
    from openmymemo import cli

    other = tmp_path / "project/.venv"
    other.mkdir(parents=True)
    cli._hold_environment(str(other))
    assert not (other / cli.ENV_LOCK).exists()

    env = tmp_path / "venvs/9.0.0-1"
    env.mkdir(parents=True)
    monkeypatch.setattr(cli, "_env_lock", None)
    cli._hold_environment(str(env))
    try:
        assert cli._env_lock is not None
        fd = os.open(env / cli.ENV_LOCK, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):  # what the app's clean-up tries (core.rs)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        os.close(cli._env_lock)


def test_a_process_from_a_windows_app_environment_keeps_its_file_open(tmp_path, monkeypatch):
    import pytest

    if os.name != "nt":
        pytest.skip("Windows only")
    import ctypes
    from ctypes import wintypes

    from openmymemo import cli

    env = tmp_path / "venvs/9.0.0-1"
    env.mkdir(parents=True)
    monkeypatch.setattr(cli, "_env_lock", None)
    cli._hold_environment(str(env))
    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    try:
        assert cli._env_lock is not None
        # What the app's clean-up tries (core.rs): open it sharing only deletion.
        handle = create(str(env / cli.ENV_LOCK), 0x80000000, 0x4, None, 3, 0, None)
        assert handle == wintypes.HANDLE(-1).value and ctypes.get_last_error() == 32  # a sharing violation
    finally:
        os.close(cli._env_lock)


def test_openmymemo_command_is_what_configs_get_when_the_mac_app_sets_it(tmp_path, monkeypatch):
    """The Mac app runs the server from a versioned venv that is cleaned up later; configs must name its launcher."""
    from openmymemo.snippets import command_for, stdio_command

    launcher = tmp_path / "bin" / "openmymemo"
    venv_cli = tmp_path / "venvs" / "0.2.3-1700" / "bin" / "openmymemo"
    venv_cli.parent.mkdir(parents=True)
    venv_cli.write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.argv", [str(venv_cli)])
    monkeypatch.setenv("OPENMYMEMO_COMMAND", str(launcher))
    assert command_for(tmp_path / "v")[0] == str(venv_cli.resolve())  # a launcher that isn't there is ignored
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    assert command_for(tmp_path / "v") == [str(launcher), "--vault", str(tmp_path / "v")]
    assert stdio_command(tmp_path / "v", "cursor")[:4] == [str(launcher), "--vault", str(tmp_path / "v"), "mcp"]
    monkeypatch.setenv("OPENMYMEMO_COMMAND", "openmymemo")  # not absolute: ignored
    assert command_for(tmp_path / "v")[0] == str(venv_cli.resolve())


def test_configs_name_uvx_when_openmymemo_runs_from_uvx(tmp_path, monkeypatch):
    """`uvx openmymemo connect …` runs from uv's cache, which `uv cache clean` deletes: configs get uvx instead."""
    from openmymemo import snippets

    exe = ".exe" if os.name == "nt" else ""  # what `shutil.which` finds on Windows
    cached = tmp_path / "cache" / "uv" / "archive-v0" / "YOgxP7GEyeZ8I1n4"
    (cached / "bin").mkdir(parents=True)
    (cached / "bin" / f"openmymemo{exe}").write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uv", "uvx"):
        (bin_dir / f"{name}{exe}").write_text("#!/bin/sh\n")
        (bin_dir / f"{name}{exe}").chmod(0o755)
    monkeypatch.setattr("sys.prefix", str(cached))
    monkeypatch.setattr("sys.argv", [str(cached / "bin" / f"openmymemo{exe}")])
    monkeypatch.delenv("OPENMYMEMO_COMMAND", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    vault = tmp_path / "v"
    assert snippets.command_for(vault) == [str((bin_dir / f"uvx{exe}").resolve()), "openmymemo", "--vault", str(vault)]
    (bin_dir / f"uvx{exe}").unlink()
    assert snippets.command_for(vault)[:4] == [str((bin_dir / f"uv{exe}").resolve()), "tool", "run", "openmymemo"]
    (bin_dir / f"uv{exe}").unlink()  # no uv on PATH: the cached command is all there is
    assert snippets.command_for(vault)[0] == str((cached / "bin" / f"openmymemo{exe}").resolve())
    monkeypatch.setattr("sys.prefix", str(tmp_path / "tools" / "openmymemo"))  # `uv tool install`: kept as is
    (bin_dir / f"uvx{exe}").write_text("#!/bin/sh\n")
    (bin_dir / f"uvx{exe}").chmod(0o755)
    assert snippets.command_for(vault)[0] == str((cached / "bin" / f"openmymemo{exe}").resolve())


def test_import_and_export_report_file_problems_without_a_traceback(tmp_path, vault):
    base = ["--vault", str(vault.root)]
    missing = runner.invoke(app, [*base, "import", "chatgpt", str(tmp_path / "nope.txt")])
    assert missing.exit_code == 1 and isinstance(missing.exception, SystemExit)
    assert "Traceback" not in missing.output and f"Can't read {tmp_path / 'nope.txt'}" in missing.output
    binary = tmp_path / "export.zip"
    binary.write_bytes(b"PK\x03\x04\xff\xfe")
    r = runner.invoke(app, [*base, "import", "chatgpt", str(binary)])
    assert r.exit_code == 1 and "isn't UTF-8 text" in r.output

    bad = runner.invoke(app, [*base, "export", "--format", "csv"])
    assert bad.exit_code == 2 and "format must be one of markdown, claude, chatgpt, json" in bad.output
    vault.add("work", "CTO at Acme")
    nowhere = runner.invoke(app, [*base, "export", "-o", str(tmp_path / "no/such/dir/out.md")])
    assert nowhere.exit_code == 1 and f"Can't write {tmp_path / 'no/such/dir/out.md'}" in nowhere.output


def test_an_unknown_connector_gets_a_message_not_a_traceback(vault):
    base = ["--vault", str(vault.root)]
    for args in (["context", "--connector", "nobody"], ["connector", "config", "nobody"],
                 ["connector", "rotate", "nobody"], ["connector", "remove", "nobody"]):
        r = runner.invoke(app, [*base, *args])
        assert r.exit_code == 2 and isinstance(r.exception, SystemExit), (args, r.output)
        assert "No connector nobody. `openmymemo connector list` shows yours." in r.output, args
    Connectors(vault).create("Foo", "stdio")
    assert runner.invoke(app, [*base, "connector", "remove", "foo"]).exit_code == 0
    again = runner.invoke(app, [*base, "connector", "remove", "foo"])  # already revoked
    assert again.exit_code == 2 and "No connector foo." in again.output
