#!/bin/sh
# Hand the end of a session to the OpenMyMemo Assistant (Claude Code's SessionEnd hook, Codex's Stop hook): the
# hook's JSON comes on stdin and goes straight through. It does something only when the Assistant's session feature
# is on and the person allowed it for this app, and then works in the background, so this returns at once.
# Usage: session-end.sh <plugin root> <connector id>
# This must never break a session: it prints nothing and always exits 0.
root="${1:-${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}}"
connector="${2:-claude-code}"
[ -n "$root" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0
uv run --directory "$root" --frozen --no-dev --quiet openmymemo session-end --hook --connector "$connector" >/dev/null 2>&1
exit 0
