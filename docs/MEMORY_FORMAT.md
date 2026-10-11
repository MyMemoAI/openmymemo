# The OpenMyMemo memory folder

> Format version 2 (2026-10-08), written by OpenMyMemo v0.2. OpenMyMemo v0.1 writes version 1; §11 lists the differences and what readers must accept. This document is the contract: the code follows it, and other tools may read and write the folder by following it.

## 1. What it is

A memory folder is a directory of plain-text files that holds what AI assistants may know about a person. The person owns it: it lives on their computer, opens in any text editor, and can be synced or versioned with whatever they already use (iCloud, Dropbox, git, an Obsidian vault). Everything a reader needs is in `memories/*.md`; the rest is metadata and history that OpenMyMemo maintains and other tools may ignore.

Rules the format is built on:

- **Memory files are the source of truth.** If a file and the metadata disagree, the file wins and the metadata is corrected on the next read.
- **Lines a tool doesn't understand are kept byte for byte.** Parsing and rewriting a file that wasn't changed is the identity.
- **No database, no binary files.** Everything can be read with `cat`, diffed with `git` and edited by hand.
- **Follow the conventions people already use.** Markdown with YAML frontmatter (Obsidian, Hugo, Claude Code's memory files, Google Cloud's Open Knowledge Format), Obsidian block ids, one statement per list item (the shape of ChatGPT's, Claude's, Gemini's and Mem0's memory lists), and the Open Knowledge Format's reserved file names (§10.1).

## 2. Layout

```
<folder>/
├── memories/                 # the memories: one file per topic
│   ├── identity.md
│   ├── work.md
│   ├── …
│   ├── health.md             # private: true
│   └── from-chatgpt.md       # created by an import
├── MEMORY.md                 # generated: what an AI with the default scope sees
├── log.md                    # generated: human-readable change log
├── connectors.yaml           # connected apps and their permissions
└── .openmymemo/              # program state
    ├── settings.yaml
    ├── topics.yaml
    ├── meta.json
    ├── history.jsonl
    ├── activity.jsonl
    ├── proposals/<id>.json
    ├── .lock
    └── .gitignore
```

| Path | Role | Written by |
| --- | --- | --- |
| `memories/*.md` | **Source of truth** for memory content, topic, section, review date and per-memory privacy | People, editors, OpenMyMemo, other tools |
| `MEMORY.md`, `log.md` | Generated views; overwritten; never read back | OpenMyMemo |
| `connectors.yaml` | Configuration a person may edit | OpenMyMemo, people |
| `.openmymemo/` | Metadata, history, activity and proposals; plain text; rebuilt from the files where possible | OpenMyMemo |

The default location is `~/OpenMyMemo/`. A folder is a memory folder when `memories/` is a directory and `.openmymemo/topics.yaml` exists.

## 3. Memory files

### 3.1 File names

`memories/<slug>.md`. The slug is the topic's identifier and must match `^[a-z0-9][a-z0-9-]{0,47}$`. Files whose names start with `.` or `_` are ignored, and `index.md` and `log.md` are reserved (the Open Knowledge Format's names) and never read as topics. Subdirectories of `memories/` are not read in version 2 (reserved).

### 3.2 Frontmatter

A file starts with a YAML block between a `---` line (line 1) and the next line that is `---` or `...`. The block is a mapping.

| Key | Required | Value | Meaning |
| --- | --- | --- | --- |
| `type` | yes | `topic` or `document` | How the body is read (§3.3, §3.6). A file with another `type` is left untouched and not shown. |
| `title` | recommended | string | Display name. Default: the slug. |
| `description` | recommended | string | What belongs here. Shown to people, used when filing memories, sent to AI so it knows where to file proposals. |
| `group` | optional | group id | The sidebar group (§6.2). An unknown id creates the group. Default: `other`. |
| `private` | optional | boolean, default `false` | Hidden from every AI unless a connector is explicitly allowed to read private topics. |
| `tags` | optional | list of strings | Free; preserved, not interpreted. |
| `generated`, `verified`, `status`, `stale_after`, `sources` | optional | as in the Open Knowledge Format | Meaningful on documents (§3.6). On topic files they are preserved, not interpreted. |

