---
name: memory
description: Show what OpenMyMemo remembers about the user, or search it. Use when the user asks what you know or remember about them.
---

The user keeps their long-term memory in OpenMyMemo, a local folder they own, available through the
`openmymemo` MCP server.

- If the user just wants to see their memory: call the `memory_context` tool and summarise what it says, grouped by topic, in a few short lines. Don't add anything that isn't there.
- If they gave search terms: call `memory_search` with them and list the matching memories with their ids.

End by telling the user they can review, edit or remove memories in the OpenMyMemo panel at
http://127.0.0.1:8787 (it runs while this app is open), or by editing the Markdown files in their memory
folder (`~/OpenMyMemo/memories/` unless they chose another).
