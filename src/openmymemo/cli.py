"""Command line interface: ``openmymemo <command>``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__, transfer
from .connectors import Connectors
from .context import compile_context, search
from .proposals import Proposals
from .snippets import http_snippets, stdio_snippets
from .vault import Vault, VaultError, default_vault_path

app = typer.Typer(help="OpenMyMemo — your AI memory as a local folder you own.", no_args_is_help=True,
                  add_completion=False, pretty_exceptions_enable=False)
connector_app = typer.Typer(help="Manage the AI apps that can read your memory.", no_args_is_help=True)
app.add_typer(connector_app, name="connector")
setup_app = typer.Typer(help="Set up AI apps that need more than an MCP config.", no_args_is_help=True)
app.add_typer(setup_app, name="setup")

DEFAULT_PORT = 8787


def _pad(text: str, width: int) -> str:
    """Left-align text in a terminal column, counting CJK characters as two cells."""
    import unicodedata

    cells = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - cells)


def _vault(ctx: typer.Context) -> Vault:
    vault = Vault(ctx.obj["vault"])
    try:
        return vault.ensure()
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None


def _version(value: bool) -> None:
    if value:
        typer.echo(f"openmymemo {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    vault: Annotated[Path | None, typer.Option("--vault", envvar="OPENMYMEMO_HOME",
                                               help="Vault folder (default ~/OpenMyMemo).")] = None,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                          help="Show the version.")] = False,
) -> None:
    ctx.obj = {"vault": (vault or default_vault_path()).expanduser()}


@app.command()
def init(ctx: typer.Context) -> None:
    """Create a memory vault with the default topics."""
    try:
        vault = Vault.create(ctx.obj["vault"])
    except VaultError as e:
        typer.secho(str(e), fg="yellow", err=True)
        raise typer.Exit(1) from None
    typer.secho(f"Created {vault.root}", fg="green")
    typer.echo(f"  {len(vault.topics())} topics in {vault.memories_dir}")
    typer.echo("Next:\n  openmymemo import chatgpt     # bring your ChatGPT memory home"
               "\n  openmymemo connector add 'Claude Desktop'\n  openmymemo serve             # open the panel")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show what's in the vault and who has access."""
    vault = _vault(ctx)
    topics = vault.topics()
    total = sum(t["count"] for t in topics)
    approval_on = vault.settings()["require_approval"]
    props = Proposals(vault)
    typer.echo(f"Vault: {vault.root}\nMemories: {total}   Unreviewed changes: {props.unreviewed_count()}"
               f"   Pending proposals: {props.pending_count()}   Approval: {'on' if approval_on else 'off'}\n")
    group = None
    for t in topics:
        if t["group_name"] != group:
            group = t["group_name"]
            typer.secho(group, bold=True)
        lock = " (private)" if t["private"] else ""
        typer.echo(f"  {_pad(t['title'], 12)} {t['count']:>4}   {_pad(t['slug'], 20)}{lock}".rstrip())
    cons = Connectors(vault).list()
    typer.echo("\nConnectors:" if cons else "\nNo connectors yet: openmymemo connector add 'Claude Desktop'")
    for c in cons:
        last = next(iter(vault.activity(c.id, limit=1)), None)
        typer.echo(f"  {c.id:<18} {c.kind:<5} write={c.write:<7} last seen {last['at'] if last else 'never'}")


def _read_input(file: Path | None, source: str) -> str:
    if file and str(file) != "-":
        return file.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    if sys.platform == "darwin":
        clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        if clip.strip() and typer.confirm(f"Use the {len(clip.splitlines())} lines on your clipboard?", default=True):
            return clip
    hint = {
        "chatgpt": "ChatGPT → Settings → Personalization → Manage memories, select all and copy.",
        "claude": "Claude → Settings → Capabilities → View and edit your memory, or ask Claude to list its memory.",
    }.get(source, "Paste one memory per line.")
    typer.echo(f"{hint}\nPaste below, then press Ctrl-D:")
    return sys.stdin.read()


