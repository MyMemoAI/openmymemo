# OpenMyMemo product design

> Status: v0.2 implemented (2026-10-08), except the PyPI release and the first installs in the real apps. §13 lists what changed against v0.1 and what is still open; §14 describes the Mac app (2026-10-10); §15 the two ways in, one-click connections and the first-run guide (2026-10-12); §16 the optional MyMemo Cloud connector (2026-10-12, design); [MEMORY_FORMAT.md](./MEMORY_FORMAT.md) is the normative description of the folder. Differences between v0.1 and the original draft are in §12.
> Scope: the **local, open-source** version. Cloud hosting is explicitly out of scope for now.

## 0. In one sentence

OpenMyMemo is **an AI memory folder that lives on your own computer**, plus a small app: you can see exactly what models remember about you, and any AI (Claude, ChatGPT, Cursor …) can read that memory and write to it, but **you see every change and can undo it**, and anything that touches what you wrote yourself waits for you. If an account gets banned, a vendor shuts down or you switch models, your memory comes with you.

## 1. Why

- **Trigger:** today a person's AI memory lives inside one vendor's account. If the account is closed or banned, or the vendor shuts down, the history and memory go with it. Memory belongs to the user; no single company should hold it.
- **Today:** ChatGPT has no reliable memory export (you copy the "Manage memories" list by hand). Claude added "import memory from another AI" in March 2026 and Gemini followed the same month, but both work by pasting text, and the vendor then holds the result. No cross-vendor memory format exists. Existing memory layers (Mem0 / OpenMemory, CaviraOSS OpenMemory, Supermemory, Zep, Letta) are developer APIs for building agents: the AI writes memories automatically and the user can barely see or control them.
- **The gap:** memory **centred on the person** — readable and editable by people, where every AI write is visible and reversible, stored as local files you can take anywhere.

## 2. Principles (the tie-breakers for product decisions)

1. **Local files are the only source of truth.** Your memory is a folder on your computer. Any editor can open it; put it in iCloud, Dropbox, an Obsidian vault or a git repo — backup and sync are your call.
2. **AI writes; you see everything and can undo.** An AI's additions land right away and show up in a digest for you to check. Its changes to memories you wrote, imported or approved yourself, and anything in a private topic, wait for you. One switch makes every change wait, and the mode can also be set per app.
3. **What you see is what it gets.** The panel shows the exact text an AI receives, character for character, and who read what and when.
4. **Works with zero dependencies.** No API key, no Docker, no database: install and go. Model-powered extras (sorting, deduplication, extracting memories from chats) are optional and use a model you configure.
5. **Open protocols and open conventions.** AI apps connect over standard MCP. The folder follows conventions people already use — Markdown with YAML frontmatter, Obsidian block ids, one statement per line, the Open Knowledge Format's reserved files — so other tools can read and write it. Import and export use open formats; no lock-in to any vendor, MyMemo included.

## 3. Name and positioning

- Project / repo: **OpenMyMemo** (`MyMemoAI/openmymemo`); CLI `openmymemo`; default folder `~/OpenMyMemo/`.
- Relationship to MyMemo: an independent product with no dependency on MyMemo accounts or services. MyMemo may later read the memory as an ordinary connector, on equal footing with Claude. The first such connector is MyMemo Cloud (§16): optional, off until the person signs in, and it relays AI apps on the web to the folder.
- Name collisions: Mem0's "OpenMemory MCP" and CaviraOSS's "OpenMemory" already exist, so we don't use "OpenMemory". One-line distinction: *they give agents a memory; we give people control of theirs.*
- The product (panel, CLI, docs) is English only. Memories themselves can be in any language; classification and search handle English and Chinese.
- **Who it is for.** Consumers are the goal; developers come first. Developers who use two or more AI apps (Claude Code, Cursor, Codex) feel the pain today, can install a CLI, and will be the open-source community. Consumers need a way in with nothing to install and nothing to configure. Both get the same folder, the same core and the same MCP server, with two shells on top:
  - *The geek shell:* CLI, plugins, MCP config, editing the files directly. Shipped in v0.1.
  - *The simple shell:* the Claude Desktop extension (`.mcpb`) is the first one — double-click, Claude Desktop installs Python itself, the panel runs inside it. The Mac app (Tauri with the Python core as a sidecar, §14) is the second: a window and a menu-bar item for people who never open a terminal. The web panel stays and keeps evolving; the app wraps the same panel. A browser extension for ChatGPT and claude.ai comes later and is sized after the first real users.
- The core stays small and centred on the file format, so it could be reimplemented in another language for the consumer app without changing anything users keep.

## 4. User stories

1. **Moving house:** I copy the whole ChatGPT "Manage memories" page, run `openmymemo import chatgpt`, paste, and 38 memories land in a "From ChatGPT" topic in my local folder, in their original order and with their dates. I import Claude's memory too; duplicates are flagged. When I feel like it, I sort them into topics, or leave them where they are.
2. **Connecting:** I add one MCP config entry to Claude Desktop / Claude Code / Cursor. From then on every new conversation starts with my memory, and I stop introducing myself.
3. **Noticing:** during a chat Claude learns I changed jobs and writes "Now works as Y at company X" into my memory, quoting what I said. My next session starts with "Claude added 1 memory yesterday". In the panel I see the digest, keep that one, undo another one I didn't want, and tell it "don't record this kind of thing". Had Claude tried to change something I wrote myself, it would have waited for me instead.
4. **Transparency:** I want to know how much Cursor actually sees. I switch the panel to the Cursor connector and see its read scope, the exact summary it receives, and every read it made.
5. **Leaving:** I stop using an AI and revoke its access in the panel. The memory folder is untouched; I carry on with the next AI.

## 5. Product

### 5.1 The memory folder (where the data lives)

```
~/OpenMyMemo/
├── memories/                 # the memories: one file per topic, human-readable and editable
│   ├── identity.md
│   ├── history.md
│   ├── values.md
│   ├── work.md
│   ├── projects.md
│   ├── goals.md
│   ├── learning.md
│   ├── how-to-work-with-me.md   # instructions for AI
│   ├── tools.md
│   ├── interests.md
│   ├── people.md
│   ├── lifestyle.md
│   ├── health.md             # private by default
│   ├── finance.md            # private by default
│   └── from-chatgpt.md       # created by an import; sort into topics when you like
├── MEMORY.md                 # generated "what an AI sees" summary, read-only, easy to view or copy
├── log.md                    # generated, human-readable change log, newest day first
├── connectors.yaml           # connected apps and their permissions
└── .openmymemo/              # program state — all plain-text files, no database
    ├── settings.yaml         # vault-wide switches: require_approval
    ├── topics.yaml           # group names and sidebar order
    ├── meta.json             # per-memory metadata (kind, source, timestamps, version), keyed by id
    ├── history.jsonl         # version history of every memory (append-only)
    ├── activity.jsonl        # what each connector read or proposed (append-only)
    └── proposals/            # proposals (JSON, one file each)
```

`memories/*.md` is deliberately simple and compatible with Obsidian block ids:

```markdown
---
type: topic
title: Work
description: Role, company, team, responsibilities, industry
group: doing
---

- Engineering lead at Acme; stack is FastAPI, Vue 3 and Swift ^m7f3a
- Rewriting the agent service on the Claude Agent SDK since 2026-09 <!-- until: 2026-12 --> ^k2p9d

## Team
- Two backend engineers and one iOS engineer; I also run infrastructure ^q1x4e
```

- The YAML frontmatter describes the file: `type` (`topic`; `document` is reserved, see below), `title` (the display name; the file name is a slug, so it is cross-platform and git-friendly), `description` (what belongs in the topic; shown in the panel, used to classify, and sent to AI so it knows where to file proposals), `group` and `private`. It is the convention Obsidian, Hugo, Claude Code's memory files and Google Cloud's Open Knowledge Format use, so the file explains itself to any tool. v0.1 wrote a `# Title` line and a `> description` quote instead; readers keep accepting them (§13).
- One line, one memory (one list item = one self-contained statement). `##` headings are optional sub-sections (e.g. "Books" and "Music" under Interests); the panel groups by them. They don't create files.
- The trailing `^xxxxx` is a stable id, added automatically on first read. Users never have to write ids.
- Attributes a user might edit by hand (`until`, `private`) go in an inline HTML comment placed *before* the `^id` (Obsidian only recognises block ids at the very end of a line). Everything else (kind, source, created time, version) lives in `.openmymemo/meta.json`, keyed by id.
- If the user edits a file, the next read notices the changed mtime and diffs by block id: new lines, edited lines, deleted lines (archived) and lines moved between files all go into history with actor `file`. When the program edits a file it takes a lock, re-reads the file from disk, changes only the affected line and replaces the file atomically, so whatever the user's editor has elsewhere in the file is preserved.
- **Documents** (reserved now, built in v0.3): a file with `type: document` is one memory whose content is the whole body, for things that are prose rather than a list — working instructions in the style of `CLAUDE.md` or `AGENTS.md`, project context, Cursor rules. AI changes to a document are reviewed as a diff. The format reserves the type now so that files written later still read.
- The normative description of all of this is [MEMORY_FORMAT.md](./MEMORY_FORMAT.md); other tools may read and write the folder by following it.

#### 5.1.1 Default topics

Goal: out of the box, a skeleton that covers the different sides of a person, so imports and AI proposals land somewhere sensible and users don't have to invent a taxonomy. 5 groups, 14 topics:

