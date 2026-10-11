import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from openmymemo.connectors import Connectors
from openmymemo.proposals import PermissionDenied, Proposals, is_protected
from openmymemo.vault import VaultError

AI = {"source": "connector:claude", "actor": "connector:claude"}  # a memory an AI wrote


@pytest.fixture
def setup(vault):
    cons = Connectors(vault)
    claude = cons.ensure("claude", "Claude")
    return vault, cons, claude, Proposals(vault)


def edit_file(path, old, new):
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


# ---- the default: apply now, review later (§5.5) ---------------------------------------------------

def test_additions_apply_at_once_and_wait_in_the_digest(setup):
    vault, _, claude, props = setup
    assert vault.settings() == {"require_approval": False}
    r = props.propose(claude, "add", content="用户正在评估把 iOS 端迁到 SwiftUI", reason="user said so",
                      evidence="我们打算把 iOS 迁到 SwiftUI")
    assert r["status"] == "applied" and r["hold"] is None and r["topic"] == "projects"
    assert r["message"] == "Applied. The user will see it in their OpenMyMemo digest and can undo it."
    p = props.get(r["proposal_id"])
    m = vault.get(p.applied_memory_id)
    assert r["memory_id"] == m.id and m.topic == "projects" and m.evidence
    assert m.source == m.last_actor == "connector:claude" and not is_protected(m)
    assert vault.history(m.id)[0]["actor"] == "connector:claude"
    assert p.applied_version == 1 and p.reviewed_at is None and p.undo_until is None
    assert [x.id for x in props.unreviewed()] == [p.id] and props.pending_count() == 0
    stored = json.loads((vault.proposals_dir / f"{p.id}.json").read_text())
    assert stored["hold"] is None and stored["reviewed_at"] is None and stored["applied_version"] == 1


def test_changes_to_memories_the_user_wrote_wait(setup):
    vault, cons, claude, props = setup
    mine = vault.add("tools", "Uses npm for JavaScript")
    imported = vault.add("tools", "Uses Homebrew on the Mac", source="import:chatgpt", actor="import:chatgpt")
    edit_file(vault.memories_dir / "tools.md", "Uses npm for JavaScript", "Uses npm for JavaScript and Node")
    assert vault.get(mine.id).last_actor == "file"
    ai = vault.add("tools", "Prefers Ruff over flake8", **AI)
    other_ai = vault.add("tools", "Runs tests with pytest", source="connector:cursor", actor="connector:cursor")
    assert [is_protected(vault.get(m.id)) for m in (mine, imported, ai, other_ai)] == [True, True, False, False]

    r = props.propose(claude, "update", memory_id=mine.id, content="Uses pnpm, not npm")
    assert r["status"] == "pending" and r["hold"] == "protected"
    assert r["message"] == "Waiting for the user: they wrote this memory themselves."
    assert vault.get(mine.id).content == "Uses npm for JavaScript and Node"
    r = props.propose(claude, "archive", memory_id=imported.id)
    assert (r["status"], r["hold"]) == ("pending", "protected") and vault.get(imported.id).status == "active"
    assert {p.hold for p in props.list("pending")} == {"protected"}

    # A memory an AI wrote last can be corrected or retired without asking, by any app.
    r = props.propose(claude, "update", memory_id=ai.id, content="Prefers Ruff over flake8 and black")
    assert (r["status"], r["hold"]) == ("applied", None)
    assert vault.get(ai.id).content == "Prefers Ruff over flake8 and black"
    assert props.propose(claude, "archive", memory_id=other_ai.id)["status"] == "applied"
    assert vault.get(other_ai.id).status == "archived"

    # Accepting a change to an AI-written memory makes it the user's: the next change waits.
    vault.update_settings(require_approval=True)
    r = props.propose(claude, "update", memory_id=ai.id, content="Prefers Ruff for linting and formatting")
    vault.update_settings(require_approval=False)
    accepted = props.accept(r["proposal_id"])
    assert vault.get(ai.id).last_actor == "user" and accepted.applied_version == 3
    assert props.propose(claude, "update", memory_id=ai.id, content="Prefers Ruff")["hold"] == "protected"


