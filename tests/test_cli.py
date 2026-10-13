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
    import uvicorn

    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(kw["port"]))
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
