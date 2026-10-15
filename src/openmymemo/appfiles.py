"""Writing other apps' files: their MCP configs (clients.py) and Codex's hooks.json (codex.py).

Like ``vault._atomic_write`` a file is replaced through a temporary file and a rename, so the app never reads
half a file. Because these files are the person's and not ours, a replacement also keeps the file's
permissions from its first byte (some hold API keys), writes through a symlink to the real file (dotfile
repositories), and the file as it was before OpenMyMemo first changed it is kept next to it.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import stat
from pathlib import Path

BACKUP_SUFFIX = ".openmymemo-backup"


def replace(path: Path, text: str) -> None:
    """Atomically replace a file, keeping its permissions; a symlink keeps pointing at the real file."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = None
    data = text.encode("utf-8")
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    # Born with the file's own permissions (private for a new file): a config holding API keys is never
    # readable by others, not even for a moment.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as f:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)  # exactly, whatever the umask
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _make_private(path: Path) -> None:
    """chmod 0600 a regular file that others may read. Opened with O_NOFOLLOW, so a symlink (which isn't ours)
    is never followed, not even one swapped in meanwhile. Windows has no such modes: nothing to do there."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fchmod"):
        return
    with contextlib.suppress(OSError):  # not ours, gone, or a symlink: left as it is
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        try:
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o077:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def backup(path: Path) -> None:
    """Keep the file as it was before OpenMyMemo first changed it, once: ``<file>.openmymemo-backup``.

    Written private to a temporary file and renamed into place only when complete, so the copy of a file
    holding API keys is never readable by others, and a copy cut short is never taken for the backup. A copy
    left by an older version, which took the original's permissions, is made private here too."""
    copy = path.with_name(path.name + BACKUP_SUFFIX)
    if copy.exists() or copy.is_symlink():
        _make_private(copy)
        return
    if not path.exists():
        return
    source = path.resolve()
    tmp = copy.with_name(f".{copy.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f, open(source, "rb") as src:
            shutil.copyfileobj(src, f)
            f.flush()
            os.fsync(f.fileno())
        shutil.copystat(source, tmp)  # the original's times, now that it is all there
        os.chmod(tmp, 0o600)  # but readable by this user only, whatever the original allows (PRIVACY.md)
        os.replace(tmp, copy)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
