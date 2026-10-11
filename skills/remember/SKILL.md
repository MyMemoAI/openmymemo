---
name: remember
description: Save something about the user to OpenMyMemo. Use when the user says "remember that…" or asks you to keep a fact, preference or instruction for future sessions.
---

Turn what the user asked you to remember into one self-contained statement about them, for example
"Prefers answers in Chinese" or "Uses pnpm for JavaScript projects".

1. Call the `memory_search` tool (OpenMyMemo MCP server) to check whether a similar memory already exists.
2. If an existing memory says something close, call `memory_propose` with `op: "update"`, its `memory_id` and the full corrected text. Otherwise call it with `op: "add"`.
3. Pass the user's own words as `evidence` and a short `reason`.

Then tell the user in one line what happened: applied (they can see and undo it in the OpenMyMemo panel
at http://127.0.0.1:8787), waiting for them (say why), or already known. Don't ask them to approve it in
the chat.
