"""The folder format as specified in docs/MEMORY_FORMAT.md (format 2), and reading / migrating format 1."""

import json
import os

import pytest
import yaml

from openmymemo.mdfile import MemoryFile, new_topic_file
from openmymemo.vault import NotFound, Vault, VaultError

V1_WORK = "# Work\n> Role, company, team, responsibilities, industry\n\n- CTO at MyMemo ^m7f3a\n- Runs infra ^q1x4e\n"
V1_HEALTH = "# Health\n> Medical history, medication\n"
V1_VALUES = "# Values\n\n> Principles\n> and beliefs\n\nSome prose the user wrote.\n\n## Work ethic\n- Ship small ^v4lu3\n"


def touch_later(path, text):
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def make_v1_vault(root):
    """A vault exactly as OpenMyMemo v0.1 wrote it: header lines, privacy in topics.yaml, no settings.yaml."""
    (root / "memories").mkdir(parents=True)
    (root / ".openmymemo" / "proposals").mkdir(parents=True)
    (root / "memories" / "work.md").write_text(V1_WORK, encoding="utf-8")
    (root / "memories" / "health.md").write_text(V1_HEALTH, encoding="utf-8")
    (root / "memories" / "values.md").write_text(V1_VALUES, encoding="utf-8")
    (root / ".openmymemo" / "topics.yaml").write_text(yaml.safe_dump({
        "format": 1,
        "groups": [{"id": "who", "name": "Who I am", "topics": ["values"]},
                   {"id": "doing", "name": "What I'm doing", "topics": ["work"]},
                   {"id": "private", "name": "Private", "topics": ["health"]}],
        "private": ["health"],
    }, sort_keys=False), encoding="utf-8")
    entry = {"topic": "work", "section": None, "kind": "fact", "source": "user", "created_at": "2026-10-01T00:00:00+00:00",
             "updated_at": "2026-10-01T00:00:00+00:00", "version": 1, "status": "active"}
    (root / ".openmymemo" / "meta.json").write_text(json.dumps({"format": 1, "memories": {
        "m7f3a": {**entry, "content": "CTO at MyMemo"},
        "q1x4e": {**entry, "content": "Runs infra", "version": 2},
        "v4lu3": {**entry, "content": "Ship small", "topic": "values", "section": "Work ethic"},
    }}), encoding="utf-8")
    history = [
        {"at": "2026-10-01T00:00:00+00:00", "memory_id": "m7f3a", "version": 1, "op": "create", "content": "CTO at MyMemo",
         "topic": "work", "actor": "user"},
        {"at": "2026-10-01T00:00:00+00:00", "memory_id": "q1x4e", "version": 1, "op": "create", "content": "Runs infra",
         "topic": "work", "actor": "user"},
        {"at": "2026-10-02T00:00:00+00:00", "memory_id": "q1x4e", "version": 2, "op": "update", "content": "Runs infra",
         "topic": "work", "actor": "connector:claude-code", "proposal_id": "p1"},
    ]
    (root / ".openmymemo" / "history.jsonl").write_text("".join(json.dumps(h) + "\n" for h in history), encoding="utf-8")
    (root / "connectors.yaml").write_text("format: 1\nconnectors: []\n", encoding="utf-8")
    return root


# ---- §3.2–3.4 frontmatter and the body ------------------------------------------------------------

def test_frontmatter_is_parsed_and_unknown_keys_survive():
    mf = MemoryFile("---\ntype: topic\ntitle: Work\ngroup: doing\nprivate: true\ntags: [a, b]\n---\n\n- x ^xxxxx\n")
    assert mf.frontmatter_type == "topic" and mf.title == "Work" and mf.group == "doing" and mf.private
    assert mf.frontmatter["tags"] == ["a", "b"]
    mf.set_frontmatter({**mf.frontmatter, "title": "Job", "private": False})
    assert mf.render().startswith("---\ntype: topic\ntitle: Job\ngroup: doing\nprivate: false\ntags:\n- a\n- b\n---\n\n- x ^xxxxx\n")


