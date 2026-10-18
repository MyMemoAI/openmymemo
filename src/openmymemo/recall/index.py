"""The embedding index: a plain-file, rebuildable cache in ``.openmymemo/index/`` (MEMORY_FORMAT.md §6.8).

* ``manifest.json``: the model (``Embedder.id``), dimensions, the vectors file's name, and one row per memory —
  its id and the hash of the text embedded — in the order of the vectors.
* ``vectors-<token>.f32``: float32, little-endian, ``count × dims``, L2-normalized.
* ``.gitignore`` (``*``): the folder keeps itself out of git; it can be deleted at any time and is rebuilt from
  ``memories/*.md``.

Updates are incremental: a memory whose text hash changed (also after an edit in another editor) is embedded
again, a removed one is dropped. A different model starts from empty, so an index is never mixed across models.
Several processes (the panel, each AI app's server, the CLI) may update it: embedding happens outside the lock,
and the merge and write happen under ``index/.lock``; the vectors file is written under a new name and the
manifest replaced atomically, so a reader sees the old index or the new one, never half of each.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import time
from array import array
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .embed import EmbedError

if TYPE_CHECKING:
    from ..vault import Memory, Vault
    from .embed import Embedder

log = logging.getLogger("openmymemo.recall")

INDEX_DIR = "index"
FORMAT = 1
SAVE_EVERY = 5.0  # seconds between saves while a long build runs
BATCH = 32


def text_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@dataclass
class Snapshot:
    """One consistent read of the index."""

    model: str
    dims: int
    ids: list[str]
    hashes: list[str]
    vectors: array  # float32, len(ids) × dims
    updated_at: str = ""
    row: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.row = {mid: i for i, mid in enumerate(self.ids)}

    def vector(self, memory_id: str) -> list[float] | None:
        i = self.row.get(memory_id)
        return None if i is None else list(self.vectors[i * self.dims:(i + 1) * self.dims])


@dataclass
class Refreshed:
    indexed: int  # memories with a current vector
    total: int  # memories this embedder may embed
    embedded: int  # embedded by this call
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.indexed >= self.total


class EmbeddingIndex:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.dir = vault.state_dir / INDEX_DIR
        self.manifest_path = self.dir / "manifest.json"

    # ---- reading ---------------------------------------------------------------------------------------------

    def load(self) -> Snapshot | None:
        """The index as last written, or None (none yet, unreadable, or written by a newer format)."""
        for _ in range(3):  # the vectors file may be replaced between reading the manifest and opening it
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                return None
            try:
                return self._snapshot(manifest)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError):
                return None
        return None

    def _snapshot(self, manifest: dict) -> Snapshot | None:
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            return None
        name = str(manifest["vectors"])
        if "/" in name or "\\" in name or not name.startswith("vectors-") or not name.endswith(".f32"):
            return None
        dims = int(manifest["dims"])
        items = manifest["items"]
        ids = [str(it["id"]) for it in items]
        hashes = [str(it["hash"]) for it in items]
        vectors = array("f")
        data = (self.dir / name).read_bytes()
        if dims <= 0 or len(data) != len(ids) * dims * 4:
            return None
        vectors.frombytes(data)
        if sys.byteorder == "big":
            vectors.byteswap()
        return Snapshot(model=str(manifest["model"]), dims=dims, ids=ids, hashes=hashes, vectors=vectors,
                        updated_at=str(manifest.get("updated_at") or ""))

    def status(self, embedder: Embedder | None = None) -> dict:
        """For the panel and ``openmymemo semantic status``: the model, how many memories have a vector, and
        whether that matches the embedder in use."""
        snap = self.load()
        out = {"model": snap.model if snap else None, "count": len(snap.ids) if snap else 0,
               "dims": snap.dims if snap else None, "updated_at": snap.updated_at if snap else None}
        if embedder is not None:
            out["current"] = bool(snap and snap.model == embedder.id)
        return out

    # ---- writing ---------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        from ..vault import _flock

        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "a+") as fh:
            _flock(fh, True)
            try:
                yield
            finally:
                _flock(fh, False)

    def _write(self, model: str, dims: int, ids: Sequence[str], hashes: Sequence[str], vectors: array) -> None:
        from ..vault import _atomic_write, now_iso

        self.dir.mkdir(parents=True, exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            _atomic_write(ignore, "# A cache OpenMyMemo rebuilds from memories/*.md: never commit it.\n*\n")
        name = f"vectors-{secrets.token_hex(6)}.f32"
        data = array("f", vectors)
        if sys.byteorder == "big":
            data.byteswap()
        tmp = self.dir / f".{name}.tmp"
        tmp.write_bytes(data.tobytes())
        os.replace(tmp, self.dir / name)
        manifest = {"format": FORMAT, "model": model, "dims": dims, "vectors": name, "count": len(ids),
                    "updated_at": now_iso(), "items": [{"id": i, "hash": h} for i, h in zip(ids, hashes, strict=True)]}
        _atomic_write(self.manifest_path, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n")
        for old in self.dir.glob("vectors-*.f32"):
            if old.name != name:
                old.unlink(missing_ok=True)

    def _merge(self, embedder: Embedder, wanted: dict[str, str], fresh: dict[str, tuple[str, list[float]]]) -> int:
        """Under the lock: the index on disk (if it is this model's) plus ``fresh`` vectors, restricted to
        ``wanted`` (id → hash). Returns how many memories have a current vector."""
        with self._lock():
            snap = self.load()
            if snap is not None and snap.model != embedder.id:
                snap = None
            dims = snap.dims if snap else (len(next(iter(fresh.values()))[1]) if fresh else 0)
            ids, hashes, vectors = [], [], array("f")
            for mid, h in wanted.items():
                got = fresh.get(mid)
                i = snap.row.get(mid) if snap is not None else None
                if got is not None and got[0] == h and len(got[1]) == dims:
                    vec = got[1]
                elif snap is not None and i is not None and snap.hashes[i] == h:
                    vec = snap.vectors[i * dims:(i + 1) * dims]
                else:
                    continue
                ids.append(mid)
                hashes.append(h)
                vectors.extend(vec)
            unchanged = (snap is not None and snap.ids == ids and snap.hashes == hashes)
            if dims and not unchanged:
                self._write(embedder.id, dims, ids, hashes, vectors)
            return len(ids)

    def refresh(self, embedder: Embedder, memories: Sequence[Memory], *, budget: float | None = None,
                progress: Callable[[int, int], None] | None = None) -> Refreshed:
        """Bring the index up to date for ``memories`` (all active memories, normally): embed what is new or
        changed, drop what is gone. ``budget`` (seconds) stops early and leaves the rest for next time; a search
        uses whatever vectors exist. Raises nothing: an embedder failure ends the refresh with ``error`` set."""
        started = time.monotonic()
        wanted = {m.id: text_hash(m.content) for m in memories if m.status == "active" and embedder.may_embed(m)}
        text = {m.id: m.content for m in memories}
        snap = self.load()
        if snap is not None and snap.model != embedder.id:
            snap = None
        have = {mid: snap.hashes[i] for mid, i in snap.row.items()} if snap else {}
        todo = [mid for mid, h in wanted.items() if have.get(mid) != h]
        indexed = sum(1 for mid, h in wanted.items() if have.get(mid) == h)
        stale = snap is None or any(mid not in wanted for mid in have)
        fresh: dict[str, tuple[str, list[float]]] = {}
        embedded, error = 0, None
        last_save = time.monotonic()
        for i in range(0, len(todo), BATCH):
            if budget is not None and time.monotonic() - started > budget:
                break
            batch = todo[i:i + BATCH]
            try:
                vecs = embedder.embed([text[mid] for mid in batch], "passage", memory_ids=batch)
            except EmbedError as e:
                error = str(e)
                break
            if len(vecs) != len(batch):
                error = "The embedder returned the wrong number of vectors."
                break
            for mid, vec in zip(batch, vecs, strict=True):
                fresh[mid] = (wanted[mid], vec)
            embedded += len(batch)
            if progress:
                progress(indexed + embedded, len(wanted))
            if time.monotonic() - last_save > SAVE_EVERY:  # a long build keeps what it has so far
                self._merge(embedder, wanted, fresh)
                fresh, stale, last_save = {}, False, time.monotonic()
        if fresh or stale:
            indexed = self._merge(embedder, wanted, fresh)
        else:
            indexed += embedded
        return Refreshed(indexed=indexed, total=len(wanted), embedded=embedded, error=error)

    def nearest(self, snap: Snapshot, query: Sequence[float], allowed: set[str] | None, k: int,
                min_similarity: float) -> list[tuple[float, str]]:
        """The ``k`` memories closest to ``query`` (cosine; vectors are normalized), among ``allowed`` ids,
        at least ``min_similarity``; best first."""
        if snap.dims != len(query) or not snap.ids:
            return []
        try:
            import numpy as np
        except ImportError:
            np = None
        if np is not None:
            mat = np.frombuffer(snap.vectors, dtype=np.float32).reshape(len(snap.ids), snap.dims)
            sims = (mat @ np.asarray(query, dtype=np.float32)).tolist()
        else:
            q = list(query)
            d = snap.dims
            v = snap.vectors
            sims = [sum(a * b for a, b in zip(q, v[i * d:(i + 1) * d], strict=True)) for i in range(len(snap.ids))]
        scored = [(s, mid) for s, mid in zip(sims, snap.ids, strict=True)
                  if s >= min_similarity and (allowed is None or mid in allowed)]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[:k]

    def clear(self) -> None:
        """Delete the index (turning the local model off). It is a cache: nothing is lost."""
        with contextlib.suppress(OSError):
            shutil.rmtree(self.dir)


def index_path(vault: Vault) -> Path:
    return vault.state_dir / INDEX_DIR
