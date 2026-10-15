# Contributing to OpenMyMemo

Thanks for helping. OpenMyMemo keeps people's AI memory in a folder on their own computer, so changes are judged first by whether they keep that memory safe, readable and under the person's control.

## Ways to help

- **Report a bug or ask a question**: [open an issue](https://github.com/MyMemoAI/openmymemo/issues/new/choose). Don't paste your memories; describe them instead.
- **Report a security problem privately**, as [SECURITY.md](./SECURITY.md) explains. Never in a public issue.
- **Suggest a feature**: open an idea issue. Bigger changes (new features, interaction redesigns, file format changes, new dependencies) start as a proposal, and maintainers label it `accepted` before work starts, so nobody builds something that won't be merged.
- **Pick up work**: issues labeled `good first issue` or `help wanted`, or an `accepted` proposal. Say in the issue that you're on it.

## Before you start

- [docs/product-design.md](./docs/product-design.md) is the product definition. Its principles (§2) decide close calls.
- [docs/MEMORY_FORMAT.md](./docs/MEMORY_FORMAT.md) is a public contract other tools rely on. Change the spec first, and keep reading older files.
- [AGENTS.md](./AGENTS.md) has the code map and conventions, for people and coding agents alike.

## Development

You need [uv](https://docs.astral.sh/uv/), and for the panel Node 22 with pnpm 10. The Mac app also needs Rust.

```bash
uv sync                                  # Python dependencies
uv run pytest                            # tests
uv run ruff check src tests scripts      # lint
cd web && pnpm install && pnpm test      # panel tests
cd web && pnpm build                     # rebuild the panel into src/openmymemo/static/ (committed)
```

Try your change against a scratch folder, never your real memory: `uv run python scripts/demo_vault.py /tmp/omm-demo`, then `uv run openmymemo --vault /tmp/omm-demo serve`. The README's [Development](./README.md#development) section covers panel development and the Mac app.

## Pull requests

- One topic per pull request, with a title in the imperative ("Fix …", "Add …").
- Add tests. A bug fix comes with a test that fails without the fix.
- Everything people see is in English: panel copy (all of it in `web/src/i18n.ts`), CLI output, docs, MCP tool names and descriptions. Memories themselves can be in any language.
- If you change the panel, commit the rebuilt `src/openmymemo/static`; CI checks that it matches `web/`.
- CI runs the Python tests on Linux and macOS, lint, and the panel build; all three must pass.
- Add a line under "Unreleased" in [CHANGELOG.md](./CHANGELOG.md) when the change is something people would notice.

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](./LICENSE).

## Releases

Maintainers release by bumping the version everywhere `tests/test_packaging.py` checks, merging, and pushing a tag `v<version>`. The release workflow publishes the Mac app, the Claude Desktop extension, the Python package on PyPI and the MCP Registry entry. Where else OpenMyMemo is listed is in [docs/distribution.md](./docs/distribution.md).
