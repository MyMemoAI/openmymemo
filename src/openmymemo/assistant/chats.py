"""Reading chats for the Assistant: chat exports for import (product-design.md §17.5) and agent transcripts for
session-end extraction (§17.7).

Formats, each read only in its known shape (anything else is skipped, never guessed at):

* ``chatgpt-export``: ChatGPT's data export, ``conversations.json`` (or the export's ``.zip``, from which only that
  file is read): conversations with a ``mapping`` of message nodes; the branch shown last (``current_node``) is used.
* ``claude-export``: Claude's data export, ``conversations.json`` (or its ``.zip``): conversations with
  ``chat_messages`` (``sender`` ``human`` / ``assistant``).
* ``claude-code-sessions``: Claude Code transcripts, ``~/.claude/projects/<project>/<session>.jsonl``.
* ``codex-sessions``: Codex session files, ``~/.codex/sessions/YYYY/MM/DD/rollout-….jsonl`` (and the older layout
  without ``payload``).

Only what the person typed and the assistants' text replies are kept; tool calls and their results, hook output,
system and instructions messages are skipped. Before anything is sent to a model (``chunks``), messages that
contain a memory the Assistant may not send are dropped (``Assistant.may_send``: private topics never go to a remote
model) and anything that looks like a key or token is masked. Chunks go newest first, each at most ``CHUNK_CHARS``,
and the model answers with candidates that cite the person's own message: the evidence kept with each candidate is
that message's words and date, checked against the transcript, never the model's paraphrase. Transcripts and exports
are read into memory and never stored.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..textutil import KINDS, similarity
from ..topics import IMPORTED_GROUP
from ..vault import Vault
from .core import Assistant, Data
from .guard import contains_any

EXPORT_SOURCES = ("chatgpt-export", "claude-export")
SESSION_SOURCES = ("claude-code-sessions", "codex-sessions")
CHAT_SOURCES = EXPORT_SOURCES + SESSION_SOURCES
LABELS = {"chatgpt-export": "ChatGPT", "claude-export": "Claude", "claude-code-sessions": "Claude Code",
          "codex-sessions": "Codex"}

MAX_EXPORT_BYTES = 300_000_000  # conversations.json of a long-time ChatGPT user can be a few hundred MB
MAX_UPLOAD_BYTES = 50_000_000  # sent through the panel as text
MAX_TRANSCRIPT_BYTES = 64_000_000
MAX_SESSION_FILES = 2000
CHUNK_CHARS = 12_000
USER_CHARS = 2_000  # per message
REPLY_CHARS = 400  # an assistant's reply is context, not a source of facts
QUOTE_CHARS = 300
CALL_OVERHEAD_TOKENS = 700  # instructions, schema and the topic list, per call
OUTPUT_TOKENS = 500  # a typical answer
CHARS_PER_TOKEN = 3.5

# Common secret shapes, masked before a chunk is sent. Linear patterns only (no nested quantifiers).
_SECRETS = re.compile(
    r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}|omc_[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----)")
# Codex and Claude Code put these into "user" messages themselves.
_SYSTEMISH = ("<environment_context", "<user_instructions", "# agents.md instructions", "<permissions instructions",
              "<command-", "<local-command-", "caveat: the messages below", "<system-reminder", "<user-prompt-submit-hook",
              "[request interrupted", "<turn_aborted", "<user_shell_command")


@dataclass
class Message:
    role: str  # user | assistant
    text: str
    at: str | None = None  # ISO 8601


@dataclass
class Conversation:
    id: str
    title: str
    source: str
    updated_at: str | None = None
    messages: list[Message] = field(default_factory=list)


@dataclass
class Chunk:
    """One call's worth of messages. ``refs`` maps the numbers shown to the model to (conversation, message)."""

    text: str
    refs: dict[int, tuple[Conversation, Message]]
    users: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Candidate:
    content: str
    kind: str | None
    topic: str | None
    evidence: str
    at: str | None
    sensitive: bool = False
    conversation: str = ""


# ---- reading ------------------------------------------------------------------------------------------------