def test_an_add_turned_into_an_update_follows_the_target(setup):
    vault, cons, claude, props = setup
    mine = vault.add("tools", "用 Neovim 写代码")
    r = props.propose(claude, "add", content="不再用 Neovim 写代码了")
    p = props.get(r["proposal_id"])
    assert p.op == "update" and p.original_op == "add" and p.memory_id == mine.id and p.conflict
    assert (r["status"], r["hold"], r["memory_id"]) == ("pending", "protected", mine.id) and "note" in r
    assert vault.get(mine.id).content == "用 Neovim 写代码"

    ai = vault.add("tools", "Uses VS Code for Python", **AI)
    r = props.propose(claude, "add", content="Uses VS Code for Python and Go")
    p = props.get(r["proposal_id"])
    assert p.original_op == "add" and p.memory_id == ai.id and r["status"] == "applied"
    assert vault.get(ai.id).content == "Uses VS Code for Python and Go" and p.applied_version == 2

    # A topic the user blocked stays blocked, even when the change arrives as an add filed elsewhere.
    cons.update("claude", blocked_topics=["tools"])
    with pytest.raises(PermissionDenied):
        props.propose(cons.get("claude"), "add", content="Uses VS Code for Python, Go and Rust", topic="interests")
    assert vault.get(ai.id).content == "Uses VS Code for Python and Go"


def test_private_topics_and_memories_always_wait(setup):
    vault, cons, claude, props = setup
    cons.update("claude", include_private=True)
    claude = cons.get("claude")
    cursor = cons.update(cons.ensure("cursor").id, write="auto", include_private=True)
    pills = vault.add("health", "Takes vitamin D", **AI)
    flagged = vault.add("tools", "Uses 1Password for work secrets", private=True, **AI)
    for app in (claude, cursor):
        r = props.propose(app, "add", content="对花生过敏", topic="health")
        assert (r["status"], r["hold"]) == ("pending", "private")
        assert r["message"] == "Waiting for the user: it is in a private topic."
        assert props.propose(app, "update", memory_id=pills.id, content="Takes vitamin D in winter")["hold"] == "private"
        r = props.propose(app, "update", memory_id=flagged.id, content="Uses 1Password for all secrets")
        assert r["hold"] == "private" and r["message"] == "Waiting for the user: the memory is marked private."
        moved = props.propose(app, "update", memory_id=flagged.id, content="Uses 1Password", topic="finance")
        assert moved["hold"] == "private"
    vault.update_settings(require_approval=True)
    assert props.propose(claude, "add", content="Sleeps badly", topic="health")["hold"] == "private"
    assert [m.content for m in vault.memories() if m.topic == "health"] == ["Takes vitamin D"]
    assert vault.get(flagged.id).content == "Uses 1Password for work secrets"


def test_auto_mode_applies_even_to_protected_memories_and_undo_reverts(setup):
    vault, cons, _, props = setup
    cursor = cons.update(cons.ensure("cursor").id, write="auto")
    r = props.propose(cursor, "add", content="Uses pnpm for JavaScript projects", topic="tools")
    assert r["status"] == "applied" and "undo_until" not in r
    p = props.get(r["proposal_id"])
    assert vault.get(p.applied_memory_id).status == "active"
    props.undo(p.id)
    assert vault.get(p.applied_memory_id).status == "archived"

    m = vault.add("tools", "Uses npm")
    r = props.propose(cursor, "update", memory_id=m.id, content="Uses pnpm, not npm")
    assert r["status"] == "applied" and vault.get(m.id).content == "Uses pnpm, not npm"
    props.undo(r["proposal_id"])
    assert vault.get(m.id).content == "Uses npm"

    vault.update_settings(require_approval=True)
    assert props.propose(cursor, "add", content="Writes Go at work", topic="work")["status"] == "applied"


