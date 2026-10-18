# OpenMyMemo — agent guide

A local-first, open-source AI memory folder with an approval panel. [docs/product-design.md](./docs/product-design.md) is the product definition; if an implementation would contradict it, update the design doc first (§12 and §13 record where v0.1 and v0.2 differ from earlier plans).

## Non-negotiables

- Local only: no cloud storage, sync, accounts or telemetry, except the optional MyMemo Cloud connector (product-design.md §16), which does nothing until the person signs in and never holds memory in readable form at rest. The server listens on `127.0.0.1` only. Memory text leaves the computer only when the person turns the OpenMyMemo Assistant on with a remote model, and only to that model (product-design.md §17); a model on this computer keeps everything on the computer, and private topics never go to a remote model.
- All user data is plain text: `memories/*.md` is the source of truth for memory content; metadata, history, activity, proposals and settings under `.openmymemo/` are plain text too. Don't introduce a database.
- Every AI write goes through a proposal (`proposals.py`) and never edits files directly. A change that applies without the user (the default, or a connector set to `auto`) must stay undoable, must never write into a private topic, and must never silently change a memory the user wrote last (`last_actor`); the table in product-design.md §5.5 is normative.
- Works with zero external dependencies: anything that needs a model endpoint is optional and degrades instead of failing.
- No dependency on MyMemo services, accounts or databases. The MyMemo Cloud connector is the one optional piece that talks to MyMemo; everything else works without it, and removing it changes nothing else. Code and UI patterns may be borrowed from MyMemo's own apps; other runtime coupling may not.
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
| `docs/distribution.md`, `llms-install.md` | Where OpenMyMemo is published and listed, the shared listing details, and the setup guide AI agents such as Cline follow |
| `src/openmymemo/textutil.py` | Similarity, search scoring, rule-based classification |
| `src/openmymemo/connectors.py` | Connector permissions, tokens, revocation tombstones |
| `src/openmymemo/clients.py` | The catalog of AI apps (product-design.md §15.1): detection, status, one-click connect and disconnect by editing an app's MCP config (JSON or Codex TOML) or running its `mcp add`, and the manual snippets |
| `src/openmymemo/appfiles.py` | Writing other apps' files (their MCP configs, Codex's `hooks.json`): atomic, permissions kept from the first byte, through symlinks, with a one-time `<file>.openmymemo-backup` |
| `src/openmymemo/proposals.py` | Proposal lifecycle, dedupe, auto-apply and undo |
| `src/openmymemo/context.py` | Compiles what an AI sees, MEMORY.md, log.md, search |
| `src/openmymemo/transfer.py` | Import (ChatGPT / Claude / Markdown / JSON, and other apps' memory files) and export |
| `src/openmymemo/memfiles.py` | Finds other AI apps' memory and instructions files (CLAUDE.md, AGENTS.md, GEMINI.md, rules, memory.md) for import: known names and places only, never a whole tree, nothing else read |
| `src/openmymemo/mcp_server.py` | MCP tools (mcp SDK 2.x `MCPServer`); `relayed=` builds the MyMemo Cloud variant |
| `src/openmymemo/web.py` | Panel API, `/mcp` and `/cloud/callback` combined into one ASGI app; `panel_server()` is the uvicorn server that starts MyMemo Cloud once its port is bound |
| `src/openmymemo/cloud.py` | MyMemo Cloud (product-design.md §16): the per-computer link (`cloud.json`, never in the folder), linking with PKCE and a loopback callback, and the bridge: one outbound WebSocket that answers relayed tool calls as `cloud-<slug>` connectors and files the offline queue (HPKE-sealed proposals) through `proposals.py`. The wire format is pinned by `tests/fixtures/cloud/` (the MyMemo service tests against the same files); `tests/cloud_fake.py` is a fake service built from it |
| `src/openmymemo/assistant/` | OpenMyMemo Assistant (product-design.md §17), off by default: `settings.py` (per-computer `assistant.json` next to `cloud.json`, Keychain on macOS, `OPENMYMEMO_ASSISTANT_*` overrides, monthly usage), `client.py` (OpenAI-compatible chat or Anthropic Messages over httpx2; `TRANSPORT` is the test hook), `schema.py` (checks the model's JSON), `core.py` (`Assistant`: `available(feature)`, `json_call(...)` that returns None instead of raising, `propose(...)` as connector `assistant`; privacy and spending rules are enforced here, not in callers), `features.py` (model-powered import splitting with the rule parser as cross-check, and sort suggestions). Every feature keeps its rule-based path as the fallback |
| `src/openmymemo/cli.py` | Typer CLI |
| `src/openmymemo/doctor.py` | `openmymemo doctor`: setup checks (folder, uv, panel port, each app's config and which folder it uses, MyMemo Cloud) that print paths and counts, never memories, and never write: it parses the topic files itself instead of calling `Vault.sync()` |
| `src/openmymemo/codex.py` | Codex session hook: merges it into `~/.codex/hooks.json` and takes it out again (Codex doesn't run plugin hooks) |
| `web/` | Vue 3 + Tailwind v4 panel source; all copy lives in `web/src/i18n.ts` |
| `web/src/components/ConnectApps.vue` | The app list with Connect / Disconnect / Set up, used by the Connectors page and the first-run guide; `ConnectNote` shows what's left to do, `ConnectorPanel` a connected app's permissions, `CloudRow` the MyMemo Cloud row (sign-in, status, setup for ChatGPT and claude.ai, its web apps) |
| `web/src/desktop.ts` | The Mac layout switch (`?desktop=1`), the shell's commands and shortcuts; `MemoriesDesktopView`, `MemoryInspector`, `DesktopSidebar`, `DesktopToolbar` are its screens; `memorySaves.ts` queues the inspector's saves per memory. `PageHeader` becomes the window toolbar there |
| `desktop/` | Mac app (Tauri 2, product-design.md §14, §15.2): `src-tauri/src/core.rs` installs the core (non-editable) and finds or starts the server for the folder, `launcher.rs` writes the `openmymemo` launcher AI apps run, `install.rs` moves the app from the disk image to Applications, `main.rs` has the window, menus, menu-bar item, notifications, settings and link handling; `scripts/stage_desktop.py` stages the core and `uv` before each build |
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
- Plugins run `uv run` from the installed copy of this repo, so anything the package needs at runtime must be inside the repo (and inside the `.mcpb` staging list in `scripts/build_mcpb.py`). Keep the version in `pyproject.toml`, `src/openmymemo/__init__.py`, both `plugin.json` files, `server.json` (the MCP Registry entry, both of its versions), the `.mcpb` manifest, `web/package.json` and the Mac app (`desktop/package.json`, `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml`) in sync (`tests/test_packaging.py` checks), and run `uv lock` after a bump.
- CI (`.github/workflows/ci.yml`) is three jobs, one per runner: the Python tests and lint on Linux (Python 3.11 and 3.13), the Python tests and the Mac app's tests on macOS (Python 3.12), and the panel (its tests, and that the committed build matches `web/`) with packing the `.mcpb`. The `main` ruleset requires these three checks by name, so renaming a job means updating it. To release: bump the version, merge, then push a tag `v<version>`; `.github/workflows/release.yml` publishes the `.mcpb`, wheel and sdist as a GitHub release, the wheel and sdist on PyPI (trusted publishing, environment `pypi`), and then `server.json` on the MCP Registry (GitHub OIDC; the registry checks the `mcp-name:` line in `README.md`, so keep it).
- Don't add a root `.mcp.json`: Claude Code would treat it as project config for everyone working on this repo. The Claude Code MCP server is declared inline in `.claude-plugin/plugin.json`.
- Commit messages are imperative English; one topic per PR.
- MyMemo Cloud is hidden until turned on in Settings (or `openmymemo cloud settings --show`), and listed while linked (product-design.md §16.11). It reads `OPENMYMEMO_CLOUD_URL` (the service; overrides the one chosen in Settings, default `https://api.mymemo.ai`) and `OPENMYMEMO_CLOUD_DIR` (the folder of `cloud.json` and `settings.json`). `tests/conftest.py` gives every test its own HOME, cloud folder and a local URL; set both yourself before trying `openmymemo cloud …` or the panel's sign-in by hand, so nothing touches your real link or MyMemo.
- The OpenMyMemo Assistant is off until turned on in Settings → Assistant or with `openmymemo assistant set`. Code that uses it builds `Assistant.from_settings(vault)`, checks `available(feature)`, calls `json_call` with material in `Data` blocks (never mixed into the instructions) and falls back to the rule path on None; it writes only through `Assistant.propose`. Tests use a fake endpoint (`openmymemo.assistant.client.TRANSPORT`, an `httpx2.MockTransport`); `tests/conftest.py` keeps the Keychain and the shell's `OPENMYMEMO_ASSISTANT_*` variables out.
- The Mac app adds nothing to the panel's API: it is the same panel in another layout. New panel features go into the web screens; give the Mac layout its own version only where the design has one. A native command the panel calls must be listed in `desktop/src-tauri/build.rs` and granted in `capabilities/default.json`, because the panel is a remote origin to Tauri.