Readers must keep keys they don't know. Writers must write back every key they read. A file without frontmatter is a version-1 file (§11).

```markdown
---
type: topic
title: Work
description: Role, company, team, responsibilities, industry
group: doing
---
```

### 3.3 Topic files

The body of a `type: topic` file is ordinary Markdown. Only two things are interpreted:

- A `## Heading` at column 0 starts a **section**: a grouping inside the topic (e.g. "Books" and "Music" under Interests). Sections never create files.
- A list item at column 0 — `-`, `*` or `+` followed by at least one space or tab and some text — is **one memory**.

Everything else is preserved and ignored: prose, nested or indented bullets, code fences and everything inside them, horizontal rules, blank lines, HTML, a version-1 `# Title` line and `> description` quote, and anything a future version adds.

```markdown
---
type: topic
title: Interests and taste
description: Books, film, music, food, sports, hobbies, taste
group: ways
---

- Drinks pour-over coffee, no milk ^c0ff3

## Books
- Rereads Le Guin every few years ^b00k1
- Prefers paper for fiction and Kindle for non-fiction <!-- private --> ^b00k2

## Music
- Plays bass in a covers band on Thursdays <!-- until: 2027-01 --> ^mu51c
```

### 3.4 A memory line

```
<marker> <content> [ <!-- <attributes> --> ] [ ^<id> ]
```

- **marker:** `-`, `*` or `+` at column 0, then one or more spaces or tabs.
- **content:** one line of text, 1 to 1000 characters. Leading and trailing whitespace is trimmed; writers collapse runs of internal whitespace to one space. Content must not itself end with `^word`, because that would be read as an id.
- **attributes:** the last HTML comment on the line (§3.5). Optional.
- **id:** `^` followed by `[A-Za-z0-9-]+` at the very end of the line, preceded by whitespace — an Obsidian block id (§3.7). Optional on input (OpenMyMemo adds one), present on output. Readers also accept the id before the comment; writers always put it last.

Rendered by OpenMyMemo:

```markdown
- Rewriting the agent service on the Claude Agent SDK since 2026-09 <!-- until: 2026-12 --> ^k2p9d
```

### 3.5 Inline attributes

Inside the comment, attributes are separated by `;`. Each is `key: value` or a bare `key` (a flag). Keys are case-insensitive and stored lowercased.

| Attribute | Value | Meaning |
| --- | --- | --- |
| `until` | `YYYY-MM` or `YYYY-MM-DD` | Review date. Shown to AI as "(until …)"; the panel flags it 14 days before. |
| `private` | flag | This memory is hidden from AI even when its topic isn't. |

Unknown attributes are preserved and ignored. Everything else about a memory (kind, source, timestamps, version, pinned, evidence, status) lives in `meta.json` (§6.3), keyed by id, so that files stay readable and nobody has to write it by hand.

### 3.6 Document files (reserved)

A `type: document` file is **one memory whose content is the whole body**, for text that is prose rather than a list: working instructions in the style of `CLAUDE.md` or `AGENTS.md`, project context, Cursor rules. Rules, fixed now so that files written later still read:

- The memory id is `doc:<slug>`. Headings and list items in the body are plain Markdown, not sections or memories.
- The body is at most 20,000 characters.
- Frontmatter uses the same keys as a topic file plus, following the Open Knowledge Format: `generated: {by, at}` when the latest version was written by an AI (`by` is `connector:<id>`), `verified: [{by, at}]` when a person accepted it (`by` is `human:user`), `stale_after` as the review date.
- Version 2 reserves the type. A reader that doesn't implement documents leaves such files untouched and doesn't show them. OpenMyMemo implements documents in v0.3.

### 3.7 Identifiers

- OpenMyMemo generates ids of 5 characters from `[a-z0-9]`, never all digits, unique across the whole folder (active and archived memories alike), and never reused.
- Other tools may write any id matching `[A-Za-z0-9-]+` as long as it is unique in the folder; OpenMyMemo keeps foreign ids as they are.
- A memory line without an id gets one on the next read by OpenMyMemo; the file is rewritten with the id appended and nothing else changed.
- If the same id appears twice (a line was copied), the second occurrence gets a new id. Files are visited in name order and lines in file order, so the earlier occurrence keeps the id.
- Moving a line to another file keeps its id: the memory changes topic. Editing the text keeps the id: the memory gets a new version. Deleting the line archives the memory: the id stays reserved, and the content stays in `meta.json` and `history.jsonl` (§8).

