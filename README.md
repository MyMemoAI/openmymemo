# OpenMyMemo

Your AI memory as a plain folder on your own computer. See exactly what models remember about you; let Claude, Codex, Cursor or any MCP app read it and write to it, and see every change they make, with one click to undo. If an account gets banned, a vendor shuts down or you switch models, your memory comes with you.

> v0.2. Product design and status: [docs/product-design.md](./docs/product-design.md). The folder format, for any tool that wants to read or write it: [docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md).

## Why I built this

My OpenAI account was banned on October 2, 2026, with no warning and no explanation. I had used ChatGPT since it launched in late 2022, and its year-end recap put me in the top 1% of users. I appealed and never got a reply. Nearly four years of conversations, and everything ChatGPT had learned about me, went with the account.

I had thought of that memory as mine. It wasn't. It lived on OpenAI's servers, in an account they could close at any time, and when they did, the memory went with it, with no one to ask for it back.

OpenMyMemo is what I wish I'd had: my AI memory as a folder on my own computer. Any model can read it, I can see and undo every change a model makes, and no single company can take it away.

— Leo

## Works with

| App | Status |
| --- | --- |
| Claude Desktop (Chat, Cowork and Code) | ✅ Extension |
| Claude Code | ✅ Plugin |
| Codex (the CLI, and Codex in the ChatGPT desktop app) | ✅ Plugin |
| Cursor, VS Code | ✅ Install link from the panel |
| ChatGPT chat (web, desktop and mobile), claude.ai on the web | ❌ Not yet. They can only connect to MCP servers on the public internet, and OpenMyMemo stays on your computer. You can still bring your ChatGPT memories over with `openmymemo import chatgpt`. |

## Principles

1. Local files are the only source of truth — Markdown with YAML frontmatter you can open in any editor and keep in iCloud, git or an Obsidian vault.
2. AI writes; you see everything and can undo. Additions land right away and show up in a digest. Changes to what you wrote yourself, and anything in a private topic, wait for you. One switch makes every change wait.
3. What you see is what it gets — the panel shows the exact text an AI receives, and who read what.
4. Zero dependencies — no database, Docker, account, API key or telemetry.
5. Open protocols and open conventions — standard MCP, Markdown + frontmatter (an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundle), Obsidian block ids, open import and export formats, no vendor lock-in.

## Install

