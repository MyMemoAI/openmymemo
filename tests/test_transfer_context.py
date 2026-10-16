import json
import time
from datetime import UTC, date, datetime, timedelta

import pytest
import yaml

from openmymemo import transfer
from openmymemo.connectors import Connectors
from openmymemo.context import compile_context, search
from openmymemo.vault import NotFound, Vault, VaultError

CHATGPT_PASTE = """Saved memories
Memory is full
Prefers concise answers with the conclusion first.
Delete
Is the CTO of Acme, a note-taking startup.
Delete
Uses a MacBook Pro with VS Code and Python.
Delete
Has a daughter who is five years old.
Delete
Prefers concise answers with the conclusion first.
"""

CHATGPT_ZH = """管理记忆
- 用户喜欢用中文交流，回答先给结论
- 用户在 Acme 担任 CTO
- 用户正在学习日语
删除
"""

CLAUDE_EXPORT = """```
[2026-03-02] – Works as CTO at Acme, building an AI notes product
[2026-03-05] – Prefers TypeScript over JavaScript for new projects
[unknown] - Lives in San Francisco
```
"""

MEM0_JSON = json.dumps({"results": [
    {"id": "1", "memory": "Allergic to peanuts", "categories": ["health"], "created_at": "2025-05-01"},
    {"id": "2", "memory": "Enjoys hiking on weekends"},
]})


def test_parse_chatgpt_paste_strips_ui_and_duplicates():
    items = transfer.parse("chatgpt", CHATGPT_PASTE)
    assert [i.content for i in items] == [
        "Prefers concise answers with the conclusion first.",
        "Is the CTO of Acme, a note-taking startup.",
        "Uses a MacBook Pro with VS Code and Python.",
        "Has a daughter who is five years old.",
    ]
    zh = transfer.parse("chatgpt", CHATGPT_ZH)
    assert [i.content for i in zh] == ["用户喜欢用中文交流，回答先给结论", "用户在 Acme 担任 CTO", "用户正在学习日语"]


def test_parse_claude_dated_list():
    items = transfer.parse("claude", CLAUDE_EXPORT)
    assert [(i.created_at, i.content) for i in items] == [
        ("2026-03-02", "Works as CTO at Acme, building an AI notes product"),
        ("2026-03-05", "Prefers TypeScript over JavaScript for new projects"),
        (None, "[unknown] - Lives in San Francisco"),
    ]


def test_parse_strips_block_ids_and_stays_fast_on_long_runs_of_spaces():
    items = transfer.parse("text", "- Likes tea ^ab12\n- Likes coffee\u3000^cd34\n- Uses Vim^ef56")
    assert [i.content for i in items] == ["Likes tea", "Likes coffee", "Uses Vim"]
    start = time.perf_counter()
    transfer.parse("text", "abc" + " " * 100_000 + "x")  # quadratic before: about 15 seconds
    assert time.perf_counter() - start < 1


def _import(vault, source, text):
    return transfer.commit(vault, transfer.plan(vault, transfer.parse(source, text)), source)


def test_import_lands_in_its_own_topic_in_order(vault):
    items = transfer.plan(vault, transfer.parse("chatgpt", CHATGPT_PASTE))
    assert [i.topic for i in items] == [None] * 4  # nothing is classified at import time
    assert [i.kind for i in items][:2] == ["preference", "fact"]
    assert "topic_confidence" not in items[0].to_dict()
    ids = transfer.commit(vault, items, "chatgpt")
    assert len(ids) == 4
    assert [m.id for m in vault.memories()] == ids
    assert {m.topic for m in vault.memories()} == {"from-chatgpt"}
    assert {m.source for m in vault.memories()} == {"import:chatgpt"}
    assert vault.activity("user")[0] == {**vault.activity("user")[0], "action": "import", "memory_ids": ids}

    t = vault.topic("from-chatgpt")
    today = date.today().isoformat()
    assert t["title"] == "From ChatGPT" and t["group"] == "imported" and t["group_name"] == "Imported"
    assert t["description"] == (f"Imported from ChatGPT on {today}, in the original order. "
                                "Sort these into topics when you like.")
    assert not t["private"] and vault.topics()[-1]["slug"] == "from-chatgpt"  # the new group goes last
    text = (vault.memories_dir / "from-chatgpt.md").read_text()
    assert text.startswith("---\ntype: topic\ntitle: From ChatGPT\ndescription: ")
    assert "\ngroup: imported\n---\n" in text
    assert text.index("Prefers concise answers") < text.index("Is the CTO") < text.index("Has a daughter")
    cfg = yaml.safe_load(vault.topics_path.read_text())
    assert cfg["groups"][-1] == {"id": "imported", "name": "Imported", "topics": ["from-chatgpt"]}

    again = transfer.plan(vault, transfer.parse("chatgpt", CHATGPT_PASTE))
    assert all(i.duplicate_of and not i.include for i in again)  # duplicates are still flagged
    assert transfer.commit(vault, again, "chatgpt") == []


