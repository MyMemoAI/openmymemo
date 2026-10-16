"""Parse and rewrite a topic file (``memories/<topic>.md``) without disturbing anything we don't own.

The format is specified in docs/MEMORY_FORMAT.md. A topic file is ordinary Markdown. We only interpret:

* the YAML frontmatter (``type``, ``title``, ``description``, ``group``, ``private``; unknown keys are kept);
* for files without frontmatter (format 1), the first ``# Heading`` (title) and the blockquote after it
  (description) — ``migrate_header`` moves them into frontmatter;
* ``## Heading`` lines (optional sections inside the topic);
* top-level list items (``- ``, ``* ``, ``+ `` at column 0): one item = one memory.

A memory line looks like::

    - The memory text <!-- until: 2026-12; private --> ^k2p9d

The trailing ``^id`` is an Obsidian-compatible block id; the HTML comment carries the few attributes a
user might want to edit by hand. Every other line (prose, nested bullets, code, front matter) is kept
byte for byte, so parse -> render is the identity for files we haven't modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# Items and headings take the rest of the line greedily; the parser trims trailing spaces and _heading closing #s.
# A lazy `(.*?)` before `[ \t]*…$` backtracks over every split of a long run of spaces (seconds for a
# 2,000-space heading), which is the shape CodeQL flags as polynomial-redos.
ITEM_RE = re.compile(r"^([-*+])[ \t]+(.*)$")
HR_RE = re.compile(r"^([-*_])([ \t]*\1){2,}[ \t]*$")
ID_RE = re.compile(r"(?:^|[ \t])\^([A-Za-z0-9-]+)$")
# The spaces before the comment are rstripped by the caller (a leading `[ \t]*` rescanned them). Lines never
# hold a newline; DOTALL only says so to CodeQL's py/bad-tag-filter, which reads this as an HTML filter.
COMMENT_RE = re.compile(r"<!--(.*?)-->$", re.DOTALL)
FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
H1_RE = re.compile(r"^#[ \t]+(.*)$")
H2_RE = re.compile(r"^##[ \t]+(.*)$")
QUOTE_RE = re.compile(r"^>[ \t]?(.*)$")


def _heading(m: re.Match) -> str:
    """A heading's text without the optional closing #s (`## Work ##`) and the spaces around them."""
    return m.group(1).rstrip(" \t").rstrip("#").rstrip(" \t")


ATTR_KEYS = ("until", "private")
FRONTMATTER_KEYS = ("type", "title", "description", "group", "private")  # written first, in this order


@dataclass
class Item:
    line_no: int
    content: str
    id: str | None = None
    attrs: dict[str, str | bool] = field(default_factory=dict)
    section: str | None = None
    marker: str = "-"


def parse_attrs(raw: str) -> dict[str, str | bool]:
    attrs: dict[str, str | bool] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, value = part.split(":", 1)
            attrs[key.strip().lower()] = value.strip()
        else:
            attrs[part.lower()] = True
    return attrs


def render_attrs(attrs: dict[str, str | bool]) -> str:
    parts = []
    for key, value in attrs.items():
        if value is False or value is None or value == "":
            continue
        parts.append(key if value is True else f"{key}: {value}")
    return "; ".join(parts)


def parse_item_body(body: str) -> tuple[str, str | None, dict[str, str | bool]]:
    """Split ``text <!-- attrs --> ^id`` into its parts. Tolerates ``^id`` before the comment."""
    item_id = None
    attrs: dict[str, str | bool] = {}
    m = ID_RE.search(body)
    if m:
        item_id = m.group(1)
        body = body[: m.start()].rstrip()
    m = COMMENT_RE.search(body)
    if m:
        attrs = parse_attrs(m.group(1))
        body = body[: m.start()].rstrip()
        if item_id is None:
            m = ID_RE.search(body)
            if m:
                item_id = m.group(1)
                body = body[: m.start()].rstrip()
    return body.strip(), item_id, attrs


def render_item(content: str, item_id: str | None, attrs: dict[str, str | bool], marker: str = "-") -> str:
    content = " ".join(content.split())  # one memory = one line
    line = f"{marker} {content}"
    rendered = render_attrs(attrs)
    if rendered:
        line += f" <!-- {rendered} -->"
    if item_id:
        line += f" ^{item_id}"
    return line


def render_frontmatter(mapping: dict) -> list[str]:
    """The lines of a frontmatter block, known keys first in a fixed order, unknown keys after."""
    ordered = {k: mapping[k] for k in FRONTMATTER_KEYS if k in mapping}
    ordered.update({k: v for k, v in mapping.items() if k not in ordered})
    body = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100_000)
    return ["---", *body.rstrip("\n").split("\n"), "---"]