Requires [uv](https://docs.astral.sh/uv/). Not on PyPI yet, so install from the repo:

```bash
git clone https://github.com/MyMemoAI/openmymemo.git
uv tool install ./openmymemo
```

## Use it

```bash
openmymemo init                              # create ~/OpenMyMemo with 14 default topics
openmymemo import chatgpt                    # paste ChatGPT's "Manage memories" page; it lands in "From ChatGPT"
openmymemo sort                              # file imported memories into topics, one suggestion at a time
openmymemo connector add "Claude Desktop"    # prints config for Claude Desktop / Claude Code / Cursor / Codex
openmymemo serve                             # open the panel at http://127.0.0.1:8787
openmymemo review                            # what AI changed since you last looked: keep, edit or undo
```

More: `status`, `approval on|off` (on: every AI change waits for you), `context --connector <id>` (what an AI actually receives), `export --format claude|chatgpt|markdown|json`, `search`, `add`, `connector list|config|rotate|remove`, `setup codex` (load your memory at the start of every Codex session). Use `--vault` or `OPENMYMEMO_HOME` to put the folder elsewhere.

### Plugins

Each plugin uses `~/OpenMyMemo` by default (created on first use), loads your memory into new sessions where the app supports it — starting with a one-line summary of what AI changed since you last looked — and serves the panel at `http://127.0.0.1:8787` while the app is running. They need [uv](https://docs.astral.sh/uv/) except where noted.

| App | Install |
| --- | --- |
| **Claude Desktop** | Build `dist/openmymemo-0.2.2.mcpb` with `uv run python scripts/build_mcpb.py`, then double-click it (or Settings → Extensions). Claude Desktop installs Python and the dependencies itself; no uv needed. |
| **Claude Code** | `/plugin marketplace add MyMemoAI/openmymemo`, then `/plugin install openmymemo@openmymemo`. Adds `/openmymemo:memory` and `/openmymemo:remember`. |
| **Codex** | `codex plugin marketplace add MyMemoAI/openmymemo`, then `codex plugin add openmymemo@openmymemo`. Then, once, so memory loads at the start of every session: ask Codex to "set up OpenMyMemo" (or run `openmymemo setup codex`), open Codex in a terminal (`codex`), type `/hooks` and trust "Loading your OpenMyMemo memory". The ChatGPT desktop app has no `/hooks`, but it reads the same settings, so trusting it in the terminal covers it too. Adds the `memory`, `remember` and `codex-setup` skills. |
| **Cursor** | Panel → Connectors → Setup → **Add to Cursor**. |
| **VS Code** | Panel → Connectors → Setup → VS Code, then run the `code --add-mcp …` command. |

The first launch downloads Python and the dependencies (about 5 seconds on a fast connection; Codex gives MCP servers 10). If an app says the OpenMyMemo server failed to start, restart the app once.

While the repo is private, the Claude Code and Codex marketplace commands need GitHub credentials on your machine (for example `gh auth login`).

### Connecting AI apps by hand

- **Local process (recommended):** Claude Desktop, Claude Code, Cursor, Codex and other stdio MCP clients. `openmymemo connector add <name>` prints a config with absolute paths to paste in. Give each app its own `--connector` so permissions and logs stay separate.
- **Local HTTP:** `openmymemo connector add <name> --http` creates a token (shown once) for use with `openmymemo serve` at `http://127.0.0.1:8787/mcp`.
- ChatGPT chat (web, desktop and mobile) and claude.ai on the web can only reach MCP servers on the public internet, so they aren't supported yet. On the OpenAI side, Codex works today, including the Codex built into the ChatGPT desktop app.

Tools AI apps see: `memory_context`, `memory_search`, `memory_get`, `memory_propose`, `memory_proposals`, plus the `recall` prompt.

## What happens when an AI writes

- **An addition applies right away.** It is marked as new until you look at it: on the panel's Changes page, in `openmymemo review`, or in the one-line summary at the start of your next Claude Code or Codex session.
- **A change to a memory you wrote, imported or approved yourself waits for you.** So does anything in a private topic (`health` and `finance` by default). AI never silently changes what you wrote.
- **Undo is one click** for as long as the change is the memory's latest version; after that, the memory's version history.
- **"Require my approval for AI changes"** makes every change wait. Each connected app can also be set to read-only, or trusted to write directly.

## The memory folder

```
~/OpenMyMemo/
├── memories/            # one file per topic: frontmatter, then one memory per line
│   ├── work.md          # - CTO at MyMemo <!-- until: 2026-12 --> ^m7f3a
│   └── from-chatgpt.md  # what you imported, in its original order, until you sort it
├── MEMORY.md            # generated: what an AI sees with the default scope
├── log.md               # generated: what changed, by whom, newest day first
├── connectors.yaml      # connected apps and their permissions (tokens stored as hashes)
└── .openmymemo/         # settings, metadata, history, activity, proposals — all plain text
```

Each topic file starts with YAML frontmatter (`type`, `title`, `description`, `group`, `private`). Edit `memories/*.md` in any editor: a new line is a new memory, a deleted line is archived, a line moved to another file changes topic. OpenMyMemo adds the `^id` and records the change in history on its next read. Other tools can read and write the folder by following [docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md).

Upgrading from v0.1: open the folder once with v0.2. The files get their frontmatter, nothing else in them changes, and your approval setting is kept as it was.

## Security

The server listens on 127.0.0.1 only. The panel API checks `Host`, `Origin` and a header that cross-site pages can't add; `/mcp` requires a connector token. The `health` and `finance` topics are hidden from every AI by default, and AI changes to private topics always wait for you, whatever the approval switch or an app's write mode says.

## Development

```bash
uv sync                                # Python dependencies
uv run pytest                          # tests
uv run ruff check src tests scripts
uv run python scripts/demo_vault.py /tmp/omm-demo && uv run openmymemo --vault /tmp/omm-demo serve
cd web && pnpm install && pnpm build   # builds the panel into src/openmymemo/static/ (committed)
```

Panel development: run `openmymemo serve --no-open`, then `pnpm dev` in `web/` (port 5173, proxies `/api`).

## Relationship to MyMemo

An independent project with no dependency on MyMemo accounts or services. MyMemo may later read this memory as an ordinary connector, on equal footing with Claude.

## License

Apache-2.0