def test_approval_on_holds_everything(setup):
    vault, cons, claude, props = setup
    vault.update_settings(require_approval=True)
    assert "require_approval: true" in vault.settings_path.read_text()
    ai = vault.add("tools", "Uses VS Code", **AI)
    mine = vault.add("work", "CTO at Acme")
    results = [props.propose(claude, "add", content="Plays the piano"),
               props.propose(claude, "update", memory_id=ai.id, content="Uses VS Code and Zed"),
               props.propose(claude, "archive", memory_id=mine.id)]
    assert [(r["status"], r["hold"]) for r in results] == [("pending", "approval")] * 3
    assert results[0]["message"] == "Waiting for the user: they review every change first."
    assert vault.get(ai.id).content == "Uses VS Code" and props.unreviewed() == []

    vault.update_settings(require_approval=False)
    assert props.propose(claude, "add", content="Prefers dark mode everywhere")["status"] == "applied"
    ro = cons.update(cons.ensure("reader").id, write="none")
    with pytest.raises(PermissionDenied):
        props.propose(ro, "add", content="something durable")
    with pytest.raises(VaultError):
        vault.update_settings(colour="blue")


def test_accept_edit_and_reject_with_block(setup):
    vault, cons, claude, props = setup
    vault.update_settings(require_approval=True)
    r = props.propose(claude, "add", content="Likes jazz")
    p = props.accept(r["proposal_id"], content="Likes jazz, especially Bill Evans", topic="interests")
    assert p.status == "edited_accepted" and p.undo_until is None and p.applied_version == 1
    m = vault.get(p.applied_memory_id)
    assert m.content == "Likes jazz, especially Bill Evans" and m.last_actor == "user"
    assert vault.history(m.id)[0]["actor"] == "user" and props.unreviewed() == []

    r = props.propose(claude, "add", content="对青霉素过敏", topic="health")
    props.reject(r["proposal_id"], block_topic=True)
    assert cons.get("claude").blocked_topics == ["health"]
    with pytest.raises(PermissionDenied):
        props.propose(cons.get("claude"), "add", content="吃降压药", topic="health")


def test_duplicates_and_similar_adds(setup):
    vault, _, claude, props = setup
    m = vault.add("tools", "用 Neovim 写代码")
    r = props.propose(claude, "add", content="用 Neovim 写代码")
    assert r == {"status": "duplicate", "memory_id": m.id, "message": r["message"]}
    r = props.propose(claude, "add", content="不再用 Neovim 写代码了")
    p = props.accept(r["proposal_id"])
    assert vault.get(m.id).content == "不再用 Neovim 写代码了"
    # accept as a separate memory instead
    r = props.propose(claude, "add", content="用 Neovim 写 Go 代码")
    assert r["hold"] == "protected"
    p = props.accept(r["proposal_id"], as_new=True)
    assert p.applied_memory_id != m.id and p.op == "add"


def test_update_and_archive_require_readable_target(setup):
    vault, _, claude, props = setup
    secret = vault.add("health", "有轻度哮喘")
    with pytest.raises(VaultError):
        props.propose(claude, "update", memory_id=secret.id, content="哮喘已痊愈")
    m = vault.add("work", "在 Acme 做 CTO")
    r = props.propose(claude, "archive", memory_id=m.id, reason="user left the job")
    props.accept(r["proposal_id"])
    assert vault.get(m.id).status == "archived"


# ---- the digest -------------------------------------------------------------------------------------

