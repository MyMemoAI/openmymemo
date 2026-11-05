# Distribution

Where OpenMyMemo is published or listed, what in this repository each place reads, and what a maintainer
has to do by hand. Keep it current when a listing changes; a release updates most of them by itself.

## Shared listing details

Use the same details everywhere, so listings agree with each other and with the app.

| Field | Value |
| --- | --- |
| Name | OpenMyMemo |
| Short description | Your AI memory as a local folder you own. Every app shares it; you see and can undo each change. (`server.json`, 100 characters at most) |
| Long description | The Claude Desktop extension's `long_description` in `integrations/claude-desktop/manifest.json`, with the app's name changed |
| Publisher | MyMemo |
| Categories / keywords | Productivity; memory, local-first, mcp, privacy |
| License | Apache-2.0 |
| Icon | `integrations/icons/icon-512.png` (also 16 to 128 px in the same folder); a directory that wants another size gets a resize of the 512 px icon |
| Screenshots | `docs/images/panel-*-light.png` and `-dark.png` |
| Homepage, source | https://github.com/MyMemoAI/openmymemo |
| Support | https://github.com/MyMemoAI/openmymemo/issues |
| Privacy policy | https://github.com/MyMemoAI/openmymemo/blob/main/PRIVACY.md |
| Security contact | `SECURITY.md` |

Every tool has a title and read-only or destructive hints (`src/openmymemo/mcp_server.py`, checked by
`tests/test_mcp.py`), which the Claude and OpenAI directories require.

## Channels