def test_new_topic_file_writes_frontmatter_only():
    text = new_topic_file("Health", "Medical history: medication", group="private", private=True)
    assert text == "---\ntype: topic\ntitle: Health\ndescription: 'Medical history: medication'\ngroup: private\nprivate: true\n---\n"
    mf = MemoryFile(text)
    assert mf.title == "Health" and mf.description == "Medical history: medication" and mf.private
    mf.add("Allergic to penicillin", "aaaaa")
    assert mf.render() == text + "\n- Allergic to penicillin ^aaaaa\n"


def test_frontmatter_title_wins_over_a_heading():
    mf = MemoryFile("---\ntype: topic\ntitle: Work\n---\n# Old heading\n- x ^xxxxx\n")
    assert mf.title == "Work" and mf.heading_title == "Old heading"
    assert mf.render() == "---\ntype: topic\ntitle: Work\n---\n# Old heading\n- x ^xxxxx\n"  # nothing rewritten


def test_broken_frontmatter_is_tolerated_and_never_rewritten():
    mf = MemoryFile("---\n: [bad\n---\n# Weird\n- a line ^aaaaa\n")
    assert mf.frontmatter_broken and mf.frontmatter_type is None and mf.title == "Weird"
    assert [i.id for i in mf.items] == ["aaaaa"]
    assert mf.migrate_header() is False


# ---- §11 migration of a format-1 file --------------------------------------------------------------

def test_migrate_header_moves_title_and_description_into_frontmatter():
    mf = MemoryFile(V1_VALUES)
    assert mf.migrate_header(group="who", private=False)
    assert mf.render() == (
        "---\ntype: topic\ntitle: Values\ndescription: Principles and beliefs\ngroup: who\n---\n\n"
        "Some prose the user wrote.\n\n## Work ethic\n- Ship small ^v4lu3\n")
    assert mf.migrate_header() is False  # already format 2


def test_migrate_header_keeps_existing_frontmatter_keys():
    mf = MemoryFile("---\ntags: [memory]\ntitle: Kept\n---\n# Ignored heading\n> desc\n\n- x ^xxxxx\n")
    assert mf.migrate_header(private=True)
    assert mf.render() == "---\ntype: topic\ntitle: Kept\ndescription: desc\nprivate: true\ntags:\n- memory\n---\n\n- x ^xxxxx\n"


def test_migrate_header_of_an_empty_topic():
    mf = MemoryFile(V1_HEALTH)
    mf.migrate_header(private=True)
    assert mf.render() == "---\ntype: topic\ntitle: Health\ndescription: Medical history, medication\nprivate: true\n---\n"


def test_format_1_vault_is_migrated_once(tmp_path):
    root = make_v1_vault(tmp_path / "v1")
    vault = Vault(root)
    topics = {t["slug"]: t for t in vault.topics()}

    # Files: header lines became frontmatter, the body is byte for byte what it was.
    work = (root / "memories" / "work.md").read_text()
    assert work == ("---\ntype: topic\ntitle: Work\ndescription: Role, company, team, responsibilities, industry\n"
                    "group: doing\n---\n\n- CTO at MyMemo ^m7f3a\n- Runs infra ^q1x4e\n")
    assert (root / "memories" / "health.md").read_text().endswith("group: private\nprivate: true\n---\n")
    assert topics["health"]["private"] and not topics["work"]["private"]
    assert topics["values"]["description"] == "Principles and beliefs" and topics["values"]["sections"] == ["Work ethic"]
    assert [t["group"] for t in vault.topics()] == ["who", "doing", "private"]

    # topics.yaml: format 2, order only.
    cfg = yaml.safe_load((root / ".openmymemo" / "topics.yaml").read_text())
    assert cfg["format"] == 2 and "private" not in cfg
    assert [g["topics"] for g in cfg["groups"]] == [["values"], ["work"], ["health"]]

    # meta.json: format 2, last_actor from the latest history record (user when there is none).
    meta = json.loads((root / ".openmymemo" / "meta.json").read_text())
    assert meta["format"] == 2
    assert meta["memories"]["m7f3a"]["last_actor"] == "user"
    assert meta["memories"]["q1x4e"]["last_actor"] == "connector:claude-code"
    assert vault.get("q1x4e").last_actor == "connector:claude-code" and vault.get("q1x4e").version == 2
    assert not any(h["actor"] == "file" for h in vault.history())  # migration is not an edit

    # settings.yaml: written so the vault keeps v0.1's behaviour.
    assert yaml.safe_load((root / ".openmymemo" / "settings.yaml").read_text()) == {"format": 1, "require_approval": True}
    assert (root / "MEMORY.md").exists()

    # Opening it again changes nothing.
    before = {p.name: p.read_text() for p in (root / "memories").glob("*.md")}
    before["topics.yaml"] = (root / ".openmymemo" / "topics.yaml").read_text()
    before["meta.json"] = (root / ".openmymemo" / "meta.json").read_text()
    Vault(root).topics()
    after = {p.name: p.read_text() for p in (root / "memories").glob("*.md")}
    after["topics.yaml"] = (root / ".openmymemo" / "topics.yaml").read_text()
    after["meta.json"] = (root / ".openmymemo" / "meta.json").read_text()
    assert after == before


