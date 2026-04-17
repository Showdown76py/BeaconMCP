"""Bootstrap slugs for OAuth Dynamic Client Registration.

ChatGPT (and other MCP clients that hard-wire RFC 7591 DCR) cannot accept
a pre-shared bearer or client_id. This module mints short-lived, single-
use "connector slugs": the dashboard hands the human a one-off URL of the
shape ``https://<host>/mcp/c/<slug>``; when the MCP client discovers the
OAuth metadata under that path, it is pointed at a slug-scoped register
endpoint that consumes the slug atomically and provisions a dynamic
client bound to the human who minted the slug.

The slug itself carries no authority — stealing one only lets an attacker
register a dynamic client, which they still cannot authorize without the
owner's TOTP. The slug's single-use + short-TTL contract is enforced by a
single ``UPDATE ... WHERE used_at IS NULL`` in SQLite so concurrent claims
deterministically have one winner.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database


SLUG_TTL_SECONDS = 15 * 60  # 15 min - long enough to copy-paste + DCR, short enough to rot fast.


@dataclass
class DynamicSlug:
    slug: str
    label: str
    owner_client_id: str
    created_at: float
    expires_at: float
    used_at: float | None
    resulting_client_id: str | None


class SlugAlreadyConsumed(Exception):
    """Raised when a slug is re-used or expired at claim time."""


class DynamicSlugStore:
    """Thin wrapper around the ``oauth_dynamic_slugs`` table."""

    TTL_SECONDS = SLUG_TTL_SECONDS

    def __init__(self, database: "Database") -> None:
        self._db = database

    # --- mint / list / revoke (dashboard side) ---------------------------

    def mint(self, *, owner_client_id: str, label: str) -> DynamicSlug:
        """Create and persist a fresh slug for ``owner_client_id``.

        Returns the new row. The slug is a URL-safe token with 32 bytes
        of entropy — enough that enumeration is hopeless even if an
        attacker can probe the server.
        """
        slug = secrets.token_urlsafe(24)
        now = time.time()
        row = DynamicSlug(
            slug=slug,
            label=label,
            owner_client_id=owner_client_id,
            created_at=now,
            expires_at=now + self.TTL_SECONDS,
            used_at=None,
            resulting_client_id=None,
        )
        self._db.conn().execute(
            "INSERT INTO oauth_dynamic_slugs "
            "(slug, label, owner_client_id, created_at, expires_at, "
            " used_at, resulting_client_id) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            (row.slug, row.label, row.owner_client_id, row.created_at, row.expires_at),
        )
        return row

    def list_for_owner(self, owner_client_id: str) -> list[DynamicSlug]:
        cur = self._db.conn().execute(
            "SELECT slug, label, owner_client_id, created_at, expires_at, "
            "       used_at, resulting_client_id "
            "FROM oauth_dynamic_slugs "
            "WHERE owner_client_id = ? "
            "ORDER BY created_at DESC "
            "LIMIT 50",
            (owner_client_id,),
        )
        return [DynamicSlug(**dict(r)) for r in cur.fetchall()]

    def delete_unused(self, slug: str, owner_client_id: str) -> bool:
        """Remove a slug that was never consumed (user gave up). Returns True
        on success. Consumed slugs survive as an audit trail; revoke the
        derived client instead if you want to break access."""
        cur = self._db.conn().execute(
            "DELETE FROM oauth_dynamic_slugs "
            "WHERE slug = ? AND owner_client_id = ? AND used_at IS NULL",
            (slug, owner_client_id),
        )
        return cur.rowcount > 0

    def prune_expired(self) -> int:
        """Drop unused, expired rows. Returns the number removed."""
        cur = self._db.conn().execute(
            "DELETE FROM oauth_dynamic_slugs "
            "WHERE used_at IS NULL AND expires_at < ?",
            (time.time(),),
        )
        return cur.rowcount

    # --- DCR path (OAuth side) -------------------------------------------

    def load(self, slug: str) -> DynamicSlug | None:
        cur = self._db.conn().execute(
            "SELECT slug, label, owner_client_id, created_at, expires_at, "
            "       used_at, resulting_client_id "
            "FROM oauth_dynamic_slugs WHERE slug = ?",
            (slug,),
        )
        row = cur.fetchone()
        return DynamicSlug(**dict(row)) if row else None

    def consume(self, slug: str, resulting_client_id: str) -> DynamicSlug:
        """Atomically claim a slug and bind it to a freshly-registered client.

        Uses ``UPDATE ... WHERE used_at IS NULL AND expires_at > now``
        so exactly one concurrent caller wins. Raises
        :class:`SlugAlreadyConsumed` if the row is missing, expired, or
        already claimed.
        """
        now = time.time()
        conn = self._db.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE oauth_dynamic_slugs "
                "SET used_at = ?, resulting_client_id = ? "
                "WHERE slug = ? AND used_at IS NULL AND expires_at > ?",
                (now, resulting_client_id, slug, now),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise SlugAlreadyConsumed(slug)
            conn.execute("COMMIT")
        except Exception:
            # BEGIN IMMEDIATE may have left us in a transaction on error
            # paths other than the ROLLBACK above (e.g. a SQL error).
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        loaded = self.load(slug)
        assert loaded is not None  # we just wrote it
        return loaded

    # --- lookup by resulting client (for /mcp/c/<slug> path rewrite) ------

    def find_by_client(self, client_id: str) -> DynamicSlug | None:
        cur = self._db.conn().execute(
            "SELECT slug, label, owner_client_id, created_at, expires_at, "
            "       used_at, resulting_client_id "
            "FROM oauth_dynamic_slugs WHERE resulting_client_id = ?",
            (client_id,),
        )
        row = cur.fetchone()
        return DynamicSlug(**dict(row)) if row else None
