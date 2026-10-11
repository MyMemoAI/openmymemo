#!/bin/sh
# Install (or with --remove, take out) the Codex session hook from the installed plugin, so plugin users
# don't need a separate CLI install. Usage: sh <plugin root>/integrations/codex/setup.sh [--remove]
root=$(cd "$(dirname "$0")/../.." && pwd)
PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "OpenMyMemo needs uv (https://docs.astral.sh/uv/). Install it, then run this again." >&2
  exit 1
fi
exec uv run --directory "$root" --frozen --no-dev --quiet openmymemo setup codex "$@"