def _iso(value) -> str | None:
    """An epoch number or an ISO string as ISO 8601 with an offset; None when it's neither."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat(timespec="seconds")
    return None


def _text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _systemish(text: str) -> bool:
    return text.lstrip()[:60].casefold().startswith(_SYSTEMISH)


def parse_chatgpt(data) -> list[Conversation]:
    """ChatGPT's ``conversations.json``."""
    out = []
    for i, conv in enumerate(data if isinstance(data, list) else []):
        if not isinstance(conv, dict) or not isinstance(conv.get("mapping"), dict):
            continue
        mapping = conv["mapping"]
        branch: list[dict] = []
        node_id = conv.get("current_node")
        seen = set()
        while isinstance(node_id, str) and node_id in mapping and node_id not in seen and len(seen) < 100_000:
            seen.add(node_id)
            node = mapping[node_id] if isinstance(mapping[node_id], dict) else {}
            branch.append(node)
            node_id = node.get("parent")
        if branch:
            branch.reverse()
        else:  # no current_node: every message, by time
            branch = sorted((n for n in mapping.values() if isinstance(n, dict) and isinstance(n.get("message"), dict)),
                            key=lambda n: n["message"].get("create_time") or 0)
        messages = []
        for node in branch:
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            role = (msg.get("author") or {}).get("role") if isinstance(msg.get("author"), dict) else None
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation"):
                continue
            content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
            if content.get("content_type") not in ("text", "multimodal_text"):
                continue
            parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            text = _text(" ".join(p for p in parts if isinstance(p, str)))
            if text:
                messages.append(Message(role, text, _iso(msg.get("create_time"))))
        if messages:
            out.append(Conversation(id=str(conv.get("id") or conv.get("conversation_id") or i),
                                    title=_text(conv.get("title")) or "Untitled", source="chatgpt-export",
                                    updated_at=_iso(conv.get("update_time")) or messages[-1].at, messages=messages))
    return out


def parse_claude(data) -> list[Conversation]:
    """Claude's ``conversations.json``."""
    out = []
    for i, conv in enumerate(data if isinstance(data, list) else []):
        if not isinstance(conv, dict) or not isinstance(conv.get("chat_messages"), list):
            continue
        messages = []
        for msg in conv["chat_messages"]:
            if not isinstance(msg, dict):
                continue
            role = {"human": "user", "assistant": "assistant"}.get(msg.get("sender"))
            if role is None:
                continue
            blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
            text = _text(" ".join(b.get("text", "") for b in blocks
                                  if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)))
            text = text or _text(msg.get("text"))
            if text:
                messages.append(Message(role, text, _iso(msg.get("created_at"))))
        if messages:
            out.append(Conversation(id=str(conv.get("uuid") or i), title=_text(conv.get("name")) or "Untitled",
                                    source="claude-export",
                                    updated_at=_iso(conv.get("updated_at")) or messages[-1].at, messages=messages))
    return out


def _jsonl(lines: Iterable[str]) -> Iterable[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def parse_claude_code(lines: Iterable[str], name: str = "") -> Conversation | None:
    """One Claude Code transcript (JSON lines)."""
    messages, title, session, cwd = [], "", "", ""
    for row in _jsonl(lines):
        if row.get("type") == "summary" and isinstance(row.get("summary"), str):
            title = title or _text(row["summary"])
            continue
        if row.get("type") not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
            continue
        session = session or (row.get("sessionId") if isinstance(row.get("sessionId"), str) else "")
        cwd = cwd or (row.get("cwd") if isinstance(row.get("cwd"), str) else "")
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, str):
            text = _text(content)
        elif isinstance(content, list):
            text = _text(" ".join(b.get("text", "") for b in content
                                  if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)))
        else:
            text = ""
        if not text or _systemish(text):
            continue
        messages.append(Message(row["type"], text, _iso(row.get("timestamp"))))
    if not messages:
        return None
    first = next((m.text for m in messages if m.role == "user"), "")
    project = Path(cwd).name if cwd else ""
    title = title or (first[:60] + ("…" if len(first) > 60 else ""))
    return Conversation(id=session or name, title=f"{title} ({project})" if project else title,
                        source="claude-code-sessions", updated_at=messages[-1].at, messages=messages)


