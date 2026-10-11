# OpenMyMemo — agent guide

A local-first, open-source AI memory folder with an approval panel. [docs/product-design.md](./docs/product-design.md) is the product definition; if an implementation would contradict it, update the design doc first (§12 and §13 record where v0.1 and v0.2 differ from earlier plans).

## Non-negotiables

- Local only: no cloud storage, sync, accounts or telemetry. The server listens on `127.0.0.1` only.
- All user data is plain text: `memories/*.md` is the source of truth for memory content; metadata, history, activity, proposals and settings under `.openmymemo/` are plain text too. Don't introduce a database.
- Every AI write goes through a proposal (`proposals.py`) and never edits files directly. A change that applies without the user (the default, or a connector set to `auto`) must stay undoable, must never write into a private topic, and must never silently change a memory the user wrote last (`last_actor`); the table in product-design.md §5.5 is normative.
- Works with zero external dependencies: anything that needs a model endpoint is optional and degrades instead of failing.
- No dependency on MyMemo services, accounts or databases. Code and UI patterns may be borrowed from MyMemo's own apps; runtime coupling may not.
- Don't weaken the panel API guard (`guard` in `web.py`: Host, Origin, `X-OpenMyMemo` header) or the `/mcp` token check.

## Commands

```bash
uv sync
uv run pytest                               # all tests, including MCP end to end over stdio and real HTTP
uv run ruff check src tests scripts
uv run openmymemo --vault /tmp/v init       # CLI
uv run python scripts/demo_vault.py /tmp/d  # demo data
cd web && pnpm install && pnpm build        # panel → src/openmymemo/static/ (commit the build output)
cd web && pnpm test                         # panel tests (vitest)
cd desktop && pnpm install && pnpm tauri dev # Mac app (needs Rust); `pnpm tauri build` for the .app
python3 scripts/stage_desktop.py && (cd desktop/src-tauri && cargo test)  # Mac app tests
```

## Code map

| File | Responsibility |
| --- | --- |
| `src/openmymemo/mdfile.py` | Parse and rewrite topic files (frontmatter, sections, memory lines; format-1 header migration); untouched lines must survive byte for byte |
| `src/openmymemo/vault.py` | The vault: sync (editor edits → meta/history), add/update/archive/restore/rollback, settings, locking |
| `src/openmymemo/topics.py` | The 14 default topics, groups, classification keywords |
| `docs/MEMORY_FORMAT.md` | The folder format; change it before changing what the code reads or writes |
| `src/openmymemo/textutil.py` | Similarity, search scoring, rule-based classification |
| `src/openmymemo/connectors.py` | Connector permissions, tokens, revocation tombstones |
| `src/openmymemo/proposals.py` | Proposal lifecycle, dedupe, auto-apply and undo |
| `src/openmymemo/context.py` | Compiles what an AI sees, MEMORY.md, log.md, search |
| `src/openmymemo/transfer.py` | Import (ChatGPT / Claude / Markdown / JSON) and export |
| `src/openmymemo/mcp_server.py` | MCP tools (mcp SDK 2.x `MCPServer`) |
| `src/openmymemo/web.py` | Panel API and `/mcp` combined into one ASGI app |
| `src/openmymemo/cli.py` | Typer CLI |
| `src/openmymemo/codex.py` | Codex session hook: merges it into `~/.codex/hooks.json` and takes it out again (Codex doesn't run plugin hooks) |
| `web/` | Vue 3 + Tailwind v4 panel source; all copy lives in `web/src/i18n.ts` |
| `web/src/desktop.ts` | The Mac layout switch (`?desktop=1`), the shell's commands and shortcuts; `MemoriesDesktopView`, `MemoryInspector`, `DesktopSidebar`, `DesktopToolbar` are its screens. `PageHeader` becomes the window toolbar there |
| `desktop/` | Mac app (Tauri 2, product-design.md §14): `src-tauri/src/core.rs` finds or starts the server for the folder, `main.rs` has the window, menus, menu-bar item and notifications; `scripts/stage_desktop.py` stages the core and `uv` before each build |
| `.claude-plugin/` | Claude Code plugin manifest and marketplace (the repo root is the plugin) |
| `plugin.json`, `mcp.json`, `.agents/plugins/` | Codex plugin (Agent Plugins format) and its marketplace |
| `skills/` | Skills shared by the Claude Code and Codex plugins |
| `integrations/hooks/` | Session-start hook script shared by Claude Code (plugin hook in `integrations/claude-code/hooks/hooks.json`) and Codex (user-level hook installed by `setup codex`; `integrations/codex/setup.sh` runs it from the installed plugin) |
| `integrations/claude-desktop/` | `.mcpb` manifest; build with `scripts/build_mcpb.py` |
| `integrations/icons/`, `web/public/logo.png` | The MyMemo logo at each size, rendered from MyMemo's app icon by `scripts/make_icons.py` |

## Conventions

- Everything user-facing is English: docs, panel copy, CLI output, MCP tool names and descriptions. Chinese belongs only in data handling (classifier keywords, parsing pasted pages, test fixtures), because users' memories may be in Chinese.
- File formats (`memories/*.md`, `connectors.yaml`, `.openmymemo/*`) are a public contract specified in [docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md): change the spec first and keep reading older files.
- Topic slugs are the file names; titles come from the files.
- All writes go through `Vault` methods, which lock, re-read from disk, write atomically and update meta and history.
- The mcp SDK is 2.x: `from mcp.server.mcpserver import MCPServer, Context`; static resources can't take a `Context`.
- Plugins run `uv run` from the installed copy of this repo, so anything the package needs at runtime must be inside the repo (and inside the `.mcpb` staging list in `scripts/build_mcpb.py`). Keep the version in `pyproject.toml`, `src/openmymemo/__init__.py`, both `plugin.json` files, the `.mcpb` manifest, `web/package.json` and the Mac app (`desktop/package.json`, `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml`) in sync (`tests/test_packaging.py` checks), and run `uv lock` after a bump.
- CI (`.github/workflows/ci.yml`) runs the tests on Linux and macOS, checks that the committed panel build matches `web/`, runs the panel's and the Mac app's tests, and packs the `.mcpb`. To release: bump the version, merge, then push a tag `v<version>`; `.github/workflows/release.yml` publishes the `.mcpb`, wheel and sdist as a GitHub release.
- Don't add a root `.mcp.json`: Claude Code would treat it as project config for everyone working on this repo. The Claude Code MCP server is declared inline in `.claude-plugin/plugin.json`.
- Commit messages are imperative English; one topic per PR.
- The Mac app adds nothing to the panel's API: it is the same panel in another layout. New panel features go into the web screens; give the Mac layout its own version only where the design has one. A native command the panel calls must be listed in `desktop/src-tauri/build.rs` and granted in `capabilities/default.json`, because the panel is a remote origin to Tauri.