def test_claude_dates_survive_the_import(vault):
    ids = _import(vault, "claude", CLAUDE_EXPORT)
    mems = [vault.get(i) for i in ids]
    assert [m.topic for m in mems] == ["from-claude"] * 3
    assert [m.created_at[:10] for m in mems[:2]] == ["2026-03-02", "2026-03-05"]
    assert mems[0].created_at == "2026-03-02T00:00:00+00:00"
    # No date in the source: stamped now, in UTC, so compare instants rather than the local calendar date.
    assert abs(datetime.now(UTC) - datetime.fromisoformat(mems[2].created_at)) < timedelta(minutes=1)
    assert vault.topic("from-claude")["title"] == "From Claude"


def test_a_second_import_appends_and_keeps_the_first_description(vault, monkeypatch):
    _import(vault, "text", "Lives in Berlin\nPlays the cello")
    first = vault.topic("from-text")
    assert first["title"] == "From text"
    assert first["description"].startswith(f"Imported from text on {date.today().isoformat()},")

    class Later(date):
        @classmethod
        def today(cls):
            return cls(2030, 1, 1)

    monkeypatch.setattr("openmymemo.vault.date", Later)
    _import(vault, "text", "Speaks German\nLives in Berlin")
    assert vault.topic("from-text")["description"] == first["description"]
    assert [m.content for m in vault.memories() if m.topic == "from-text"] == [
        "Lives in Berlin", "Plays the cello", "Speaks German"]
    assert [t["slug"] for t in vault.topics() if t["group"] == "imported"] == ["from-text"]


def test_json_import_keeps_dates_and_hints_without_classifying(vault):
    items = transfer.plan(vault, transfer.parse("json", MEM0_JSON))
    assert [(i.content, i.topic, i.hint) for i in items] == [
        ("Allergic to peanuts", None, "health"), ("Enjoys hiking on weekends", None, None)]
    transfer.commit(vault, items, "json")
    peanut = next(m for m in vault.memories() if "peanut" in m.content)
    assert peanut.created_at.startswith("2025-05-01") and peanut.topic == "from-json"
    assert vault.topic("from-json")["title"] == "From JSON"
    with pytest.raises(ValueError):
        transfer.commit(vault, items, "mem0")


def test_openmymemo_json_export_round_trips_into_the_original_topics(tmp_path, vault):
    work = vault.add("work", "CTO at Acme", created_at="2024-02-03T04:05:06+00:00")
    vault.add("people", "Has a daughter who is five")
    _import(vault, "chatgpt", "Prefers green tea")
    exported = transfer.export(vault, "json")

    other = Vault.create(tmp_path / "other")
    items = transfer.plan(other, transfer.parse("json", exported))
    assert [i.topic for i in items] == ["work", "people", None]  # from-chatgpt doesn't exist there
    ids = transfer.commit(other, items, "json")
    assert [other.get(i).topic for i in ids] == ["work", "people", "from-json"]
    assert other.get(ids[0]).created_at == work.created_at

    third = Vault.create(tmp_path / "third")
    named = transfer.plan(third, transfer.parse("json", exported))[:2]
    assert len(transfer.commit(third, named, "json")) == 2
    assert "from-json" not in {t["slug"] for t in third.topics()}  # no empty import topic

    again = transfer.plan(vault, transfer.parse("json", exported))  # into the vault it came from
    assert [i.topic for i in again] == ["work", "people", "from-chatgpt"]
    assert all(i.duplicate_of for i in again)