class MemoryFile:
    def __init__(self, text: str = ""):
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.trailing_newline = text.endswith("\n") or text == ""
        body = text[: -len(self.newline)] if text.endswith(self.newline) else text
        self.lines: list[str] = body.split(self.newline) if body else []
        self._reparse()

    # ---- parsing -------------------------------------------------------------------------------

    def _reparse(self) -> None:
        self.heading_title: str | None = None  # from a ``# Title`` line (format 1)
        self.title_line: int | None = None
        self.heading_description: str | None = None  # from the ``> …`` quote after it (format 1)
        self.description_end: int | None = None  # index of last description line
        self.sections: list[tuple[str, int]] = []
        self.items: list[Item] = []
        self.frontmatter: dict | None = None  # None: no block; {} and frontmatter_broken: unreadable block
        self.frontmatter_broken = False
        self.fm_end: int | None = None  # index of the closing ``---``

        in_fence = False
        fence_token = ""
        section: str | None = None
        start = 0
        if self.lines and self.lines[0].strip() == "---":
            for i in range(1, len(self.lines)):
                if self.lines[i].strip() in ("---", "..."):
                    start = i + 1
                    self.fm_end = i
                    try:
                        data = yaml.safe_load("\n".join(self.lines[1:i]))
                    except yaml.YAMLError:
                        data = None
                    if data is None and not any(line.strip() for line in self.lines[1:i]):
                        data = {}
                    if isinstance(data, dict):
                        self.frontmatter = data
                    else:
                        self.frontmatter, self.frontmatter_broken = {}, True
                    break

        desc_lines: list[str] = []
        collecting_desc = False
        for i in range(start, len(self.lines)):
            line = self.lines[i]
            fm = FENCE_RE.match(line)
            if fm:
                if not in_fence:
                    in_fence, fence_token = True, fm.group(1)
                elif fm.group(1) == fence_token:
                    in_fence = False
                collecting_desc = False
                continue
            if in_fence:
                continue

            if collecting_desc:
                qm = QUOTE_RE.match(line)
                if qm:
                    desc_lines.append(qm.group(1).strip())
                    self.description_end = i
                    continue
                collecting_desc = False
                if not line.strip() and not desc_lines:
                    collecting_desc = True  # allow one blank line between title and description
                    continue

            h2 = H2_RE.match(line)
            if h2:
                section = _heading(h2).strip()
                self.sections.append((section, i))
                continue
            h1 = H1_RE.match(line)
            if h1:
                if self.heading_title is None and not self.items and not self.sections:
                    self.heading_title = _heading(h1).strip()
                    self.title_line = i
                    collecting_desc = True
                continue
            if HR_RE.match(line):
                continue
            im = ITEM_RE.match(line)
            if im:
                content, item_id, attrs = parse_item_body(im.group(2).rstrip(" \t"))
                if content:
                    self.items.append(Item(i, content, item_id, attrs, section, im.group(1)))
        if desc_lines:
            self.heading_description = " ".join(d for d in desc_lines if d)
        fm = self.frontmatter or {}
        self.title: str | None = str(fm["title"]) if fm.get("title") is not None else self.heading_title
        self.description: str | None = (str(fm["description"]) if fm.get("description") is not None
                                        else self.heading_description)

    @property
    def frontmatter_type(self) -> str | None:
        """The ``type`` key, or None for a format-1 file (no frontmatter, or none that names a type)."""
        value = (self.frontmatter or {}).get("type")
        return str(value) if value else None

    @property
    def private(self) -> bool:
        value = (self.frontmatter or {}).get("private")
        return bool(value) and str(value).strip().lower() not in ("false", "no", "off", "0")

    @property
    def group(self) -> str | None:
        value = (self.frontmatter or {}).get("group")
        return str(value) if value else None

    # ---- rendering -----------------------------------------------------------------------------

    def render(self) -> str:
        text = self.newline.join(self.lines)
        if self.trailing_newline and self.lines:
            text += self.newline
        return text

    # ---- edits ---------------------------------------------------------------------------------

    def find(self, item_id: str) -> Item | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def set_line(self, item: Item, content: str, attrs: dict[str, str | bool] | None = None) -> None:
        self.lines[item.line_no] = render_item(content, item.id, item.attrs if attrs is None else attrs, item.marker)
        self._reparse()

    def set_frontmatter(self, mapping: dict) -> None:
        """Replace (or insert) the frontmatter block. The body is untouched."""
        block = render_frontmatter(mapping)
        if self.fm_end is not None:
            self.lines[0 : self.fm_end + 1] = block
        else:
            self.lines[0:0] = block + ([""] if self.lines and self.lines[0].strip() else [])
            self.trailing_newline = True
        self._reparse()

    def migrate_header(self, *, group: str | None = None, private: bool = False) -> bool:
        """Format 1 → 2: move ``# Title`` and ``> description`` into frontmatter and drop those lines.

        ``group`` and ``private`` are the values the old ``topics.yaml`` held for this file. Keys already in
        the frontmatter win. Returns False when there is nothing to do or the block can't be read.
        """
        if self.frontmatter_broken or self.frontmatter_type is not None:
            return False
        fm = dict(self.frontmatter or {})
        new: dict = {"type": "topic"}
        for key, fallback in (("title", self.heading_title), ("description", self.heading_description),
                              ("group", group), ("private", private)):
            value = fm.get(key, fallback)
            if value is not None and value is not False and value != "":
                new[key] = value
        new.update({k: v for k, v in fm.items() if k not in new})

        if self.title_line is not None:
            last = self.description_end if self.description_end is not None else self.title_line
            del self.lines[self.title_line : last + 1]
            body_start = (self.fm_end + 1) if self.fm_end is not None else 0
            while len(self.lines) > body_start + 1 and not self.lines[body_start].strip() \
                    and not self.lines[body_start + 1].strip():
                del self.lines[body_start]
            if self.lines[body_start:] and self.lines[body_start].strip():
                self.lines.insert(body_start, "")
            if len(self.lines) == body_start + 1 and not self.lines[body_start].strip():
                del self.lines[body_start]  # nothing but the header: no stray blank line
        self.set_frontmatter(new)
        return True

    def assign_ids(self, new_id) -> list[Item]:
        """Give every item without an id (or with a duplicate id) a fresh one. Returns changed items."""
        seen: set[str] = set()
        changed = []
        for item in self.items:
            if item.id is None or item.id in seen:
                item.id = new_id()
                self.lines[item.line_no] = render_item(item.content, item.id, item.attrs, item.marker)
                changed.append(item)
            seen.add(item.id)
        if changed:
            self._reparse()
        return changed

    def remove(self, item_id: str) -> bool:
        item = self.find(item_id)
        if item is None:
            return False
        del self.lines[item.line_no]
        self._reparse()
        return True

    def add(self, content: str, item_id: str, attrs: dict[str, str | bool] | None = None,
            section: str | None = None) -> None:
        line = render_item(content, item_id, attrs or {})
        if section and section not in [s for s, _ in self.sections]:
            self._append_block([f"## {section}", "", line])
            return

        if section:
            start = next(idx for s, idx in self.sections if s == section)
            following = [idx for _, idx in self.sections if idx > start]
            end = following[0] if following else len(self.lines)
            header_end = start
        else:
            start = 0
            end = self.sections[0][1] if self.sections else len(self.lines)
            header_end = self.description_end if self.description_end is not None else self.title_line
            if header_end is None:
                header_end = self.fm_end

        region_items = [it for it in self.items if start <= it.line_no < end and it.section == section]
        if region_items:
            self._insert(region_items[-1].line_no + 1, line)
            return
        if header_end is None:
            # No title: put the item before the first section (or at the end of the file).
            if end < len(self.lines):
                self._insert(end, line, blank_before=False)
            else:
                self._append_block([line])
            return
        self._insert(header_end + 1, line)

    def _insert(self, index: int, line: str, blank_before: bool = True) -> None:
        block = [line]
        prev = self.lines[index - 1] if index > 0 else None
        nxt = self.lines[index] if index < len(self.lines) else None
        if blank_before and prev is not None and prev.strip() and not ITEM_RE.match(prev):
            block.insert(0, "")
        if nxt is not None and nxt.strip() and not ITEM_RE.match(nxt):
            block.append("")
        self.lines[index:index] = block
        if index >= len(self.lines) - len(block):
            self.trailing_newline = True
        self._reparse()

    def _append_block(self, block: list[str]) -> None:
        while self.lines and not self.lines[-1].strip():
            self.lines.pop()
        if self.lines:
            last = self.lines[-1]
            if not (ITEM_RE.match(last) and ITEM_RE.match(block[0])):
                self.lines.append("")
        self.lines.extend(block)
        self.trailing_newline = True
        self._reparse()


def new_topic_file(title: str, description: str | None, *, group: str | None = None,
                   private: bool = False) -> str:
    fm: dict = {"type": "topic", "title": title}
    if description:
        fm["description"] = description
    if group:
        fm["group"] = group
    if private:
        fm["private"] = True
    return "\n".join(render_frontmatter(fm)) + "\n"
