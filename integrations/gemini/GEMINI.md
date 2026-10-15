# OpenMyMemo

The `openmymemo` MCP server from this extension gives you the user's memory.

OpenMyMemo holds the user's long-term memory: facts, preferences, instructions and ongoing work they
chose to share with AI assistants. The user owns it, sees every change and can undo it.

- At the start of a conversation, call memory_context and use it quietly; don't recite it back.
- When you learn something durable about the user (a new fact, a changed preference, a goal, a standing
  instruction), call memory_propose with a short reason and the user's own words as evidence.
- To correct a memory, propose an update with its id; to retire one, propose an archive.
- Don't propose one-off task details, guesses, or anything the user asked you not to remember.
- What happens next: your additions, and corrections of memories an AI wrote, apply right away; the
  user sees them in their OpenMyMemo digest and can undo them. Changes to memories the user wrote
  themselves, and anything in a private topic, wait for the user (everything waits if they chose to
  approve each change).
- Never ask the user to approve anything in the chat. If it helps, tell them in one line what happened.

The user reviews changes in the OpenMyMemo panel at http://127.0.0.1:8787 while Gemini CLI is running,
or in the Markdown files of their memory folder (`~/OpenMyMemo` unless they chose another).