def parse_codex(lines: Iterable[str], name: str = "") -> Conversation | None:
    """One Codex session file (JSON lines; the ``payload`` layout and the older flat one)."""
    messages, session, cwd = [], "", ""
    for row in _jsonl(lines):
        kind = row.get("type")
        if kind == "session_meta" and isinstance(row.get("payload"), dict):
            session = session or str(row["payload"].get("id") or "")
            cwd = cwd or str(row["payload"].get("cwd") or "")
            continue
        item = row.get("payload") if kind == "response_item" else row if kind == "message" else None
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") not in ("user", "assistant"):
            continue
        blocks = item.get("content") if isinstance(item.get("content"), list) else []
        text = _text(" ".join(b.get("text", "") for b in blocks if isinstance(b, dict)
                              and b.get("type") in ("input_text", "output_text", "text") and isinstance(b.get("text"), str)))
        if not text or _systemish(text):
            continue
        messages.append(Message(item["role"], text, _iso(row.get("timestamp"))))
    if not messages:
        return None
    first = next((m.text for m in messages if m.role == "user"), "")
    project = Path(cwd).name if cwd else ""
    title = first[:60] + ("…" if len(first) > 60 else "")
    return Conversation(id=session or name, title=f"{title} ({project})" if project else title,
                        source="codex-sessions", updated_at=messages[-1].at, messages=messages)


def parse_export(source: str, raw: bytes | str) -> list[Conversation]:
    """A chat export's ``conversations.json`` (bytes or text). Raises ValueError when it isn't one."""
    if source not in EXPORT_SOURCES:
        raise ValueError(f"source must be one of {', '.join(EXPORT_SOURCES)}")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("That isn't a conversations.json file (it isn't JSON).") from None
    if isinstance(data, dict) and isinstance(data.get("conversations"), list):
        data = data["conversations"]
    convs = parse_chatgpt(data) if source == "chatgpt-export" else parse_claude(data)
    if not convs and isinstance(data, list) and data:
        other = parse_claude(data) if source == "chatgpt-export" else parse_chatgpt(data)
        hint = "Claude's" if source == "chatgpt-export" else "ChatGPT's"
        if other:
            raise ValueError(f"This looks like {hint} export; choose that source instead.")
    if not convs and not (isinstance(data, list) and not data):
        raise ValueError("No conversations found: choose the conversations.json from the export.")
    return convs


def read_export(source: str, path: Path) -> list[Conversation]:
    """A chat export from disk: ``conversations.json``, or the export's ``.zip`` (only ``conversations.json`` in it
    is read). The caller has checked the path is in the home folder. Raises ValueError or OSError."""
    name = path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            info = next((i for i in z.infolist() if Path(i.filename).name.lower() == "conversations.json"
                         and not i.is_dir()), None)
            if info is None:
                raise ValueError(f"{path.name} has no conversations.json.")
            if info.file_size > MAX_EXPORT_BYTES:
                raise ValueError("That export is larger than OpenMyMemo reads (300 MB).")
            return parse_export(source, z.read(info))
    if not name.endswith(".json"):
        raise ValueError("Choose the export's conversations.json, or the .zip it came in.")
    if path.stat().st_size > MAX_EXPORT_BYTES:
        raise ValueError("That file is larger than OpenMyMemo reads (300 MB).")
    return parse_export(source, path.read_bytes())


def read_transcript(source: str, path: Path) -> Conversation | None:
    """One session file. The caller has checked where it is. Raises ValueError or OSError."""
    if not path.name.endswith(".jsonl"):
        raise ValueError("A session transcript is a .jsonl file.")
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise ValueError("That transcript is larger than OpenMyMemo reads (64 MB).")
    with path.open(encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    parse = parse_claude_code if source == "claude-code-sessions" else parse_codex
    return parse(lines, path.stem)


def session_files(source: str, folder: Path) -> list[Path]:
    """The session files in an app's folder, newest first: only the known layout, never a whole tree.
    ``folder`` is ``~/.claude/projects`` (or one project in it) or ``~/.codex/sessions`` (or a year, month or
    day in it)."""
    if source == "claude-code-sessions":
        patterns = ("*/*.jsonl", "*.jsonl")
    elif source == "codex-sessions":
        patterns = ("*/*/*/rollout-*.jsonl", "*/*/rollout-*.jsonl", "*/rollout-*.jsonl", "rollout-*.jsonl")
    else:
        raise ValueError(f"source must be one of {', '.join(SESSION_SOURCES)}")
    found: dict[str, Path] = {}
    for pattern in patterns:
        for p in folder.glob(pattern):
            if p.is_file() and not p.is_symlink():
                found[str(p)] = p
            if len(found) >= MAX_SESSION_FILES * 2:
                break
    files = sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_SESSION_FILES]


