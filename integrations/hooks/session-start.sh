#!/bin/sh
# Print the user's OpenMyMemo memory for a session-start hook (Claude Code and Codex add stdout to the
# context). Usage: session-start.sh <plugin root> <connector id>
# This must never break a session: missing uv, a missing vault or a revoked connector all print nothing.
root="${1:-${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}}"
connector="${2:-default}"
[ -n "$root" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0
uv run --directory "$root" --frozen --no-dev --quiet openmymemo context --hook --connector "$connector" 2>/dev/null
exit 0
