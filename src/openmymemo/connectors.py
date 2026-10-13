"""Connectors: the AI apps allowed to read the vault and propose changes (``connectors.yaml``).

Each connector has a read scope (topics, private or not), a write mode and topic blocks:

* ``write: none``    read only
* ``write: propose`` (default) changes follow the vault's rules: additions and corrections of memories an
                     AI wrote apply at once and show up in the user's digest; changes to memories the user
                     wrote and anything in a private topic wait for them; with "require approval" on,
                     every change waits
* ``write: auto``    every change applies at once, even to memories the user wrote and with approval on;
                     private topics still wait

Every change that applies stays undoable for as long as it is the memory's latest version.

stdio connectors are identified by ``openmymemo mcp --connector <id>``; HTTP connectors by a bearer
token. Only the SHA-256 of a token is stored. ``cloud`` connectors (``cloud-chatgpt``, ``cloud-claude``, …) are
web apps that reach the folder through MyMemo Cloud (``cloud.py``); like stdio apps they have no token and are
created on first use.

Removing a connector leaves a ``revoked`` tombstone. A stdio app keeps launching ``openmymemo mcp
--connector <id>`` after the user removes it, and without the tombstone its next call would quietly
re-register it. Adding a connector with the same id again is an explicit re-grant.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass, field

import yaml

from .vault import NotFound, Vault, VaultError, _atomic_write, now_iso, slugify

WRITE_MODES = ("none", "propose", "auto")
KINDS = ("stdio", "http", "cloud")


class Revoked(VaultError):
    pass


@dataclass
class Connector:
    id: str
    name: str
    kind: str = "stdio"  # stdio | http | cloud
    read_topics: list[str] | str = "all"
    include_private: bool = False
    write: str = "propose"
    blocked_topics: list[str] = field(default_factory=list)
    token_hash: str | None = None
    token_preview: str | None = None
    created_at: str = ""
    revoked_at: str | None = None

    def can_read_topic(self, topic: str, private_topics: set[str]) -> bool:
        if topic in private_topics and not self.include_private:
            return False
        return self.read_topics == "all" or topic in self.read_topics

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("token_hash")
        d["has_token"] = self.token_hash is not None
        return d

    def to_yaml(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "read": {"topics": self.read_topics, "include_private": self.include_private},
            "write": self.write, "blocked_topics": list(self.blocked_topics),
            "token_hash": self.token_hash, "token_preview": self.token_preview, "created_at": self.created_at,
            **({"revoked_at": self.revoked_at} if self.revoked_at else {}),
        }

    @classmethod
    def from_yaml(cls, d: dict) -> Connector:
        read = d.get("read") or {}
        return cls(
            id=d["id"], name=d.get("name") or d["id"], kind=d.get("kind", "stdio"),
            read_topics=read.get("topics", "all"), include_private=bool(read.get("include_private", False)),
            write=d.get("write", "propose"), blocked_topics=list(d.get("blocked_topics") or []),
            token_hash=d.get("token_hash"), token_preview=d.get("token_preview"), created_at=d.get("created_at", ""),
            revoked_at=d.get("revoked_at"),
        )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Connectors:
    def __init__(self, vault: Vault):
        self.vault = vault

    def _load(self) -> list[Connector]:
        try:
            data = yaml.safe_load(self.vault.connectors_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            data = {}
        return [Connector.from_yaml(d) for d in data.get("connectors") or []]

    def _save(self, items: list[Connector]) -> None:
        data = {"format": 1, "connectors": [c.to_yaml() for c in items]}
        _atomic_write(self.vault.connectors_path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def list(self) -> list[Connector]:
        return [c for c in self._load() if not c.revoked_at]

    def get(self, connector_id: str) -> Connector:
        for c in self.list():
            if c.id == connector_id:
                return c
        raise NotFound(f"No connector {connector_id}")

    def by_token(self, token: str) -> Connector | None:
        digest = hash_token(token)
        for c in self.list():
            if c.token_hash and secrets.compare_digest(c.token_hash, digest):
                return c
        return None

    def create(self, name: str, kind: str = "stdio", *, connector_id: str | None = None, write: str = "propose",
               read_topics: list[str] | str = "all", include_private: bool = False) -> tuple[Connector, str | None]:
        if kind not in KINDS:
            raise VaultError("kind must be stdio, http or cloud")
        if write not in WRITE_MODES:
            raise VaultError(f"write must be one of {', '.join(WRITE_MODES)}")
        with self.vault.lock():
            cid = connector_id or slugify(name)
            items = [c for c in self._load() if not (c.id == cid and c.revoked_at)]  # re-grant replaces the tombstone
            if any(c.id == cid for c in items):
                raise VaultError(f"Connector {cid} already exists")
            c = Connector(id=cid, name=name, kind=kind, write=write, read_topics=read_topics,
                          include_private=include_private, created_at=now_iso())
            token = None
            if kind == "http":
                token = self._new_token(c)
            items.append(c)
            self._save(items)
        return c, token

    def ensure(self, connector_id: str, name: str | None = None, kind: str = "stdio") -> Connector:
        """Get a connector without a token, creating it with default permissions on first use (never after
        revocation)."""

        def existing() -> Connector | None:
            for c in self._load():
                if c.id == connector_id:
                    if c.revoked_at:
                        raise Revoked("The user revoked this app's access in OpenMyMemo.")
                    return c
            return None

        c = existing()
        if c is None:
            try:
                c, _ = self.create(name or connector_id, kind, connector_id=connector_id)
            except VaultError:
                c = existing()  # a concurrent first call created it
                if c is None:
                    raise
        return c

    def update(self, connector_id: str, **changes) -> Connector:
        with self.vault.lock():
            items = self._load()
            c = next((x for x in items if x.id == connector_id and not x.revoked_at), None)
            if c is None:
                raise NotFound(f"No connector {connector_id}")
            if "write" in changes and changes["write"] is not None:
                if changes["write"] not in WRITE_MODES:
                    raise VaultError(f"write must be one of {', '.join(WRITE_MODES)}")
                c.write = changes["write"]
            if changes.get("name"):
                c.name = changes["name"]
            if changes.get("read_topics") is not None:
                rt = changes["read_topics"]
                c.read_topics = "all" if rt == "all" else list(rt)
            if changes.get("include_private") is not None:
                c.include_private = bool(changes["include_private"])
            if changes.get("blocked_topics") is not None:
                c.blocked_topics = sorted(set(changes["blocked_topics"]))
            self._save(items)
            return c

    def block_topic(self, connector_id: str, topic: str) -> Connector:
        c = self.get(connector_id)
        return self.update(connector_id, blocked_topics=[*c.blocked_topics, topic])

    def rotate(self, connector_id: str) -> tuple[Connector, str]:
        with self.vault.lock():
            items = self._load()
            c = next((x for x in items if x.id == connector_id and not x.revoked_at), None)
            if c is None:
                raise NotFound(f"No connector {connector_id}")
            if c.kind == "cloud":
                raise VaultError("A MyMemo Cloud app signs in with your MyMemo account; it has no token")
            token = self._new_token(c)
            if c.kind == "stdio":
                c.kind = "http"
            self._save(items)
            return c, token

    def remove(self, connector_id: str) -> None:
        """Revoke access: keep a tombstone so a stdio app can't silently re-register itself."""
        with self.vault.lock():
            items = self._load()
            c = next((x for x in items if x.id == connector_id and not x.revoked_at), None)
            if c is None:
                raise NotFound(f"No connector {connector_id}")
            c.revoked_at = now_iso()
            c.token_hash = c.token_preview = None
            self._save(items)

    def revoke(self, connector_id: str, name: str) -> None:
        """Turn an app away from now on, whether or not it has registered yet (an installed plugin, a pasted
        config): a live connector becomes a tombstone, and an app never seen gets one."""
        with self.vault.lock():
            items = self._load()
            c = next((x for x in items if x.id == connector_id), None)
            if c is None:
                c = Connector(id=connector_id, name=name, created_at=now_iso())
                items.append(c)
            elif c.revoked_at:
                return
            c.revoked_at = now_iso()
            c.token_hash = c.token_preview = None
            self._save(items)

    @staticmethod
    def _new_token(c: Connector) -> str:
        token = "omm_" + secrets.token_urlsafe(24)
        c.token_hash = hash_token(token)
        c.token_preview = token[:10] + "…"
        return token
