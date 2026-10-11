import itertools

from openmymemo.mdfile import MemoryFile, parse_item_body, render_item

SAMPLE = """---
tags: [memory]
---
# 工作
> 职位、公司、团队、职责、行业

Some free prose the user wrote.

- 在 MyMemo 做 CTO ^m7f3a
- 2026-09 起在用 Claude Agent SDK <!-- until: 2026-12 --> ^k2p9d
  - a nested note that is not a memory
* star bullet without id

```
- not a memory, inside a fence
```

## 团队
- 后端两人 ^q1x4e

---
Trailing prose.
"""


def ids():
    counter = itertools.count()
    return lambda: f"n{next(counter)}"


def test_round_trip_is_identity():
    for text in [SAMPLE, "", "# Only title", "# T\r\n> d\r\n\r\n- a ^aaaaa\r\n", "no newline - at end"]:
        assert MemoryFile(text).render() == text


def test_parse_structure():
    mf = MemoryFile(SAMPLE)
    assert mf.title == "工作"
    assert mf.description == "职位、公司、团队、职责、行业"
    assert [s for s, _ in mf.sections] == ["团队"]
    assert [(i.content, i.id, i.section) for i in mf.items] == [
        ("在 MyMemo 做 CTO", "m7f3a", None),
        ("2026-09 起在用 Claude Agent SDK", "k2p9d", None),
        ("star bullet without id", None, None),
        ("后端两人", "q1x4e", "团队"),
    ]
    assert mf.items[1].attrs == {"until": "2026-12"}


def test_assign_ids_only_touches_missing():
    mf = MemoryFile(SAMPLE)
    changed = mf.assign_ids(ids())
    assert [c.content for c in changed] == ["star bullet without id"]
    out = mf.render()
    assert "* star bullet without id ^n0" in out
    assert out.replace("* star bullet without id ^n0", "* star bullet without id") == SAMPLE


def test_id_before_comment_is_tolerated():
    assert parse_item_body("text ^abcde <!-- private -->") == ("text", "abcde", {"private": True})
    assert render_item("text", "abcde", {"private": True}) == "- text <!-- private --> ^abcde"


def test_add_into_region_and_section():
    mf = MemoryFile(SAMPLE)
    mf.add("new top-level", "aaaaa")
    mf.add("new in team", "bbbbb", section="团队")
    mf.add("new section", "ccccc", section="客户")
    out = mf.render()
    assert "* star bullet without id\n- new top-level ^aaaaa\n" in out
    assert "- 后端两人 ^q1x4e\n- new in team ^bbbbb\n" in out
    assert out.endswith("Trailing prose.\n\n## 客户\n\n- new section ^ccccc\n")
    assert MemoryFile(out).find("ccccc").section == "客户"


def test_add_to_empty_topic_file():
    mf = MemoryFile("# 身份\n> 名字、称呼\n")
    mf.add("叫我 Leo", "aaaaa")
    assert mf.render() == "# 身份\n> 名字、称呼\n\n- 叫我 Leo ^aaaaa\n"
    mf.add("住在旧金山", "bbbbb")
    assert mf.render() == "# 身份\n> 名字、称呼\n\n- 叫我 Leo ^aaaaa\n- 住在旧金山 ^bbbbb\n"


def test_add_before_existing_section_when_top_is_empty():
    mf = MemoryFile("# T\n\n## S\n- x ^xxxxx\n")
    mf.add("top", "aaaaa")
    out = mf.render()
    assert out == "# T\n\n- top ^aaaaa\n\n## S\n- x ^xxxxx\n"


def test_set_and_remove():
    mf = MemoryFile(SAMPLE)
    mf.set_line(mf.find("m7f3a"), "在 MyMemo 做 CTO 和产品", {"private": True})
    assert "- 在 MyMemo 做 CTO 和产品 <!-- private --> ^m7f3a" in mf.render()
    assert mf.remove("k2p9d")
    assert "k2p9d" not in mf.render()
    assert "  - a nested note that is not a memory" in mf.render()


def test_multiline_content_is_collapsed():
    assert render_item("a\nb   c", "x1234", {}) == "- a b c ^x1234"