## 4. Generated files

### 4.1 `MEMORY.md`

What an AI with the default scope (every non-private topic, no private memories) sees, rendered from the memory files on every change. It begins with an HTML comment saying so; edits to it are overwritten. Structure: a fixed header, then topics in context order (`how-to-work-with-me`, `identity`, `work`, `projects`, `goals`, then the rest in sidebar order), each as `## Title (slug)`, optional `### Section` headings, and one `- content (until …) [id]` line per memory, pinned first, then by kind: instruction, preference, fact, ongoing, event.

### 4.2 `log.md`

The human-readable change log, following the Open Knowledge Format's `log.md`: `## YYYY-MM-DD` headings, newest day first, one line per change, with the actor in bold:

```markdown
## 2026-10-08
- **Claude Code** added to Work: "Now works as CTO at MyMemo" (^m7f3a)
- **You** edited Projects: "Rewriting the agent service …" → "Rewrote the agent service …" (^k2p9d)
- **You** undid Cursor's change to Tools (^t00l5)
```

It is rendered from `history.jsonl`, `connectors.yaml` (for app names) and the proposals (to tell an undo from an ordinary change). The file starts with an HTML comment saying it is generated and a `# Change log` heading. The text of a private memory is left out ("added a private memory to Health"), as in `MEMORY.md`; quoted text is cut at 160 characters; an edit that also moves the memory reads `edited Work → People: "old" → "new"`. Only the newest 500 lines are kept, with a closing line pointing at `history.jsonl` when older entries were left out. Readers must not parse it for data; use `history.jsonl`.

## 5. `connectors.yaml`

The AI apps allowed to read the folder and write to it.

```yaml
format: 1
connectors:
  - id: claude-code
    name: Claude Code
    kind: stdio                 # stdio | http
    read:
      topics: all               # all, or a list of slugs
      include_private: false
    write: propose              # none | propose | auto
    blocked_topics: []
    token_hash: null            # SHA-256 of the bearer token (http connectors only)
    token_preview: null         # first characters of the token, for display
    created_at: 2026-10-08T09:12:00+00:00
    # revoked_at: …             # present once the user removed the app; the id stays reserved
```

- `write`: `none` is read only; `propose` follows the folder's `require_approval` setting and the protected-memory rule (§6.1); `auto` applies every change except in private topics.
- Tokens are never stored; only the SHA-256 and a short preview are.
- A connector with `revoked_at` refuses every call. Adding the same id again is an explicit re-grant.

## 6. `.openmymemo/`

Program state. All files are plain text. OpenMyMemo keeps `.lock` and `*.tmp` out of git with its own `.gitignore` here; everything else is meant to be committed if the person keeps the folder in git.

### 6.1 `settings.yaml`

```yaml
format: 1
require_approval: false
```

`require_approval` (default `false` in version 2, `true` in version 1): when on, every AI change waits for the person. When off, additions and changes to AI-written memories apply at once and are listed for review; changes to **protected** memories — those whose latest version was written by the person (`last_actor` in §6.3 is `user`, `file` or `import:*`) — and anything in a private topic still wait. Unknown keys are preserved.

### 6.2 `topics.yaml`

Group names and the sidebar order.

```yaml
format: 2
groups:
  - id: who
    name: Who I am
    topics: [identity, history, values]
  - id: doing
    name: What I'm doing
    topics: [work, projects, goals, learning]
  - id: ways
    name: How I work
    topics: [how-to-work-with-me, tools, interests]
  - id: world
    name: My world
    topics: [people, lifestyle]
  - id: private
    name: Private
    topics: [health, finance]
  - id: imported
    name: Imported
    topics: [from-chatgpt]
```

A topic's group is its frontmatter `group`; `topics.yaml` only orders them. If the two disagree, frontmatter wins and the list is corrected on the next read. Topics missing from the list are appended to their group in name order; topics with no group go to `other`. Version 1 also kept a `private: [slugs]` list here; version 2 reads it only from version-1 folders (§11).

