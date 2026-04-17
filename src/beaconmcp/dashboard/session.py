"""Server-side session store for the dashboard.

A session record holds the encrypted client_secret and the current MCP
bearer issued via ``client_credentials`` + TOTP. The cookie carries only
the opaque session_id; everything else lives in SQLite encrypted at rest
with AES-256-GCM, keyed by the env var ``BEACONMCP_SESSION_KEY``.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import Database


SESSION_TTL_SECONDS = 90 * 24 * 3600  # 90 days


def load_session_key() -> bytes:
    """Load the AES master key from the env, returning 32 raw bytes.

    Raises ``RuntimeError`` if the env var is missing or malformed. We
    refuse to start with no key rather than auto-generate a fresh one,
    because that would silently invalidate every existing session.
    """
    raw = os.environ.get("BEACONMCP_SESSION_KEY", "").strip()
    if not raw:
        raise RuntimeError(
            "BEACONMCP_SESSION_KEY is required to enable the dashboard. "
            "Run `openssl rand -base64 32` and set it in /opt/beaconmcp/.env."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"BEACONMCP_SESSION_KEY is not valid base64: {e}"
        ) from e
    if len(key) != 32:
        raise RuntimeError(
            f"BEACONMCP_SESSION_KEY must decode to 32 bytes, got {len(key)}."
        )
    return key


@dataclass
class Session:
    session_id: str
    client_id: str
    mcp_bearer: Optional[str]
    mcp_bearer_expires_at: Optional[float]
    created_at: float
    last_seen_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def bearer_valid(self, now: float | None = None) -> bool:
        if not self.mcp_bearer or not self.mcp_bearer_expires_at:
            return False
        return (now or time.time()) < self.mcp_bearer_expires_at


class SessionStore:
    """SQLite-backed session store with encrypted client_secret payload."""

    def __init__(self, db: Database, key: bytes | None = None) -> None:
        self._db = db
        self._key = key if key is not None else load_session_key()
        if len(self._key) != 32:
            raise ValueError("session key must be 32 bytes")
        self._aes = AESGCM(self._key)

    # --- encryption helpers ------------------------------------------------

    def _encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ct

    def _decrypt(self, blob: bytes) -> str:
        if len(blob) < 12 + 16:  # nonce + at least the GCM tag
            raise ValueError("session ciphertext too short")
        nonce, ct = blob[:12], blob[12:]
        return self._aes.decrypt(nonce, ct, None).decode("utf-8")

    # --- public API --------------------------------------------------------

    def create(
        self,
        *,
        client_id: str,
        client_secret: str,
        mcp_bearer: str,
        bearer_ttl_seconds: int,
        user_agent: str | None,
    ) -> Session:
        """Create and persist a new session, returning it."""
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        bearer_expires = now + bearer_ttl_seconds
        expires = now + SESSION_TTL_SECONDS
        enc = self._encrypt(client_secret)

        self._db.conn().execute(
            """
            INSERT INTO sessions (
              session_id, client_id, client_secret_enc,
              mcp_bearer, mcp_bearer_expires_at,
              created_at, last_seen_at, expires_at, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                client_id,
                enc,
                mcp_bearer,
                bearer_expires,
                now,
                now,
                expires,
                user_agent,
            ),
        )

        return Session(
            session_id=session_id,
            client_id=client_id,
            mcp_bearer=mcp_bearer,
            mcp_bearer_expires_at=bearer_expires,
            created_at=now,
            last_seen_at=now,
            expires_at=expires,
        )

    def load(self, session_id: str) -> Session | None:
        if not session_id:
            return None
        row = self._db.conn().execute(
            """
            SELECT session_id, client_id, mcp_bearer, mcp_bearer_expires_at,
                   created_at, last_seen_at, expires_at
              FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        session = Session(
            session_id=row["session_id"],
            client_id=row["client_id"],
            mcp_bearer=row["mcp_bearer"],
            mcp_bearer_expires_at=row["mcp_bearer_expires_at"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
        )
        if session.is_expired():
            self.delete(session_id)
            return None
        return session

    def get_client_secret(self, session_id: str) -> str | None:
        row = self._db.conn().execute(
            "SELECT client_secret_enc FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return self._decrypt(row["client_secret_enc"])
        except Exception:  # noqa: BLE001
            # Wrong key, corrupted blob, or anything else: pretend it does
            # not exist so callers force a fresh login.
            return None

    def update_bearer(
        self, session_id: str, *, mcp_bearer: str, bearer_ttl_seconds: int
    ) -> None:
        self._db.conn().execute(
            """
            UPDATE sessions
               SET mcp_bearer = ?, mcp_bearer_expires_at = ?, last_seen_at = ?
             WHERE session_id = ?
            """,
            (mcp_bearer, time.time() + bearer_ttl_seconds, time.time(), session_id),
        )

    def touch(self, session_id: str) -> None:
        self._db.conn().execute(
            "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )

    def delete(self, session_id: str) -> str | None:
        """Delete a session. Returns the bearer that was attached, if any."""
        row = self._db.conn().execute(
            "SELECT mcp_bearer FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        self._db.conn().execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        return row["mcp_bearer"]

    def delete_all_for_client(self, client_id: str) -> list[str]:
        """Delete every session for a client_id. Returns the revoked bearers."""
        bearers = [
            row["mcp_bearer"]
            for row in self._db.conn().execute(
                "SELECT mcp_bearer FROM sessions WHERE client_id = ?",
                (client_id,),
            ).fetchall()
            if row["mcp_bearer"]
        ]
        self._db.conn().execute(
            "DELETE FROM sessions WHERE client_id = ?", (client_id,)
        )
        return bearers

    def list_for_client(self, client_id: str) -> list[Session]:
        rows = self._db.conn().execute(
            """
            SELECT session_id, client_id, mcp_bearer, mcp_bearer_expires_at,
                   created_at, last_seen_at, expires_at
              FROM sessions WHERE client_id = ?
              ORDER BY last_seen_at DESC
            """,
            (client_id,),
        ).fetchall()
        return [
            Session(
                session_id=r["session_id"],
                client_id=r["client_id"],
                mcp_bearer=r["mcp_bearer"],
                mcp_bearer_expires_at=r["mcp_bearer_expires_at"],
                created_at=r["created_at"],
                last_seen_at=r["last_seen_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    def cleanup_expired(self) -> int:
        cur = self._db.conn().execute(
            "DELETE FROM sessions WHERE expires_at < ?", (time.time(),)
        )
        return cur.rowcount or 0
