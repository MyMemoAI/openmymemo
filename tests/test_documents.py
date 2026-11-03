"""Documents (MEMORY_FORMAT.md §3.6, product-design.md §23): one memory, doc:<slug>, whose content is a file's whole
text; apps get it whole and propose a complete new text, reviewed as a diff."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client.client import Client
from typer.testing import CliRunner

from openmymemo import score, transfer
from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.context import compile_context, demote_headings
from openmymemo.mcp_server import build_server
from openmymemo.proposals import PermissionDenied, Proposals
from openmymemo.vault import DOC_MAX, Conflict, NotFound, VaultError
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
TEXT = "Start with the answer, then the reasons.\n\n## Code\n- Small commits with imperative messages\n"


def tools(vault, steps, connector="claude-code"):
    async def main():
        async with Client(build_server(vault, stdio_connector=connector)) as c:
            return await steps(c)
    return anyio.run(main)


def propose(**args):
    return lambda c: c.call_tool("memory_propose", args)


# ---- the file ---------------------------------------------------------------------------------------------------


def test_a_document_file_is_one_memory_and_never_a_topic(vault):
    path = vault.memories_dir / "working-with-me.md"
    path.write_text("---\ntype: document\ntitle: Working with me\n---\n\n" + TEXT, encoding="utf-8")
    d = vault.document("working-with-me")
    assert (d.id, d.title, d.memory.kind, d.memory.last_actor) == ("doc:working-with-me", "Working with me",
                                                                  "document", "file")
    assert d.content == TEXT.rstrip()
    assert vault.get("doc:working-with-me").content == d.content
    assert "working-with-me" not in {t["slug"] for t in vault.topics()}
    assert vault.memories() == []  # its list item is part of the text, not a memory
    assert path.read_text(encoding="utf-8").endswith("messages\n")  # no ids were added to it

    path.write_text(path.read_text(encoding="utf-8").replace("then the reasons", "then why"), encoding="utf-8")
    m = vault.get(d.id)
    assert (m.version, m.last_actor) == (2, "file") and "then why" in m.content
    path.unlink()
    assert vault.get(d.id).status == "archived" and vault.documents() == []
    assert [x.slug for x in vault.documents("archived")] == ["working-with-me"]
    assert vault.memories("archived") == []  # documents are listed apart
    vault.restore(d.id)
    assert path.exists() and vault.document("working-with-me").content == m.content
    assert [h["op"] for h in vault.history(d.id)] == ["restore", "archive", "update", "create"]


def test_windows_line_endings_and_a_broken_frontmatter(vault):
    path = vault.memories_dir / "notes.md"
    path.write_bytes(b"---\r\ntype: document\r\ntitle: Notes\r\n---\r\n\r\n- one\r\n- two\r\n")
    assert vault.document("notes").content == "- one\n- two"
    vault.update("doc:notes", content="- one\r\n- two\r\n- three")
    assert vault.document("notes").content == "- one\n- two\n- three"

    path.write_text("---\ntype: document\ntitle: [unclosed\n---\n\n- one\n", encoding="utf-8")
    vault.sync()
    assert vault.get("doc:notes").status == "active"  # kept as it was until the frontmatter reads again
    assert vault.memories() == [] and "^" not in path.read_text(encoding="utf-8")  # never turned into a topic


def test_adding_names_slugs_and_limits(vault):
    d = vault.add_document("Working with me", TEXT)
    assert d.slug == "working-with-me" and d.memory.last_actor == "user"
    vault.archive(d.id)
    again = vault.add_document("Working with me", "new")
    assert again.slug == "working-with-me-2"  # an archived document keeps its id: never reused
    assert vault.add_document("工作方式", "text").slug == "document"
    with pytest.raises(VaultError):
        vault.add_document("Too long", "x" * (DOC_MAX + 1))
    with pytest.raises(VaultError):
        vault.add_document("Taken", "x", slug="work")  # a topic's file
    with pytest.raises(VaultError):
        vault.update(again.id, content="x", topic="work")
    private = vault.update_document(again.slug, title="Project notes", private=True)
    assert (private.title, private.private) == ("Project notes", True)
    assert again.slug in vault.private_topics()


def test_an_apps_change_names_it_in_the_frontmatter_until_someone_else_writes(vault):
    d = vault.add_document("Working with me", TEXT)
    path = vault.memories_dir / f"{d.slug}.md"
    vault.update(d.id, content=TEXT + "\nOne more line.", actor="connector:claude-code")
    assert "generated:\n  by: connector:claude-code\n" in path.read_text(encoding="utf-8")
    vault.update(d.id, content=TEXT, actor="user")
    text = path.read_text(encoding="utf-8")
    assert "generated" not in text and text.startswith("---\ntype: document\ntitle: Working with me\n---\n\n")
    with pytest.raises(Conflict):
        vault.update(d.id, content="stale", expected_version=1)


# ---- what AI apps get --------------------------------------------------------------------------------------------


def test_documents_come_first_whole_with_their_headings_nested(vault):
    vault.add("work", "Works at Acme as CTO")
    vault.add_document("Working with me", "# Rules\n\n```\n# a comment\n```\n" + TEXT)
    r = compile_context(vault, None)
    text = r["text"]
    assert "`doc:<slug>` as `memory_id` and the complete new text as `content`" in text
    assert text.index("## Working with me [doc:working-with-me]") < text.index("## Work (work)")
    assert "### Rules" in text and "\n# a comment\n" in text and "#### Code" in text
    assert r["memory_ids"][0] == "doc:working-with-me"
    assert "## Working with me [doc:working-with-me]" in vault.memory_md_path.read_text(encoding="utf-8")
    assert demote_headings("###### deep") == "###### deep"


def test_within_a_budget_documents_take_half_and_say_where_they_were_cut(vault):
    vault.add("work", "Works at Acme as CTO")
    vault.add_document("Long", "\n".join(f"Line {i} of a long working agreement." for i in range(400)))
    vault.add_document("Zebra notes", "z " * 3000)
    r = compile_context(vault, None, max_chars=4000)
    text = r["text"]
    assert "## Long [doc:long]" in text and "more characters. `memory_get` with `doc:long`" in text
    assert "_Documents not shown to stay within the budget: Zebra notes [doc:zebra-notes]." in text
    assert "Works at Acme as CTO" in text and len(text) <= 4000
    assert r["memory_ids"] == ["doc:long", r["memory_ids"][1]] and len(r["memory_ids"]) == 2



def test_however_many_documents_there_are_the_context_stays_within_its_budget(vault):
    for i in range(100):
        vault.add_document(f"Project {i:03d} working instructions", "Synthetic working guidance.\n" * 80)
    for budget in (8000, 2000):
        assert compile_context(vault, None, max_chars=budget)["chars"] <= budget
    text = compile_context(vault, None, max_chars=8000)["text"]
    (note,) = [line for line in text.splitlines() if line.startswith("_Documents not shown")]
    assert note.startswith("_Documents not shown to stay within the budget: Project 0")
    assert note.endswith(" more. `memory_get` returns them, and `memory_search` finds the others by their words._")
    assert len(note) <= 700
    small = compile_context(vault, None, max_chars=2000)["text"]
    assert "_100 documents not shown to stay within the budget: `memory_search` finds them by their words._" in small
    work = vault.add("work", "Works at Acme as CTO")
    r = compile_context(vault, None, max_chars=8000)
    assert r["chars"] <= 8000 and work.id in r["memory_ids"]

def test_private_documents_and_read_lists(vault):
    vault.add_document("Health notes", "Allergic to penicillin.", private=True)
    vault.add_document("Working with me", TEXT)
    cons = Connectors(vault)
    cursor = cons.ensure("cursor", "Cursor")
    assert "Health notes" not in compile_context(vault, None)["text"]
    assert "Health notes" not in compile_context(vault, cursor)["text"]
    cons.update("cursor", include_private=True)
    assert "penicillin" in compile_context(vault, cons.get("cursor"))["text"]
    cons.update("cursor", read_topics=["work"])
    assert "Working with me" not in compile_context(vault, cons.get("cursor"))["text"]


def test_apps_get_search_and_propose_documents(vault):
    vault.add_document("Working with me", TEXT)
    vault.add_document("Secret plans", "Nothing to see.", private=True)

    async def steps(c):
        got = await c.call_tool("memory_get", {"ids": ["doc:working-with-me", "doc:secret-plans"]})
        found = await c.call_tool("memory_search", {"query": "imperative commits"})
        return got, found

    got, found = tools(vault, steps)
    (doc,) = got.structured_content["memories"]
    assert (doc["type"], doc["title"], doc["content"]) == ("document", "Working with me", TEXT.rstrip())
    assert got.structured_content["missing"] == ["doc:secret-plans"]
    hit = found.structured_content["documents"][0]
    assert hit["id"] == "doc:working-with-me" and "imperative" in hit["snippet"]
    assert found.structured_content["results"] == []
    assert vault.activity("claude-code")[0]["memory_ids"] == ["doc:working-with-me"]


# ---- how apps change one ----------------------------------------------------------------------------------------


def test_a_change_to_the_persons_document_waits_and_shows_as_a_diff(vault):
    d = vault.add_document("Working with me", TEXT)
    new = TEXT + "- Run the tests before pushing\n"
    r = tools(vault, propose(op="update", memory_id=d.id, content=new, reason="The user said so."))
    out = r.structured_content
    assert (out["status"], out["hold"], out["topic"]) == ("pending", "protected", d.slug)
    (p,) = Proposals(vault).list("pending")
    assert (p.before_content, p.content, p.checked_by) == (TEXT.rstrip(), new.rstrip(), None)
    assert vault.document(d.slug).content == TEXT.rstrip()
    Proposals(vault).accept(p.id, content=new + "- And lint\n")
    after = vault.get(d.id)
    assert after.content.endswith("- And lint") and after.last_actor == "user"
    assert "- **You** edited the document Working with me (doc:working-with-me)" in vault.log_md_path.read_text()
    assert "Run the tests" not in vault.log_md_path.read_text()  # named, never quoted


def test_a_document_an_app_wrote_last_changes_at_once_and_can_be_undone(vault):
    d = vault.add_document("Project notes", "Started in October.")
    vault.update(d.id, content="Started in October. Ships in December.", actor="connector:cursor")
    r = tools(vault, propose(op="update", memory_id=d.id, content="Started in October. Ships in January."))
    assert r.structured_content["status"] == "applied"
    props = Proposals(vault)
    (p,) = props.unreviewed()
    assert props.as_dict(p)["can_undo"] is True
    assert "generated:\n  by: connector:claude-code" in (vault.memories_dir / "project-notes.md").read_text()
    props.edit_applied(p.id, "Started in October.\n\nShips in January, maybe February.")
    assert vault.get(d.id).content == "Started in October.\n\nShips in January, maybe February."  # newlines kept
    props.undo(p.id)
    assert vault.get(d.id).content == "Started in October. Ships in December."
    log = vault.log_md_path.read_text(encoding="utf-8")
    assert "undid Claude Code's change to the document Project notes (doc:project-notes)" in log


def test_what_an_app_may_not_do_with_a_document(vault):
    d = vault.add_document("Working with me", TEXT)
    cons = Connectors(vault)
    claude = cons.ensure("claude-code", "Claude Code")
    props = Proposals(vault)
    with pytest.raises(PermissionDenied):
        props.propose(claude, "archive", memory_id=d.id)
    with pytest.raises(VaultError, match="at most 20,000"):
        props.propose(claude, "update", memory_id=d.id, content="x" * (DOC_MAX + 1))
    with pytest.raises(VaultError, match="leave out topic"):
        props.propose(claude, "update", memory_id=d.id, content="new", topic="work")
    assert props.propose(claude, "update", memory_id=d.id, content=TEXT + "\n\n")["status"] == "duplicate"
    added = props.propose(claude, "add", content="Prefers tea", topic=d.slug)  # never filed into a document
    assert added["topic"] != d.slug and "Prefers tea" not in (vault.memories_dir / f"{d.slug}.md").read_text()
    cons.block_topic("claude-code", d.slug)
    with pytest.raises(PermissionDenied):
        props.propose(cons.get("claude-code"), "update", memory_id=d.id, content="new text")


def test_a_private_document_always_waits(vault):
    d = vault.add_document("Health notes", "Allergic to penicillin.", private=True)
    cons = Connectors(vault)
    cons.ensure("claude-code", "Claude Code")
    cons.update("claude-code", include_private=True, write="auto")
    r = tools(vault, propose(op="update", memory_id=d.id, content="Allergic to penicillin and latex."))
    assert (r.structured_content["status"], r.structured_content["hold"]) == ("pending", "private")



def test_the_assistant_holds_back_private_documents_only(vault):
    """The Assistant isn't given documents, but only a private one (archived or not) or one it may not read stops a
    request that quotes it; a long document would otherwise match almost anything."""
    from openmymemo.assistant import Data
    from openmymemo.assistant.core import ensure_connector
    from openmymemo.assistant.guard import Guard
    from openmymemo.assistant.settings import Settings

    vault.add_document("Working with me", "Start with the answer, then the reasons behind it.")
    vault.add_document("Health notes", "Allergic to penicillin since childhood.", private=True)
    old = vault.add_document("Old plans", "Moving to Lisbon in the spring of next year.", private=True)
    vault.archive(old.id)
    hidden = vault.add_document("Side project", "Building a sailing app for weekend racers.")
    assistant = ensure_connector(vault)
    Connectors(vault).update("assistant", read_topics=[t["slug"] for t in vault.topics()] + ["working-with-me"])
    guard = Guard(vault, Connectors(vault).get(assistant.id), Settings(enabled=True, kind="remote"))

    def refused(text):
        return guard.refusal([Data(text, label="chat")])

    assert refused("Please start with the answer, then the reasons behind it.") is None
    assert refused("Note: allergic to penicillin since childhood") == "content"
    assert refused("We are moving to Lisbon in the spring") == "content"
    assert refused("I am building a sailing app for weekend racers") == "content"  # not in its read list
    assert hidden.slug not in Connectors(vault).get("assistant").read_topics


def test_the_brief_names_a_changed_document_and_never_quotes_it(vault):
    from datetime import UTC, datetime

    from openmymemo import brief

    d = vault.add_document("Project notes", "Started in October.")
    vault.update(d.id, content="Started in October!", actor="connector:cursor")  # an app wrote it last
    secret = vault.add_document("Health notes", "Allergic to penicillin.", private=True)
    cons = Connectors(vault)
    cons.ensure("claude-code", "Claude Code")
    cons.update("claude-code", include_private=True)
    tools(vault, propose(op="update", memory_id=d.id, content="Started in October. Ships in January."))
    r = tools(vault, propose(op="update", memory_id=secret.id, content="Allergic to penicillin and latex."))
    Proposals(vault).accept(r.structured_content["proposal_id"])
    b = brief.build(vault, datetime.now(UTC).astimezone().date())
    (app_row,) = b["learned"]
    assert app_row["changes"] == [{"op": "update", "memory_id": d.id, "content": None, "topic": None,
                                   "document": "Project notes"}]
    assert app_row["private"] == 1
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "brief", "--date", b["date"]]).output
    assert "changed the document Project notes" in out
    assert "Ships in January" not in out and "latex" not in out

# ---- import, export, CLI, panel ----------------------------------------------------------------------------------


def test_instructions_files_are_offered_whole(tmp_path):
    rule = transfer.as_document("cursor", "/h/.cursor/rules/style.mdc",
                                "---\ndescription: Code style\nglobs: '*.py'\nalwaysApply: true\n---\n\n# Style\n")
    assert rule == {"title": "style.mdc", "description": "Code style", "content": "# Style", "chars": 7,
                    "prefer": True}
    assert transfer.as_document("claude-code", "/h/.claude/CLAUDE.md", TEXT)["prefer"] is True
    assert transfer.as_document("claude-code", "/h/.claude/projects/p/memory/MEMORY.md", "- a")["prefer"] is False
    assert transfer.as_document("json", "/h/memory.json", "[]") is None
    assert transfer.as_document("codex", "/h/AGENTS.md", "x" * (DOC_MAX + 1)) is None


def test_importing_a_claude_md_as_a_document_keeps_it_the_persons(vault, tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text(TEXT, encoding="utf-8")
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "import", "auto", str(f), "--document"])
    assert r.exit_code == 0, r.stdout
    d = vault.document("claude-md")
    assert (d.title, d.content, d.memory.last_actor) == ("CLAUDE.md", TEXT.rstrip(), "import:claude-code")
    out = tools(vault, propose(op="update", memory_id=d.id, content="Rewritten."))
    assert out.structured_content["hold"] == "protected"  # imported text is the person's


def test_export_includes_documents_and_leaves_out_private_ones(vault):
    vault.add_document("Working with me", TEXT)
    vault.add_document("Health notes", "Allergic to penicillin.", private=True)
    data = json.loads(transfer.export(vault, "json"))
    assert [(d["id"], d["content"]) for d in data["documents"]] == [("doc:working-with-me", TEXT.rstrip())]
    assert "## Working with me\n\n" + TEXT.rstrip() in transfer.export(vault, "markdown")
    assert "penicillin" in transfer.export(vault, "markdown", include_private=True)
    assert "Working with me" not in transfer.export(vault, "claude")



def test_an_export_without_the_documents_is_no_backup(vault):
    def backup_issue():
        return "backup" in [i["id"] for i in score.compute(vault)["issues"]]

    vault.add_document("Working with me", TEXT)
    assert backup_issue()  # a folder with only a document has something to lose
    vault.add("work", "Works at Acme as CTO")
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    r = api.get("/api/export", params={"format": "claude", "include_private": "true"}, headers=H)
    assert r.status_code == 200 and "Works at Acme" in r.text and "Start with the answer" not in r.text
    assert backup_issue()
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "export", "--format", "chatgpt", "--include-private"])
    assert out.exit_code == 0 and backup_issue()
    r = api.get("/api/export", params={"format": "json", "include_private": "true"}, headers=H)
    assert "Start with the answer" in r.text and not backup_issue()

def test_the_safety_score_looks_for_secrets_in_documents(vault):
    vault.add_document("Setup", "Deploy with key sk-ant-api03-" + "a" * 40)
    vault.add_document("Private setup", "Deploy with key sk-ant-api03-" + "b" * 40, private=True)
    issues = [i for i in score.compute(vault)["issues"] if i["id"].startswith("secret:")]
    assert [(i["fix"], i["target"]) for i in issues] == [("document", "setup")]
    assert issues[0]["message"].startswith("The document Setup looks like it contains")


def test_cli_doc_commands(vault, tmp_path):
    base = ["--vault", str(vault.root), "doc"]
    runner = CliRunner()
    f = tmp_path / "AGENTS.md"
    f.write_text(TEXT, encoding="utf-8")
    assert runner.invoke(app, [*base, "new", "Working with me", "--file", str(f)]).exit_code == 0
    assert "working-with-me" in runner.invoke(app, [*base, "list"]).stdout
    assert runner.invoke(app, [*base, "show", "working-with-me"]).stdout == TEXT.rstrip() + "\n"
    assert runner.invoke(app, [*base, "edit", "working-with-me"], input="New text\n").exit_code == 0
    assert vault.document("working-with-me").content == "New text"
    assert runner.invoke(app, [*base, "archive", "working-with-me"]).exit_code == 0
    assert runner.invoke(app, [*base, "show", "working-with-me"]).exit_code == 1
    assert runner.invoke(app, [*base, "restore", "working-with-me"]).exit_code == 0
    assert vault.document("working-with-me").content == "New text"


def test_review_shows_a_document_change_as_a_diff(vault):
    d = vault.add_document("Working with me", TEXT)
    claude = Connectors(vault).ensure("claude-code", "Claude Code")
    Proposals(vault).propose(claude, "update", memory_id=d.id, content=TEXT.replace("Small", "Tiny"))
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "review"], input="e\ns\n")
    assert "Claude Code wants to change the document Working with me" in r.stdout
    assert "-- Small commits with imperative messages" in r.stdout
    assert "+- Tiny commits with imperative messages" in r.stdout
    assert "a document is edited in the panel" in r.stdout


def test_the_panel_api(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    made = api.post("/api/documents", json={"title": "Working with me", "content": TEXT}, headers=H).json()
    assert made["id"] == "doc:working-with-me" and made["content"] == TEXT.rstrip()
    state = api.get("/api/state", headers=H).json()
    assert [d["slug"] for d in state["documents"]] == ["working-with-me"] and "content" not in state["documents"][0]
    saved = api.put("/api/documents/working-with-me", json={"content": "v2", "expected_version": 1}, headers=H)
    assert saved.json()["version"] == 2
    stale = api.put("/api/documents/working-with-me", json={"content": "v3", "expected_version": 1}, headers=H)
    assert stale.status_code == 409
    assert api.patch("/api/documents/working-with-me", json={"private": True}, headers=H).json()["private"] is True
    history = api.get("/api/documents/working-with-me/history", headers=H).json()
    assert [h["version"] for h in history] == [2, 1]
    back = api.post("/api/documents/working-with-me/rollback", json={"version": 1}, headers=H).json()
    assert back["content"] == TEXT.rstrip()
    assert api.post("/api/documents/working-with-me/archive", headers=H).json() == {"ok": True}
    assert api.get("/api/documents/working-with-me", headers=H).status_code == 404
    assert [d["slug"] for d in api.get("/api/documents?status=archived", headers=H).json()] == ["working-with-me"]
    assert api.post("/api/documents/working-with-me/restore", headers=H).json()["status"] == "active"
    imported = api.post("/api/documents", json={"title": "AGENTS.md", "content": "Use uv.", "source": "codex"},
                        headers=H).json()
    assert imported["last_actor"] == "import:codex"
    assert api.post("/api/documents", json={"title": "x", "source": "nope"}, headers=H).status_code == 400
    with pytest.raises(NotFound):
        vault.document("nope")