### 6.3 `meta.json`

Per-memory metadata, keyed by id.

```json
{
 "format": 2,
 "memories": {
  "m7f3a": {
   "content": "CTO at MyMemo; stack is FastAPI, Vue 3 and Swift",
   "topic": "work",
   "section": null,
   "kind": "fact",
   "source": "user",
   "last_actor": "user",
   "created_at": "2026-10-08T09:12:00+00:00",
   "updated_at": "2026-10-08T09:12:00+00:00",
   "version": 1,
   "status": "active"
  }
 }
}
```

| Field | Values | Notes |
| --- | --- | --- |
| `content`, `topic`, `section` | | A copy of what the file says, so changes made in an editor can be detected |
| `kind` | `fact`, `preference`, `ongoing`, `instruction`, `event` | What sort of statement it is; guessed from the text when not given |
| `source` | `user`, `import:<vendor>`, `connector:<id>` | Who created the memory |
| `last_actor` | `user`, `file`, `import:<vendor>`, `connector:<id>` | Who wrote the latest version; `user`, `file` and `import:*` make the memory protected. Version 2. |
| `created_at`, `updated_at`, `archived_at` | ISO 8601 with offset | |
| `version` | integer from 1 | Increments on every content, topic, archive or restore change |
| `status` | `active`, `archived` | Archived memories have no line in any file |
| `until`, `private` | as inline | Mirrors of the inline attributes |
| `pinned` | boolean | Listed first in what AI sees |
| `evidence` | string ≤ 2000 chars | The conversation snippet behind the proposal that created it |

`meta.json` is a cache with a memory of its own: if it is lost, every memory survives — ids, content, topic, section, `until` and `private` come back from the files; `kind` is guessed again, `source` becomes `user` and `last_actor` becomes `file`, and timestamps restart. Only `history.jsonl` keeps the real past.

### 6.4 `history.jsonl`

Append-only, one JSON object per line, one line per version of a memory. The full content is stored each time, so any version can be restored without replaying.

```json
{"at": "2026-10-08T09:12:00+00:00", "memory_id": "m7f3a", "version": 2, "op": "update", "content": "…", "topic": "work", "actor": "connector:claude-code", "proposal_id": "p19a3f…"}
```

- `op`: `create`, `update`, `move`, `archive`, `restore`.
- `actor`: `user` (panel or CLI), `file` (a change noticed in the file), `import:<vendor>`, `connector:<id>`.
- `proposal_id`: present when the change came from a proposal.

### 6.5 `activity.jsonl`

Append-only, one JSON object per line: what each connector did.

```json
{"at": "2026-10-08T09:15:00+00:00", "connector": "claude-code", "action": "context", "memory_ids": ["m7f3a", "k2p9d"], "chars": 1840, "via": "hook"}
```

`action` is `context`, `search`, `get`, `propose` or `import`; other fields depend on it (`query`, `memory_ids`, `proposal_id`, `result`, `op`, `chars`, `via`, `source`). Reads never rewrite `connectors.yaml`; "last seen" is derived from here.

### 6.6 `proposals/<id>.json`

One file per proposal, named by its id (`p` plus hexadecimal; the id must be alphanumeric).

| Field | Notes |
| --- | --- |
| `id`, `connector_id`, `connector_name` | |
| `op` | `add`, `update`, `archive` |
| `content`, `topic`, `memory_id`, `kind`, `reason`, `evidence`, `idempotency_key` | What was proposed |
| `created_at` | |
| `status` | `pending`, `accepted`, `edited_accepted`, `rejected`, `expired`, `applied`, `undone` |
| `decided_at` | When the status left `pending` or `applied` was set |
| `reviewed_at` | Version 2: when the person saw an applied change in the digest. Applied proposals without it are "unreviewed". |
| `hold` | Version 2: why a pending proposal waits: `approval` (the person reviews every change first), `protected` (the target memory was last written by the person) or `private` (a private topic or memory). Absent on applied proposals. |
| `applied_version` | Version 2: the memory's version right after the change was applied or accepted. Undo is offered while this is still the memory's current version. |
| `final_content`, `final_topic`, `applied_memory_id` | What was actually written |
| `before_content`, `before_topic` | The memory before an update or archive, for undo and for the diff |
| `original_op`, `similarity`, `conflict`, `topic_confidence` | How the proposal was adjusted: an `add` turned into an `update` of a similar memory, whether it looks contradictory, how sure the classifier was |
| `undo_until` | Version 1 only. Version 2 allows undo for as long as the change is the memory's latest version, and ignores this field. |