| Channel | Users get it with | Read from | Updated by | Manual steps |
| --- | --- | --- | --- | --- |
| GitHub releases | the Mac app's `.dmg`, the Claude Desktop `.mcpb`, the wheel | `release.yml` | each tag | none |
| PyPI (`openmymemo`) | `uv tool install openmymemo`, `uvx openmymemo` | `pyproject.toml`, `README.md` as the description | `release.yml`, job `pypi` (trusted publishing) | one-time trusted publisher on pypi.org (done) |
| Official MCP Registry (`io.github.MyMemoAI/openmymemo`) | MCP clients and directories that read the registry | `server.json`; the `mcp-name:` line in `README.md` proves the PyPI package is ours | `release.yml`, job `mcp-registry` (GitHub OIDC) | none expected; if the namespace is refused, an owner of the MyMemoAI organization runs `mcp-publisher login github` and `mcp-publisher publish` once. VS Code's MCP gallery shows GitHub's MCP registry, not this one (below) |
| Claude Code, Cowork and Claude's plugin directory | `/plugin marketplace add MyMemoAI/openmymemo`; once listed, Customize → Plugins → Discover on claude.ai or `/plugin directory` in Claude Code | `.claude-plugin/plugin.json` (the repository root is the plugin folder), `README.md` as the listing's description, `.claude-plugin/icon.png` as its icon | `main` | submit once in the developer portal, see [Claude's plugin directory](#claudes-plugin-directory) |
| Claude Desktop | the `.mcpb` from a release | `integrations/claude-desktop/manifest.json` | each tag | none: Claude's directory no longer takes desktop extensions (MCPB). People install the file themselves. Chats in Claude Desktop don't run a plugin's local server, so the plugin listing doesn't replace the extension there |
| Codex | `codex plugin marketplace add MyMemoAI/openmymemo` | `plugin.json`, `mcp.json` | `main` | none |
| Gemini CLI | `gemini extensions install https://github.com/MyMemoAI/openmymemo`; the [extension gallery](https://geminicli.com/extensions/browse/) | `gemini-extension.json`, `integrations/gemini/GEMINI.md` | `main`; the gallery crawls tagged repositories daily | add the topic `gemini-cli-extension` to the repository's About section |
| Cline | one-click install in Cline | `README.md`, `llms-install.md` | `main` | submitted 2026-10-16 as [cline/mcp-marketplace#3029](https://github.com/cline/mcp-marketplace/issues/3029), unanswered (that repository hasn't changed since June 2025). The newer [cline/marketplace](https://github.com/cline/marketplace) takes a pull request adding `registry/mcps/openmymemo/entry.json` and an icon; it merges slowly too |
| Cursor | the panel's one-click link, `openmymemo connect cursor` | `src/openmymemo/snippets.py` | each release | not submitted. Cursor's Marketplace (cursor.com/marketplace/publish) needs a `.cursor-plugin/` manifest and reviews every submission by hand; in October 2026 submissions were reported waiting more than six weeks |
| ChatGPT / OpenAI apps directory | — | — | — | only remote servers can be listed; that would be OpenMyMemo Cloud, a separate MyMemo service, not this repository |

Other directories, as checked on 2026-11-05. Claim or correct a listing only through the directory's own
site, from the details above.

| Directory | Status | What to do |
| --- | --- | --- |
| [Glama](https://glama.ai/mcp/servers/MyMemoAI/openmymemo) | Listed, with details from 0.2 | Claim it: add a `glama.json` at the repository root naming the maintainers' GitHub accounts (`{"$schema": "https://glama.ai/mcp/schemas/server.json", "maintainers": [...]}`), then "Claim with GitHub" there. Its score needs a Dockerfile that starts the server. [awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) takes a pull request with a Glama score badge, so this comes first |
| mcp.so | Not listed | An issue "Submit MCP Server: OpenMyMemo" in [chatmcp/mcpso](https://github.com/chatmcp/mcpso/issues) with the name, repository, registry name, install command and category (the paid form on the site isn't needed) |
| GitHub's MCP registry (github.com/mcp), which VS Code's MCP gallery shows | Not listed | It doesn't copy the official registry: ask in the Discussions of [github/github-mcp-server](https://github.com/github/github-mcp-server/discussions); later versions then follow the official registry |
| Smithery | Not listed | None: it lists only remote servers now |
| PulseMCP | Not listed | None for now: new submissions are paused; it reads the official registry |

### Claude's plugin directory

The directory lists plugins for Claude Code and Cowork (and their skills in claude.ai chats, which don't run
a plugin's local server, so the skills say so there). Desktop extensions (MCPB) are no longer accepted.

- **Who submits:** someone on a Pro, Max or Ultra plan, or an Owner of a Team or Enterprise organization,
  whose GitHub account connected to claude.ai can push to this repository. The first organization to submit
  the repository holds the listing, so submit from the one that should own it.
- **Before:** `claude plugin validate .` passes without warnings. `.claude-plugin/icon.png` must be in `main`
  before the plugin is first saved in the portal: the listing takes its icon then and never again.
- **Submit:** [claude.ai/directory/manage](https://claude.ai/directory/manage) → Submit new → Plugin bundle.
  Source: `MyMemoAI/openmymemo`, no plugin path, branch `main`, then Validate.
- **Expected holds** (an Anthropic reviewer reads the version; they aren't rejections): the MCP server and hooks
  run `uv run --frozen` (always held, since the dependencies resolve at install); `uv.lock` and the built panel
  are over 256 KiB, and the panel is minified; the Mac app's `.icns` and `.ico`. Anything marked Blocking has
  to be fixed first.
- **Data handling answers:** it reads and stores personal data: the memories the person and their AI apps
  save, only on their computer, in the memory folder, until the person deletes them. It sends nothing to
  other services by default. What the person can turn on: OpenMyMemo Cloud and MyMemo Memory sync (MyMemo),
  the Assistant (the model the person chooses: on the computer, their own API key, or MyMemo's hosted model),
  search by meaning (a model download from Hugging Face); and the first start downloads Python and the
  libraries from GitHub and PyPI. `PRIVACY.md` has the details. Not directed at people under 18.
- **After:** the directory follows `main` and scans each new commit; README.md's "What a plugin runs and
  changes" has to stay true, since the security scan compares the plugin's behavior with it.

## Before submitting anywhere

- The listed version is released: PyPI, the GitHub release and the registry all show it.
- Install it the way that directory's users will, on a clean machine or a temporary home, and check that
  `memory_context` answers and the panel opens at http://127.0.0.1:8787.
- For Claude's plugin directory: `claude plugin validate .` prints no warnings.
- `PRIVACY.md` still describes what the app does.