def read_sessions(source: str, folder: Path) -> list[Conversation]:
    out = []
    for p in session_files(source, folder):
        try:
            conv = read_transcript(source, p)
        except (ValueError, OSError):
            continue
        if conv:
            out.append(conv)
    return out


# ---- what may be sent ---------------------------------------------------------------------------------------


def original_text(conv: Conversation) -> str:
    """A conversation's messages as they were read, for ``Assistant.screen``."""
    return "\n".join(m.text for m in conv.messages)


def screen_chunks(assistant: Assistant, feature: str, todo: list[Chunk]) -> list[Chunk]:
    """``Assistant.screen`` each conversation in ``todo`` on its original text; when any is refused, the chunks are
    rebuilt without the refused conversations (as many as before, at most)."""
    convs: dict[int, Conversation] = {}
    for ch in todo:
        for conv, _ in ch.refs.values():
            convs.setdefault(id(conv), conv)
    refused = {key for key, conv in convs.items() if not assistant.screen(feature, original_text(conv))}
    if not refused:
        return todo
    kept = [conv for key, conv in convs.items() if key not in refused]
    return chunks(kept, forbidden=forbidden_texts(assistant))[: len(todo)]


def mask_secrets(text: str) -> str:
    return _SECRETS.sub("[secret]", text)


def forbidden_texts(assistant: Assistant) -> list[str]:
    """Everything the Assistant may not send to its model (``Guard.withheld``: private and out-of-scope memories,
    their history and the proposals about them); a message containing any of it is left out of what is sent."""
    g = assistant.guard()
    return g.withheld()[2] if g is not None else []


def chunks(conversations: Sequence[Conversation], *, forbidden: Sequence[str] = (), chunk_chars: int = CHUNK_CHARS,
           skip: Callable[[Conversation, Message], bool] | None = None) -> list[Chunk]:
    """Rendered chunks for the model, newest conversation first. Messages that contain a forbidden memory, or that
    ``skip`` says to leave out, aren't included; keys and tokens are masked. A conversation longer than a chunk
    continues in the next one. Chunks without a message from the person are dropped."""
    out: list[Chunk] = []
    lines: list[str] = []
    refs: dict[int, tuple[Conversation, Message]] = {}
    size = users = 0
    n = 0

    def flush() -> None:
        nonlocal lines, refs, size, users
        if users:
            out.append(Chunk("\n".join(lines), refs, users))
        lines, refs, size, users = [], {}, 0, 0

    ordered = sorted(conversations, key=lambda c: c.updated_at or "", reverse=True)
    for conv in ordered:
        header = f'== Conversation "{conv.title[:120]}" ({LABELS.get(conv.source, conv.source)}' + (
            f", {conv.updated_at[:10]})" if conv.updated_at else ")")
        started = False
        for msg in conv.messages:
            if skip and skip(conv, msg):
                continue
            if forbidden and contains_any(msg.text, forbidden):
                continue
            text = mask_secrets(msg.text)
            limit = USER_CHARS if msg.role == "user" else REPLY_CHARS
            text = text[:limit] + ("…" if len(text) > limit else "")
            n += 1
            line = (f"#{n} {msg.role}" + (f" {msg.at[:10]}" if msg.at else "") + f": {text}") if msg.role == "user" \
                else f"#{n} assistant: {text}"
            extra = len(line) + (0 if started else len(header) + 1) + 1
            if size and size + extra > chunk_chars:
                flush()
                started = False
                extra = len(line) + len(header) + 2
            if not started:
                lines.append(header)
                started = True
            lines.append(line)
            refs[n] = (conv, msg)
            size += extra
            if msg.role == "user":
                users += 1
    flush()
    return out