@app.command("import")
def import_(ctx: typer.Context,
            source: Annotated[str, typer.Argument(help=f"One of: {', '.join(transfer.SOURCES)}")],
            file: Annotated[Path | None, typer.Argument(help="File to read; omit to paste or use the clipboard.")] = None,
            yes: Annotated[bool, typer.Option("--yes", "-y", help="Import without asking.")] = False) -> None:
    """Import memories from ChatGPT, Claude, Gemini, Markdown or JSON into a from-<source> topic, in their order."""
    vault = _vault(ctx)
    if source not in transfer.SOURCES:
        typer.secho(f"source must be one of {', '.join(transfer.SOURCES)}", fg="red", err=True)
        raise typer.Exit(2)
    items = transfer.plan(vault, transfer.parse(source, _read_input(file, source)))
    if not items:
        typer.echo("Nothing to import.")
        return
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    slug, title = transfer.import_topic(vault, source)
    typer.echo(f"Into {title} ({slug}), in this order:")
    for i, it in enumerate(items, 1):
        flag = " = already have" if it.duplicate_of else (" ~ similar exists" if it.similar_to else "")
        where = f" → {titles[it.topic]} ({it.topic})" if it.topic else ""
        mark = "x" if it.include else " "
        typer.echo(f"[{mark}] {i:>3}. {it.kind:<11} {it.content}{where}{flag}")
    todo = [it for it in items if it.include]
    if not todo:
        typer.echo("Everything here is already in your vault.")
        return
    if not yes and not typer.confirm(f"Import {len(todo)} memories?", default=True):
        raise typer.Exit()
    ids = transfer.commit(vault, items, source)
    inboxed = len(ids) - sum(1 for it in todo if it.topic)  # items naming an existing topic went there
    if inboxed == len(ids):
        typer.secho(f"Imported {len(ids)} memories into {title} ({slug}). "
                    "Run `openmymemo sort` to file them into topics.", fg="green")
    elif inboxed:
        typer.secho(f"Imported {len(ids)} memories: {inboxed} into {title} ({slug}), the rest into the topics they "
                    "named. Run `openmymemo sort` to file them into topics.", fg="green")
    else:
        typer.secho(f"Imported {len(ids)} memories into the topics they named.", fg="green")


@app.command("sort")
def sort_(ctx: typer.Context,
          topic: Annotated[str | None, typer.Option("--topic", help="Only this imported topic, e.g. from-chatgpt.")] = None,
          yes: Annotated[bool, typer.Option("--yes", "-y", help=f"Move every memory whose suggested topic has a "
                                            f"confidence of {transfer.SORT_SURE_AT} or more without asking; "
                                            "leave the rest where they are.")] = False) -> None:
    """File imported memories into topics: a suggestion for each, and only the ones you accept move."""
    vault = _vault(ctx)
    try:
        rows = transfer.sort_plan(vault, topic)
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    if not rows:
        typer.echo("Nothing to sort.")
        return
    titles = {t["slug"]: t["title"] for t in vault.topics()}

    def where(slug: str | None) -> str:
        return f"{titles[slug]} ({slug})" if slug else "no suggestion"

    def move(row: dict, slug: str) -> int:
        return transfer.sort_commit(vault, [(row["memory_id"], slug)])

    moved = 0
    if yes:
        sure = [r for r in rows if r["topic"] and r["confidence"] >= transfer.SORT_SURE_AT]
        for r in sure:
            typer.echo(f"\"{r['content']}\" → {where(r['topic'])}")
            moved += move(r, r["topic"])
        if len(rows) > len(sure):
            typer.echo(f"Left {len(rows) - len(sure)} memories without a confident suggestion where they are.")
    else:
        words = {"yes": "y", "no": "n", "all": "a", "quit": "q"}
        for i, r in enumerate(rows, 1):
            typer.echo(f"[{i}/{len(rows)}] \"{r['content']}\" → {where(r['topic'])}")
            while True:  # until a valid answer
                answer = typer.prompt("  [y]es [n]o [t]opic (type a slug) [a]ll remaining [q]uit", default="n").strip()
                if answer.lower() == "t":
                    answer = typer.prompt("  topic slug").strip()
                choice = answer if answer in titles else words.get(answer.lower(), answer.lower())
                if choice in titles or choice in ("n", "a", "q") or (choice == "y" and r["topic"]):
                    break
                typer.echo("  No suggestion here: type a slug, or n to skip." if choice == "y"
                           else f"  Unknown topic: {answer}")
            if choice in titles:
                moved += move(r, choice)
            elif choice == "y":
                moved += move(r, r["topic"])
            elif choice == "a":
                moved += transfer.sort_commit(vault, [(x["memory_id"], x["topic"]) for x in rows[i - 1:] if x["topic"]])
                break
            elif choice == "q":
                break
    typer.secho(f"Moved {moved} memories.", fg="green")