| Group | Topic (file) | What goes there | Special rules |
| --- | --- | --- | --- |
| Who I am | Identity `identity` | Name, how to address me, age, location, languages, time zone | |
| | Background `history` | Education, career history, major life events | mostly "event" kind |
| | Values `values` | Principles, beliefs, what I care about, what I won't accept | one click to make private |
| What I'm doing | Work `work` | Role, company, team, responsibilities, industry | |
| | Projects `projects` | Things I'm actively working on | review date by default |
| | Goals `goals` | Short- and long-term goals and plans | review date by default |
| | Learning `learning` | What I'm learning or want to learn, and how I learn best | review date by default |
| How I work | How to work with me `how-to-work-with-me` | Answer style, language, format, things to avoid — instructions for AI | always first in what AI sees |
| | Tools and setup `tools` | Devices, OS, editors, tech stack, everyday software | |
| | Interests and taste `interests` | Books, film, music, food, sports, hobbies, taste | |
| My world | People `people` | Family, friends, colleagues, pets, and how I relate to them | |
| | Lifestyle `lifestyle` | Routine, home, eating habits, getting around, daily schedule | |
| Private | Health `health` | Medical history, medication, dietary restrictions, fitness | hidden from every AI by default |
| | Finance `finance` | Income, assets, budget, insurance | hidden from every AI by default |

- Groups are only a visual grouping in the panel sidebar, not folders. A topic's group is the `group` field in its frontmatter; `.openmymemo/topics.yaml` holds the group names and the sidebar order.
- All 14 files are created on `init` (frontmatter only). Empty topics are greyed out in the panel until they have content.
- Users can add, rename and remove topics: a new file is a new topic (the panel asks for title, description, group and privacy).
- **Imports create their own topics:** `from-chatgpt`, `from-claude`, … in an "Imported" group, readable by AI like any other topic. They keep the source's order and dates. Sorting into the regular topics is a separate, optional step (§5.6).
- Auto-filing with the local rule-based classifier (curated English and Chinese keywords per topic, a few decisive phrases such as "CTO" and "my daughter", lower weight for tool and taste words, sensitive topics winning ties) is used when an AI proposes an `add` without a topic, and for the suggestions in the sorting step, where every suggestion is previewed. Custom topics are matched on the meaningful words of their description. Any memory can be moved later. Model-based classification is optional (§10).
- Privacy is a topic-level property set in frontmatter (`private: true`; `health` and `finance` by default) and can also be set on a single memory.

### 5.2 The local program

