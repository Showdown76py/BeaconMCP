"""SQLite connection helper and schema migrations for the dashboard.

One database holds everything dashboard-related: sessions, conversations,
messages. WAL mode for concurrent reads while a chat stream writes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DEFAULT_DB_PATH = Path("/opt/beaconmcp/dashboard.db")


def db_path() -> Path:
    override = os.environ.get("BEACONMCP_DASHBOARD_DB")
    return Path(override) if override else DEFAULT_DB_PATH


_LATEST_VERSION = 4


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    version = cur.fetchone()[0]
    if version >= _LATEST_VERSION:
        return

    if version < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              session_id            TEXT PRIMARY KEY,
              client_id             TEXT NOT NULL,
              client_secret_enc     BLOB NOT NULL,
              mcp_bearer            TEXT,
              mcp_bearer_expires_at REAL,
              created_at            REAL NOT NULL,
              last_seen_at          REAL NOT NULL,
              expires_at            REAL NOT NULL,
              user_agent            TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_client
              ON sessions(client_id);

            CREATE TABLE IF NOT EXISTS conversations (
              id              TEXT PRIMARY KEY,
              client_id       TEXT NOT NULL,
              title           TEXT,
              model           TEXT NOT NULL DEFAULT 'gemini-3-flash-preview',
              thinking_effort TEXT NOT NULL DEFAULT 'low',
              created_at      REAL NOT NULL,
              updated_at      REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_client
              ON conversations(client_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS messages (
              id               TEXT PRIMARY KEY,
              conversation_id  TEXT NOT NULL
                                 REFERENCES conversations(id) ON DELETE CASCADE,
              role             TEXT NOT NULL,
              content          TEXT,
              tool_calls       TEXT,
              thinking_summary TEXT,
              model            TEXT,
              effort           TEXT,
              created_at       REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv
              ON messages(conversation_id, created_at);
            """
        )

    if version < 2:
        # Google's real Gemini 3 model IDs are suffixed `-preview` at the
        # time of writing. The initial schema shipped without the suffix;
        # migrate existing rows so stored conversations/messages keep
        # resolving to a real model the API accepts.
        conn.execute(
            "UPDATE conversations SET model = 'gemini-3-flash-preview' "
            "WHERE model = 'gemini-3-flash'"
        )
        conn.execute(
            "UPDATE conversations SET model = 'gemini-3.1-pro-preview' "
            "WHERE model = 'gemini-3.1-pro'"
        )
        conn.execute(
            "UPDATE messages SET model = 'gemini-3-flash-preview' "
            "WHERE model = 'gemini-3-flash'"
        )
        conn.execute(
            "UPDATE messages SET model = 'gemini-3.1-pro-preview' "
            "WHERE model = 'gemini-3.1-pro'"
        )

    if version < 3:
        # Usage accounting: a per-turn ledger + a materialized 5h session
        # row per client. The ledger is the source of truth (used for the
        # rolling 7-day window); the session row avoids a GROUP BY scan
        # on every pre-turn budget check.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
              id               TEXT PRIMARY KEY,
              client_id        TEXT NOT NULL,
              conversation_id  TEXT,
              message_id       TEXT,
              ts               REAL NOT NULL,
              model            TEXT NOT NULL,
              prompt_tokens    INTEGER NOT NULL DEFAULT 0,
              cached_tokens    INTEGER NOT NULL DEFAULT 0,
              output_tokens    INTEGER NOT NULL DEFAULT 0,
              cost_usd         REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_usage_client_ts
              ON usage_events(client_id, ts DESC);

            CREATE TABLE IF NOT EXISTS usage_5h_sessions (
              client_id      TEXT PRIMARY KEY,
              started_at     REAL NOT NULL,
              last_event_at  REAL NOT NULL,
              cost_usd       REAL NOT NULL DEFAULT 0
            );
            """
        )

    if version < 4:
        # OAuth Dynamic Client Registration bootstrap slugs: one row per
        # single-use URL minted from the dashboard for a client that needs
        # DCR (e.g. ChatGPT). See ``dashboard.dyn_reg`` for the lifecycle.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_dynamic_slugs (
              slug                 TEXT PRIMARY KEY,
              label                TEXT NOT NULL,
              owner_client_id      TEXT NOT NULL,
              created_at           REAL NOT NULL,
              expires_at           REAL NOT NULL,
              used_at              REAL,
              resulting_client_id  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_oauth_slugs_owner
              ON oauth_dynamic_slugs(owner_client_id, created_at DESC);
            """
        )

    conn.execute(f"PRAGMA user_version = {_LATEST_VERSION}")
    conn.commit()


class Database:
    """Thread-safe SQLite wrapper.

    sqlite3 connections are not safe to share across threads by default. We
    cache one connection per thread; Starlette runs handlers in a thread pool
    by default, so this matches the access pattern.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Run migrations once, on the init thread.
        with self._connect() as conn:
            _migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit; we manage transactions explicitly
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing
