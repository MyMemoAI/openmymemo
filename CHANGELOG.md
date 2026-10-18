# Changelog

What changed in each release, for people who use OpenMyMemo. Every release is also on [GitHub](https://github.com/MyMemoAI/openmymemo/releases).

## Unreleased

- **OpenMyMemo Assistant (optional, off by default).** Settings → Assistant (in a browser, at the bottom of the Connectors page) or `openmymemo assistant set` lets OpenMyMemo use a model: one on this computer (Ollama, LM Studio, llama.cpp, vLLM or any other OpenAI-compatible server), or your own API key for Anthropic or any OpenAI-compatible service. "Test" sends one tiny request and shows the model and how long it took. MyMemo's hosted model is listed as coming soon. Everything keeps working without it.
  - **Imports:** pasted lists, Markdown and other apps' memory files are split into memories by the model, which also drops instructions meant for tools and suggests a topic. The preview says "Split by OpenMyMemo Assistant" and shows, unticked, any line it left out that the rules would have kept.
  - **Sorting:** topic suggestions for imported memories come from the model.
  - Each feature has its own switch, and there are monthly limits on calls and tokens; at a limit, OpenMyMemo goes back to its rules.
  - Private topics are never sent to a remote model; a model on this computer reads only the private topics you pick. The key is kept on this computer only, in the Keychain on a Mac, never in your memory folder.
  - **Checking what AI apps propose:** when an app calls `memory_propose` (directly, over HTTP or through MyMemo Cloud), the Assistant takes a second look within 2 seconds (`--check-budget` to change): it turns a reworded duplicate or a contradiction into an update of that memory, picks a topic when the app gave none, and sends health, money or credentials to a private topic or makes them wait ("Looks sensitive"). It can only make a change more careful, never apply one that would wait. Its note shows with the change on the Changes page. Slow or failing: the change is filed as before.
  - **Chats:** the Import page has a Chats tab, and `openmymemo import` new sources, for ChatGPT's and Claude's data exports and for Claude Code and Codex sessions (`chatgpt-export`, `claude-export`, `claude-code-sessions`, `codex-sessions`). You see the number of calls and tokens before it starts; it reads newest first, and each memory it suggests carries your words and the date as evidence.
  - **After a session:** with "Reading sessions" on and your OK for the app (asked once per app, in Settings → Assistant, on the Changes page or with `openmymemo assistant sessions`), the Assistant reads a Claude Code or Codex session's transcript when it ends and proposes what you said that's worth remembering and the app didn't save. The Claude Code plugin gains a SessionEnd hook and `openmymemo setup codex` a Stop hook (trust it once in Codex, like the first); both return at once and do nothing until you turn this on.
  - The Assistant is listed on the Connectors page as "OpenMyMemo Assistant", with its own read scope, writing mode and blocked topics, and every request it makes is in the activity log (feature, model, where, tokens), never the text.

## 0.2.6 — 2026-10-16

- **Import from files.** The Import page has a Files tab that finds the memory files other AI apps keep on your computer and imports them after a preview. It looks in your home folder or a folder you pick, or you can choose a file. It knows `CLAUDE.md`, Claude Code's auto memory, `AGENTS.md`, `GEMINI.md`, Cursor and Windsurf rules, Cline rules and memory bank, Copilot instructions, Kiro steering files and `memory.md`. Each list item or paragraph becomes a memory, in a topic named after the app. From the terminal: `openmymemo import auto <file>`.
- The Mac app's window can be moved again, and its close, minimise and zoom buttons no longer sit on top of the panel. When an AI app's older OpenMyMemo (such as a Claude Desktop extension that hasn't been updated) was already serving your memory folder, the app showed that version's panel, which predates the Mac layout. It now starts its own server beside it instead.
- Editing a memory in the panel's list no longer writes old text over a change another app made while the edit box was open. Leaving the box without typing writes nothing; if you did type, your text is saved and the panel tells you the other version is in the history. Pressing Return no longer saves the same edit twice, and a second edit made while the first is still saving waits for it.
- AI apps you've uninstalled no longer show as installed. Their settings folders stay behind after an uninstall, and some are shared: Gemini Code Assist and Antigravity also write `~/.gemini`, so Gemini CLI showed up without being installed. An app now counts as installed when its app, its command or its editor extension is there. On Windows and Linux, an app that has no command to look for, such as Claude Desktop, still goes by its settings folder.

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
