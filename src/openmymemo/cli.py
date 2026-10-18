"""Command line interface: ``openmymemo <command>``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from . import __version__, memfiles, transfer
from .assistant import features as assistant_features
from .connectors import Connectors
from .context import compile_context, search
from .proposals import Proposals, batch_label
from .snippets import http_snippets, stdio_snippets
from .vault import NotFound, Vault, VaultError, vault_path

app = typer.Typer(help="OpenMyMemo — your AI memory as a local folder you own.", no_args_is_help=True,
                  add_completion=False, pretty_exceptions_enable=False)
connector_app = typer.Typer(help="Manage the AI apps that can read your memory.", no_args_is_help=True)
app.add_typer(connector_app, name="connector")
setup_app = typer.Typer(help="Set up AI apps that need more than an MCP config.", no_args_is_help=True)
app.add_typer(setup_app, name="setup")
cloud_app = typer.Typer(help="MyMemo Cloud: use your memory in ChatGPT and claude.ai on the web (optional; needs a "
                             "MyMemo account).", no_args_is_help=True)
app.add_typer(cloud_app, name="cloud")
assistant_app = typer.Typer(help="OpenMyMemo Assistant: optional model help with imports, sorting and more "
                                 "(off by default).", no_args_is_help=True)
app.add_typer(assistant_app, name="assistant")

DEFAULT_PORT = 8787


def _pad(text: str, width: int) -> str:
    """Left-align text in a terminal column, counting CJK characters as two cells."""
    import unicodedata

    cells = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - cells)


def _vault(ctx: typer.Context, init: bool = False) -> Vault:
    """The vault; with `init`, created first when the folder is missing or empty (a folder holding anything
    else is refused). Plugins and the Mac app pass --init so a first launch just works."""
    try:
        return Vault.open_or_create(ctx.obj["vault"]) if init else Vault(ctx.obj["vault"]).ensure()
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
    vault: Annotated[str | None, typer.Option("--vault", envvar="OPENMYMEMO_HOME",
                                              help="Vault folder (default ~/OpenMyMemo).")] = None,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                          help="Show the version.")] = False,
) -> None:
    ctx.obj = {"vault": vault_path(vault)}


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
    typer.echo("Next:\n  openmymemo apps               # the AI apps on this computer"
               "\n  openmymemo connect <app>      # e.g. claude-desktop, cursor, codex"
               "\n  openmymemo import chatgpt     # bring your ChatGPT memory home"
               "\n  openmymemo serve              # open the panel")


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
    typer.echo("\nConnectors:" if cons else "\nNo connectors yet: see `openmymemo apps`, then `openmymemo connect <app>`")
    for c in cons:
        last = next(iter(vault.activity(c.id, limit=1)), None)
        typer.echo(f"  {c.id:<18} {c.kind:<5} write={c.write:<7} last seen {last['at'] if last else 'never'}")


def _read_input(file: Path | None, source: str) -> str:
    if file and str(file) != "-":
        try:
            return file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            typer.secho(f"{file} isn't UTF-8 text. Export or copy the memories as text first.", fg="red", err=True)
        except OSError as e:
            typer.secho(f"Can't read {file}: {e.strerror or e}.", fg="red", err=True)
        raise typer.Exit(1)
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
            source: Annotated[str, typer.Argument(help=f"auto (from the file's name), or one of: "
                                                       f"{', '.join(transfer.SOURCES)}")],
            file: Annotated[Path | None, typer.Argument(help="File to read; omit to paste or use the clipboard.")] = None,
            yes: Annotated[bool, typer.Option("--yes", "-y", help="Import without asking.")] = False) -> None:
    """Import memories from ChatGPT, Claude, Gemini, Markdown, JSON or another AI app's memory file (CLAUDE.md,
    AGENTS.md, GEMINI.md, Cursor rules, memory.md, …) into a from-<source> topic, in their order."""
    vault = _vault(ctx)
    if source == "auto":
        if file is None:
            typer.secho("auto needs a file: openmymemo import auto ~/.claude/CLAUDE.md", fg="red", err=True)
            raise typer.Exit(2)
        try:
            source, text = memfiles.read(file)
        except (ValueError, OSError) as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"{file} reads as {memfiles.LABELS[source]}.")
    elif source not in transfer.SOURCES:
        typer.secho(f"source must be auto or one of {', '.join(transfer.SOURCES)}", fg="red", err=True)
        raise typer.Exit(2)
    else:
        text = _read_input(file, source)
    try:
        items = assistant_features.import_preview(vault, source, text)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    if not items:
        typer.echo("Nothing to import.")
        return
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    if any(it.by == "assistant" for it in items):
        typer.echo("Split by OpenMyMemo Assistant. Lines marked ! were left out by the Assistant but kept by the "
                   "rules; they aren't imported unless you tick them in the panel.")
    elif any(it.by == "rules" for it in items):
        typer.echo("Split by rules: the Assistant couldn't be used this time.")
    slug, title = transfer.import_topic(vault, source)
    typer.echo(f"Into {title} ({slug}), in this order:")
    for i, it in enumerate(items, 1):
        flag = " = already have" if it.duplicate_of else (" ~ similar exists" if it.similar_to else "")
        if it.flag == "dropped":
            flag += " ! left out by the Assistant"
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
        rows = assistant_features.sort_plan(vault, topic)
    except VaultError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    if not rows:
        typer.echo("Nothing to sort.")
        return
    titles = {t["slug"]: t["title"] for t in vault.topics()}
    if any(r.get("by") == "assistant" for r in rows):
        typer.echo("Suggestions by OpenMyMemo Assistant (the rules where it gave none).")

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


@app.command("tidy")
def tidy_(ctx: typer.Context,
          dry_run: Annotated[bool, typer.Option("--dry-run", help="Make no calls and change nothing: show what "
                                                "would be sent, likely duplicates and the estimated calls and "
                                                "tokens.")] = False,
          topic: Annotated[str | None, typer.Option("--topic", help="Only this topic (its slug).")] = None,
          max_calls: Annotated[int | None, typer.Option("--max-calls", min=1, max=200,
                                                        help="At most this many model calls in this run.")] = None,
          schedule: Annotated[str | None, typer.Option(
              "--schedule", help="Set when it runs by itself, on this computer, while the panel runs (the Mac "
                                 "app, `openmymemo serve` or an AI app's plugin): off, daily, weekly or monthly. "
                                 "Doesn't run it now.")] = None) -> None:
    """Tidy up with the OpenMyMemo Assistant: one batch of suggested merges, fixes for contradictions, outdated
    memories, review dates and rewrites, for you to keep or undo. Needs the Assistant, with tidy-up turned on."""
    from .assistant import SettingsError, tidy

    vault = _vault(ctx)
    if schedule is not None:
        try:
            tidy.set_schedule(schedule)
        except SettingsError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from None
        s = tidy.schedule(vault)
        if s["every_days"] is None:
            typer.echo("Tidy-up doesn't run by itself on this computer.")
        else:
            typer.echo(f"Tidy-up runs {schedule} on this computer while the panel runs; next: {s['next_run']}.")
            typer.echo("Nothing runs when no panel does (the Mac app, `openmymemo serve`, or an AI app's plugin).")
        return
    result = tidy.run(vault, topic=topic, dry_run=dry_run, max_calls=max_calls or tidy.MAX_CALLS)
    if dry_run:
        typer.echo(f"Memories it would look at: {result.memories}"
                   + (f" ({result.left_out} private or not readable by the Assistant are left out)"
                      if result.left_out else ""))
        typer.echo(f"Estimated: {result.estimate['calls']} calls, about {result.estimate['tokens']:,} tokens")
        if result.candidates:
            mems = {m.id: m for m in vault.memories()}
            typer.secho(f"\nLikely duplicates for the model to confirm: {len(result.candidates)}", bold=True)
            for a, b in result.candidates[:20]:
                typer.echo(f"  - {mems[a].content}\n    {mems[b].content}")
            if len(result.candidates) > 20:
                typer.echo(f"  … and {len(result.candidates) - 20} more")
        typer.echo("")
    for item in result.items:
        head = {"merge": "Merge", "contradiction": "Contradiction", "outdated": "Possibly outdated",
                "until": "Review date", "rewrite": "Rewrite"}[item.kind]
        statuses = ", ".join(r.get("status", "?") for r in item.filed) or "not filed"
        typer.secho(f"\n{head} ({statuses})", bold=True)
        for text in item.before:
            typer.secho(f"  - {text}", fg="red" if item.kind != "until" else None)
        if item.content and item.kind != "contradiction":
            typer.secho(f"  + {item.content}", fg="green")
        if item.kind == "contradiction":
            typer.secho(f"  + {item.content}  (replaces the first)", fg="green")
        if item.until:
            typer.secho(f"  until {item.until}", fg="green")
        typer.echo(f"  why: {item.reason}")
        if item.error:
            typer.secho(f"  {item.error}", fg="yellow")
    typer.secho(("\n" if result.items else "") + result.message, fg=None if result.ok else "yellow",
                err=not result.ok)
    if result.applied or result.waiting:
        typer.echo("Review them with `openmymemo review` or on the panel's Changes page.")
    if not result.ok:
        raise typer.Exit(1)


@app.command()
def export(ctx: typer.Context,
           fmt: Annotated[str, typer.Option("--format", "-f", help="markdown, claude, chatgpt or json")] = "markdown",
           include_private: Annotated[bool, typer.Option(help="Include private topics and memories.")] = False,
           output: Annotated[Path | None, typer.Option("--output", "-o")] = None) -> None:
    """Export memories (e.g. to paste into Claude's or ChatGPT's memory)."""
    if fmt not in transfer.EXPORT_FORMATS:
        typer.secho(f"format must be one of {', '.join(transfer.EXPORT_FORMATS)}", fg="red", err=True)
        raise typer.Exit(2)
    text = transfer.export(_vault(ctx), fmt, include_private=include_private)
    if output:
        try:
            output.write_text(text, encoding="utf-8")
        except OSError as e:
            typer.secho(f"Can't write {output}: {e.strerror or e}.", fg="red", err=True)
            raise typer.Exit(1) from None
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
            from .clients import display_name

            c = Connectors(vault).ensure(connector, display_name(connector)) if connector else None
            result = compile_context(vault, c, max_chars=max_chars)
            digest = _hook_digest_line(Proposals(vault).unreviewed())
            vault.log_activity(c.id if c else "user", "context", memory_ids=result["memory_ids"],
                               chars=result["chars"], via="hook")
            typer.echo((digest + "\n" if digest else "") + result["text"])
        except Exception:  # a hook must never break the host app (revoked, unreadable vault, …)
            pass
        return
    vault = _vault(ctx)
    try:
        c = Connectors(vault).get(connector) if connector else None
    except VaultError as e:
        _connector_failed(e)
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
    "review": "this change is only made if you agree",
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
        if not _review_batches(props, digest, titles):
            return
    if pending:
        typer.secho(f"\nWaiting for you: {len(pending)}", bold=True)
        _review_pending(props, pending, titles)


def _review_batches(props: Proposals, digest: list, titles: dict[str, str]) -> bool:
    """The digest with each batch (a tidy-up) offered as a whole first: keep all, undo all, or one by one.
    Returns False when the user quits."""
    groups: list[tuple[str | None, list]] = []
    for p in digest:
        if p.batch_id and groups and groups[-1][0] == p.batch_id:
            groups[-1][1].append(p)
        elif p.batch_id and any(b == p.batch_id for b, _ in groups):
            next(items for b, items in groups if b == p.batch_id).append(p)
        else:
            groups.append((p.batch_id, [p]))
    singles: list = []
    for batch, items in groups:
        if batch is None:
            singles.extend(items)
            continue
        if singles and not _review_digest(props, singles, titles):
            return False
        singles = []
        typer.secho(f"\n{batch_label(batch, len(items))} by {items[0].connector_name}", bold=True)
        choice = typer.prompt("  [k]eep all  [u]ndo all  [o]ne by one  [s]kip  [q]uit", default="o").lower()[:1]
        if choice == "k":
            n = props.keep_batch(batch)
            typer.secho(f"  kept {n}", fg="green")
        elif choice == "u":
            done = props.undo_batch(batch)
            extra = f"; {done['kept']} had changed since and stay" if done["kept"] else ""
            typer.echo(f"  undid {done['undone']}{extra}")
        elif choice == "o":
            if not _review_digest(props, items, titles):
                return False
        elif choice == "q":
            return False
    return not singles or _review_digest(props, singles, titles)


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

    run_stdio(_vault(ctx, init), connector, panel_port or None)


@app.command()
def serve(ctx: typer.Context, port: int = DEFAULT_PORT,
          open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the panel.")] = True,
          init: Annotated[bool, typer.Option("--init", help="Create the vault if the folder is missing or empty.")] = False,
          ) -> None:
    """Start the local panel and HTTP MCP endpoint on 127.0.0.1."""
    import socket
    import threading
    import webbrowser

    from .web import serve_panel

    vault = _vault(ctx, init)
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
    serve_panel(vault, port)


# ---- AI apps ---------------------------------------------------------------------------------------

def _ago(iso: str | None) -> str:
    from datetime import UTC, datetime

    if not iso:
        return "-"
    try:
        secs = (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return iso
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{int(secs // size)} {unit} ago"
    return "just now"


@app.command("apps")
def apps_(ctx: typer.Context) -> None:
    """List the AI apps OpenMyMemo can connect: installed here, connected, in use."""
    from . import clients

    vault = _vault(ctx)
    rows = clients.catalog(vault)
    typer.secho(f"{'App':<34} {'Installed':<10} {'Connected':<18} In use", bold=True)
    for r in sorted(rows, key=lambda r: not (r["installed"] or r["connected"])):
        if r["id"] == clients.OTHER:
            continue
        connected = ("yes (plugin)" if r["via"] == "plugin" else "yes, broken" if r["broken"] else
                     "linked" if r["via"] == "account" else "yes"
                     ) if r["connected"] else "access removed" if r["revoked"] else "no"
        name = f"{r['name']} ({r['id']})"
        installed = "-" if r["method"] == "account" else "yes" if r["installed"] else "no"
        typer.echo(f"{_pad(name, 34)} {installed:<10} {connected:<18} "
                   f"{_ago(r['last_seen']) if r['in_use'] else '-'}")
    typer.echo("\nConnect one: openmymemo connect <app>    Any other MCP app: openmymemo connect other")
    if any(r["id"] == clients.CLOUD for r in rows):
        typer.echo("ChatGPT and claude.ai on the web: openmymemo cloud link (needs a MyMemo account)")


@app.command()
def doctor(ctx: typer.Context,
           port: Annotated[int, typer.Option(help="The panel's port.")] = DEFAULT_PORT) -> None:
    """Check the setup: the memory folder, uv, the panel and each AI app. Prints no memories."""
    from . import doctor as doc

    checks = doc.run(Path(ctx.obj["vault"]), port, _panel_for)
    marks = {"ok": ("✓", "green"), "info": ("·", None), "warn": ("!", "yellow"), "fail": ("✗", "red")}
    for c in checks:
        mark, color = marks[c.level]
        typer.secho(f"{mark} {_pad(c.area, 16)} {c.message}", fg=color)
        if c.fix and c.level != "ok":
            typer.echo(f"  {' ' * 16} → {c.fix}")
    problems = [c for c in checks if c.level in ("warn", "fail")]
    typer.echo("\nNo problems found." if not problems else
               f"\n{len(problems)} thing{'s' if len(problems) > 1 else ''} to look at. Paths above can include your user "
               "name; check before posting this anywhere.")
    if any(c.level == "fail" for c in checks):
        raise typer.Exit(1)


def _print_result(result) -> None:
    color = {"done": "green", "manual": "yellow", "error": "red"}[result.status]
    typer.secho(result.message, fg=color, err=result.status == "error")
    if result.snippet:
        typer.echo(f"\n{result.snippet.rstrip()}\n")
    if result.link:
        typer.echo(f"{result.link['label']}: {result.link['url']}")
    for step in result.steps:
        typer.echo(f"- {step}")


@app.command("connect")
def connect_(ctx: typer.Context, app_id: Annotated[str, typer.Argument(metavar="APP", help="e.g. claude-desktop, "
                                                                         "claude-code, codex, cursor, vscode")]) -> None:
    """Connect an AI app: add OpenMyMemo to its settings and allow it to read your memory."""
    from . import clients

    if app_id == clients.CLOUD:
        return _cloud_link(ctx, DEFAULT_PORT, True)
    vault = _vault(ctx, init=True)
    try:
        result = clients.connect(vault, app_id)
    except VaultError as e:
        typer.secho(f"{e}. Run `openmymemo apps` to see the list." if isinstance(e, NotFound) else str(e),
                    fg="red", err=True)
        raise typer.Exit(2 if isinstance(e, NotFound) else 1) from None
    _print_result(result)
    if result.status == "error":
        raise typer.Exit(1)


@app.command("disconnect")
def disconnect_(ctx: typer.Context, app_id: Annotated[str, typer.Argument(metavar="APP")]) -> None:
    """Disconnect an AI app: take OpenMyMemo out of its settings and revoke its access."""
    from . import clients

    if app_id == clients.CLOUD:
        return _cloud_unlink(DEFAULT_PORT, yes=False)
    vault = _vault(ctx)
    try:
        result = clients.disconnect(vault, app_id)
    except VaultError as e:
        typer.secho(f"{e}. Run `openmymemo apps` to see the list." if isinstance(e, NotFound) else str(e),
                    fg="red", err=True)
        raise typer.Exit(2 if isinstance(e, NotFound) else 1) from None
    _print_result(result)
    if result.status == "error":
        raise typer.Exit(1)


# ---- connectors ------------------------------------------------------------------------------------

def _connector_failed(e: VaultError) -> NoReturn:
    """An unknown or revoked connector id (or a change it can't take), as one line instead of a traceback."""
    typer.secho(f"{e}. `openmymemo connector list` shows yours." if isinstance(e, NotFound) else str(e),
                fg="red", err=True)
    raise typer.Exit(2 if isinstance(e, NotFound) else 1) from None


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
    try:
        Connectors(vault).get(connector_id)
    except VaultError as e:
        _connector_failed(e)
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
    try:
        _, token = Connectors(vault).rotate(connector_id)
    except VaultError as e:
        _connector_failed(e)
    _print_snippets(vault, connector_id, token)


@connector_app.command("remove")
def connector_remove(ctx: typer.Context, connector_id: str) -> None:
    """Revoke a connector's access."""
    try:
        Connectors(_vault(ctx)).remove(connector_id)
    except VaultError as e:
        _connector_failed(e)
    typer.echo(f"Revoked {connector_id}. Its MCP calls will now fail; `connector add --id {connector_id}` re-grants.")


# ---- MyMemo Cloud -----------------------------------------------------------------------------------

# The ports the Mac app looks at and starts its server on (desktop/src-tauri/src/core.rs `PORTS`): 8787 and the next nine.
PANEL_PORTS = range(DEFAULT_PORT, DEFAULT_PORT + 10)


def _panel_for(vault_root: Path, port: int) -> str | None:
    """The panel serving this folder, if one is running (it then runs MyMemo Cloud too): on `port`, or on any of
    the ports the Mac app uses. A panel for another folder is never used."""
    import json
    import urllib.request

    from .cloud import same_folder

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for p in dict.fromkeys([port, *PANEL_PORTS]):
        url = f"http://127.0.0.1:{p}"
        try:
            req = urllib.request.Request(f"{url}/api/state", headers={"X-OpenMyMemo": "1"})
            with opener.open(req, timeout=1) as resp:
                served = json.load(resp).get("vault")
        except Exception:
            continue
        if isinstance(served, str) and same_folder(served, vault_root):
            return url
    return None


def _panel_call(panel: str, method: str, path: str) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(f"{panel}{path}", method=method, headers={"X-OpenMyMemo": "1"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as resp:
        return json.load(resp)


BRIDGE_STATES = {
    "online": "online",
    "connecting": "connecting…",
    "offline": "offline, reconnecting",
    "refused": "turned away by MyMemo (it didn't say the link is gone), trying again",
    "plan": "paused: your MyMemo plan doesn't include MyMemo Cloud (it checks again every hour)",
    "replaced": "stopped: another OpenMyMemo took over this link",
    "key": "stopped: run `openmymemo cloud link` again",
    "unlinked": "unlinked",
    "closed": "stopped",
    "not_running": "not connected yet",
}


def _web_steps(endpoint: str) -> None:
    typer.echo(f"\nAdd this URL as a custom connector in ChatGPT or claude.ai, exactly as shown:\n  {endpoint}")
    typer.echo("- claude.ai: Settings → Connectors → Add custom connector; Claude signs you in with your MyMemo "
               "account.\n- ChatGPT: turn on Developer Mode in Settings, then add a connector with this URL "
               "(OAuth). Whether ChatGPT may use write tools can depend on your ChatGPT plan.")


def _cloud_link(ctx: typer.Context, port: int, open_browser: bool) -> None:
    import time
    import webbrowser

    from . import cloud

    vault = _vault(ctx, init=True)

    def show(url: str) -> None:
        typer.echo("Sign in to MyMemo in your browser to link this computer.")
        if open_browser and webbrowser.open(url):
            typer.echo(f"If your browser didn't open, go to:\n  {url}")
        else:
            typer.echo(f"Open this link:\n  {url}")

    before = cloud.load_link()
    panel = _panel_for(vault.root, port)
    if panel:  # the running panel takes the callback and starts the connection right away
        show(_panel_call(panel, "POST", "/api/cloud/link")["url"])
        deadline = time.monotonic() + cloud.LINK_TTL
        link = None
        while time.monotonic() < deadline:
            time.sleep(1)
            link = cloud.load_link()
            if link and (before is None or link.device_id != before.device_id):
                break
            status = _panel_call(panel, "GET", "/api/cloud")
            if not status["pending"] and status["link_error"]:
                typer.secho(f"Not linked: {status['link_error']}", fg="red", err=True)
                raise typer.Exit(1)
            link = None
        if link is None:
            typer.secho("Nobody signed in within ten minutes. Run `openmymemo cloud link` again.", fg="red", err=True)
            raise typer.Exit(1)
    else:
        outcome = cloud.link_with_listener(vault, show)
        if outcome.link is None:
            typer.secho(f"Not linked: {outcome.title}. {outcome.message}", fg="red", err=True)
            raise typer.Exit(1)
        link = outcome.link
    plan = cloud.plan_label(link.plan)
    typer.secho(f"Linked to {link.account}" + (f" · {plan}." if plan else "."), fg="green")
    typer.echo("If that isn't your MyMemo account, run `openmymemo cloud unlink` now.")
    _web_steps(cloud.endpoint(link.service))
    if not panel:
        typer.echo("\nMyMemo Cloud works while OpenMyMemo runs on this computer: the Mac app, `openmymemo serve`, or "
                   "an AI app that opens the panel.")


@cloud_app.command("link")
def cloud_link(ctx: typer.Context,
               port: Annotated[int, typer.Option(help="The port of the panel, if one is running.")] = DEFAULT_PORT,
               open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the browser.")] = True) -> None:
    """Sign in to MyMemo and link this computer, so ChatGPT and claude.ai on the web can use this folder."""
    _cloud_link(ctx, port, open_browser)


@cloud_app.command("status")
def cloud_status(ctx: typer.Context,
                 port: Annotated[int, typer.Option(help="The port of the panel, if one is running.")] = DEFAULT_PORT
                 ) -> None:
    """Show whether this computer is linked to MyMemo Cloud, and whether it is connected."""
    from . import cloud

    link = cloud.load_link()
    if link is None:
        typer.echo("Not linked. `openmymemo cloud link` signs in to MyMemo so ChatGPT and claude.ai on the web can use "
                   "your memory.")
        return
    plan = cloud.plan_label(link.plan)
    typer.echo(f"Linked to {link.account}" + (f" · {plan}" if plan else ""))
    root = Path(ctx.obj["vault"]).expanduser()
    here = link.serves(root)
    typer.echo(f"Folder: {link.vault}" + ("" if here else f"  (not {root}: link again from that folder to move it)"))
    typer.echo(f"Endpoint for ChatGPT and claude.ai: {cloud.endpoint(link.service)}")
    panel = _panel_for(Path(link.vault), port)
    if panel:
        status = _panel_call(panel, "GET", "/api/cloud")
        state = BRIDGE_STATES.get(status["reason"] if status["state"] != "online" and status["reason"]
                                  else status["state"], status["state"])
        typer.echo(f"Connection: {state}")
        if status.get("queued"):
            typer.echo(f"Waiting in MyMemo Cloud: {status['queued']}")
    else:
        typer.echo("Connection: not running. MyMemo Cloud works while OpenMyMemo runs on this computer: the Mac app, "
                   "`openmymemo serve`, or an AI app that opens the panel.")


def _cloud_unlink(port: int, yes: bool) -> None:
    from . import cloud

    link = cloud.load_link()
    if link is None:
        typer.echo("This computer isn't linked to MyMemo Cloud.")
        return
    if not yes and not typer.confirm("Unlink this computer from MyMemo Cloud? ChatGPT and claude.ai on the web won't "
                                     "reach your memory anymore.", default=True):
        raise typer.Exit()
    panel = _panel_for(Path(link.vault), port)
    message = _panel_call(panel, "DELETE", "/api/cloud")["message"] if panel else cloud.unlink()
    typer.echo(message)


@cloud_app.command("unlink")
def cloud_unlink(port: Annotated[int, typer.Option(help="The port of the panel, if one is running.")] = DEFAULT_PORT,
                 yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False) -> None:
    """Unlink this computer from MyMemo Cloud: the web apps lose access and the link is deleted here and at MyMemo."""
    _cloud_unlink(port, yes)


@cloud_app.command("settings")
def cloud_settings(
    show: Annotated[bool | None, typer.Option("--show/--hide", help="List MyMemo Cloud in the panel and `apps`.")] = None,
    service: Annotated[str | None, typer.Option(help="The MyMemo service to link to; \"\" for MyMemo's own.")] = None,
) -> None:
    """Show or change this computer's MyMemo Cloud settings: whether it is listed, and which service it links to."""
    from . import cloud

    try:
        prefs = cloud.save_settings(show=show, service=service, default_service=service is not None and not service.strip())
    except cloud.CloudError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    linked = cloud.load_link() is not None
    typer.echo(f"Listed: {'yes' if prefs['show'] else 'yes, while linked' if linked else 'no'}")
    typer.echo(f"Service: {cloud.service_url()}" + ("" if prefs["service"] else " (MyMemo's own)"))


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
    trust = (f'trust "{codex.STATUS_MESSAGE}" in the ChatGPT desktop app (Settings → Coding → Hooks), or open Codex '
             "in a terminal (`codex`), type /hooks and trust it under SessionStart. The app and the terminal share "
             "these settings, so either place covers both.")
    if changed:
        typer.echo(f"\nOne step left, once: {trust} New sessions then start with your memory.")
    else:
        typer.echo(f"\nIf new sessions still start without your memory, it isn't trusted yet: {trust}")


# ---- OpenMyMemo Assistant (product-design.md §17) -----------------------------------------------------------


def _assistant_status(vault: Vault) -> None:
    from .assistant import Assistant

    s = Assistant.from_settings(vault).status()
    typer.echo(f"OpenMyMemo Assistant: {'on' if s['enabled'] else 'off'}")
    kind = {"local": "a model on this computer", "key": "your own API key", "mymemo": "MyMemo (coming soon)"}[s["kind"]]
    typer.echo(f"  Model:     {s['model'] or '(none)'} via {kind}, {s['protocol']} API")
    typer.echo(f"  Endpoint:  {s['endpoint'] or '(none)'}")
    if s["kind"] != "local":
        where = {"keychain": " (in the Keychain)", "file": " (in assistant.json)", "env": " (from the environment)"}
        typer.echo(f"  Key:       {'set' + where.get(s['key_store'] or '', '') if s['has_key'] else 'not set'}")
    typer.echo("  Features:  " + ", ".join(f"{k} {'on' if v else 'off'}" for k, v in s["features"].items()))
    if s["private_topics"]:
        note = "" if s["local"] else " (only with a model on this computer)"
        typer.echo(f"  Private topics it may read: {', '.join(s['private_topics'])}{note}")
    caps, usage = s["caps"], s["usage"]
    limit = lambda v: "no limit" if v is None else f"{v:,}"  # noqa: E731
    typer.echo(f"  This month: {usage['calls']:,} calls (limit {limit(caps['calls'])}), "
               f"{usage['tokens']:,} tokens (limit {limit(caps['tokens'])})")
    if s["from_env"]:
        typer.echo(f"  Set by OPENMYMEMO_ASSISTANT_* variables: {', '.join(s['from_env'])}")
    typer.echo(f"\n{s['where']}")
    if s["enabled"] and s["reason"]:
        typer.secho(s["reason"], fg="yellow")


@assistant_app.command("status")
def assistant_status(ctx: typer.Context) -> None:
    """Show the Assistant's settings, this month's usage, and where memory text goes."""
    _assistant_status(_vault(ctx))


@assistant_app.command("set")
def assistant_set(
    ctx: typer.Context,
    kind: Annotated[str | None, typer.Option(help="local (a model on this computer) or key (your own API key).")] = None,
    protocol: Annotated[str | None, typer.Option(help="openai (OpenAI-compatible) or anthropic.")] = None,
    base_url: Annotated[str | None, typer.Option(help="The endpoint's base URL; empty for the default.")] = None,
    model: Annotated[str | None, typer.Option(help="The model's name, e.g. llama3.2 or claude-haiku-5-5.")] = None,
    key: Annotated[bool, typer.Option("--key", help="Ask for the API key (typed, not shown).")] = False,
    key_stdin: Annotated[bool, typer.Option("--key-stdin", help="Read the API key from standard input.")] = False,
    remove_key: Annotated[bool, typer.Option("--remove-key", help="Forget the stored API key.")] = False,
    feature: Annotated[list[str] | None, typer.Option(help="name=on|off, e.g. --feature sort=off. Repeatable.")] = None,
    cap_calls: Annotated[int | None, typer.Option(help="Monthly limit on calls; 0 for none.")] = None,
    cap_tokens: Annotated[int | None, typer.Option(help="Monthly limit on tokens; 0 for none.")] = None,
    private_topic: Annotated[list[str] | None, typer.Option(
        help="A private topic a model on this computer may read (repeatable; replaces the list).")] = None,
    no_private_topics: Annotated[bool, typer.Option("--no-private-topics", help="No private topics at all.")] = False,
) -> None:
    """Configure the Assistant and turn it on. Off by default; `openmymemo assistant off` turns it off again."""
    from .assistant import KINDS, SettingsError, ensure_connector, save_settings

    vault = _vault(ctx)
    if kind == "mymemo":
        typer.secho("MyMemo's hosted model isn't available yet.", fg="red", err=True)
        raise typer.Exit(2)
    if kind is not None and kind not in KINDS:
        typer.secho("kind must be local or key", fg="red", err=True)
        raise typer.Exit(2)
    secret = None
    if key_stdin:
        secret = sys.stdin.readline().strip()
    elif key:
        secret = typer.prompt("API key", hide_input=True).strip()
    elif remove_key:
        secret = ""
    features: dict[str, bool] = {}
    for f in feature or []:
        name, _, value = f.partition("=")
        if value.lower() not in ("on", "off"):
            typer.secho(f"--feature takes name=on or name=off, not {f}", fg="red", err=True)
            raise typer.Exit(2)
        features[name.strip()] = value.lower() == "on"
    caps = {k: v for k, v in (("calls", cap_calls), ("tokens", cap_tokens)) if v is not None}
    try:
        settings = save_settings(
            enabled=True, kind=kind, protocol=protocol, base_url=base_url, model=model, api_key=secret,
            features=features or None, caps=caps or None,
            private_topics=[] if no_private_topics else (private_topic or None))
    except SettingsError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None
    ensure_connector(vault, regrant=True)
    _assistant_status(vault)
    if settings.problem():
        raise typer.Exit(1)


@assistant_app.command("test")
def assistant_test(ctx: typer.Context) -> None:
    """Send one tiny request to the configured model and show its name and how long it took."""
    from .assistant import Assistant

    result = Assistant.from_settings(_vault(ctx)).test()
    if result["ok"]:
        typer.secho(f"{result['model']} answered in {result['ms']} ms.", fg="green")
    else:
        typer.secho(result["error"], fg="red", err=True)
        raise typer.Exit(1)


@assistant_app.command("off")
def assistant_off() -> None:
    """Turn the Assistant off: everything works by rules again, and no memory text goes to a model."""
    from .assistant import save_settings

    save_settings(enabled=False)
    typer.echo("OpenMyMemo Assistant is off.")


# The Mac app's environments, venvs/<version>-<stamp>: each process run from one holds a shared lock on
# this file while it runs, so the app removes an old environment only once nothing runs from it (core.rs).
ENV_LOCK = ".openmymemo-in-use"
_env_lock: int | None = None


def _hold_environment(prefix: str | None = None) -> None:
    """Hold the shared lock on ENV_LOCK in the Mac app's environment this process runs from, if it does."""
    global _env_lock
    env = Path(prefix if prefix is not None else sys.prefix)
    if (prefix is None and sys.prefix == sys.base_prefix) or env.parent.name != "venvs":
        return
    try:
        import fcntl

        fd = os.open(env / ENV_LOCK, os.O_RDONLY | os.O_CREAT, 0o644)
    except (ImportError, OSError):
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:  # the app is removing this environment right now
        os.close(fd)
        return
    _env_lock = fd


def main() -> None:
    _hold_environment()
    app()
