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
| Official MCP Registry (`io.github.MyMemoAI/openmymemo`) | MCP clients that browse the registry, such as VS Code's MCP server gallery, and directories that copy it | `server.json`; the `mcp-name:` line in `README.md` proves the PyPI package is ours | `release.yml`, job `mcp-registry` (GitHub OIDC) | none expected; if the namespace is refused, an owner of the MyMemoAI organization runs `mcp-publisher login github` and `mcp-publisher publish` once |
| Claude Code | `/plugin marketplace add MyMemoAI/openmymemo` | `.claude-plugin/` | `main` | optional: submit to Anthropic's plugin directory |
| Claude Desktop | the `.mcpb` from a release | `integrations/claude-desktop/manifest.json` | each tag | submit to Anthropic's desktop extension directory (an Anthropic form; Anthropic reviews it) |
| Codex | `codex plugin marketplace add MyMemoAI/openmymemo` | `plugin.json`, `mcp.json` | `main` | none |
| Gemini CLI | `gemini extensions install https://github.com/MyMemoAI/openmymemo`; the [extension gallery](https://geminicli.com/extensions/browse/) | `gemini-extension.json`, `integrations/gemini/GEMINI.md` | `main`; the gallery crawls tagged repositories daily | add the topic `gemini-cli-extension` to the repository's About section |
| Cline MCP Marketplace | one-click install in Cline | `README.md`, `llms-install.md` | `main` | open a submission issue at [cline/mcp-marketplace](https://github.com/cline/mcp-marketplace) with the repository URL, a 400×400 PNG icon and why it helps Cline users; check first that Cline installs it from `llms-install.md` alone |
| Cursor | the panel's one-click link, `openmymemo connect cursor` | `src/openmymemo/snippets.py` | each release | check Cursor's current MCP directory submission process in its docs |
| ChatGPT / OpenAI apps directory | — | — | — | only remote servers can be listed; that would be OpenMyMemo Cloud, a separate MyMemo service, not this repository |

Third-party directories (Smithery, Glama, PulseMCP, mcp.so and others) mostly find servers on their own,
often from the official registry. Claim a listing only through the directory's own site, and correct it
from the details above.

## Before submitting anywhere

- The listed version is released: PyPI, the GitHub release and the registry all show it.
- Install it the way that directory's users will, on a clean machine or a temporary home, and check that
  `memory_context` answers and the panel opens at http://127.0.0.1:8787.
- `PRIVACY.md` still describes what the app does.