Pending proposals expire after 30 days.

### 6.7 `.lock`

An empty file used for advisory locking: `flock` on POSIX, `msvcrt.locking` on the first byte on Windows. OpenMyMemo holds it for the duration of any write and of a sync. Other tools that want to avoid racing a running OpenMyMemo take the same lock; tools that don't are still safe as long as they replace files atomically (§8).

## 7. Reconciling changes made outside OpenMyMemo

Before every read or write, OpenMyMemo compares the size and mtime of every memory file, `meta.json` and `topics.yaml` with what it last saw. If anything differs, it re-reads the files and diffs them against `meta.json` by id:

| In the file | In `meta.json` | Result |
| --- | --- | --- |
| a line without an id | — | a new memory: an id is appended to the line, `create` in history, `source: user`, `last_actor: file` |
| a line with an unknown id | — | a new memory with that id |
| a line whose text differs | the id exists | `update` (new version, `last_actor: file`) |
| a line in a different file or section | the id exists | `move` |
| a line present again | the id is archived | `restore` |
| no line | the id is active | `archive` |
| different inline attributes | | `until` / `private` updated; no new version |

Other tools can rely on this: write memory lines and nothing else, and OpenMyMemo fills in the metadata, ids and history on its next read.

## 8. Rules for other tools

Reading:

- Parse only what §3 defines. Treat everything else as opaque.
- Accept version-1 files (§11).
- Respect privacy: a topic with `private: true`, or a memory with the `private` attribute, is not meant for an AI unless the person said so for that app (`connectors.yaml`).

Writing:

- Edit lines; don't regenerate files. Keep every line you didn't change byte for byte, including the newline style.
- Keep ids. Never change, reuse or renumber one. Leave new lines without an id, or generate unique ones.
- Keep frontmatter keys and inline attributes you don't understand.
- Replace files atomically (write a temporary file in the same directory, then rename).
- Take `.openmymemo/.lock` while writing if you can.
- Don't write `MEMORY.md`, `log.md`, `meta.json`, `history.jsonl` or `activity.jsonl`; OpenMyMemo rebuilds or appends to them on its next read. Writing `meta.json` is allowed only if you reproduce §6.3 exactly.
- Deleting a line archives the memory; its text remains in `meta.json` and `history.jsonl`. Removing it completely ("forget") also deletes those records. OpenMyMemo doesn't offer that yet; until it does, it is a manual edit.

## 9. What an AI receives

Not part of the storage format, but documented because "what you see is what it gets" is a principle: an AI gets the Markdown in §4.1, filtered to the topics its connector may read, cut to a character budget with the most important memories first, with ids in brackets so it can reference them in proposals. The panel shows the exact text per connector, and every read is logged in `activity.jsonl`.

## 10. Interoperability

### 10.1 Open Knowledge Format

A version-2 `memories/` directory is a conformant Open Knowledge Format bundle: every file in it is a concept file with YAML frontmatter and a non-empty `type`, unknown keys are preserved, and the reserved names `index.md` and `log.md` are never used for anything else. The change log OpenMyMemo writes sits one level up, at the folder root next to `MEMORY.md`, so people find it; it follows OKF's `log.md` structure, and a tool that wants it inside the bundle can copy it there. The mapping:

| OKF | OpenMyMemo |
| --- | --- |
| `type` (required) | `topic` or `document` |
| `title`, `description`, `tags` | the same keys |
| `generated: {by, at}` | on documents, the AI that wrote the latest version; for topic files the equivalent per memory is `last_actor` and `updated_at` in `meta.json` |
| `verified: [{by, at}]` | on documents, the person's acceptance; per memory, an accepted proposal in `proposals/` |
| `status`, `stale_after` | preserved; per memory, `until` plays the role of `stale_after` |
| `sources` | preserved; per memory, `evidence` in `meta.json` |
| `log.md` | the generated change log (§4.2), written at the folder root |
| `index.md` | not written; readers must not require it |
| actor strings `human:<id>`, `<producer>/<version>` | `human:user` and `connector:<id>` |

