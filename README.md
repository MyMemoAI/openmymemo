# OpenMyMemo

Your AI memory as a plain folder on your own computer. See exactly what models remember about you; let Claude, Codex, Cursor or any MCP app share it, so what you tell one the others know too, and see every change they make, with one click to undo. If an account gets banned, a vendor shuts down or you switch models, your memory comes with you.

> v0.2. Product design and status: [docs/product-design.md](./docs/product-design.md). The folder format, for any tool that wants to read or write it: [docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/panel-memories-dark.png">
  <img alt="The OpenMyMemo panel: memories grouped by topic, each showing where it came from, and a note that AI apps made changes to review" src="docs/images/panel-memories-light.png">
</picture>

## Why I built this

My OpenAI account was banned on October 2, 2026, with no warning and no specific explanation. I had used ChatGPT since it launched in late 2022, and its year-end recap put me in the top 1% of users. I appealed and never got a reply. Nearly four years of conversations, and everything ChatGPT had learned about me, went with the account.

I had thought of that memory as mine. It wasn't. It lived on OpenAI's servers, in an account they could close at any time, and when they did, the memory went with it, with no one to ask for it back.

OpenMyMemo is what I wish I'd had: my AI memory as a folder on my own computer. Any model can read it, I can see and undo every change a model makes, and no single company can take it away.

— Leo

## Get started

There are two ways in. Both use the same memory folder, the same panel and the same core, so you can switch or use both.

### The Mac app

For everyone: no terminal, no config files, no Python to install.

1. Download `OpenMyMemo-<version>-arm64.dmg` from the [latest release](https://github.com/MyMemoAI/openmymemo/releases/latest) (a Mac with Apple silicon, macOS 12 or later).
2. Open it and drag OpenMyMemo to Applications. (Opened straight from the disk image, it offers to move itself there.)
3. Open OpenMyMemo from Applications. Release builds are signed and notarized and open like any app. A build without a Developer ID signature (one you built, or a fork's) needs one extra step the first time: on macOS 15 and later, try to open it, then go to System Settings → Privacy & Security and click **Open Anyway**; on earlier versions, right-click the app and choose **Open**.
4. The first start downloads Python and the libraries OpenMyMemo runs on, about 25 MB and usually under a minute, into `~/Library/Application Support/ai.mymemo.openmymemo/`. It happens once; after that the app starts in a couple of seconds, also offline.
5. A short guide connects the AI apps on your Mac with one click each and brings over the memory ChatGPT, Claude or Gemini already have. Your memories live in `~/OpenMyMemo`.

The app shows the panel in its own window, puts the number of changes to review in the menu bar, and notifies you when an AI app changes something. To update, download the new disk image and drag it to Applications again; your memory folder isn't touched.

The app also installs an `openmymemo` command, which the AI apps it connects run (Settings → Advanced shows where). It is the full command line below, so you can use it in Terminal too.

### The command line

For developers, and anyone who prefers a terminal. Nothing is fenced in: everything the app does is a command, and the folder format ([docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md)) is the interface. Requires [uv](https://docs.astral.sh/uv/). Not on PyPI yet, so install from the repo:

```bash
git clone https://github.com/MyMemoAI/openmymemo.git
uv tool install ./openmymemo
```

```bash
openmymemo init                              # create ~/OpenMyMemo with 14 default topics
openmymemo apps                              # the AI apps on this computer, and which are connected
openmymemo connect cursor                    # connect one: claude-desktop, claude-code, codex, cursor, vscode, …
openmymemo disconnect cursor                 # remove its OpenMyMemo entry and revoke its access
openmymemo import chatgpt                    # paste ChatGPT's "Manage memories" page; it lands in "From ChatGPT"
openmymemo sort                              # file imported memories into topics, one suggestion at a time
openmymemo serve                             # open the panel at http://127.0.0.1:8787
openmymemo review                            # what AI changed since you last looked: keep, edit or undo
```

`connect` edits only the app's own MCP config (keeping a backup) or runs the app's own `mcp add` command, and tells you to restart the app; where it can't do that safely it prints the config to paste. More: `status`, `approval on|off` (on: every AI change waits for you), `context --connector <id>` (what an AI actually receives), `export --format claude|chatgpt|markdown|json`, `search`, `add`, `connector add|list|config|rotate|remove` (configs by hand), `setup codex` (load your memory at the start of every Codex session). Use `--vault` or `OPENMYMEMO_HOME` to put the folder elsewhere.

#### Plugins

**Install as many as you like: they all share one memory.** Every plugin uses the same `~/OpenMyMemo` folder by default (created on first use), so something you tell Claude, Codex knows in its next session, and the other way round. Each plugin loads your memory into new sessions where the app supports it — starting with a one-line summary of what AI changed since you last looked — and serves the panel at `http://127.0.0.1:8787` while the app is running. They need [uv](https://docs.astral.sh/uv/) except where noted.

| App | Install |
| --- | --- |
| **Claude Desktop** | Download `openmymemo-<version>.mcpb` from the [latest release](https://github.com/MyMemoAI/openmymemo/releases/latest) (or build it with `uv run python scripts/build_mcpb.py`), then double-click it (or Settings → Extensions). Nothing to fill in: leave the memory folder empty to use `~/OpenMyMemo`. Then quit Claude Desktop and open it again. Claude Desktop installs Python and the dependencies itself; no uv needed. |
| **Claude Code** | `/plugin marketplace add MyMemoAI/openmymemo`, then `/plugin install openmymemo@openmymemo`, then restart Claude Code. Adds `/openmymemo:memory` and `/openmymemo:remember`. |
| **Codex** | Three steps, see [Codex](#codex) below. |
| **Cursor** | Panel → Connectors → **Connect**, or `openmymemo connect cursor`. |
| **VS Code** | Panel → Connectors → **Connect**, or `openmymemo connect vscode`. |

**After installing, quit the app completely and open it again** so it loads OpenMyMemo. The first launch downloads Python and the dependencies (about 5 seconds on a fast connection; Codex gives MCP servers 10). If an app then says the OpenMyMemo server failed to start, restart it once more.

**Then open the panel at [http://127.0.0.1:8787](http://127.0.0.1:8787)** in your browser (the page pictured at the top); bookmark it. It's there whenever Claude Desktop, Claude Code or Codex is running: your memories, what AI changed (Changes), the exact text each app gets (What AI sees) and the connected apps (Connectors). Without a plugin, `openmymemo serve` starts it and opens the browser.

##### Codex

You need [uv](https://docs.astral.sh/uv/). Steps 2 and 3 are needed only once: Codex doesn't run hooks that plugins ship, so OpenMyMemo adds its own session-start hook.

1. **Install the plugin.** In a terminal:

   ```bash
   codex plugin marketplace add MyMemoAI/openmymemo
   codex plugin add openmymemo@openmymemo
   ```

   Then restart Codex: quit the ChatGPT desktop app and open it again, or start `codex` again in the terminal.

2. **Set up the hook.** Start a new Codex session and ask it to "set up OpenMyMemo". It adds a hook to `~/.codex/hooks.json` and leaves your other hooks alone. If you installed the CLI, `openmymemo setup codex` does the same.
3. **Trust the hook.** In the ChatGPT desktop app, open Settings → Coding → Hooks and trust "Loading your OpenMyMemo memory". Or open Codex in a terminal (`codex`), type `/hooks`, select SessionStart and trust it there. The app and the terminal share these settings, so trusting it in either place covers both.

From your next new session on, Codex starts with your memory. The plugin also adds the `memory`, `remember` and `codex-setup` skills.

#### Connecting AI apps by hand

- **Local process (recommended):** Claude Desktop, Claude Code, Cursor, Codex and other stdio MCP clients. `openmymemo connect <app>` writes the app's config for you (`openmymemo apps` lists them); for anything else, `openmymemo connector add <name>` prints a config with absolute paths to paste in. Give each app its own `--connector` so permissions and logs stay separate.
- **Local HTTP:** `openmymemo connector add <name> --http` creates a token (shown once) for use with `openmymemo serve` at `http://127.0.0.1:8787/mcp`.
- ChatGPT chat (web, desktop and mobile) and claude.ai on the web can only reach MCP servers on the public internet, so they aren't supported yet. On the OpenAI side, Codex works today, including the Codex built into the ChatGPT desktop app.

Tools AI apps see: `memory_context`, `memory_search`, `memory_get`, `memory_propose`, `memory_proposals`, plus the `recall` prompt.

## Works with

| App | Status |
| --- | --- |
| Claude Desktop (Chat, Cowork and Code) | ✅ Extension |
| Claude Code | ✅ Plugin |
| Codex (the CLI, and Codex in the ChatGPT desktop app) | ✅ Plugin |
| Cursor, VS Code and other MCP apps | ✅ One click in the app, `openmymemo connect`, or a config to paste |
| ChatGPT chat (web, desktop and mobile), claude.ai on the web | ❌ Not yet. They can only connect to MCP servers on the public internet, and OpenMyMemo stays on your computer. You can still bring your ChatGPT memories over with `openmymemo import chatgpt`. |

## Principles

1. Local files are the only source of truth — Markdown with YAML frontmatter you can open in any editor and keep in iCloud, git or an Obsidian vault.
2. AI writes; you see everything and can undo. Additions land right away and show up in a digest. Changes to what you wrote yourself, and anything in a private topic, wait for you. One switch makes every change wait.
3. What you see is what it gets — the panel shows the exact text an AI receives, and who read what.
4. Zero dependencies — no database, Docker, account, API key or telemetry.
5. Open protocols and open conventions — standard MCP, Markdown + frontmatter (an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundle), Obsidian block ids, open import and export formats, no vendor lock-in.

## What happens when an AI writes

![The Changes page: an AI's change to a memory you wrote and an addition to a private topic wait for you, with Accept, Edit and accept, and Reject](docs/images/panel-changes.png)

- **An addition applies right away.** It is marked as new until you look at it: on the panel's Changes page, in `openmymemo review`, or in the one-line summary at the start of your next Claude Code or Codex session.
- **A change to a memory you wrote, imported or approved yourself waits for you.** So does anything in a private topic (`health` and `finance` by default). AI never silently changes what you wrote.
- **Undo is one click** for as long as the change is the memory's latest version; after that, the memory's version history.
- **"Require my approval for AI changes"** makes every change wait. Each connected app can also be set to read-only, or trusted to write directly.

## The memory folder

```
~/OpenMyMemo/
├── memories/            # one file per topic: frontmatter, then one memory per line
│   ├── work.md          # - Engineering lead at Acme <!-- until: 2026-12 --> ^m7f3a
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

Found a vulnerability? Please report it privately as described in [SECURITY.md](./SECURITY.md).

## Privacy Policy

Your memory stays on your computer: MyMemo doesn't receive it, and OpenMyMemo has no account, telemetry, analytics or update check. AI apps you connect read the topics you allow (never `health` or `finance` unless you say so) and send what they read to their own model provider under their own policies. OpenMyMemo goes online only to download Python and its libraries when it is installed or first started, and for MyMemo Cloud, which is off until you turn it on and link this computer; then the memories web apps read pass through the MyMemo service in transit. The full policy, including what is stored where: [PRIVACY.md](https://github.com/MyMemoAI/openmymemo/blob/main/PRIVACY.md).

## Development

```bash
uv sync                                # Python dependencies
uv run pytest                          # tests
uv run ruff check src tests scripts
uv run python scripts/demo_vault.py /tmp/omm-demo && uv run openmymemo --vault /tmp/omm-demo serve
cd web && pnpm install && pnpm build   # builds the panel into src/openmymemo/static/ (committed)
cd web && pnpm test                    # panel tests
```

Panel development: run `openmymemo serve --no-open`, then `pnpm dev` in `web/` (port 5173, proxies `/api` to 8787, or to `OPENMYMEMO_PORT`). Add `?desktop=1` to the URL to see the Mac app's layout.

### Mac app

`desktop/` is a [Tauri](https://tauri.app) shell around the same panel. It bundles `uv` and this package; on first launch uv downloads Python and the dependencies into `~/Library/Application Support/ai.mymemo.openmymemo/`, then the app runs `openmymemo serve`. If a server for the same folder already runs (Claude Desktop's extension, or `openmymemo serve`), the app uses it. Needs Rust (`rustup`) and Node.

```bash
cd web && pnpm build                   # the panel the app shows
cd desktop && pnpm install
pnpm tauri dev                         # run it; OPENMYMEMO_HOME=/tmp/omm-demo to use another folder
CI=true pnpm tauri build --bundles app,dmg  # → desktop/src-tauri/target/release/bundle/
```

Local builds are signed ad hoc, so macOS asks once before opening them (see [The Mac app](#the-mac-app)); the release workflow signs and notarizes when the Apple Developer secrets are set (`.github/workflows/release.yml` lists them). `CI=true` makes Tauri skip the Finder scripting that lays out the disk image window, which otherwise asks for permission to control Finder. On every start the app writes the `openmymemo` launcher AI apps run to `~/Library/Application Support/ai.mymemo.openmymemo/bin/`; it keeps working when the app is moved. The server's log is in `~/Library/Logs/ai.mymemo.openmymemo/core.log`.

## Relationship to MyMemo

An independent project with no dependency on MyMemo accounts or services. MyMemo may later read this memory as an ordinary connector, on equal footing with Claude.

## License

Apache-2.0