def estimate(chs: Sequence[Chunk], assistant: Assistant | None = None) -> dict:
    """The cost of extracting from these chunks, for the person to see before starting: one call per chunk."""
    chars = sum(c.chars for c in chs)
    tokens_in = int(chars / CHARS_PER_TOKEN) + CALL_OVERHEAD_TOKENS * len(chs)
    out = {"calls": len(chs), "tokens_in": tokens_in, "tokens_out": OUTPUT_TOKENS * len(chs),
           "tokens": tokens_in + OUTPUT_TOKENS * len(chs), "chars": chars,
           "messages": sum(len(c.refs) for c in chs)}
    if assistant is not None:
        s = assistant.settings
        usage = s.month_usage()
        out["calls_left"] = None if s.caps.get("calls") is None else max(0, s.caps["calls"] - usage["calls"])
        out["tokens_left"] = None if s.caps.get("tokens") is None else max(0, s.caps["tokens"] - usage["tokens"])
        out["remote"] = assistant.remote
        out["host"] = s.host
    return out


def summary(conversations: Sequence[Conversation]) -> dict:
    dates = sorted(c.updated_at for c in conversations if c.updated_at)
    return {"conversations": len(conversations), "messages": sum(len(c.messages) for c in conversations),
            "newest": dates[-1][:10] if dates else None, "oldest": dates[0][:10] if dates else None}


# ---- extracting ---------------------------------------------------------------------------------------------

EXTRACT_INSTRUCTIONS = """You read a person's conversations with AI assistants and find what is worth remembering \
about the person for the long term, so their other AI assistants know it too.

- Keep only durable things about the person: facts about them and their life, their preferences, how they want AI \
assistants to work with them, their ongoing projects and goals. Leave out one-off tasks and questions, details of a \
single piece of code or repository, what the assistant said or suggested, guesses, and anything the person asked not \
to be remembered.
- Only what the person said themselves, in a message marked "user". For each memory give that message's number \
("message") and quote the person's words it rests on ("quote", at most 200 characters, copied exactly).
- Write each memory as one standalone statement in the language the person used, e.g. "Is vegetarian". Keep \
names, numbers and dates as written. Don't merge unrelated facts.
- Pick the topic it belongs in from the topic list, by slug, or null. Pick a kind: fact, preference, ongoing or \
instruction, or null.
- Set "sensitive" to true when it is about the person's health, money, or a password, key or other secret.
- Fewer, better memories are best; return an empty list when there is nothing durable."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array", "maxItems": 60,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 3, "maxLength": 1000},
                    "message": {"type": "integer", "minimum": 1},
                    "quote": {"type": "string"},
                    "topic": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"]},
                    "sensitive": {"type": "boolean"},
                },
                "required": ["content", "message", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["memories"],
    "additionalProperties": False,
}


def targets(vault: Vault, assistant: Assistant | None = None) -> list[dict]:
    """Topics a candidate may name: the regular ones that aren't private (private topics' names aren't sent), and
    within the Assistant's read scope."""
    if assistant is not None:
        return assistant.topic_choices()
    return [t for t in vault.topics() if t["group"] != IMPORTED_GROUP and not t["private"]]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def evidence_for(msg: Message, conv: Conversation, quote: str) -> str:
    """The person's own words with the date and where they said it. The model's quote is used only when it really
    is in the message; otherwise the start of the message."""
    quote = " ".join(quote.split())
    text = mask_secrets(msg.text)
    if not quote or _norm(quote) not in _norm(text):
        quote = text[:QUOTE_CHARS] + ("…" if len(text) > QUOTE_CHARS else "")
    where = f'{LABELS.get(conv.source, conv.source)}, "{conv.title[:80]}"'
    return f"“{quote[:QUOTE_CHARS]}” — {where}" + (f", {msg.at[:10]}" if msg.at else "")


