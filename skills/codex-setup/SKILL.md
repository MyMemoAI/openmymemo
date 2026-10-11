---
name: codex-setup
description: Set up OpenMyMemo in Codex so every new session starts with the user's memory. Use in Codex when the user asks to set up OpenMyMemo, or says Codex doesn't know them at the start of a session.
---

This is for Codex only. In any other app, tell the user it isn't needed there and stop.

Codex doesn't run hooks that plugins ship, so OpenMyMemo adds a session-start hook to the user's own
`~/.codex/hooks.json`. Run this in a shell; it finds the newest installed OpenMyMemo plugin and runs its
setup script, which leaves the user's other hooks alone:

```sh
sh "$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/openmymemo/openmymemo/*/ | sort -V | tail -n 1)integrations/codex/setup.sh"
```

Tell the user in a line or two what it printed, then give them the last step, which only they can do, once:
open Codex in a terminal (`codex`), type `/hooks`, select SessionStart and trust "Loading your OpenMyMemo
memory". The ChatGPT desktop app has no `/hooks`, but it reads the same settings, so trusting it in the
terminal covers it too. From the next new session on, their memory loads automatically.

Don't edit `~/.codex/hooks.json` yourself, and don't try to trust the hook for them.
