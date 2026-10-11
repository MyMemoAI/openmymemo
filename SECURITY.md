# Security policy

OpenMyMemo keeps people's personal memory on their own machine, so we treat security reports as a priority.

## Reporting a vulnerability

Please don't open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub instead: open the [Security tab](https://github.com/MyMemoAI/openmymemo/security) and choose **Report a vulnerability** ([direct link](https://github.com/MyMemoAI/openmymemo/security/advisories/new)). Include the version, your OS, the client involved (Claude Desktop, Claude Code, Codex, Cursor, the panel) and the steps to reproduce.

We're a small team. We aim to acknowledge a report within a week, keep you updated while we work on a fix, and credit you in the advisory unless you'd rather stay anonymous.

## Supported versions

Fixes go into the latest release. Please check that the problem still happens on it before reporting.

## What counts

The design promises in [docs/product-design.md](./docs/product-design.md) are the security boundary. Examples of things we want to hear about:

- A web page or another local process reading or changing memory through the panel API, getting past the `Host` / `Origin` / `X-OpenMyMemo` checks, or reaching `/mcp` without a valid connector token.
- An AI connector reading a topic outside its scope, writing into a private topic, or changing a memory the user wrote without the user's approval.
- A crafted memory file, import (ChatGPT, Claude, Markdown, JSON) or MCP request that writes outside the vault folder or runs code.
- Tokens or memory content leaking into logs, generated files or the network.

Out of scope: anyone who already controls the user's account or files can read the vault, which is plain text by design.