@pytest.mark.parametrize(("settings", "expected"), [
    ("format: 1\nrequire_approval: false\n", False),  # the user had turned approval off in v0.1
    ("format: 1\n", True),  # v0.1 defaulted to on
])
def test_migration_keeps_the_v1_approval_behaviour(tmp_path, settings, expected):
    root = make_v1_vault(tmp_path / "v1")
    (root / ".openmymemo" / "settings.yaml").write_text(settings, encoding="utf-8")
    vault = Vault(root)
    vault.topics()  # the first read migrates
    assert vault.settings() == {"require_approval": expected}
    assert Vault.create(tmp_path / "new").settings() == {"require_approval": False}  # new vaults: off


# ---- §3.2 / §6.2 groups and privacy come from the file ---------------------------------------------

def test_frontmatter_group_wins_over_topics_yaml(vault):
    path = vault.memories_dir / "work.md"
    touch_later(path, path.read_text().replace("group: doing", "group: who"))
    topics = {t["slug"]: t for t in vault.topics()}
    assert topics["work"]["group"] == "who"
    cfg = yaml.safe_load(vault.topics_path.read_text())
    by_id = {g["id"]: g["topics"] for g in cfg["groups"]}
    assert "work" in by_id["who"] and "work" not in by_id["doing"]


def test_a_new_group_in_frontmatter_is_created(vault):
    path = vault.memories_dir / "interests.md"
    touch_later(path, path.read_text().replace("group: ways", "group: fun-stuff"))
    t = vault.topic("interests")
    assert t["group"] == "fun-stuff" and t["group_name"] == "Fun stuff"
    assert vault.topics()[-1]["slug"] == "interests"  # new groups go last


def test_privacy_is_read_from_and_written_to_frontmatter(vault):
    path = vault.memories_dir / "values.md"
    touch_later(path, path.read_text().replace("group: who", "group: who\nprivate: true"))
    assert "values" in vault.private_topics()
    assert "values" in Vault(vault.root).private_topics()  # a fresh instance syncs before answering

    vault.update_topic("values", private=False, title="Beliefs", description="")
    text = path.read_text()
    assert text.startswith("---\ntype: topic\ntitle: Beliefs\ngroup: who\n---\n")
    assert "private" not in text and "description" not in text
    assert "values" not in vault.private_topics() and vault.topic("values")["title"] == "Beliefs"
    assert "## Beliefs (values)" not in vault.memory_md_path.read_text()  # empty topics aren't listed
    vault.add("values", "Honesty first")
    assert "## Beliefs (values)" in vault.memory_md_path.read_text()