OKF has no notion of a per-line memory; that layer is OpenMyMemo's own, and an OKF reader sees a topic file as one concept whose body is a list.

### 10.2 Obsidian

The folder can live inside an Obsidian vault. Block ids are Obsidian's own syntax, so `[[work#^m7f3a]]` links to a memory; frontmatter shows up as properties. Obsidian only recognises a block id at the very end of a line, which is why the attribute comment comes before it.

### 10.3 Import formats recognised

| Source | Shape |
| --- | --- |
| ChatGPT | The text of Settings → Personalization → Manage memories; UI labels ("Delete", "Memory is full", …) are dropped |
| Claude | The memory list from Settings, or the `[YYYY-MM-DD] – text` lines its export prompt produces; the date becomes `created_at` |
| Gemini, Grok, any assistant | One memory per line, bullets or numbers optional |
| Markdown | List items; headings are shown in the preview as the source's own grouping |
| JSON | A list of strings, or of objects with `content` / `memory` / `text` / `value` and optional `topic` (used when it names an existing topic, which is how OpenMyMemo's own export round-trips), `kind`, `created_at`, `categories` (Mem0) |

Imports land in `memories/from-<source>.md` in the source's order; nothing is classified at import time.

### 10.4 Export formats produced

- **The folder itself** is the complete export.
- **JSON:** `{"format": "openmymemo/1", "exported_at", "topics": [...], "memories": [{"id", "content", "topic", "section", "kind", "source", "created_at", "updated_at", "until", "private", "pinned", "version"}]}`.
- **Claude:** `[YYYY-MM-DD] – content`, one per line, the shape Claude's import expects.
- **ChatGPT:** a prompt asking ChatGPT to save each listed item.
- **Markdown:** `## Title` per topic and `- content` per memory, without ids.

### 10.5 Documents and other tools' instruction files

Once documents exist (v0.3), `CLAUDE.md`, `AGENTS.md`, `.cursor/rules/*.mdc` and Claude's prose memory summary import as `type: document` files, and a document exports back to those shapes.

## 11. Versions

### Version 1 (OpenMyMemo v0.1)

- No frontmatter (a leading `---` block is skipped and ignored). The title is the first `# Heading`; the description is the blockquote right after it (one blank line allowed; several `>` lines are joined with spaces).
- Privacy and groups live in `.openmymemo/topics.yaml` (`private: [slugs]`, `groups[].topics`).
- `settings.yaml` defaults `require_approval` to `true`.
- `meta.json` has `"format": 1` and no `last_actor`; proposals have `undo_until`.
- No `log.md`; imports are classified into topics.

### Version 2 (this document)

Frontmatter with `type`; privacy in frontmatter; `topics.yaml` for order only; `last_actor`; `reviewed_at`; `log.md`; `from-<source>` import topics; `require_approval` off by default; `type: document` reserved.

### Reading version-1 folders

A version-2 reader must accept files without frontmatter: title and description from the header lines, group and privacy from `topics.yaml`. A version-1 reader opening a version-2 folder sees the frontmatter skipped, titles fall back to slugs, and privacy from `topics.yaml` only — so a version-2 folder should not be opened with v0.1 once private topics have been set in frontmatter.

### Migration

On the first open after the upgrade, OpenMyMemo rewrites each memory file once: a frontmatter block is inserted from the `# Title` line, the `> description` quote and `topics.yaml`; those header lines are removed, since the frontmatter now carries them; every other line is untouched. `topics.yaml` loses `private` and becomes format 2; `meta.json` becomes format 2 and gets `last_actor` from the latest history record of each memory (`user` when there is none). If `settings.yaml` is missing, `require_approval: true` is written so an existing vault keeps its behaviour; new vaults start with the new default.

### Detecting the version

`.openmymemo/meta.json` → `format`. Per file: frontmatter with a `type` key means version 2.

### Compatibility promise

Within a major version, additions are optional and preserved: new frontmatter keys, new inline attributes, new `meta.json` fields, new history ops. A reader of version N must accept files of version N−1. Changes that break reading get a new major version and a migration.