@app.command()
def export(ctx: typer.Context,
           fmt: Annotated[str, typer.Option("--format", "-f", help="markdown, claude, chatgpt or json")] = "markdown",
           include_private: Annotated[bool, typer.Option(help="Include private topics and memories.")] = False,
           output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Export memories (e.g. to paste into Claude's or ChatGPT's memory)."""
    text = transfer.export(_vault(ctx), fmt, include_private=include_private)
    if output:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {output}")
    else:
        typer.echo(text, nl=False)


def _hook_digest_line(unreviewed: list) -> str | None:
    """One line for the session-start hook about AI changes the user hasn't looked at, or None.

    e.g. "OpenMyMemo: Claude Desktop added 3 memories and Cursor changed 1 since you last looked. …"
    """
    if not unreviewed:
        return None
    verbs = {"add": "added", "update": "changed", "archive": "archived"}
    counts: dict[str, dict[str, int]] = {}
    for p in reversed(unreviewed):  # oldest first, so apps are named in the order they wrote
        per_app = counts.setdefault(p.connector_name or p.connector_id, {})
        verb = verbs.get(p.op, "changed")
        per_app[verb] = per_app.get(verb, 0) + 1

    def join(parts: list[str], last: str = " and ") -> str:
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + last + parts[-1]

    phrases, first = [], True
    for app_name, per_app in counts.items():
        parts = []
        for verb in ("added", "changed", "archived"):
            if n := per_app.get(verb):
                noun = (" memory" if n == 1 else " memories") if first else ""
                parts.append(f"{verb} {n}{noun}")
                first = False
        phrases.append(f"{app_name} {join(parts)}")
    nested = len(phrases) > 1 and any(len(per_app) > 1 for per_app in counts.values())
    return (f"OpenMyMemo: {join(phrases, ', and ' if nested else ' and ')} since you last looked. "
            f"Review them at http://127.0.0.1:{DEFAULT_PORT}/#/changes or with `openmymemo review`.")


@app.command()
def context(ctx: typer.Context, connector: Annotated[str | None, typer.Option(help="Connector id.")] = None,
            max_chars: int = 8000,
            hook: Annotated[bool, typer.Option("--hook", help="For session-start hooks: register the connector "
                                               "if needed, log the read, and print nothing on any error.")] = False
            ) -> None:
    """Print exactly what an AI sees."""
    if hook:
        try:
            vault = Vault(ctx.obj["vault"])
            if not vault.is_initialized():
                return
            c = Connectors(vault).ensure(connector) if connector else None
            result = compile_context(vault, c, max_chars=max_chars)
            digest = _hook_digest_line(Proposals(vault).unreviewed())
            vault.log_activity(c.id if c else "user", "context", memory_ids=result["memory_ids"],
                               chars=result["chars"], via="hook")
            typer.echo((digest + "\n" if digest else "") + result["text"])
        except Exception:  # a hook must never break the host app (revoked, unreadable vault, …)
            pass
        return
    vault = _vault(ctx)
    c = Connectors(vault).get(connector) if connector else None
    result = compile_context(vault, c, max_chars=max_chars)
    typer.echo(result["text"])
    typer.secho(f"-- {result['chars']} chars, ~{result['approx_tokens']} tokens", dim=True, err=True)


@app.command("search")
def search_(ctx: typer.Context, query: str) -> None:
    """Search your memories."""
    for _score, m in search(_vault(ctx), None, query, limit=20):
        typer.echo(f"{m.id}  {_pad(m.topic, 20)} {m.content}")


@app.command()
def add(ctx: typer.Context, topic: str, content: str) -> None:
    """Add a memory yourself."""
    try:
        m = _vault(ctx).add(topic, content)
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Added {m.id} to {m.topic}")


WAITS_BECAUSE = {
    "protected": "you wrote this memory yourself",
    "private": "it is in a private topic",
    "approval": "approval is on, so every AI change waits for you",
}


@app.command()
def review(ctx: typer.Context) -> None:
    """Review what AI apps changed since you last looked, then anything waiting for your decision."""
    vault = _vault(ctx)
    props = Proposals(vault)
    digest = props.unreviewed()
    pending = props.list("pending")[::-1]
    if not digest and not pending:
        typer.echo("Nothing to review: no new AI changes and nothing waiting for you.")
        return
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    if digest:
        typer.secho(f"Changed since you last looked: {len(digest)}", bold=True)
        if not _review_digest(props, digest, titles):
            return
    if pending:
        typer.secho(f"\nWaiting for you: {len(pending)}", bold=True)
        _review_pending(props, pending, titles)


def _review_digest(props: Proposals, digest: list, titles: dict[str, str]) -> bool:
    """Changes that applied without the user, newest first. Returns False when the user quits."""
    for i, p in enumerate(digest, 1):
        topic = p.final_topic or p.topic
        title = titles.get(topic, topic)
        text = p.final_content or p.content or ""
        if p.op == "add":
            head = f'{p.connector_name} added to {title} · "{text}"'
        elif p.op == "update":
            head = f"{p.connector_name} changed a memory in {title}"
        else:
            head = f"{p.connector_name} archived a memory in {title}"
        typer.secho(f"\n[{i}/{len(digest)}] {head}", bold=True)
        if p.op in ("update", "archive") and p.before_content:
            typer.secho(f"  - {p.before_content}", fg="red")
        if p.op == "update":
            typer.secho(f"  + {text}", fg="green")
        if p.original_op == "add":
            typer.secho("  (proposed as new; it updated a similar memory)", dim=True)
        if p.reason:
            typer.echo(f"  why: {p.reason}")
        if not props.can_undo(p):
            typer.secho("  (the memory has changed since; roll it back from its history in the panel)", dim=True)
        choice = typer.prompt("  [k]eep  [u]ndo  [e]dit  [b]lock topic  [s]kip  [q]uit", default="s").lower()[:1]
        try:
            if choice == "k":
                props.mark_reviewed(p.id)
                typer.secho("  kept", fg="green")
            elif choice == "u":
                props.undo(p.id)
                typer.echo("  undone")
            elif choice == "e":
                if p.op == "archive":
                    typer.secho("  an archive has no text to edit; undo it instead", fg="yellow")
                    continue
                props.edit_applied(p.id, typer.prompt("  new text", default=text))
                typer.secho("  saved your version", fg="green")
            elif choice == "b":
                props.undo(p.id, block_topic=True)
                typer.echo(f"  undone; {p.connector_name} can no longer propose to {p.topic}")
            elif choice == "q":
                return False
        except VaultError as e:
            typer.secho(f"  {e}", fg="red")
    return True


def _review_pending(props: Proposals, pending: list, titles: dict[str, str]) -> None:
    """Proposals waiting for a decision, oldest first."""
    for i, p in enumerate(pending, 1):
        typer.secho(f"\n[{i}/{len(pending)}] {p.connector_name} wants to {p.op} · {titles.get(p.topic, p.topic)}",
                    bold=True)
        if p.before_content:
            typer.secho(f"  - {p.before_content}", fg="red")
        if p.content:
            typer.secho(f"  + {p.content}", fg="green")
        if p.original_op == "add":
            typer.secho("  (proposed as new; looks like an update of an existing memory)", dim=True)
        if p.conflict:
            typer.secho("  ! contradicts the existing memory", fg="yellow")
        if p.reason:
            typer.echo(f"  why: {p.reason}")
        if p.evidence:
            typer.echo(f"  said: “{p.evidence}”")
        if p.hold in WAITS_BECAUSE:
            typer.secho(f"  waiting because {WAITS_BECAUSE[p.hold]}", dim=True)
        choice = typer.prompt("  [a]ccept  [e]dit  [r]eject  [b]lock topic  [s]kip  [q]uit", default="s").lower()[:1]
        try:
            if choice == "a":
                props.accept(p.id)
                typer.secho("  accepted", fg="green")
            elif choice == "e":
                text = typer.prompt("  new text", default=p.content or "")
                props.accept(p.id, content=text)
                typer.secho("  accepted with edits", fg="green")
            elif choice == "r":
                props.reject(p.id)
                typer.echo("  rejected")
            elif choice == "b":
                props.reject(p.id, block_topic=True)
                typer.echo(f"  rejected; {p.connector_name} can no longer propose to {p.topic}")
            elif choice == "q":
                break
        except VaultError as e:
            typer.secho(f"  {e}", fg="red")


@app.command()
def approval(ctx: typer.Context,
             state: Annotated[str | None, typer.Argument(help="on: AI changes wait for you; off (the default): "
                                                               "they apply right away and you review them "
                                                               "afterwards.")] = None) -> None:
    """Show or set whether AI changes need your approval."""
    vault = _vault(ctx)
    if state is not None:
        if state not in ("on", "off"):
            typer.secho("Use `on` or `off`.", fg="red", err=True)
            raise typer.Exit(2)
        vault.update_settings(require_approval=state == "on")
    on = vault.settings()["require_approval"]
    typer.echo("Approval is ON: AI changes wait for you (`openmymemo review` or the panel)." if on else
               "Approval is OFF: AI changes apply right away and show up in `openmymemo review`; changes to "
               "memories you wrote yourself and private topics still wait for you.")


@app.command()
def mcp(ctx: typer.Context,
        connector: Annotated[str, typer.Option(help="Which app this is, e.g. claude-desktop.")] = "default",
        init: Annotated[bool, typer.Option("--init", help="Create the vault if the folder is missing or empty.")] = False,
        panel_port: Annotated[int, typer.Option(help="Also serve the panel on this port (0 = don't).")] = 0) -> None:
    """Run the MCP server over stdio (for Claude Desktop, Claude Code, Cursor, Codex …)."""
    from .mcp_server import run_stdio

    if init:
        try:
            vault = Vault.open_or_create(ctx.obj["vault"])
        except VaultError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(1) from None
    else:
        vault = _vault(ctx)
    run_stdio(vault, connector, panel_port or None)


@app.command()
def serve(ctx: typer.Context, port: int = DEFAULT_PORT,
          open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the panel.")] = True) -> None:
    """Start the local panel and HTTP MCP endpoint on 127.0.0.1."""
    import socket
    import threading
    import webbrowser

    import uvicorn

    from .web import create_app

    vault = _vault(ctx)
    url = f"http://127.0.0.1:{port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        # An AI app's MCP process (--panel-port) may already be serving this vault's panel.
        from .web import start_background_panel

        if start_background_panel(vault, port):
            typer.secho(f"The OpenMyMemo panel is already running at {url}", fg="green")
            if open_browser:
                webbrowser.open(url)
            return
        typer.secho(f"Port {port} is in use by another program. Try --port {port + 1}.", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"OpenMyMemo panel: {url}   MCP: {url}/mcp", fg="green")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(vault, port=port), host="127.0.0.1", port=port, log_level="warning")


# ---- connectors ------------------------------------------------------------------------------------

@connector_app.command("add")
def connector_add(ctx: typer.Context, name: str,
                  http: Annotated[bool, typer.Option("--http", help="Use the local HTTP endpoint with a token.")] = False,
                  write: Annotated[str, typer.Option(help="none, propose or auto")] = "propose",
                  connector_id: Annotated[str | None, typer.Option("--id")] = None) -> None:
    """Register an AI app and print its MCP configuration."""
    vault = _vault(ctx)
    try:
        c, token = Connectors(vault).create(name, "http" if http else "stdio", connector_id=connector_id, write=write)
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    typer.secho(f"Added connector {c.id} (write: {c.write}).", fg="green")
    _print_snippets(vault, c.id, token)


@connector_app.command("config")
def connector_config(ctx: typer.Context, connector_id: str) -> None:
    """Print the MCP configuration for a connector."""
    vault = _vault(ctx)
    Connectors(vault).get(connector_id)
    _print_snippets(vault, connector_id, None)


def _print_snippets(vault: Vault, connector_id: str, token: str | None) -> None:
    c = Connectors(vault).get(connector_id)
    if c.kind == "http":
        if token:
            typer.secho(f"Token (shown once): {token}", fg="yellow")
        snippets = http_snippets(f"http://127.0.0.1:{DEFAULT_PORT}/mcp", token)
        typer.echo("Requires `openmymemo serve` to be running.")
    else:
        snippets = stdio_snippets(vault.root, connector_id)
    labels = {"claude_code": "Claude Code", "claude_desktop": "Claude Desktop", "cursor": "Cursor (~/.cursor/mcp.json)",
              "cursor_link": "Cursor (open this link to install)", "vscode": "VS Code", "codex": "Codex",
              "json": "Any MCP client"}
    for key, text in snippets.items():
        typer.secho(f"\n{labels.get(key, key)}:", bold=True)
        typer.echo(text)


@connector_app.command("list")
def connector_list(ctx: typer.Context) -> None:
    """List connectors."""
    for c in Connectors(_vault(ctx)).list():
        scope = "all topics" if c.read_topics == "all" else ", ".join(c.read_topics)
        priv = " + private" if c.include_private else ""
        typer.echo(f"{c.id:<18} {c.kind:<5} write={c.write:<7} read={scope}{priv}"
                   + (f" token={c.token_preview}" if c.token_preview else ""))


@connector_app.command("rotate")
def connector_rotate(ctx: typer.Context, connector_id: str) -> None:
    """Issue a new HTTP token (the old one stops working)."""
    vault = _vault(ctx)
    _, token = Connectors(vault).rotate(connector_id)
    _print_snippets(vault, connector_id, token)


@connector_app.command("remove")
def connector_remove(ctx: typer.Context, connector_id: str) -> None:
    """Revoke a connector's access."""
    Connectors(_vault(ctx)).remove(connector_id)
    typer.echo(f"Revoked {connector_id}. Its MCP calls will now fail; `connector add --id {connector_id}` re-grants.")


# ---- setup ------------------------------------------------------------------------------------------

@setup_app.command("codex")
def setup_codex(ctx: typer.Context,
                remove: Annotated[bool, typer.Option("--remove", help="Take the hook out again.")] = False) -> None:
    """Load your memory at the start of every Codex session (installs a hook in ~/.codex/hooks.json)."""
    from . import codex
    from .snippets import command_for

    if sys.platform == "win32":
        typer.secho("The Codex session hook needs a POSIX shell, so it isn't available on Windows yet.",
                    fg="red", err=True)
        raise typer.Exit(1)
    path = codex.hooks_path()
    try:
        if remove:
            changed = codex.remove(path)
            typer.echo(f"Removed the OpenMyMemo hook from {path}." if changed else f"No OpenMyMemo hook in {path}.")
            return
        plugin = codex.plugin_installed()
        cli = None if plugin else command_for(ctx.obj["vault"].expanduser().resolve())
        changed = codex.install(codex.hook_command(cli), path)
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    source = "the installed OpenMyMemo plugin" if plugin else "this OpenMyMemo installation"
    if changed:
        typer.secho(f"Added the OpenMyMemo session hook to {path} (it runs {source}).", fg="green")
    else:
        typer.echo(f"The OpenMyMemo session hook in {path} is already up to date.")
    if codex.hooks_in_config_toml():
        typer.secho(f"{codex.codex_home() / 'config.toml'} also defines an OpenMyMemo hook; remove it there, "
                    "or memory loads twice.", fg="yellow")
    trust = (f'open Codex in a terminal (`codex`), type /hooks, select SessionStart and trust "{codex.STATUS_MESSAGE}". '
             "The ChatGPT desktop app has no /hooks, but it uses the same settings, so trusting it in the terminal "
             "covers it too.")
    if changed:
        typer.echo(f"\nOne step left, once: {trust} New sessions then start with your memory.")
    else:
        typer.echo(f"\nIf new sessions still start without your memory, it isn't trusted yet: {trust}")


def main() -> None:
    app()