A single CLI on PyPI: `uv tool install openmymemo`, or `uvx openmymemo` without installing (the configs it writes into AI apps then start it with `uvx openmymemo`, not from uv's cache). A clone installs with `uv tool install <repo dir>`. Each release tag publishes it (`release.yml`, trusted publishing).

| Command | What it does |
| --- | --- |
| `openmymemo init` | Create the memory folder with the default topics |
| `openmymemo import chatgpt\|claude\|gemini\|text\|markdown\|json [file]` | Import from a file, stdin or the clipboard into a `from-<source>` topic; preview before anything is saved |
| `openmymemo sort [--topic from-chatgpt]` | Suggest a topic for each unsorted memory, preview, move the ones you tick |
| `openmymemo export --format markdown\|claude\|chatgpt\|json` | Export |
| `openmymemo mcp --connector <id>` | **stdio MCP server** for local clients: Claude Desktop, Claude Code, Cursor, Codex, Gemini CLI … |
| `openmymemo serve` | Local server: panel at `http://127.0.0.1:8787` plus HTTP MCP at `http://127.0.0.1:8787/mcp` |
| `openmymemo review` | What AI changed since you last looked (undo per item), then anything waiting for your decision |
| `openmymemo approval [on\|off]` | Show or set whether AI changes wait for your approval (off by default) |
| `openmymemo status` | Memory counts, unreviewed changes, pending proposals, connectors, last access |
| `openmymemo connector add\|list\|config\|rotate\|remove` | Manage connectors; prints MCP config for each client using absolute paths |
| `openmymemo context [--connector id]` | Print exactly what a connector sees right now |
| `openmymemo add <topic> <text>` / `search <query>` | Add a memory yourself / search |

The server only listens on 127.0.0.1. The Mac app (§14) runs the same server for people who don't use a terminal.

### 5.3 The panel

1. **Memories** (home when nothing is waiting): topics on the left with kind / source / status filters; the memory list on the right. Double-click to edit inline; change kind, pin, mark private, move to another topic, archive; search at the top. On phones the topics become a horizontal scroll bar. Memories an AI added and the user hasn't looked at yet are marked as new. Anything waiting for a decision shows up as a card inside its topic, and as a single banner on the "All" view.
2. **Changes** (the panel opens here when something is unreviewed): at the top, the **"Require my approval for AI changes"** switch (off by default). Below it, first whatever waits for a decision — proposals in approval mode, changes to protected memories, anything in a private topic — as cards with the app, the operation (add / update / archive), old vs. new text, reason and evidence, and the actions accept, edit and accept, add as a new memory (when an add was turned into an update), reject, reject and stop accepting this app's proposals for the topic. Then the digest of applied changes, grouped by day and app, each with keep, edit and undo; "Looks good" marks everything as seen. A "History" tab lists past decisions.
3. **What AI sees:** pick a connector and see the full text it gets from `memory_context`, with character and approximate token counts and an adjustable length limit.
4. **Connectors:** each connected app with its read scope (topic allow-list, private or not), write mode (read only / write with review / write directly), blocked topics, last seen, read and proposal counts. Setup snippets for each client with a copy button; token rotation; revoke.
5. **Activity:** a timeline of who read or searched what, what they wrote or proposed and what happened to it.
6. **History:** the versions of any memory, with rollback (a dialog on each memory row rather than a separate page).
7. **Import & export:** paste, preview with duplicates and near-duplicates flagged, import into "From X". A "Sort into topics" action on any imported topic shows a suggested topic per memory and moves the ones you tick. Export to the formats in §5.6.

### 5.4 AI access (MCP)

Tool names use a `memory_` prefix, following MyMemo's MCP conventions (`structuredContent` plus text, an `idempotency_key` for writes).

| Tool | Description |
| --- | --- |
| `memory_context(topics?, max_chars?)` | The memory this connector may read, as Markdown, most important first. Meant to be called at the start of a conversation. Over stdio it is also exposed as the MCP resource `openmymemo://context` (mcp SDK 2.x static resources can't see who is asking, so over HTTP it is tool-only). |
| `memory_search(query, topics?, limit?)` | Keyword search in any language; returns matching memories with ids. |
| `memory_get(ids)` | Fetch memories by id with their metadata. |
| `memory_propose(op, content?, memory_id?, topic?, kind?, reason?, evidence?, idempotency_key?)` | `op ∈ add / update / archive`. Returns `{proposal_id, status}` where status is `applied` (the default for additions and for changes to memories an AI wrote; the user sees it in their digest and can undo it), `pending` (changes to protected memories, anything in a private topic, or every change when the user requires approval) or `duplicate`. **Never blocks the AI.** |
| `memory_proposals(status?, limit?)` | This app's own proposals and what the user decided, so it doesn't repeat rejected or undone ones. |

- MCP prompt `recall`: a standard instruction — "call memory_context before answering; when you learn something durable about me, call memory_propose with a reason and my words as evidence".
- The server instructions tell the model what happens to a write: additions apply right away and the user sees them in a digest; changes to memories the user wrote wait for the user; never ask the user to approve anything in the chat.
- For clients that support MCP elicitation, `memory_propose` could ask for approval right in the chat for the cases that wait; others get `pending` and the user decides in the panel later. (Not in v0.1.)
- Authentication: stdio is local by nature; identity comes from `--connector <id>` (first use creates a connector with default permissions). Local HTTP uses one `omm_` token per connector; `connectors.yaml` stores only its SHA-256, and the panel can rotate it. Same "store the hash, show the token once" approach as MyMemo's MCP.
- Protecting the local panel: binding to 127.0.0.1 isn't enough, because any web page in the user's browser can call localhost. Every request must carry a localhost `Host` (defeats DNS rebinding); panel API calls must also carry an `X-OpenMyMemo` header and, if present, a localhost `Origin` (a cross-site page can't add custom headers without a CORS preflight we never grant); `/mcp` requires a connector token.

claude.ai and chatgpt.com (web and desktop apps) can only reach public MCP servers, so they are not covered by the local server. The optional MyMemo Cloud connector (§16, 2026-10-12) relays them to the folder for people who sign in with a MyMemo account; the two routes below remain for people who don't. On the OpenAI side, Codex works today over stdio. Two later routes, neither needing a cloud service: (a) a browser extension acting as a localhost bridge — inject the summary into the page and pull the page's "Manage memories" list back; (b) the user runs their own Cloudflare Tunnel / Tailscale. This is the consumer shell in §10.

### 5.4.1 Plugins

Pasting config is the fallback; each mainstream app gets a native install path. Every plugin runs the same stdio MCP server with its own connector id, creates `~/OpenMyMemo` on first use (`mcp --init`), and serves the review panel from inside the MCP process (`--panel-port 8787`; if another app already serves the same vault there, it reuses it).

| App | Package | Memory at session start | Notes |
| --- | --- | --- | --- |
| Claude Desktop | `.mcpb` extension, MCPB "uv" server type (`integrations/claude-desktop/`, built by `scripts/build_mcpb.py`) | via `memory_context` / `recall` prompt | Claude Desktop installs Python and dependencies. Nothing to fill in on install: the memory folder is optional (empty means `~/OpenMyMemo`) and reaches the server as `OPENMYMEMO_HOME`, where an empty value or an unsubstituted `${…}` counts as unset; the panel port defaults to 8787. A literal `${HOME}` default showed up as text in Claude Desktop's settings dialog and invited users to browse to a different folder. Claude Desktop starts two copies of the server at once (chat, and Cowork/Code), so creating a new vault happens under the vault lock. This is the first zero-setup path (§3). |
| Claude Code | Plugin + marketplace at the repo root (`.claude-plugin/`) | SessionStart hook | `/openmymemo:memory`, `/openmymemo:remember` |
| Codex | Agent Plugins package at the repo root (`plugin.json`, `mcp.json`) + `.agents/plugins/marketplace.json` | User-level SessionStart hook in `~/.codex/hooks.json`, installed once by `openmymemo setup codex` (or the `codex-setup` skill, which runs `integrations/codex/setup.sh`); the user trusts it once with `/hooks` in the Codex CLI | `memory`, `remember`, `codex-setup` skills. Codex doesn't run plugin-shipped hooks (as of 0.162, bundled in the ChatGPT desktop app, its `plugin_hooks` feature is marked removed; verified with `.codex-plugin/plugin.json`, the default `hooks/hooks.json` and an Agent Plugins extension). Codex trusts hooks by content hash, so the installed command never names a plugin version: it finds the newest installed one when it runs, and upgrades keep it trusted. The ChatGPT desktop app reads the same settings but has no `/hooks` of its own. |
| Cursor | "Add to Cursor" install link in the panel | — | |
| VS Code | `code --add-mcp` command in the panel | — | |

Hooks call `openmymemo context --hook`, which registers the connector on first use, logs the read, and prints the memory for the session context, preceded by a one-line digest of AI changes the user hasn't reviewed ("OpenMyMemo: Claude Code added 3 memories since you last looked; review at …") so the assistant can mention it. It prints nothing on any failure, so a session is never blocked. Gemini CLI is not covered.

### 5.5 Review and undo

- **Default: apply now, review later.** What happens to a proposal:

  | Proposal | `require_approval` off (default) | `require_approval` on |
  | --- | --- | --- |
  | `add` | applied, listed in the digest | pending |
  | `update` / `archive` of a memory an AI wrote last | applied, listed in the digest | pending |
  | `update` / `archive` of a **protected** memory | pending | pending |
  | anything in a private topic | pending | pending |
  | a topic the user blocked for this app | refused | refused |

- **Protected memories:** a memory is protected when its latest version was written by the user — created by hand or in an editor, imported, edited, or accepted from a proposal. In one sentence: *AI never silently changes what you wrote.* A memory whose latest version came from an AI is not protected, so an app can correct its own earlier additions without asking.
- **Approval switch (whole vault):** `require_approval` in `.openmymemo/settings.yaml`, off by default; toggled on the Changes page or with `openmymemo approval on|off`. On: every proposal waits for the user, as in v0.1.
- **Trust levels (per connector):** `none` (read only — unaffected by the switch) / `propose` (follows the table above) / `auto` (applies everything except private topics, for an app the user trusts fully, even with the switch on).
- **Private topics always wait:** neither the switch nor `auto` ever writes into a private topic; those proposals always need the user.
- **The digest:** an applied proposal stays "unreviewed" until the user has seen it (`reviewed_at` on the proposal). The digest is the list of unreviewed applied proposals. It surfaces on the panel's Changes page, as the first line of the session-start hook output, in `openmymemo review` and `status`, in the generated `log.md`, and later as system notifications. "Looks good" marks everything seen; keeping, editing or undoing a single change marks that one.
- **Undo without a window:** undo is available for as long as the change is still the memory's latest version — undoing an add archives the memory, undoing an update restores the previous text and topic, undoing an archive restores the memory. Once something else has changed the memory (the user, an editor, another app, or an undo of a later change), the panel offers its version history instead. Undoing or editing a change also marks it seen; editing makes the memory the user's, so it becomes protected. `history.jsonl` is append-only, so nothing is ever lost. (v0.1 used a 7-day window.)
- **Proposal lifecycle:** `pending → accepted | edited_accepted | rejected | expired (30 days)`; `applied` (reviewed or not) `→ undone`. Accepted proposals can be undone the same way as applied ones.
- **Duplicates and conflicts:** before a proposal is stored it is compared with the memories that connector can read (Jaccard over Chinese character bigrams and English words, combined with edit distance). At ≥ 0.92 it is a duplicate and no proposal is created. An `add` at ≥ 0.6 becomes an `update` of the closest memory and is shown as a diff; the user can still "add as a new memory". If one side contains a negation and the other doesn't, it is flagged as contradicting. Embedding-based matching with a configured model is optional (§10).
- **Revocation:** removing a connector leaves a `revoked_at` tombstone. A stdio app keeps launching with the same `--connector` after removal, and without the tombstone its next call would quietly re-register it. After revocation every call returns "revoked"; adding the same id again is an explicit re-grant.
- **Rules:** a rejection or an undo can become a rule: "stop accepting this app's proposals about this topic", enforced per connector × topic at the point of proposal. An `add` that turns into an update of a memory in a blocked topic is refused too, so apply-now can never slip into a blocked topic.
- **Notifications:** a badge in the panel navigation, refreshed every 4 seconds; the hook line in AI apps; `openmymemo review` in the terminal. System notifications come later.

### 5.6 Import / export

Import sources in this phase:

| Source | How |
| --- | --- |
| ChatGPT | Paste the text of Settings → Personalization → Manage memories (button labels such as "Delete" and repeated items are dropped). Extracting memories from the official `conversations.json` export needs a model (§10). |
| Claude | Paste Settings → Capabilities → View and edit memory, or the `[date] – memory` list Claude produces when asked |
| Gemini / Grok and others | Ask "list everything you remember about me, one per line" and paste |
| Markdown / JSON / Mem0 export | File; Markdown headings and Mem0 categories are used as topic hints |

Every import goes parse → preview → duplicate check → save into `from-<source>` (created on the first import, in the "Imported" group), keeping the source's order and dates. Near-duplicates (≥ 0.8 similarity) are unticked by default. Nothing is classified at import time: the first thing a user sees after bringing their memory home is their list, not a taxonomy with mistakes in it. **Sorting** is a separate step (`openmymemo sort` or "Sort into topics" in the panel): a suggested topic per memory from the rule-based classifier, previewed, and only the ticked ones move; with a model endpoint configured, the suggestions come from the model. Memories may stay in `from-chatgpt` forever; AI reads them either way.

Export: the folder itself is a complete export; in addition there is Claude's import format (`[date] – content`), a prompt you can paste into ChatGPT, Markdown and JSON.

## 6. Data model

```
Memory
  id            stable short id (the ^block-id)
  content       one self-contained statement, ≤ 1000 characters
  kind          fact | preference | ongoing | instruction | event
                (goals, projects and people are topics; kind only says what sort of statement it is)
  topic         from the file name; section from the ## heading
  private       per-memory privacy (inline); topic privacy lives in the topic's frontmatter
  source        user | import:<vendor> | connector:<id>   (who created it)
  last_actor    user | file | import:<vendor> | connector:<id>   (who wrote the latest version; decides "protected")
  evidence      optional, the conversation snippet behind a proposal
  until         optional (YYYY-MM or YYYY-MM-DD, inline); the panel flags it 14 days before.
                Memories that AI or imports add to projects / goals / learning get one automatically.
  pinned        listed first in what AI sees
  status        active | archived (archived keeps the id so undo and history work)
  version, created_at, updated_at

Topic (frontmatter of memories/<slug>.md)
  type          topic | document (reserved)
  title, description, group, private

Settings (settings.yaml)
  require_approval: true | false   (default false)

Proposal
  id, connector_id, connector_name, op(add|update|archive), memory_id?, content, kind?, topic?
  reason, evidence, created_at, idempotency_key
  status(pending|accepted|edited_accepted|rejected|expired|applied|undone)
  decided_at, reviewed_at?, final_content?, final_topic?, applied_memory_id?
  before_content?, before_topic?, original_op?, similarity?, conflict
  undo_until?   (v0.1 only; v0.2 ignores it)

Connector
  id, name, kind(stdio|http), token_hash, token_preview
  read: { topics: [...] | all, include_private: false }
  write: none | propose | auto
  blocked_topics: [...]
  created_at, revoked_at?   (last seen is derived from activity.jsonl so reads never rewrite the config)

Activity (activity.jsonl)
  at, connector, action(context|search|get|propose|import), query?, memory_ids[], proposal_id?, result?

History (history.jsonl)
  at, memory_id, version, op(create|update|move|archive|restore), content, topic, actor, proposal_id?
```

## 7. Architecture

```
┌──────────────────────────── user's computer ─────────────────────────────┐
│                                                                          │
│  ~/OpenMyMemo/            (source of truth: Markdown + YAML + JSON)       │
│      ▲        ▲                                                          │
│      │ mtime check / block-id diff                                       │
│      │        │                                                          │
│  ┌───┴────────┴──────────────────────────────┐                           │
│  │  openmymemo core (Python)                 │                           │
│  │  parse/rewrite · in-memory search ·       │                           │
│  │  proposals · dedupe · import/export ·     │                           │
│  │  context compiler · activity log          │                           │
│  └───┬───────────────┬──────────────┬────────┘                           │
│      │ stdio MCP     │ HTTP MCP     │ HTTP API                           │
│      ▼               ▼              ▼                                    │
│  Claude Desktop   Cursor and     Panel (Vue 3, 127.0.0.1:8787)           │
│  Claude Code      other local                                            │
│  Codex, Gemini CLI  clients                                              │
└──────────────────────────────────────────────────────────────────────────┘
```

- The only optional external dependency is an OpenAI-compatible inference endpoint (or Ollama) for sorting, extracting memories from chats and semantic dedupe. Without it everything still works.
- No database server, no Docker, no account, no telemetry.
- The consumer shells (the Claude Desktop extension, the Mac app; a browser extension later) wrap the same core and the same folder.

## 8. Technology

| Layer | Choice | Why |
| --- | --- | --- |
| Core / CLI / MCP | Python 3.11+, packaged with `uv`; official `mcp` SDK 2.x (`MCPServer`); FastAPI + uvicorn for local HTTP; Typer for the CLI. Search runs in memory (enough for thousands of memories); no database. | Same language as MyMemo's backend, so MCP conventions and import logic carry over; few dependencies; one-line install with `uvx` |
| Panel | Vue 3 + Vite + Tailwind v4 (`web/`). The build output is committed to `src/openmymemo/static/` and ships inside the Python package, so running it needs no Node. | Same stack as MyMemo's web app; components can move between them |
| Mac app (`desktop/`) | Tauri 2 shell; bundles `uv` and the package source and runs the Python core as a child process; the window shows the same Vue panel in its Mac layout | One core; no second implementation until the format and the behaviour have settled with real users. Tauri keeps Windows and Linux open later |
| Optional model calls | OpenAI-compatible API (including Ollama) | No vendor lock-in |
| License | Apache-2.0 | The more open the format and protocol, the more widely it can be adopted |
| File format | Markdown + YAML frontmatter + Obsidian block ids; OKF-conformant | Human-readable, opens in any editor, diffs cleanly in git, readable by other tools |

## 9. What we reuse from MyMemo (code and patterns only, no services)

| OpenMyMemo needs | Taken from |
| --- | --- |
| MCP tool descriptions, `structuredContent` + text, `idempotency_key` on writes | MyMemo's MCP service |
| Token hashing, preview and rotation | MyMemo's MCP service |
| Connector settings panel (toggle, URL, one-time token, copyable config) | MyMemo's web app |
| "AI decision → undo / retry" list and status components | MyMemo's web app |
| Auto / manual mode state machine | MyMemo's backend |
| Inline tag chips | MyMemo's web app |
| Mixed Chinese / English tokenisation (if we add local semantic search) | MyMemo's backend |
| Browser extension skeleton (the localhost bridge) | MyMemo's browser extension |

Explicitly **not** reused: MyMemo's databases, job queues, sign-in, cloud storage and account system — none are needed here.

## 10. Phases

**Phase 0 — bring your memory home** ✅ v0.1
File format with parse and rewrite; `init` / `import` / `export`; stdio MCP (`memory_context` / `memory_search` / `memory_propose` writing proposal files); `review` in the terminal. Usable the same day by Claude Code, Claude Desktop and Cursor users.

**Phase 1 — panel and approvals** ✅ v0.1 (except system notifications)
`serve`, the Vue panel, local HTTP MCP with per-connector tokens, duplicate and conflict hints, activity log, history and rollback.

**Plugins** ✅ (§5.4.1): Claude Desktop, Claude Code, Codex, Cursor, VS Code.

**v0.2 — defaults for people, a format for the long run** (decided 2026-10-08, §13)
Format 2 (frontmatter, `log.md`, `from-*` import topics) with migration and `MEMORY_FORMAT.md`; apply-now with the digest, protected memories, undo without a window; `sort`; the Changes page and the hook line; repository rename and the PyPI release; the first real installs in Claude Desktop, Claude Code, Codex and Cursor, with the team using it daily and measuring how often models write and how many people turn approval on.

**v0.3 — documents and optional model help**
Document memories with diff review (import of `CLAUDE.md` / `AGENTS.md` / Cursor rules); with a model endpoint configured: model-based sorting and semantic dedupe, extracting proposals from ChatGPT `conversations.json` and local chat logs; system notifications; in-chat approval via MCP elicitation where clients support it.

**Consumer shells**
The Mac app (Tauri, Python core as a sidecar) that installs with one click, imports by paste, shows the digest and notifies — pulled forward on 2026-10-10, see §14. A browser extension as a localhost bridge for ChatGPT and claude.ai (inject the summary, read the "Manage memories" page, turn new facts from web chats into proposals). Still no cloud service. Scope and order are decided after v0.2 has real users.

## 11. Open questions

1. ~~Which metadata goes inline vs. in a sidecar?~~ Decided: only `until` and `private` inline; the rest in `meta.json`.
2. Proposal evidence contains conversation snippets that may be sensitive. Keep it only N days by default? (v0.1 truncates to 2,000 characters and keeps it.) Still open; leaning towards a retention setting.
3. ~~Should auto mode be per topic?~~ Superseded by protected memories (§5.5): the line is drawn by who wrote the memory last, not by topic.
4. Are 14 default topics too many? An alternative is folding Learning into Goals and Background into Identity (12 topics). Decide once the team has imported and sorted real memory.
5. ~~Chinese tokenisation~~ Decided: v0.1 doesn't use SQLite FTS5 (its trigram tokenizer returns nothing for two-character Chinese queries); search scores Chinese character bigrams and English words in memory.
6. ~~Should the repo carry a format spec so other tools can read and write the folder directly?~~ Decided: [MEMORY_FORMAT.md](./MEMORY_FORMAT.md).
7. ~~The rule-based classifier has limits.~~ Decided: nothing is classified at import time; sorting is a previewed step and uses a model when one is configured.
8. Should `how-to-work-with-me` become a document (prose) by default once documents exist, or stay a list of one-line instructions? A list is easier to review and undo line by line; prose is what `CLAUDE.md` users already write.
9. How should an AI propose a change to a document: the full new body, or a patch? The full body is simpler for models and for review as a diff; a patch is smaller and conflicts less.
10. With apply-now as the default, is a per-session cap on AI writes needed to keep the digest readable? Decide from the dogfood numbers.

## 12. v0.1 status (2026-10-08)

Implemented: everything in the original draft of §5.1–§5.6 except the items listed below. Tests cover parser round-trips, reconciling edits made in an editor, the full proposal lifecycle, import / export, classification, MCP end to end over a stdio subprocess, HTTP MCP with tokens plus approval through the panel API, and connector revocation.

Differences from the draft:

| Draft | v0.1 | Why |
| --- | --- | --- |
| `.openmymemo/` disposable; metadata in SQLite | Metadata, history, activity and proposals are all plain-text files; no database | Data the user owns should be files they can read, back up and put in git |
| SQLite FTS5 full-text index | In-memory search (character bigrams + words) | FTS5 trigram returns nothing for two-character Chinese queries; data is small |
| File watching (watchfiles) | mtime check before every read or write | One less long-running dependency; the stdio process and the panel behave the same |
| 7 memory kinds | 5: fact, preference, ongoing, instruction, event | Goals, projects and people are already topics |
| `openmymemo://context` resource in every mode | stdio only | mcp SDK 2.x static resources can't see who is asking |
| Bilingual panel, topic names in the user's language | English-only panel and default topic names | Product decision for the open-source release; memories can still be in any language |

Added after the draft: the vault-wide approval switch (§5.5).

Plugins for Claude Desktop, Claude Code and Codex, plus install links for Cursor and VS Code, were added on 2026-10-08 (§5.4.1). They were verified by running each package's exact launch command and hook from a copy of the package; they haven't yet been installed in the real apps.

Not implemented in v0.1: in-chat approval via MCP elicitation, system notifications, extraction from `conversations.json`, model classification and semantic dedupe, merging memories, PyPI release, the format spec, the browser extension.

## 13. v0.2 decisions and status (2026-10-08)

Decided after the first review of v0.1, before any user outside the team had installed it. Each row changes the shipped behaviour; all of them are implemented in v0.2 unless the row says otherwise.

| Area | v0.1 (shipped) | v0.2 | Why |
| --- | --- | --- | --- |
| Default for AI changes | `require_approval` on: every proposal waits in a queue | Off: additions and corrections of AI-written memories apply at once and appear in a digest; protected memories and private topics wait | Approving every line is friction most people switch off within days; what people need is to see and undo |
| Protected memories | Per-topic auto mode was an open question | A memory whose latest version the user wrote (created, imported, edited, accepted) never changes without them | One sentence to explain, keeps "it's my memory" true even with apply-now |
| Undo | 7-day window | While the change is the memory's latest version; version history after that | A consumer may not open the panel for weeks; history is append-only anyway |
| Digest | — | Unreviewed applied changes (`reviewed_at` on proposals): Changes page, hook line, `review`, `status`, `log.md` | Review happens where the user already is |
| Topic file header | `# Title` + `> description`; privacy and groups in `topics.yaml` | YAML frontmatter: `type`, `title`, `description`, `group`, `private`; `topics.yaml` keeps only group names and order | The convention every other Markdown tool uses; self-describing files; OKF-conformant bundle |
| `log.md` | — | Generated human-readable change log, newest day first | People read prose; the Open Knowledge Format reserves the name for exactly this |
| Documents | — | `type: document` reserved in the format; implemented in v0.3 | Prose instructions and project context don't fit one-line items; reserving the type now avoids a format bump later |
| Import target | Classified into the 14 topics at import | `from-<source>` topic in an "Imported" group, original order and dates; `sort` as a separate previewed step | The first impression after bringing memory home must not be a misfiled taxonomy |
| Panel | Memories home; "Review" page | Opens on "Changes" when something is unreviewed; Review becomes Changes | The digest is the main interaction, not the queue |
| Hook output | The memory context | A one-line digest first, then the context | Zero-cost notification inside the apps people use |
| Spec | — | `docs/MEMORY_FORMAT.md` | Other tools can read and write the folder; the format is the asset |

Compatibility: readers keep accepting format-1 files (title and description from `# Title` and `> …`, groups and privacy from `topics.yaml`). On the first open after the upgrade, OpenMyMemo migrates each file (frontmatter inserted from the header lines and `topics.yaml`; the header lines removed; every other line untouched) and bumps `meta.json` to format 2. A format-1 vault whose `settings.yaml` is missing or has no `require_approval` key gets `require_approval: true` written out so an existing vault keeps its behaviour; new vaults start with the new default. Details in [MEMORY_FORMAT.md §11](./MEMORY_FORMAT.md).

Implementation order, all done in one branch: (1) ✅ format 2 with migration and tests against `MEMORY_FORMAT.md`; (2) ✅ apply-now defaults, protected memories, undo without a window, `reviewed_at` and the digest in the core, MCP, HTTP API and CLI; (3) ✅ the Changes page, the hook line and `log.md`; (4) ✅ import into `from-*` and `sort`; (5) ✅ repository rename, README, plugin descriptions and version 0.2.0. Still open: the PyPI release, and the team installing the real plugins in Claude Desktop, Claude Code, Codex and Cursor and using them for two weeks before anything else is planned.

## 14. The Mac app (2026-10-10)

Pulled forward from the consumer shells (§10) so that people who never open a terminal, and don't use Claude Desktop, can have the panel as an app. It supplements the web panel, it doesn't replace it: both are the same Vue code, and the app switches it to a Mac layout. The design is the "Mac app · Memories" artboard on the panel redesign canvas; the other sections reuse the web screens inside the Mac window.

| Area | Decision | Why |
| --- | --- | --- |
| Shell | Tauri 2 in `desktop/`; one window plus a menu-bar item | §8: one core, a thin shell; Tauri keeps Windows and Linux open |
| Core | The app bundles `uv` and the package source (as the `.mcpb` does). On launch it runs `openmymemo serve --no-open` with `UV_PROJECT_ENVIRONMENT` in `~/Library/Application Support/OpenMyMemo/`; the first launch downloads Python and the dependencies | No second implementation; nothing to install by hand |
| One server per folder | Before starting its own server the app asks the port (8787 by default) which folder it serves. If it is the same folder (e.g. Claude Desktop's extension got there first) the app uses it, and starts its own when that one goes away; if the port belongs to something else it tries the next ports | Claude Desktop, the CLI and the app share one panel and never fight over the files |
| Window | The panel loaded from `http://127.0.0.1:<port>/?desktop=1`, so it passes the panel guard (§5.4) unchanged. Closing the window keeps the app in the menu bar; the app quits from the menu | Menu-bar apps keep running so they can notify |
| Mac layout | Sidebar: sections, topics by group (collapsible), "New topic", and a status card ("Running on this Mac · N apps connected"). Memories: a list and an inspector for the selected memory — text, topic, kind, review date, source, file, which apps can read the topic, version history with restore, "Show in Finder", archive. Other sections keep their web screens; their page title becomes the window toolbar | The artboard; the web screens already work |
| Menu bar | The number of changes to review next to the icon; a system notification when an AI app changes something or something starts waiting; Open, Quit | "Notifies" from §10, without opening the panel |
| Shortcuts | ⌘N new memory, ⌘F search, ⌃⌘S hide the sidebar, ⌘, settings | The artboard's tooltips |
| Settings | A small sheet (not drawn on the canvas): the memory folder (show in Finder, change), the approval switch, open at login | The few app-level choices there are |
| Not in the app | "Delete" from the artboard: memories are archived, never deleted, because history is append-only (§5.5, §6) | Undo and history must keep working |

Not done yet: automatic updates, an Intel build, Windows and Linux, and a first launch without a network connection (Python is downloaded then; the setup page says so and offers Retry, §15.2). Signing and notarization happen in the release workflow when the Apple Developer secrets are set; local builds and forks are signed ad hoc, so macOS asks once to open them. The Settings row above is superseded by §15.2.

## 15. Two ways in, one-click connections, a first-run guide (2026-10-12)

Status: built (§15.1 one-click connections, §15.2 the Mac app as an installer, §15.3 the first-run guide).

The Mac app becomes the way in for everyone who doesn't live in a terminal; the command line stays the way in for developers. Both use the same folder, core and panel (§3), so this section adds no second implementation.

- **The app:** download `OpenMyMemo-<version>.dmg` from the GitHub release, drag it to Applications, open it. A short guide connects the AI apps already on the Mac with one click each and brings existing memory over. No terminal, no config files, no Python to install.
- **The command line:** `uv tool install`, the plugins, `openmymemo connect <app>`, editing the files. It isn't fenced in: everything the app does is available from the CLI, and the folder format (MEMORY_FORMAT.md) is the interface.

### 15.1 AI apps: a catalog and one-click connect

| Area | Decision | Why |
| --- | --- | --- |
| Catalog | `src/openmymemo/clients.py` lists the AI apps OpenMyMemo knows. Each entry: an id (also its connector id: `claude-desktop`, `claude-code`, `codex`, `cursor`, `vscode`, … — the ids the plugins already use), a display name, how to detect it on this computer (app bundle, config folder, or command on `PATH` and the usual install folders, since an app launched from the Finder has no shell `PATH`), and how to connect it: **file** (OpenMyMemo edits the app's own MCP config file at a fixed path), **cli** (it runs the app's own `mcp add` command), or **manual** (a snippet to paste, plus an install link where the app has one). A config location and schema goes in as `file` or `cli` only once it has been checked against the app's current documentation; anything unverified is `manual`. A generic "Other MCP app" entry gives the raw stdio and HTTP configs. | Pasting JSON is where people give up; the apps that matter most should take one click |
| Status | Per app: **installed** (detected here), **connected** (its config has an `openmymemo` entry, or a plugin of ours is installed), **in use** (its connector has read memory: `last_seen` from `activity.jsonl`). The panel lists installed apps first. | People need to see what is wired up and whether it works |
| Connect | Writes an entry named `openmymemo` that runs `<command> mcp --connector <id> --panel-port 8787` (§5.4.1), creates the connector with default permissions (re-granting it if it was revoked), and for Codex also installs the session hook (`codex.py`) and shows the one-time trust step. Then it tells the person to restart that app. | One action does everything an app needs |
| Safety of writes | Only the fixed config path of that app is touched. The file is parsed and written back atomically with only the `openmymemo` key changed; before the first change a backup `<file>.openmymemo-backup` is kept. A file that doesn't parse as plain JSON (VS Code's and Zed's JSONC with comments, a broken file) is never rewritten: the person gets the snippet to paste instead. Claude Code's `~/.claude.json` is its live state and is never edited; `claude mcp add` is used. Tests point every writer at a temporary home. | Other apps' settings are the person's; breaking them costs more trust than the feature earns |
| Disconnect | Removes only the `openmymemo` entry (and Codex's hook) and revokes the connector (§5.5 tombstone). Removing a connector in the panel offers to remove the entry too. | Revoking access shouldn't leave a broken server in the app's list |
| Where | Panel (Connectors page and the first-run guide), behind the same API guard as every other write; CLI `openmymemo apps`, `openmymemo connect <app>`, `openmymemo disconnect <app>`. | Both shells, one implementation |
| The command written into configs | `snippets.command_for()`: the `OPENMYMEMO_COMMAND` environment variable when set — the absolute path of an executable that behaves exactly like the `openmymemo` CLI — otherwise the installed CLI, as before. The Mac app sets it to its launcher (§15.2) for the server it starts. | Configs written by the app must not point into a venv or a bundle path that moves |

As built (2026-10-12), where the table left a choice open:

- **The catalog** (checked against each app's docs that day; the URL is next to each entry in `clients.py`): **file** — Claude Desktop, Codex (CLI, IDE extension and the ChatGPT app share `~/.codex/config.toml`), Cursor, VS Code, Gemini CLI, Zed, Cline (`~/.cline/data/settings/`, shared by its extension and CLI since 4.0), LM Studio, Kiro, opencode, GitHub Copilot CLI, Qwen Code, Amp; **cli** — Claude Code; **manual** — Windsurf (now Devin Desktop, whose pages disagree on the config path), Goose (YAML, and its install link only runs a fixed list of commands), Trae, Cherry Studio, JetBrains AI Assistant, Warp, Antigravity, and "Other MCP app". Install links where an app has one: Cursor, VS Code, LM Studio, Kiro, Trae, Cherry Studio.
- **Codex** gets its table appended to `config.toml` as text, not through `codex mcp add`: a server started from the Finder has no `codex` on `PATH`, and ChatGPT-app users may have no CLI at all. The table is added only when `tomllib` shows it is absent (or replaces our own older one), taken out again line for line, and both are checked by parsing that nothing else changed; anything else (an inline `mcp_servers = {…}`, a broken file) gets the snippet.
- **Writes** (`appfiles.py`, for app configs and Codex's `hooks.json` alike) follow `vault._atomic_write` (a temporary file in the same folder, then a rename), and because these are other apps' files they also keep the file's permissions from the temporary file's first byte (some hold API keys), write through a symlink to the real file (dotfile repositories) and keep the one-time backup. Connecting again keeps an `env` the person added to our entry. A broken `hooks.json` doesn't stop Codex from being connected or disconnected; the result says the hook is missing.
- **Plugins:** an app that already has our plugin (the `.mcpb` extension listed in Claude Desktop's `extensions-installations.json`, the Claude Code plugin in `~/.claude/plugins/installed_plugins.json` unless `enabledPlugins` turns it off, the Codex plugin cache) counts as connected and gets no second config entry; Connect only grants its connector (and, for Codex, installs the hook in plugin mode). Disconnect revokes it and says where to remove the plugin.
- **Manual apps:** "Set up" shows the snippet; it re-grants a revoked connector (so the pasted config works) but creates none, so an app isn't shown as connected before anything was pasted — its first call registers it. Status reads the app's config where its location is known, comments allowed.
- **Status** also reports `revoked` (the entry is still in the app but access was removed; Connect re-grants) and `broken` (the entry runs a program by an absolute path that no longer exists; Connect rewrites it). Connectors an app registers on first use get the app's name from the catalog.
- **Panel API:** `GET /api/clients` (catalog with status, connector summaries for connected apps, and connectors added by name), `GET /api/clients/{id}/setup`, `POST /api/clients/{id}/connect`, `POST /api/clients/{id}/disconnect`; each action returns `{status: done | manual | error, message, path, snippet, link, steps}` and the app's new status. `openmymemo connect` creates the memory folder when it is missing.

### 15.2 The Mac app as an installer

| Area | Decision | Why |
| --- | --- | --- |
| Launcher | The app writes a small `sh` launcher at `~/Library/Application Support/ai.mymemo.openmymemo/bin/openmymemo` on every start and passes its path to the server as `OPENMYMEMO_COMMAND`. AI apps' configs point at the launcher, never into the bundle. It must keep working when the app was first opened from the disk image and then dragged to Applications, when the app moved, when the app isn't running (an AI app may start it at login), and while the first start is still downloading Python. | The MCP command is what other apps keep; it has to outlive app moves and updates |
| How the launcher works | The app installs the package **non-editable** (`uv sync --no-editable`) into an environment per build, `venvs/<version>-<stamp>` (the stamp is the staged `pyproject.toml`'s modification time), writes `.openmymemo-ready` into it last, and only then points the `venv` link at it (a new link renamed over the old one). It does this on every start before it looks for a server, so an update is installed even while an AI app's MCP process already serves the folder; if the install fails (no network) but a ready environment exists, it starts from that one. A moved app keeps its build, so moving installs nothing. Servers run from their environment's real folder, so a running AI app keeps its version until it restarts, and the app keeps the current and the previous environment; older ones are removed once no process runs from them (every `openmymemo` process started from an environment holds a shared `flock` on its `.openmymemo-in-use` for as long as it runs, so a server an AI app kept alive across two updates keeps its files). The launcher runs `venv/bin/openmymemo` when `venv` is marked ready (no uv, no bundle). Otherwise (the first start is still downloading Python, or an install was interrupted) it finds the app — the path it was written from unless the app said that was its disk image or a translocated copy, then `/Applications`, `~/Applications`, Spotlight by bundle id, and last the disk-image copy while it is still mounted — and runs the bundled uv with the app's `UV_*` settings into that build's environment folder; uv locks the environment and the Python download, so a launcher started during the first start waits for it, finishes an interrupted install, and one killed by an AI app's timeout leaves nothing broken. It exports `OPENMYMEMO_COMMAND` (itself) for the servers it starts, unsets `PYTHONPATH`/`PYTHONHOME`/`VIRTUAL_ENV`, prints nothing to stdout, and exits 1 with a message on stderr when no app or environment is found. | The environment survives moves and updates under running AI apps; the bundle is only needed once |
| Moving to Applications | Opened by macOS from its disk image (a read-only volume under `/Volumes`) or from an App Translocation path, the app asks, as a sheet on its window, "Move to Applications" / "Not now". It copies itself with `ditto` under a hidden name, drops the quarantine flag (the person just opened this app; a copy the Finder didn't move would be translocated again), renames the copy into place, reopens from there and quits, then ejects the disk image. A copy already in Applications is replaced only after asking, and quit first if it is running. Started from a terminal (no `__CFBundleIdentifier` of its own), it doesn't ask. | Running from the disk image is the most common way to break a dragged-in app |
| Download | The release workflow builds `OpenMyMemo-<version>-arm64.dmg` on macOS and publishes it in the same GitHub release as the `.mcpb` and the wheel; the release is created only once all of them are built. With the Apple Developer secrets in the repository it is signed with a Developer ID and notarized (an App Store Connect API key; an Apple ID as a fallback); without them it is signed ad hoc, and the README says how to open it the first time (macOS 15: System Settings → Privacy & Security → Open Anyway). | One file to download, one drag |
| First start | The setup page says what is happening while Python and the libraries are downloaded (about 25 MB, usually under a minute, once), in two steps, and without a network says so and offers Retry (the app also retries by itself). | The only wait in the whole flow should be explained |
| Settings | A sheet (⌘,) in groups: General (the memory folder, open at login, notifications on or off), Appearance (System / Light / Dark, for the whole app; the panel follows through WebKit's `prefers-color-scheme`), AI changes (the approval switch), Help (the log, GitHub, the version), Advanced (the launcher's path). `appearance` and `notifications` are saved in the app's `settings.json`; older files load with the defaults. | The few app-level choices there are |
| Links | The panel's links to the web and mail, and the AI apps' install links (`cursor:`, `vscode:`, `vscode-insiders:`, `lmstudio:`, `kiro:`, `trae:`, `cherrystudio:`), open outside the app; other schemes are blocked, and the app never opens a second web view window. | Install buttons must work in the app as in a browser |

### 15.3 The first-run guide

No sample data is ever written into a person's folder; `scripts/demo_vault.py` stays a developer tool. Instead, an empty folder opens on a short guide (`#/welcome`, in both the web and the Mac layout), every step skippable:

1. **Welcome** — what OpenMyMemo is in three lines (a folder on this computer; AI apps read it and add to it; you see every change and can undo it) and where the folder is.
2. **Connect your AI apps** — the installed apps from the catalog with a Connect button each (the same component as on the Connectors page).
3. **Bring your memory** — copy the prompt to ask ChatGPT, Claude or Gemini for what they remember, paste the answer, preview, import (the existing import flow, into `from-<source>`; "Other" imports a one-per-line list into `from-text`).
4. **A few things about you** (optional) — what AI should call you, what you do, how you like answers. Saved as your own memories in Identity, Work and How to work with me, so they are protected from the start (§5.5). Each answer becomes one statement around the person's own words — "Prefers to be called …", "Work: …", "Answer style: …" — shown under the field before it is saved. Going back and changing an answer updates the memory the guide saved for it (or the identical one it found and kept), and clearing it archives that memory (restorable, §5.5), unless someone changed it meanwhile; an answer is never saved twice.
5. **Done** — what happens next: try "what do you remember about me?" in a connected app; AI changes show up in Changes; the folder is yours.

When it shows: `onboarded` in `settings.yaml` (MEMORY_FORMAT.md §6.1). New folders are created with `onboarded: false`. A folder without the key counts as onboarded if it has any memory or any connector (so people upgrading never see it); otherwise as not onboarded, and the panel writes `false` when it opens the guide, so connecting an app in step 2 doesn't end the guide early. Finishing or skipping writes `true`; reopening a finished guide doesn't reset it. Until then "Get started" heads the navigation, and the panel opened without an address goes to the guide, except that Changes comes first when an AI app has changed something or something waits (a folder a plugin created with `--init` may have weeks of changes before the panel is first opened). The guide can be reopened from Settings (the Mac app), from the panel's footer and from the empty Memories page. Each step has its own address (`#/welcome/import`), so the back button goes back a step. `scripts/demo_vault.py` marks its folder onboarded, since a demo is about the panel itself.

## 16. MyMemo Cloud: an optional connector for AI apps on the web (2026-10-12, design)

ChatGPT on the web and claude.ai can only reach MCP servers on the public internet (§5.4), so until now nothing they learn reaches the folder. MyMemo Cloud closes that gap with a relay that a person turns on by signing in with their MyMemo account. It is an optional connector, the way §3 always anticipated MyMemo: off by default, listed in the catalog next to Claude and Cursor, with the same permissions, activity log and revocation as every other app. Nothing else in OpenMyMemo depends on it, and everything works without it.

Decided by the product owner on 2026-10-12: (1) the cloud is an optional connector, and the non-negotiables in AGENTS.md change by one sentence instead of being dropped; (2) when the computer is offline, writes wait in a queue encrypted to the computer's key, so the service can't read what it stores, and reads fail (option "b"; the service never keeps a readable copy of the memory at rest, though like any MCP server it sees each call in memory as it passes through); (3) subscribers aren't billed per call for the memory tools for now; (4) the offline queue is for Ultra subscriptions and AppSumo tier 3 only.

### 16.1 What changes in the principles

- AGENTS.md, "Local only": no cloud storage, sync, accounts or telemetry — **except the optional MyMemo Cloud connector (§16), which does nothing until the person signs in and never holds memory in readable form at rest.**
- AGENTS.md, "No dependency on MyMemo services": the core never depends on them; the MyMemo Cloud connector is the one optional piece that talks to MyMemo, and removing it changes nothing else.
- §3 (relationship to MyMemo) and §5.4 (web apps) point here. Principle 1 in §2 still holds: the folder is the only source of truth; the cloud relays and, offline, queues encrypted proposals.

### 16.2 How it works

```
ChatGPT (web) ─┐   remote MCP + OAuth (MyMemo account)
claude.ai ─────┼──▶ MyMemo service  /openmymemo/mcp
               │        │  routes the call to the person's linked computer
               │        ▼
               │    bridge connection (WebSocket, opened by the computer; no inbound port)
               │        │
               └──────▶ OpenMyMemo on the computer ── same tools as local MCP ──▶ proposals.py ──▶ the folder
                         (the process that serves the panel)
```

- **The web side** (the MyMemo service, closed source): one remote MCP endpoint for everyone, `/openmymemo/mcp`, Streamable HTTP. OAuth 2.1 with PKCE (S256) identifies the MyMemo member: Protected Resource Metadata (RFC 9728) on the endpoint, authorization server metadata with RFC 9207 `iss`, and Client ID Metadata Documents, which both claude.ai and ChatGPT prefer, with Dynamic Client Registration only as a fallback (it is deprecated in the 2026-07-28 MCP authorization spec). Clients and redirect URIs are allow-listed at first. The person signs in with their MyMemo account and consents once per app on a page in the MyMemo web app; the service itself has no browser session. The tools and their schemas are exactly the local ones (§5.4): `memory_context`, `memory_search`, `memory_get`, `memory_propose`, `memory_proposals`, so skills and prompts carry over. `initialize` and `tools/list` must answer while the computer is offline: the computer sends its tool descriptors when it connects (a cloud variant that mentions `queued` and leaves out the panel's localhost link), and the service keeps those descriptors — never memory.
- **The local side** (OpenMyMemo): the bridge runs in the process that serves the panel for the folder (`openmymemo serve`, the Mac app's server, or a plugin's MCP process serving the panel; one per folder, §14). It keeps one outbound WebSocket to the service, answers relayed tool calls by running the same code the stdio and HTTP MCP use, and reconnects with backoff.
- **Identity of web apps locally:** each OAuth client becomes its own local connector — `cloud-chatgpt`, `cloud-claude`, or `cloud-<slug>` for others, derived by the service from the client's redirect host, never from the name a client claims — shown as "ChatGPT (web)", "Claude (web)", …, created on first use with the defaults (all topics except private ones, `write: propose`). Read scope, writing mode, blocked topics, "what AI sees" and revocation work as for any app. A revoked connector answers every relayed call with "revoked", and the service passes that on.
- **Writes** always become proposals locally and follow the connector's writing mode exactly as for a local app (§5.5): with the default `write: propose`, additions apply and show up in the digest while changes to memories the person wrote wait; with `write: auto`, those changes apply too and stay undoable; anything private always waits. The cloud never applies anything itself.

### 16.3 Two modes

| | Live relay | Offline queue |
| --- | --- | --- |
| Who | Every paid tier (Pro, Ultra, AppSumo and Oncely) | Ultra subscriptions and AppSumo tier 3 only; AppSumo and Oncely tiers 1–2 get live relay |
| Computer online | Every call is relayed and answered by the computer within 20 s; nothing is stored | Same |
| Computer offline: reads (`memory_context`, `memory_search`, `memory_get`, `memory_proposals`) | Fail with a plain message: "OpenMyMemo isn't running on your computer right now" | Fail the same way (decision 2) |
| Computer offline: `memory_propose` | Fails the same way | Queued: the service encrypts it to the computer's public key on arrival and stores the ciphertext; the AI gets `status: "queued"` with a `queue_id` and no `proposal_id`. That is a receipt, not an acceptance: the connector's permissions are only checked on the computer, so it may still be turned down there |
| Kept on the service | Nothing beyond request metadata | Encrypted proposals, for at most 30 days, deleted once the computer confirms it stored them |

- **Encryption of the queue:** when linking, the computer creates an X25519 key pair and registers the public key. The service encrypts each queued proposal to it with HPKE (RFC 9180: X25519, HKDF-SHA256, ChaCha20-Poly1305, from `cryptography`, which both sides already have) before writing anything to storage. Plaintext exists on the service only in memory while a call passes through, as for any MCP server; it is never logged. The guarantee is about data at rest: only the computer can open what is stored, but the service does see each write as it arrives.
- **Draining:** on reconnect the computer receives queued items in order, opens each, files it through `proposals.py` as that connector with the original time, and acknowledges by queue id. The queue id is derived on the service from the AI's own `idempotency_key` when it sent one (an HMAC, so the raw key isn't stored in the clear) and random otherwise; it names the row and the acknowledgement. The AI's own key travels inside the sealed item (the arguments are sealed whole) and becomes the proposal's `idempotency_key` locally, falling back to the queue id when the AI sent none. So a replayed item is a duplicate, and so is the same request retried live with the same key once the computer is back. An item turned down on the computer (the app was revoked, is read-only or blocked from the topic, or the item is malformed) is acknowledged as `rejected` and deleted, never retried; the refusal is recorded in the activity log. At most 500 items or 4 MB per computer. Items it can't open (a key that changed) are reported and dropped.
- `memory_propose`'s result gains one status, `queued`, only ever returned by the cloud, with the `queue_id`. A proposal filed from the queue keeps that `queue_id`, and `memory_proposals` returns it with each proposal, so the AI can match its receipt to the proposal once it is pending, applied or rejected. An item turned down while draining never becomes a proposal. A receipt without a match therefore means only that its outcome isn't known yet: the item may still be queued or being filed, may have been turned down, or may sit outside the newest proposals `memory_proposals` lists (at most 100). The AI must not take it as a refusal, nor send the write again under a new key; retrying with the same `idempotency_key` is always safe, since it returns the proposal already filed.
- **A relayed call that times out** has an unknown outcome: the AI is told so and pointed to `memory_proposals`; a timed-out `memory_propose` is never retried through the queue. Results are capped at 512 KB, requests at 64 KB.

### 16.4 Linking a computer

- **From the panel** (catalog entry "MyMemo Cloud — use your memory in ChatGPT and claude.ai on the web", method `account`) or **the CLI** (`openmymemo cloud link | status | unlink`).
- **Flow:** the panel opens the browser at MyMemo's link page with a PKCE challenge and a loopback redirect (`http://127.0.0.1:<port>/cloud/callback`, RFC 8252); the person signs in to MyMemo; the callback's code and the verifier are exchanged for a device token (`omd_…`, stored on the service only as a SHA-256, like the existing MCP tokens), and the public key is registered. The callback route checks the `state` and accepts only that one exchange; the panel API guard is otherwise unchanged.
- **Where the link lives:** per computer, not in the folder — the folder may be synced to other machines or kept in git. `~/Library/Application Support/OpenMyMemo/cloud.json` on macOS (`%APPDATA%\OpenMyMemo\` on Windows, `$XDG_CONFIG_HOME/openmymemo/` elsewhere; `OPENMYMEMO_CLOUD_DIR` overrides it), mode 0600: the device token, the private key, the folder it serves and the account it belongs to. If the folder moves, the panel asks to re-link.
- **One linked computer per account** at first. Linking another one replaces it; several computers come later (open question 1). Re-linking (another computer, or a new key after the folder moved) first drains the queue to the current computer; if that computer can't come online, the panel says how many queued proposals would be lost before it goes ahead.
- **Unlinking** (panel, CLI, or the MyMemo website) deletes the device token, the public key and the queue on the service, and the local `cloud.json` — at once from the panel or CLI; when it was done on the website while the computer was off, at the next connection attempt, and only on MyMemo's own word: a close 4401 from the service, or a 401 `invalid_token` from `GET /openmymemo/device`, which the computer asks after any refused handshake. A bare 403, which a proxy or firewall in front of the service can also send, never deletes the link (it holds the private key). The `cloud-*` connectors stay as revoked tombstones, as for any app (§5.5). The web apps' OAuth grants survive an unlink, so after linking again ChatGPT and claude.ai are still connected but turned away until the person allows each one again in the panel (an explicit re-grant, §5.5).

### 16.5 Setting up the web apps

After linking, the panel shows the endpoint URL and short steps per app. The steps are checked against the vendors' current docs when this is built, since they move:
- The URL is shown with a copy button: claude.ai needs it exactly, without a trailing slash.
- **ChatGPT:** custom connectors need Developer Mode (in Settings; where it lives moves), then a connector with the URL and OAuth. ChatGPT asks before running a tool that writes. Which ChatGPT plans allow write tools in custom connectors is not settled: some sources limit full MCP to Business, Enterprise and Edu and make others read-only. Check this in ChatGPT itself before promising writes from ChatGPT to everyone. A listing in ChatGPT's app directory (Apps SDK) would remove the Developer Mode step later.
- **claude.ai:** Settings → Connectors → Add custom connector with the URL; Claude signs in through OAuth using its published client identity.

### 16.6 Status in the panel

The MyMemo Cloud row on the Connectors page shows: signed in as (name and masked email), online or offline, the plan's modes, how many proposals are queued (from the service), the last relayed call, and the web apps using it, each a normal connector row underneath. Activity from the cloud is recorded in `activity.jsonl` like any other read or write, with the connector id.

### 16.7 Limits, billing, abuse

- Memory tools are not charged against MyMemo's MCP call credits for subscribers (decision 3); they are rate limited per member (a starting point: 60 calls a minute, 5,000 a day) and capped at 64 KB per request, as the existing `/mcp` is.
- Free accounts can't link; the panel says which plan includes what.
- The service keeps per-call metadata for operations and abuse (member, client, tool, time, size, outcome), never arguments or results.

### 16.8 Built where

- **OpenMyMemo** (this repo): `src/openmymemo/cloud.py` (link state, the WebSocket bridge, relayed calls through the existing tool code, queue draining), the callback route and `/api/cloud/*` in `web.py`, `openmymemo cloud …` in `cli.py`, the catalog entry and panel row, tests against a fake service. It adds two dependencies: `websockets` (the standard library has no WebSocket client) and `cryptography>=48` declared directly (HPKE; it is already installed through `mcp`).
- **The MyMemo service** (closed source): the OAuth authorization server for MCP clients, `/openmymemo/mcp`, device linking, the bridge endpoint, the encrypted queue, entitlements and limits. This repo depends only on the wire protocol, pinned by the fixtures in `tests/fixtures/cloud/`.
- **The MyMemo web app** (closed source): the consent page for the OAuth flow and a MyMemo Cloud section in account settings (linked computer, unlink, queued count).
- **Order:** the service's OAuth and endpoint with live relay, and the local bridge (phase 1); the offline queue (phase 2); the ChatGPT app directory listing (later).

### 16.9 Open questions

1. Several computers per account: route reads to the most recently active one and queue writes for all, or let the person pick one per web app?
2. Should a web app's connector default to fewer topics than local apps (e.g. only "How I work" and "Who I am") until the person widens it?
3. Mobile ChatGPT and Claude apps use the same connectors; anything specific to check there?
4. MCP elicitation for in-chat approval (§5.4) applies here too once the web apps support it.

### 16.10 As built (2026-10-12)

Where the sections above left a choice open, the build settled it as follows (phase 1, the live relay):

- **Connectors:** web apps get the connector kind `cloud` (MEMORY_FORMAT.md §5), named "ChatGPT (web)", "Claude (web)", or "… (web, unverified)" for an unverified client. A revoked one answers `revoked`; after unlinking, or after removing one by itself, it comes back only through **Allow again** on the MyMemo Cloud row — linking again doesn't re-grant it.
- **Which process connects:** the bridge runs on a thread of the process serving the panel, starts only once that process holds the panel's port (two AI apps may start a panel for the same folder at once; only the one that gets the port connects) and closes with 1000 when the panel stops. A panel inside an AI app's MCP process closes it at interpreter exit, not when the process is killed.
- **The connection:** it offers the WebSocket subprotocol `openmymemo-bridge.v1`, sends no `Origin` and no compression. After an upgrade refused with 401 or 403, or close code 4401, it asks MyMemo with `GET /openmymemo/device` and the device token (no redirects followed). Only MyMemo's exact answer that it no longer knows the device (HTTP 401 `{"error": "invalid_token"}`) deletes `cloud.json`, private key included, and revokes the web apps. Anything else (a 403 or an HTML page from a firewall or proxy in front of the service, a redirect, no network, a 200) keeps both and retries with backoff; the panel says MyMemo turned the connection away and offers Try again. `cloud.json` is only ever deleted while it still holds that device, and every change to it (save, compare-and-delete, update) holds a lock on `.cloud.lock` next to it, so linking again — from the panel, which stops the old connection before exchanging the code, or from another process — can't lose the new link.
- **Relayed calls:** a result carries `content`, `structuredContent` and `isError` only, and is replaced by a "too large to relay" message above the service's `result_max_bytes`. Tool errors (read-only app, bad arguments) come back as `isError` results, as over local MCP; a malformed call gets `bad_request`, a crash `internal`.
- **The callback** requires RFC 9207's `iss` and compares it with the service the sign-in went to (§16.11) exactly; an answer without it, or from another issuer, links nothing. Against a local service the URL must therefore be written exactly as the service's own issuer.
- **The linked account** is shown with MyMemo's masked email wherever it appears, "Sam (s***@example.com)": on the callback page (which says to unlink if that isn't your account), on the panel row ("Signed in as Sam · s***@example.com"), in `openmymemo cloud link` and `status`, and as a `link` entry in `activity.jsonl` (MEMORY_FORMAT.md §6.5). Someone who learns a pending sign-in's id could approve it with their own account and link this computer to it; the email makes that visible. `cloud.json` keeps `member.email_masked` from the link response and takes a newer one from `welcome`; a `welcome` without it keeps the stored one.
- **Unlinking** while MyMemo can't be reached still forgets the link here and says to remove the computer in the MyMemo account settings too.
- **The CLI** (`openmymemo cloud link`) signs in through the running panel when one serves the folder (on `--port` or any of the Mac app's ports, 8787–8796), and otherwise through a short-lived listener on an ephemeral port. Linking again replaces the device: the service tells the old connection 4409, which stops it without touching `cloud.json` or the web apps, and the running panel connects with the new link. `openmymemo connect mymemo-cloud` does the same.
- **The panel:** MyMemo Cloud has its own "On the web" section on the Connectors page once it is shown (§16.11); the first-run guide (§15.3) leaves it out.

Phase 2 (the offline queue), as built:

- **Idempotency:** a queued item is filed with the AI's own `idempotency_key` from inside the sealed arguments, or with its queue id when the AI sent none, and keeps the queue id as `queue_id`. A resent item is therefore a duplicate, and so is a live retry with the same key once the computer is back. `memory_proposals` returns `queue_id`, so the AI can match its `queued` receipt to the proposal. An item turned down (revoked, read-only, blocked topic, malformed) is acknowledged `rejected`, recorded in the activity log with its `queue_id`, and never retried. A passing failure while filing (a full disk, say) acknowledges nothing and hangs up, so nothing after it is filed first; the service sends the queue again, in order, after reconnecting. When the live retry is filed before the queued copy, the queued copy's `queue_id` is added to that proposal.
- **Time:** the proposal's `created_at` is the item's `received_at`, so the 30-day expiry of a pending proposal (§5.5) counts from when the AI made it, not from when it was filed. It is kept within the last 31 days (the service keeps items 30): a time in the future becomes now, an older one 31 days ago, and one that isn't a time is filed as now.
- **Order and acks:** items are filed one at a time in the order they arrive and acknowledged `stored`, `duplicate` (already filed, or the memory already says it), `undecryptable` (another key, another row, a damaged ciphertext) or `rejected` (a revoked or read-only web app, a blocked topic, an item that doesn't decode or validate, however malformed). An item whose filing fails unexpectedly isn't acknowledged, so the service sends it again.
- **Naming:** the sealed item carries the web app's slug and name but no `verified` flag (the service doesn't seal one), so only the slugs `chatgpt` and `claude` are named "ChatGPT (web)" and "Claude (web)"; any other slug is "… (web, unverified)", whatever name it gives.
- **The panel** shows how many changes are waiting (the service's count at connection, counted down as they are filed). The re-link warning about queued proposals (§16.4) is the consent page's, on the service side.

### 16.11 Showing it, and choosing the service (2026-10-15)

MyMemo Cloud is opt-in twice over: it isn't listed until the person turns it on, and it does nothing until they link.

- **Settings → MyMemo Cloud → Show MyMemo Cloud** (off by default) lists it on the Connectors page and in `openmymemo apps`. While this computer is linked it is always listed, whatever the switch says, so a link can always be seen and undone; the switch is disabled then. Linking from the CLI (`openmymemo cloud link`) works while it is hidden.
- **Service** (shown with the switch on): the MyMemo service a new link goes to, MyMemo's own (`https://api.mymemo.ai`) unless the person enters another. It must be `https://` (`http://` only for a service on this computer's loopback), without user, query or fragment; it is stored without a trailing slash and is the issuer the callback must name. A link keeps the service it was made with, so the service can't change while linked (unlink first). `OPENMYMEMO_CLOUD_URL`, when set, decides the service and the field only shows it.
- **Stored** per computer, like the link and never in the folder: `settings.json` next to `cloud.json` (mode 0600), `{"show": bool, "service": url | null}`; keys a later version adds are kept, and an unreadable file or an unusable service counts as hidden and MyMemo's own. The panel changes it with `PATCH /api/cloud/settings` (`{"show"?, "service"?}`, `""` for MyMemo's own; 400 for an unusable address, 409 while it can't change), the CLI with `openmymemo cloud settings [--show|--hide] [--service URL]`; `GET /api/cloud` reports `shown` and `settings`.
