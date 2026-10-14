import json
import os
import time

import pytest

from openmymemo.vault import NotFound, Vault, VaultError


def touch_later(path, text):
    # Make sure the mtime changes even on coarse-grained filesystems.
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_init_creates_default_taxonomy(vault):
    topics = vault.topics()
    assert [t["slug"] for t in topics][:3] == ["identity", "history", "values"]
    assert len(topics) == 14
    health = next(t for t in topics if t["slug"] == "health")
    assert health["private"] and health["title"] == "Health"
    text = (vault.root / "memories" / "how-to-work-with-me.md").read_text()
    assert text.startswith("---\ntype: topic\ntitle: How to work with me\ndescription: ")
    assert "group: ways\n---\n" in text and "private" not in text
    assert "private: true" in (vault.root / "memories" / "health.md").read_text()
    assert [g["name"] for g in vault._topics_cfg["groups"]][0] == "Who I am"
    assert vault.settings() == {"require_approval": False, "onboarded": False} and vault.settings_path.exists()
    assert vault.memory_md_path.exists()
    with pytest.raises(VaultError):
        Vault.create(vault.root)


def test_add_update_archive_restore_and_history(vault):
    m = vault.add("work", "在 Acme 做 CTO", source="user")
    path = vault.memories_dir / "work.md"
    assert f"- 在 Acme 做 CTO ^{m.id}" in path.read_text()

    m2 = vault.update(m.id, content="在 Acme 做 CTO，兼管基础设施")
    assert m2.version == 2
    assert f"- 在 Acme 做 CTO，兼管基础设施 ^{m.id}" in path.read_text()

    moved = vault.update(m.id, topic="history")
    assert moved.topic == "history"
    assert m.id not in path.read_text()
    assert m.id in (vault.memories_dir / "history.md").read_text()

    vault.archive(m.id)
    assert m.id not in (vault.memories_dir / "history.md").read_text()
    assert vault.get(m.id).status == "archived"
    vault.restore(m.id)
    assert vault.get(m.id).status == "active"

    ops = [h["op"] for h in vault.history(m.id)][::-1]
    assert ops == ["create", "update", "move", "archive", "restore"]

    vault.rollback(m.id, 1)
    assert vault.get(m.id).content == "在 Acme 做 CTO"
    assert vault.get(m.id).topic == "work"


def test_user_edits_in_editor_are_reconciled(vault):
    path = vault.memories_dir / "interests.md"
    touch_later(path, path.read_text() + "\n- 喜欢科幻小说\n- 周末跑步\n")
    mems = [m for m in vault.memories() if m.topic == "interests"]
    assert [m.content for m in mems] == ["喜欢科幻小说", "周末跑步"]
    assert all(m.source == "user" for m in mems)
    text = path.read_text()
    assert f"- 喜欢科幻小说 ^{mems[0].id}" in text  # ids were written back

    # edit one line and delete the other by hand
    text = text.replace("喜欢科幻小说", "喜欢硬科幻小说").replace(f"- 周末跑步 ^{mems[1].id}\n", "")
    touch_later(path, text)
    assert vault.get(mems[0].id).content == "喜欢硬科幻小说"
    assert vault.get(mems[0].id).version == 2
    assert vault.get(mems[1].id).status == "archived"
    actors = {h["actor"] for h in vault.history(mems[0].id)}
    assert actors == {"file"}


def test_line_moved_between_files_keeps_identity(vault):
    m = vault.add("tools", "用 Neovim 写代码")
    tools, learning = vault.memories_dir / "tools.md", vault.memories_dir / "learning.md"
    line = f"- 用 Neovim 写代码 ^{m.id}\n"
    touch_later(tools, tools.read_text().replace(line, ""))
    touch_later(learning, learning.read_text() + "\n" + line)
    got = vault.get(m.id)
    assert got.topic == "learning" and got.status == "active"


def test_duplicate_ids_across_files_are_split(vault):
    m = vault.add("tools", "用 Neovim")
    learning = vault.memories_dir / "learning.md"
    touch_later(learning, learning.read_text() + f"\n- 用 Neovim ^{m.id}\n")
    ids = [x.id for x in vault.memories() if x.content == "用 Neovim"]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_attrs_and_privacy(vault):
    m = vault.add("people", "女儿今年五岁", private=True)
    assert "<!-- private -->" in (vault.memories_dir / "people.md").read_text()
    assert vault.is_private(vault.get(m.id))
    vault.update(m.id, private=False, until="2027-01")
    text = (vault.memories_dir / "people.md").read_text()
    assert f"- 女儿今年五岁 <!-- until: 2027-01 --> ^{m.id}" in text
    assert "女儿" in vault.memory_md_path.read_text()
    h = vault.add("health", "对青霉素过敏")
    assert vault.is_private(vault.get(h.id))
    assert "青霉素" not in vault.memory_md_path.read_text()


def test_projects_get_review_date_when_added_by_ai(vault):
    m = vault.add("projects", "在写 OpenMyMemo", source="connector:claude")
    assert m.until
    assert vault.add("projects", "手写项目", source="user").until is None


def test_meta_is_rebuilt_consistently_by_another_process(vault):
    m = vault.add("work", "做 CTO")
    other = Vault(vault.root)
    assert other.get(m.id).content == "做 CTO"
    other.update(m.id, content="做 CTO 和 CEO")
    time.sleep(0.01)
    assert vault.get(m.id).content == "做 CTO 和 CEO"
    meta = json.loads(vault.meta_path.read_text())
    assert meta["memories"][m.id]["version"] == 2


def test_errors(vault):
    with pytest.raises(NotFound):
        vault.add("nope", "x")
    with pytest.raises(VaultError):
        vault.add("work", "   ")
    with pytest.raises(NotFound):
        vault.get("zzzzz")


def test_custom_topic(vault):
    t = vault.add_topic("faith", "信仰", "宗教与精神生活", group="who", private=True)
    assert t["private"] and t["group"] == "who"
    vault.update_topic("faith", title="信仰与精神", description="")
    t = vault.topic("faith")
    assert t["title"] == "信仰与精神" and t["description"] is None


def test_open_or_create_for_plugins(tmp_path):
    fresh = Vault.open_or_create(tmp_path / "new")
    assert fresh.is_initialized()
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_text("")
    assert Vault.open_or_create(empty).is_initialized()
    assert Vault.open_or_create(tmp_path / "new").root == fresh.root  # opens, doesn't recreate
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    with pytest.raises(VaultError):
        Vault.open_or_create(busy)
