# Installing OpenMyMemo (for AI agents)

These steps are for an AI agent, such as Cline, that sets up MCP servers for its user. OpenMyMemo is a local
MCP server: it keeps the user's AI memory in Markdown files in a folder on their computer. It needs no
account, API key or environment variable.

1. Check that `uv` is installed: run `uv --version`. If it is missing, install it as described at
   https://docs.astral.sh/uv/getting-started/installation/ (macOS and Linux:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows:
   `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`). Then find its
   absolute path (`command -v uvx`, or `where uvx` on Windows), because apps that start MCP servers often
   don't see the shell's PATH.
2. Add this server to the MCP settings, using the absolute path of `uvx` for `command`. For Cline, the
   connector name is `cline`; for another app, use that app's name, so the user can tell apps apart.

   ```json
   {
     "mcpServers": {
       "openmymemo": {
         "command": "/absolute/path/to/uvx",
         "args": ["openmymemo", "mcp", "--connector", "cline", "--init", "--panel-port", "8787"]
       }
     }
   }
   ```

   The first start downloads OpenMyMemo and Python if needed (usually under a minute); if the app reports a
   timeout, restart the server once. `--init` creates the memory folder, `~/OpenMyMemo`, on first use.
3. Only if the user wants their memory somewhere else, add `"env": {"OPENMYMEMO_HOME": "/absolute/path"}`.
   If they already use OpenMyMemo in another app, use the same folder so every app shares one memory.
4. Check it works: call the `memory_context` tool. On a new folder it answers that no memories are
   shared yet; that is expected.
5. Tell the user, briefly:
   - their memory is the folder `~/OpenMyMemo` (or the one they chose), plain Markdown files they own;
   - the review panel is at http://127.0.0.1:8787 while this app is running. There they see every change an
     AI makes and can undo it, and choose what each app may read;
   - to bring over what ChatGPT, Claude or Gemini already remember, they can use the panel's Import & export page.

Don't add memories during setup, and don't print or upload anything from the memory folder. Privacy policy:
https://github.com/MyMemoAI/openmymemo/blob/main/PRIVACY.md