def test_update_topic_refuses_a_file_it_cannot_rewrite_safely(vault):
    (vault.memories_dir / "weird.md").write_text("---\n: [bad\n---\n# Weird\n- a line\n", encoding="utf-8")
    t = vault.topic("weird")
    assert t["title"] == "Weird" and t["group"] == "other" and t["count"] == 1
    assert (vault.memories_dir / "weird.md").read_text().startswith("---\n: [bad\n---\n# Weird\n- a line ^")
    with pytest.raises(VaultError):
        vault.update_topic("weird", title="Fixed")


def test_add_topic_writes_frontmatter_and_orders_it(vault):
    t = vault.add_topic("faith", "Faith", "Religion and spiritual life", group="who", private=True)
    assert t["private"] and t["group"] == "who" and t["group_name"] == "Who I am"
    assert (vault.memories_dir / "faith.md").read_text() == (
        "---\ntype: topic\ntitle: Faith\ndescription: Religion and spiritual life\ngroup: who\nprivate: true\n---\n")
    cfg = yaml.safe_load(vault.topics_path.read_text())
    assert next(g for g in cfg["groups"] if g["id"] == "who")["topics"] == ["identity", "history", "values", "faith"]
    named = vault.add_topic("cats", "Cats", group="Pets and animals")
    assert named["group"] == "pets-and-animals" and named["group_name"] == "Pets and animals"
    with pytest.raises(VaultError):
        vault.add_topic("log", "Log")


# ---- §3.1 / §3.6 files that are not topics ---------------------------------------------------------

def test_reserved_names_and_documents_are_left_alone(vault):
    doc = "---\ntype: document\ntitle: Notes\n---\n\n- looks like a memory but is prose\n"
    (vault.memories_dir / "notes.md").write_text(doc, encoding="utf-8")
    (vault.memories_dir / "log.md").write_text("- not a memory\n", encoding="utf-8")
    (vault.memories_dir / "index.md").write_text("- not a memory\n", encoding="utf-8")
    (vault.memories_dir / "Not A Slug.md").write_text("- not a memory\n", encoding="utf-8")
    slugs = {t["slug"] for t in vault.topics()}
    assert not slugs & {"notes", "log", "index", "Not A Slug"}
    assert (vault.memories_dir / "notes.md").read_text() == doc  # no id was added
    assert (vault.memories_dir / "log.md").read_text() == "- not a memory\n"
    assert all(m.content != "not a memory" for m in vault.memories())
    with pytest.raises(NotFound):
        vault.add("notes", "x")


def test_handwritten_file_without_frontmatter_becomes_a_topic(vault):
    (vault.memories_dir / "cats.md").write_text("- Has two cats\n", encoding="utf-8")
    t = vault.topic("cats")
    assert t["title"] == "cats" and t["group"] == "other" and t["count"] == 1
    text = (vault.memories_dir / "cats.md").read_text()
    assert text.startswith("---\ntype: topic\n---\n\n- Has two cats ^")
    m = next(m for m in vault.memories() if m.topic == "cats")
    assert m.source == "user" and m.last_actor == "file"


# ---- §6.3 last_actor ------------------------------------------------------------------------------

def test_last_actor_follows_who_wrote_the_latest_version(vault):
    m = vault.add("work", "Uses pnpm", source="connector:cursor", actor="connector:cursor")
    assert vault.get(m.id).last_actor == "connector:cursor"
    vault.update(m.id, pinned=True)  # an attribute tweak is not a new version
    assert vault.get(m.id).last_actor == "connector:cursor" and vault.get(m.id).version == 1

    path = vault.memories_dir / "work.md"
    touch_later(path, path.read_text().replace("Uses pnpm", "Uses pnpm everywhere"))
    assert vault.get(m.id).last_actor == "file" and vault.get(m.id).version == 2

    vault.update(m.id, content="Uses pnpm for JavaScript", actor="user")
    assert vault.get(m.id).last_actor == "user"
    vault.archive(m.id, actor="connector:cursor")
    assert vault.get(m.id).last_actor == "connector:cursor"
    vault.restore(m.id)
    assert vault.get(m.id).last_actor == "user"
    assert json.loads(vault.meta_path.read_text())["format"] == 2