def test_sort_suggests_and_moves_only_what_is_listed(vault):
    ids = _import(vault, "chatgpt", CHATGPT_PASTE)
    _import(vault, "text", "Grows tomatoes")
    rows = transfer.sort_plan(vault)
    assert [r["memory_id"] for r in rows] == [*ids, rows[-1]["memory_id"]]
    assert set(rows[0]) == {"memory_id", "content", "from_topic", "topic", "confidence"}
    by_content = {r["content"]: r for r in rows}
    assert by_content["Is the CTO of Acme, a note-taking startup."]["topic"] == "work"
    assert by_content["Has a daughter who is five years old."]["topic"] == "people"
    assert by_content["Grows tomatoes"] == {**by_content["Grows tomatoes"], "from_topic": "from-text", "topic": None}
    assert all(r["topic"] is None or not r["topic"].startswith("from-") for r in rows)
    assert 0 < by_content["Is the CTO of Acme, a note-taking startup."]["confidence"] <= 1

    assert [r["from_topic"] for r in transfer.sort_plan(vault, "from-text")] == ["from-text"]
    with pytest.raises(VaultError):
        transfer.sort_plan(vault, "work")  # only imported topics are sorted
    with pytest.raises(NotFound):
        transfer.sort_plan(vault, "nope")

    assert transfer.sort_commit(vault, [(ids[1], "work"), (ids[3], "values")]) == 2
    assert vault.get(ids[1]).topic == "work" and vault.get(ids[3]).topic == "values"
    assert vault.get(ids[0]).topic == "from-chatgpt"  # not listed: stays
    assert vault.history(ids[1])[0]["op"] == "move" and vault.get(ids[1]).last_actor == "user"
    assert transfer.sort_commit(vault, [(ids[1], "work")]) == 0  # already there
    with pytest.raises(NotFound):
        transfer.sort_commit(vault, [(ids[0], "work"), (ids[2], "nope")])
    assert vault.get(ids[0]).topic == "from-chatgpt"  # nothing moved when one move is invalid
    assert len(transfer.sort_plan(vault)) == len(rows) - 2


def test_ai_additions_are_never_filed_into_an_import_topic(vault):
    from openmymemo.proposals import Proposals
    from openmymemo.textutil import classify_topic

    _import(vault, "chatgpt", "Prefers green tea")
    content = "Uses ChatGPT for drafting emails"
    assert classify_topic(content, vault.topics())[0] == "from-chatgpt"  # what would happen unfiltered
    c = Connectors(vault).ensure("claude")
    r = Proposals(vault).propose(c, "add", content=content)
    assert r["topic"] == "tools"


def test_context_topic_index_skips_import_topics_but_shows_their_memories(vault):
    _import(vault, "chatgpt", "Prefers green tea")
    vault.add("work", "CTO at Acme")
    text = compile_context(vault, Connectors(vault).ensure("claude"))["text"]
    index = text.split("Topics to file proposals under", 1)[1].split("\n\n", 1)[0]
    assert "`work`" in index and "from-chatgpt" not in index and "From ChatGPT" not in index
    assert "## From ChatGPT (from-chatgpt)" in text and "Prefers green tea" in text
    assert "Prefers green tea" in vault.memory_md_path.read_text()


def test_exports(vault):
    vault.add("work", "在 Acme 做 CTO")
    vault.add("health", "对青霉素过敏")
    assert "对青霉素过敏" not in transfer.export(vault, "markdown")
    assert "对青霉素过敏" in transfer.export(vault, "markdown", include_private=True)
    claude = transfer.export(vault, "claude")
    assert claude.startswith("[20") and "– 在 Acme 做 CTO" in claude
    assert transfer.export(vault, "chatgpt").startswith("Please remember")
    data = json.loads(transfer.export(vault, "json"))
    assert data["format"] == "openmymemo/1" and len(data["memories"]) == 1


def test_context_respects_scope_order_and_budget(vault):
    cons = Connectors(vault)
    vault.add("work", "在 Acme 做 CTO")
    vault.add("how-to-work-with-me", "回答先给结论再给原因")
    vault.add("health", "对青霉素过敏")
    vault.add("people", "女儿五岁", private=True)
    c = cons.ensure("claude")
    ctx = compile_context(vault, c)
    text = ctx["text"]
    assert text.index("回答先给结论") < text.index("在 Acme 做 CTO")
    assert "青霉素" not in text and "女儿" not in text
    assert "`health`" in text  # topic index still lists where to propose
    full = cons.update("claude", include_private=True)
    assert "青霉素" in compile_context(vault, full)["text"]
    narrow = cons.update("claude", read_topics=["work"], include_private=False)
    assert "回答先给结论" not in compile_context(vault, narrow)["text"]
    for i in range(50):
        vault.add("interests", f"喜欢第 {i} 本书，这是一条比较长的记忆用来测试预算截断的行为")
    small = compile_context(vault, c, max_chars=1500)
    assert small["chars"] <= 1700 and small["omitted"] > 0