def extract(assistant: Assistant, feature: str, chunk: Chunk, topic_list: Sequence[dict],
            extra: Sequence[Data] = ()) -> list[Candidate] | None:
    """Candidates from one chunk, or None when the call failed (the reason is in ``assistant.last_error``)."""
    slugs = {t["slug"] for t in topic_list}
    topics_text = "\n".join(f"- {t['slug']}: {t['title']}" for t in topic_list)
    answer = assistant.json_call(feature, EXTRACT_INSTRUCTIONS,
                                 [Data(topics_text, label="topics"), *extra, Data(chunk.text, label="conversations")],
                                 EXTRACT_SCHEMA, max_tokens=4096)
    if answer is None:
        return None
    out = []
    for m in answer["memories"]:
        ref = chunk.refs.get(m["message"])
        if ref is None or ref[1].role != "user":
            continue  # only what the person said
        conv, msg = ref
        content = " ".join(m["content"].split())
        if len(content) < 3:
            continue
        out.append(Candidate(content=content, kind=m.get("kind") if m.get("kind") in KINDS else None,
                             topic=m.get("topic") if m.get("topic") in slugs else None,
                             evidence=evidence_for(msg, conv, m.get("quote") or ""), at=msg.at,
                             sensitive=bool(m.get("sensitive")), conversation=conv.title))
    return out


def unique(cands: Iterable[Candidate], against: Sequence[str] = (), at: float = 0.8) -> list[Candidate]:
    """Candidates without near-repeats of each other or of ``against``."""
    kept: list[Candidate] = []
    for c in cands:
        if any(similarity(c.content, k.content) >= at for k in kept) or any(
                similarity(c.content, a) >= at for a in against):
            continue
        kept.append(c)
    return kept


# ---- chat-export import (§17.5) -----------------------------------------------------------------------------


class ChatImport:
    """One chat import: parsed conversations, the chunks that would be sent and their cost, then the preview.

    ``plan()`` costs nothing (no call); ``run()`` makes the calls, newest chunk first, up to ``max_calls``, and
    returns import items for the ordinary preview (``transfer.plan`` flags duplicates). Needs the Assistant's
    ``import`` feature: there is no rule-based way to read a conversation."""

    def __init__(self, vault: Vault, source: str, conversations: Sequence[Conversation],
                 assistant: Assistant | None = None):
        if source not in CHAT_SOURCES:
            raise ValueError(f"source must be one of {', '.join(CHAT_SOURCES)}")
        self.vault = vault
        self.source = source
        self.conversations = list(conversations)
        self.assistant = assistant or Assistant.from_settings(vault)
        self.chunks = chunks(self.conversations, forbidden=forbidden_texts(self.assistant))
        self.done = 0  # chunks processed so far
        self.failed = 0
        self.error: str | None = None

    def screened(self, todo: list[Chunk]) -> list[Chunk]:
        """These chunks after ``Assistant.screen`` of each conversation in them, as it was read (before chunking,
        numbering and masking): a conversation that holds anything that may not be sent is left out entirely."""
        return screen_chunks(self.assistant, "import", todo)

    def plan(self) -> dict:
        return {"source": self.source, **summary(self.conversations),
                "estimate": estimate(self.chunks, self.assistant),
                "ready": self.assistant.available("import"), "reason": self.assistant.unavailable_reason("import")}

    def run(self, max_calls: int | None = None, *, progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> list:
        from .. import transfer

        todo = self.screened(self.chunks[: len(self.chunks) if max_calls is None else max(0, max_calls)])
        topic_list = targets(self.vault, self.assistant)
        found: list[Candidate] = []
        for chunk in todo:
            if cancelled and cancelled():
                break
            if not self.assistant.available("import"):
                self.error = self.assistant.unavailable_reason("import")
                break
            got = extract(self.assistant, "import", chunk, topic_list)
            self.done += 1
            if got is None:
                self.failed += 1
                self.error = self.assistant.last_error
            else:
                found.extend(got)
            if progress:
                progress(self.done, len(todo))
        items = [transfer.ImportItem(content=c.content, topic=c.topic, kind=c.kind,
                                     created_at=(c.at or "")[:10] or None, hint=c.conversation[:80] or None,
                                     by="assistant", evidence=c.evidence, flag="sensitive" if c.sensitive else None)
                 for c in unique(found)]
        items = transfer.plan(self.vault, items)
        for it in items:
            if it.flag == "sensitive":
                it.include = False  # shown unticked: the person decides
        return items