def test_reviewing_the_digest(setup):
    vault, _, claude, props = setup
    ids = [props.propose(claude, "add", content=c)["proposal_id"]
           for c in ("Plays the piano", "Has a five-year-old daughter", "Lives in Shanghai")]
    assert [p.id for p in props.unreviewed()] == ids[::-1]  # newest first
    p = props.mark_reviewed(ids[1])
    assert p.reviewed_at and p.status == "applied"
    assert props.mark_reviewed(ids[1]).reviewed_at == p.reviewed_at  # idempotent
    assert [p.id for p in props.unreviewed()] == [ids[2], ids[0]]
    assert props.mark_all_reviewed() == 2
    assert props.unreviewed() == [] and props.mark_all_reviewed() == 0
    assert all(props.get(i).reviewed_at for i in ids)

    vault.update_settings(require_approval=True)
    pending = props.propose(claude, "add", content="Drinks green tea")["proposal_id"]
    with pytest.raises(VaultError):
        props.mark_reviewed(pending)


# ---- undo without a window, edit after the fact ---------------------------------------------------

def test_undo_works_while_the_change_is_the_latest_version(setup):
    vault, _, claude, props = setup
    r = props.propose(claude, "add", content="Uses pnpm", topic="tools")
    p = props.get(r["proposal_id"])
    # A v0.1 deadline long gone doesn't matter any more.
    path = vault.proposals_dir / f"{p.id}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "undo_until": "2020-01-01T00:00:00+00:00"}))
    assert props.can_undo(props.get(p.id))
    undone = props.undo(p.id)
    assert undone.status == "undone" and undone.reviewed_at and props.unreviewed() == []
    assert vault.get(p.applied_memory_id).status == "archived"
    assert not props.can_undo(undone)
    with pytest.raises(VaultError):
        props.undo(p.id)

    ai = vault.add("work", "Works on the iOS app", **AI)
    r = props.propose(claude, "update", memory_id=ai.id, content="Leads the iOS team", topic="history")
    assert vault.get(ai.id).topic == "history"
    props.undo(r["proposal_id"])
    m = vault.get(ai.id)
    assert (m.content, m.topic, m.last_actor) == ("Works on the iOS app", "work", "user")

    r = props.propose(claude, "archive", memory_id=props.propose(claude, "add", content="Owns a cat")["memory_id"])
    archived = props.get(r["proposal_id"])
    assert vault.get(archived.applied_memory_id).status == "archived" and props.can_undo(archived)
    props.undo(archived.id)
    assert vault.get(archived.applied_memory_id).status == "active"


def test_undo_is_refused_once_the_memory_changed(setup):
    vault, _, claude, props = setup
    a = props.propose(claude, "add", content="Uses pnpm", topic="tools")
    b = props.propose(claude, "add", content="Plays chess on Sundays", topic="interests")
    vault.update(a["memory_id"], content="Uses pnpm and bun")  # the user edits it in the panel
    edit_file(vault.memories_dir / "interests.md", "Plays chess on Sundays", "Plays chess on Saturdays")
    for r in (a, b):
        assert not props.can_undo(props.get(r["proposal_id"]))
        with pytest.raises(VaultError, match="changed since this proposal"):
            props.undo(r["proposal_id"])
    assert vault.get(a["memory_id"]).content == "Uses pnpm and bun"
    assert props.as_dict(props.get(a["proposal_id"]))["can_undo"] is False


def test_undo_can_block_the_topic(setup):
    vault, cons, claude, props = setup
    r = props.propose(claude, "add", content="Plays the piano", topic="interests")
    props.undo(r["proposal_id"], block_topic=True)
    assert cons.get("claude").blocked_topics == ["interests"]
    with pytest.raises(PermissionDenied):
        props.propose(cons.get("claude"), "add", content="Plays the violin", topic="interests")

    vault.update_settings(require_approval=True)
    accepted = props.accept(props.propose(claude, "add", content="Reads before bed", topic="lifestyle")["proposal_id"])
    props.undo(accepted.id, block_topic=True)  # an accepted proposal can be undone the same way
    assert cons.get("claude").blocked_topics == ["interests", "lifestyle"]