def test_search_handles_short_cjk_queries(vault):
    m = vault.add("work", "在 Acme 做 CTO")
    vault.add("interests", "喜欢科幻小说")
    hits = search(vault, None, "work")
    assert [h[1].id for h in hits] == [m.id]  # matched through the topic title
    assert search(vault, None, "科幻")[0][1].content == "喜欢科幻小说"
    assert search(vault, None, "cto")[0][1].id == m.id
    assert search(vault, None, "量子力学") == []


@pytest.mark.parametrize("text,topic", [
    ("Is the CTO of Acme; the stack is FastAPI, Vue 3 and Swift.", "work"),
    ("用户在 Acme 担任 CTO，技术栈是 FastAPI + Vue 3 + Swift", "work"),
    ("Uses a MacBook Pro with VS Code as the main editor.", "tools"),
    ("Has a five-year-old daughter.", "people"),
    ("Goes running every morning at 6.", "lifestyle"),
    ("用户每天早上 6 点起床跑步", "lifestyle"),
    ("Allergic to peanuts.", "health"),
    ("用户正在学习日语，目标是明年能看懂日剧", "learning"),
])
def test_rule_classifier_files_mixed_statements(vault, text, topic):
    from openmymemo.textutil import classify_topic

    assert classify_topic(text, vault.topics())[0] == topic


def test_custom_topic_uses_description_words(vault):
    from openmymemo.textutil import classify_topic

    vault.add_topic("garden", "Garden", "Plants, vegetables, composting and the greenhouse")
    assert classify_topic("Grows tomatoes in the greenhouse", vault.topics())[0] == "garden"


def test_heading_and_item_patterns_match_the_old_lazy_ones_without_their_backtracking():
    import itertools
    import re

    from openmymemo import mdfile

    old_item = re.compile(r"^([-*+])[ \t]+(.*?)[ \t]*$")
    old_h1 = re.compile(r"^#[ \t]+(.*?)[ \t]*#*[ \t]*$")
    old_h2 = re.compile(r"^##[ \t]+(.*?)[ \t]*#*[ \t]*$")
    old_heading = re.compile(r"^\s*(?:#{1,6}\s+(.+?)\s*#*|\*\*(.+?)\*\*:?)\s*$")
    old_comment = re.compile(r"[ \t]*<!--(.*?)-->$")

    def same(old, new, line, text):
        o = old.match(line)
        n = new.match(line)
        assert bool(o) == bool(n), line
        if o:
            assert o.group(1 if old is not old_item else 2) == text(n), line

    for size in range(7):
        for tail in itertools.product("# \ta*:", repeat=size):
            tail = "".join(tail)
            same(old_item, mdfile.ITEM_RE, "-" + tail, lambda m: m.group(2).rstrip(" \t"))
            same(old_h1, mdfile.H1_RE, "#" + tail, mdfile._heading)
            same(old_h2, mdfile.H2_RE, "##" + tail, mdfile._heading)
            line = "#" + tail
            o, n = old_heading.match(line), transfer._HEADING.match(line)
            assert bool(o) == bool(n), line
            if o:
                got = transfer._heading_text(n.group(1)) if n.group(1) else n.group(2)
                assert (o.group(1) or o.group(2)) == got, line
            line = "**" + tail
            o, n = old_heading.match(line), transfer._HEADING.match(line)
            assert bool(o) == bool(n) and (not o or o.group(2) == n.group(2)), line
            line = tail.replace("*", "<!--").replace(":", "-->")
            o, n = old_comment.search(line), mdfile.COMMENT_RE.search(line)
            assert bool(o) == bool(n) and (not o or (o.group(1), line[: o.start()].rstrip())
                                           == (n.group(1), line[: n.start()].rstrip())), line

    start = time.perf_counter()
    for line in ("# a" + " " * 50_000 + "x", "## a" + " " * 50_000 + "x", "- a" + " " * 50_000 + "x"):
        mdfile.MemoryFile(line + "\n")
        transfer.parse("text", line)
    assert time.perf_counter() - start < 1  # the old patterns took minutes on one of these
