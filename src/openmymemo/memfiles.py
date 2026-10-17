"""Find the memory and instructions files other AI apps keep on disk, for import (``transfer``).

``find(path)`` takes a file or a folder. For a folder it looks at the files directly in it and at the places
the apps keep theirs, relative to it, so the home folder finds the apps' global files and a project folder
finds that project's. It never walks a whole tree, follows no more than the known patterns, and only reads
text files up to ``MAX_BYTES``.
"""

from __future__ import annotations

import glob
import os
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_BYTES = 1_000_000
MAX_FILES = 200

# File names (lowercase) and the source they import as. Each app's own file, then the generic ones.
NAMES = {
    "claude.md": "claude-code", "claude.local.md": "claude-code",
    "agents.md": "codex",
    "gemini.md": "gemini-cli",
    ".cursorrules": "cursor",
    ".windsurfrules": "windsurf", "global_rules.md": "windsurf",
    ".clinerules": "cline",
    "copilot-instructions.md": "copilot",
    "memory.md": "file", "memories.md": "file", "memory.txt": "file", "memories.txt": "file",
    "memory.json": "json", "memories.json": "json",
}

# Where the apps keep their files, relative to the folder chosen (globs; the home folder for global ones).
PLACES = (
    (".claude/CLAUDE.md", "claude-code"),
    (".claude/projects/*/memory/*.md", "claude-code"),  # Claude Code's auto memory: MEMORY.md and its topic files
    ("memory/*.md", "claude-code"),  # the same folder, chosen itself
    (".codex/AGENTS.md", "codex"),
    (".gemini/GEMINI.md", "gemini-cli"),
    (".cursor/rules/*.mdc", "cursor"), (".cursor/rules/*.md", "cursor"),
    (".codeium/windsurf/memories/global_rules.md", "windsurf"), (".windsurf/rules/*.md", "windsurf"),
    (".clinerules/*.md", "cline"), ("memory-bank/*.md", "cline"), ("Documents/Cline/Rules/*.md", "cline"),
    (".github/copilot-instructions.md", "copilot"), (".github/instructions/*.instructions.md", "copilot"),
    (".kiro/steering/*.md", "kiro"),
)

LABELS = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "cursor": "Cursor",
          "windsurf": "Windsurf", "cline": "Cline", "copilot": "GitHub Copilot", "kiro": "Kiro",
          "file": "Memory file", "json": "JSON"}


@dataclass
class Found:
    path: str
    name: str
    source: str  # a transfer source
    app: str  # who keeps it, for people
    size: int
    modified: float

    def to_dict(self) -> dict:
        return asdict(self)


def detect(path: Path) -> str | None:
    """The import source of a memory file, from its name and the folder it's in; None for anything else."""
    name = path.name.lower()
    if name in NAMES:
        return NAMES[name]
    parts = [p.lower() for p in path.parts[:-1]]
    if name.endswith(".mdc") and ".cursor" in parts:
        return "cursor"
    if not name.endswith((".md", ".txt")):
        return None
    if parts[-2:] == [".kiro", "steering"]:
        return "kiro"
    if parts[-2:] == [".windsurf", "rules"] or parts[-1:] == [".clinerules"] or parts[-1:] == ["memory-bank"]:
        return "windsurf" if ".windsurf" in parts[-2:] else "cline"
    if parts[-2:] == [".cursor", "rules"]:
        return "cursor"
    if name.endswith(".instructions.md") and parts[-2:] == [".github", "instructions"]:
        return "copilot"
    if parts[-1:] in (["memory"], ["memories"]):
        return "claude-code" if ".claude" in parts else "file"
    return None


def _found(path: Path, source: str) -> Found | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file() or st.st_size == 0 or st.st_size > MAX_BYTES:
        return None
    return Found(str(path), path.name, source, LABELS[source], st.st_size, st.st_mtime)


def find(root: Path, *, skip: Path | None = None) -> list[Found]:
    """Memory files at ``root`` (a file) or in it (a folder), newest first. ``skip``: a folder to leave out
    (the vault itself)."""
    root = root.expanduser()
    if root.is_file():
        source = detect(root) or ("file" if root.suffix.lower() in (".md", ".txt") else None)
        hit = _found(root, source) if source else None
        return [hit] if hit else []
    if not root.is_dir():
        raise FileNotFoundError(f"{root} doesn't exist.")
    paths: dict[str, str] = {}
    try:
        for entry in os.scandir(root):
            if entry.is_file() and entry.name.lower() in NAMES:
                paths.setdefault(entry.path, NAMES[entry.name.lower()])
    except OSError:
        pass
    for pattern, source in PLACES:
        for p in sorted(glob.glob(str(root / pattern)))[:MAX_FILES]:
            paths.setdefault(p, source)
    skipped = skip.resolve() if skip else None
    out = []
    seen: set[Path] = set()
    for p, source in paths.items():
        real = Path(p).resolve()
        if real in seen or (skipped and (real == skipped or skipped in real.parents)):
            continue
        seen.add(real)
        if hit := _found(Path(p), source):
            out.append(hit)
    out.sort(key=lambda f: -f.modified)
    return out[:MAX_FILES]


def read(path: Path) -> tuple[str, str]:
    """``(source, text)`` of one memory file. Refuses anything ``find`` wouldn't offer: unknown kinds of file,
    folders, empty or oversized files, and text that isn't UTF-8."""
    hits = find(path)
    if not hits or Path(hits[0].path) != path.expanduser():
        raise ValueError(f"{path} isn't a memory file OpenMyMemo can import.")
    try:
        text = path.expanduser().read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{path} isn't UTF-8 text.") from None
    return hits[0].source, text
