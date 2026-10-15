"""The MCP server AI apps talk to: read the user's memory, propose changes.

The same server runs over stdio (``openmymemo mcp --connector claude-desktop``), over local HTTP
(``openmymemo serve``, bearer token per connector) and, for web apps, relayed by MyMemo Cloud (``cloud.py``,
connector ``cloud-<slug>``). Every call is attributed to a connector so the user can see who read what. Every write is a proposal: most apply at once and land in the user's
digest, where they can be undone; the rest wait for the user (see ``proposals.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__
from .clients import display_name
from .connectors import Connector, Connectors
from .context import compile_context, search
from .proposals import Proposals
from .vault import Vault, VaultError

INSTRUCTIONS = """\
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
"""

RECALL_PROMPT = """\
Before answering, call the memory_context tool from OpenMyMemo and take the user's memory into
account. During our conversation, whenever you learn something about me that will still matter in a
month, call memory_propose (with a reason and my words as evidence). I'll see the changes in
OpenMyMemo and undo anything I don't want, so don't ask me to approve them here."""

# What the cloud variant of memory_propose adds to its description: only the cloud ever answers "queued".
CLOUD_QUEUED = (' Through MyMemo Cloud, when the person\'s computer is offline the status can also be "queued", '
                "with a `queue_id`: a receipt, not an acceptance. The change waits encrypted and is filed when the "
                "computer is back, where it can still be turned down; memory_proposals then shows it with that "
                "`queue_id`.")

Kind = Literal["fact", "preference", "ongoing", "instruction", "event"]
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _token_from(headers) -> str | None:
    if not headers:
        return None
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def build_server(vault: Vault, *, stdio_connector: str | None = None, panel_url: str | None = None,
                 relayed: Callable[[], Connector] | None = None) -> MCPServer:
    """The MCP server. `relayed` makes the MyMemo Cloud variant: it names the connector of the call being
    relayed, `memory_propose` mentions the cloud's "queued" status, and results carry no link to this
    computer's panel."""
    connectors = Connectors(vault)
    proposals = Proposals(vault)
    srv = MCPServer("OpenMyMemo", title="OpenMyMemo", instructions=INSTRUCTIONS, version=__version__,
                    website_url="https://github.com/MyMemoAI/openmymemo")

    def who(ctx: Context | None) -> Connector:
        if relayed is not None:
            return relayed()
        headers = None
        if ctx is not None:
            try:
                headers = ctx.headers
            except Exception:  # no request in flight (e.g. direct call in tests)
                headers = None
        token = _token_from(headers)
        if token:
            c = connectors.by_token(token)
            if c is None:
                raise ToolError("Unknown or revoked OpenMyMemo token.")
            return c
        if headers is not None and stdio_connector is None:
            raise ToolError("Missing bearer token.")
        if stdio_connector:
            try:
                return connectors.ensure(stdio_connector, display_name(stdio_connector))
            except VaultError as e:  # revoked, or an id it may not have (cloud-… is kept for web apps)
                raise ToolError(str(e)) from e
        raise ToolError("No connector configured for this OpenMyMemo server.")

    async def run(fn, *args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except VaultError as e:
            raise ToolError(str(e)) from e

    @srv.tool(title="Read memory", annotations=READ_ONLY)
    async def memory_context(ctx: Context, topics: list[str] | None = None, max_chars: int = 8000) -> str:
        """Get the user's memory as Markdown: who they are, how they want you to work, what they're doing.

        Call this once at the start of a conversation. `topics` limits it to some topic slugs;
        `max_chars` caps its length (most important memories come first).
        """
        c = who(ctx)
        result = await run(compile_context, vault, c, topics=topics, max_chars=max(500, min(max_chars, 50_000)))
        vault.log_activity(c.id, "context", memory_ids=result["memory_ids"], chars=result["chars"])
        return result["text"]

    @srv.tool(title="Search memory", annotations=READ_ONLY)
    async def memory_search(ctx: Context, query: str, topics: list[str] | None = None,
                            limit: int = 10) -> dict[str, Any]:
        """Search the user's memory by keywords (any language). Returns matching memories with their ids."""
        c = who(ctx)
        hits = await run(search, vault, c, query, topics=topics, limit=max(1, min(limit, 50)))
        rows = [{"id": m.id, "content": m.content, "topic": m.topic, "kind": m.kind, "updated_at": m.updated_at,
                 "score": round(s, 2)} for s, m in hits]
        vault.log_activity(c.id, "search", query=query, memory_ids=[r["id"] for r in rows])
        return {"results": rows}

    @srv.tool(title="Get memories", annotations=READ_ONLY)
    async def memory_get(ctx: Context, ids: list[str]) -> dict[str, Any]:
        """Fetch specific memories by id, with their topic, kind, source and timestamps."""
        c = who(ctx)
        private = vault.private_topics()

        def fetch():
            found, missing = [], []
            for mid in ids[:100]:
                try:
                    m = vault.get(mid)
                except VaultError:
                    missing.append(mid)
                    continue
                if (m.status != "active" or not c.can_read_topic(m.topic, private)
                        or (m.private and not c.include_private)):
                    missing.append(mid)
                    continue
                found.append({"id": m.id, "content": m.content, "topic": m.topic, "section": m.section,
                              "kind": m.kind, "source": m.source, "created_at": m.created_at,
                              "updated_at": m.updated_at, "until": m.until})
            return found, missing

        found, missing = await run(fetch)
        vault.log_activity(c.id, "get", memory_ids=[m["id"] for m in found])
        return {"memories": found, "missing": missing}

    async def memory_propose(
        ctx: Context,
        op: Literal["add", "update", "archive"],
        content: str | None = None,
        memory_id: str | None = None,
        topic: str | None = None,
        kind: Kind | None = None,
        reason: str | None = None,
        evidence: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add, correct or retire a memory of the user's. Returns at once; it never waits for the user.

        - op="add": `content` is one self-contained statement about the user, written in the third person
          or as the user would say it (e.g. "Prefers answers in Chinese"). Optional `topic` is a topic slug
          from memory_context; leave it out to let OpenMyMemo file it.
        - op="update": `memory_id` plus the full corrected `content`.
        - op="archive": `memory_id` of a memory that is no longer true.
        `reason` says why; `evidence` quotes what the user said. Reuse an `idempotency_key` when retrying.

        Returns `status`:
        - "applied": saved now. The user sees it in their OpenMyMemo digest and can undo it.
        - "pending": waiting for the user, and `hold` says why: "protected" (they wrote that memory
          themselves), "private" (it is in a private topic) or "approval" (they review every change first).
        - "duplicate": they already have this memory; nothing was saved.
        An add that resembles an existing memory becomes an update of that memory (see `note`).
        Don't ask the user to approve anything in the chat.
        """
        c = who(ctx)
        out = await run(proposals.propose, c, op, content=content, memory_id=memory_id, topic=topic, kind=kind,
                        reason=reason, evidence=evidence, idempotency_key=idempotency_key)
        if panel_url and relayed is None and out.get("status") in ("applied", "pending"):
            out["review_url"] = f"{panel_url}/#/changes"
        return out

    srv.tool(title="Propose a memory change",
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                         openWorldHint=False),
             description=memory_propose.__doc__.rstrip() + CLOUD_QUEUED if relayed else None)(memory_propose)

    @srv.tool(title="Check proposed changes", annotations=READ_ONLY)
    async def memory_proposals(
        ctx: Context,
        status: Literal["pending", "accepted", "edited_accepted", "rejected", "expired", "applied", "undone"]
        | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List the proposals this app made and what happened to them, so you don't repeat rejected or undone ones.

        Statuses: applied (saved; the user may still undo it), pending (waiting for the user; `hold` says
        why), accepted or edited_accepted (the user approved it, perhaps reworded), rejected, undone, expired.
        A proposal that waited in MyMemo Cloud's offline queue also has the `queue_id` it was given then.
        """
        c = who(ctx)
        rows = await run(proposals.list, status, c.id)
        out = [{"proposal_id": p.id, "op": p.op, "status": p.status, "hold": p.hold,
                "content": p.final_content or p.content, "topic": p.final_topic or p.topic,
                "memory_id": p.applied_memory_id or p.memory_id, "created_at": p.created_at,
                "decided_at": p.decided_at, **({"queue_id": p.queue_id} if p.queue_id else {})}
               for p in rows[: max(1, min(limit, 100))]]
        return {"proposals": out}

    if stdio_connector:
        # Static resources get no request context, so only offer this where the caller is fixed per process.
        @srv.resource("openmymemo://context", name="memory-context", title="User memory",
                      description="The user's memory as Markdown (same as the memory_context tool).",
                      mime_type="text/markdown")
        async def context_resource() -> str:
            c = who(None)
            result = await run(compile_context, vault, c)
            vault.log_activity(c.id, "context", memory_ids=result["memory_ids"], chars=result["chars"],
                               via="resource")
            return result["text"]

    @srv.prompt(name="recall", title="Use my memory",
                description="Load the user's memory and keep it up to date during the conversation.")
    def recall() -> str:
        return RECALL_PROMPT

    return srv


def run_stdio(vault: Vault, connector_id: str, panel_port: int | None = None) -> None:
    try:
        Connectors(vault).ensure(connector_id, display_name(connector_id))
    except VaultError:
        pass  # still start, so the app gets a readable error ("revoked", …) instead of a dead server
    panel_url = None
    if panel_port:
        from .web import start_background_panel

        panel_url = start_background_panel(vault, panel_port)
    build_server(vault, stdio_connector=connector_id, panel_url=panel_url).run("stdio")
