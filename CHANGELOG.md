# Changelog

What changed in each release, for people who use OpenMyMemo. Every release is also on [GitHub](https://github.com/MyMemoAI/openmymemo/releases).

## Unreleased

- **Import from files.** The Import page has a Files tab that finds the memory files other AI apps keep on your computer and imports them after a preview. It looks in your home folder or a folder you pick, or you can choose a file. It knows `CLAUDE.md`, Claude Code's auto memory, `AGENTS.md`, `GEMINI.md`, Cursor and Windsurf rules, Cline rules and memory bank, Copilot instructions, Kiro steering files and `memory.md`. Each list item or paragraph becomes a memory, in a topic named after the app. From the terminal: `openmymemo import auto <file>`.

## 0.2.5 — 2026-10-15

- **`openmymemo doctor`.** Checks the setup and says what to fix: the memory folder and its state files, the panel's port, each AI app's connection (including one pointed at another folder) and MyMemo Cloud. It only reads, never changes anything, and leaves out memory text, so you can paste its output into an issue.
- The Kiro install link no longer sends your memory folder's path to kiro.dev. `openmymemo connect kiro` still sets Kiro up.

## 0.2.4 — 2026-10-15

- **The Mac app.** Download a disk image from the release, drag it to Applications and you're set: no terminal and no Python to install. It shows the panel in its own window, counts changes to review in the menu bar and notifies you when an AI app changes something.
- **First-run guide.** A new folder opens on a short guide that connects your AI apps and brings over what ChatGPT, Claude or Gemini already remember, instead of an empty page.
- **One-click connect.** The panel's Connectors page and `openmymemo connect <app>` set up Claude Desktop, Claude Code, Codex, Cursor, VS Code, Gemini CLI, Zed, Cline and more. Each one keeps a backup of the app's settings, and disconnecting takes OpenMyMemo out again.
- **Install from PyPI.** `uv tool install openmymemo`, or `uvx openmymemo`, without cloning the repository.
- **Gemini CLI extension.** `gemini extensions install https://github.com/MyMemoAI/openmymemo`.
- **MCP Registry.** OpenMyMemo is listed as `io.github.MyMemoAI/openmymemo`, so MCP clients that browse the registry can find it.
- **MyMemo Cloud (optional, hidden by default).** Lets ChatGPT and claude.ai on the web use your memory through a MyMemo account while your computer is on. Changes proposed while it's offline wait encrypted and arrive when it's back. Turn on "Show MyMemo Cloud" in Settings to see it; you can also choose another compatible service.
- **Privacy policy.** [PRIVACY.md](./PRIVACY.md) describes what is stored where and when OpenMyMemo goes online. Backups of other apps' settings are now readable only by you.
- Every MCP tool has a title, as app directories require.

## 0.2.3 and earlier

See the [releases](https://github.com/MyMemoAI/openmymemo/releases) and [docs/product-design.md](./docs/product-design.md) §12–§13.