def test_format_1_proposals_can_still_be_undone(setup):
    vault, _, claude, props = setup
    r = props.propose(claude, "add", content="Uses pnpm", topic="tools")
    path = vault.proposals_dir / f"{r['proposal_id']}.json"
    data = json.loads(path.read_text())
    for key in ("hold", "reviewed_at", "applied_version"):
        data.pop(key)
    path.write_text(json.dumps({**data, "undo_until": "2020-01-01T00:00:00+00:00"}))
    p = props.get(r["proposal_id"])
    assert p.applied_version is None and props.can_undo(p) and props.as_dict(p)["can_undo"]
    props.undo(p.id)
    assert vault.get(r["memory_id"]).status == "archived"

    vault.update_settings(require_approval=True)
    pending = props.propose(claude, "add", content="Plays chess")
    path = vault.proposals_dir / f"{pending['proposal_id']}.json"
    data = json.loads(path.read_text())
    data.pop("hold")
    path.write_text(json.dumps(data))
    assert props.get(pending["proposal_id"]).hold == "approval"  # v0.1 made everything wait


def test_editing_an_applied_change(setup):
    vault, _, claude, props = setup
    r = props.propose(claude, "add", content="Likes jazz", topic="interests")
    p = props.edit_applied(r["proposal_id"], "Likes jazz, especially  Bill Evans")
    m = vault.get(r["memory_id"])
    assert (p.status, p.final_content, p.final_topic) == ("edited_accepted", "Likes jazz, especially Bill Evans",
                                                          "interests")
    assert p.reviewed_at and p.applied_version == m.version == 2 and props.unreviewed() == []
    assert m.content == "Likes jazz, especially Bill Evans" and m.last_actor == "user" and is_protected(m)
    assert vault.history(m.id)[0]["proposal_id"] == p.id
    # The memory is the user's now: an AI's next change waits.
    assert props.propose(claude, "update", memory_id=m.id, content="Likes jazz and blues")["hold"] == "protected"
    p = props.edit_applied(p.id, "Likes jazz", topic="values")
    assert vault.get(m.id).topic == "values" and p.applied_version == 3
    props.undo(p.id)  # undoing an edited addition still removes it
    assert vault.get(m.id).status == "archived"

    kept = props.propose(claude, "add", content="Owns a cat", topic="lifestyle")
    p = props.edit_applied(kept["proposal_id"], "Owns a cat")  # unchanged text: same as keep
    assert p.status == "applied" and p.reviewed_at and vault.get(kept["memory_id"]).last_actor == "connector:claude"

    archive = props.propose(claude, "archive", memory_id=kept["memory_id"])
    with pytest.raises(VaultError):
        props.edit_applied(archive["proposal_id"], "Owns two cats")
    changed = props.propose(claude, "add", content="Drinks oolong", topic="lifestyle")
    vault.update(changed["memory_id"], content="Drinks oolong tea")
    with pytest.raises(VaultError):
        props.edit_applied(changed["proposal_id"], "Drinks green tea")
    with pytest.raises(VaultError):
        props.edit_applied(changed["proposal_id"], "   ")


# ---- unchanged behaviour ----------------------------------------------------------------------------

def test_read_only_connector_and_idempotency(setup):
    vault, cons, claude, props = setup
    ro = cons.update(cons.ensure("reader").id, write="none")
    with pytest.raises(PermissionDenied):
        props.propose(ro, "add", content="x y z")
    a = props.propose(claude, "add", content="住在旧金山", idempotency_key="k1")
    b = props.propose(claude, "add", content="住在旧金山", idempotency_key="k1")
    assert a["proposal_id"] == b["proposal_id"] and b["deduplicated"] and b["status"] == "applied"
    assert len(props.list()) == 1 and len(vault.memories()) == 1


def test_pending_proposals_expire(setup):
    vault, _, claude, props = setup
    vault.update_settings(require_approval=True)
    r = props.propose(claude, "add", content="想学日语")
    path = vault.proposals_dir / f"{r['proposal_id']}.json"
    data = json.loads(path.read_text())
    data["created_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    path.write_text(json.dumps(data))
    assert props.list("pending") == []
    assert props.get(r["proposal_id"]).status == "expired"
