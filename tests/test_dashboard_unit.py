"""Unit tests for the TarkaMCP dashboard module.

Run with::

    pytest tests/test_dashboard_unit.py -v
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tarkamcp.dashboard.db import Database
from tarkamcp.dashboard.session import (
    SESSION_TTL_SECONDS,
    SessionStore,
    load_session_key,
)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "dashboard.db")


@pytest.fixture()
def store(db):
    key = os.urandom(32)
    return SessionStore(db, key=key)


# ---------------------------------------------------------------------------
# load_session_key
# ---------------------------------------------------------------------------

def test_load_session_key_missing(monkeypatch):
    monkeypatch.delenv("TARKAMCP_SESSION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TARKAMCP_SESSION_KEY"):
        load_session_key()


def test_load_session_key_invalid_base64(monkeypatch):
    monkeypatch.setenv("TARKAMCP_SESSION_KEY", "not!!!base64@@@")
    with pytest.raises(RuntimeError, match="not valid base64"):
        load_session_key()


def test_load_session_key_wrong_length(monkeypatch):
    import base64

    monkeypatch.setenv("TARKAMCP_SESSION_KEY", base64.b64encode(b"too short").decode())
    with pytest.raises(RuntimeError, match="32 bytes"):
        load_session_key()


def test_load_session_key_ok(monkeypatch):
    import base64

    raw = os.urandom(32)
    monkeypatch.setenv("TARKAMCP_SESSION_KEY", base64.b64encode(raw).decode())
    assert load_session_key() == raw


# ---------------------------------------------------------------------------
# SessionStore lifecycle
# ---------------------------------------------------------------------------

def test_create_and_load(store):
    s = store.create(
        client_id="tarkamcp_abc",
        client_secret="sk_supersecret",
        mcp_bearer="bearer_xyz",
        bearer_ttl_seconds=3600,
        user_agent="pytest",
    )
    assert s.session_id
    assert s.client_id == "tarkamcp_abc"
    assert s.mcp_bearer == "bearer_xyz"
    assert s.bearer_valid()
    assert not s.is_expired()

    loaded = store.load(s.session_id)
    assert loaded is not None
    assert loaded.session_id == s.session_id
    assert loaded.client_id == "tarkamcp_abc"


def test_load_unknown_returns_none(store):
    assert store.load("nope") is None
    assert store.load("") is None


def test_client_secret_round_trip(store):
    s = store.create(
        client_id="c", client_secret="sk_top_secret",
        mcp_bearer="b", bearer_ttl_seconds=60, user_agent=None,
    )
    assert store.get_client_secret(s.session_id) == "sk_top_secret"


def test_client_secret_with_wrong_key_fails(db):
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    s1 = SessionStore(db, key=key1)
    sess = s1.create(
        client_id="c", client_secret="sk_secret",
        mcp_bearer="b", bearer_ttl_seconds=60, user_agent=None,
    )
    s2 = SessionStore(db, key=key2)
    assert s2.get_client_secret(sess.session_id) is None


def test_update_bearer(store):
    s = store.create(
        client_id="c", client_secret="sk", mcp_bearer="old",
        bearer_ttl_seconds=10, user_agent=None,
    )
    store.update_bearer(s.session_id, mcp_bearer="new", bearer_ttl_seconds=86400)
    loaded = store.load(s.session_id)
    assert loaded.mcp_bearer == "new"
    assert loaded.bearer_valid()


def test_session_expiry(store):
    s = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b",
        bearer_ttl_seconds=60, user_agent=None,
    )
    # Force expiry
    store._db.conn().execute(
        "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
        (time.time() - 1, s.session_id),
    )
    assert store.load(s.session_id) is None
    # Auto-deleted
    row = store._db.conn().execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (s.session_id,),
    ).fetchone()
    assert row is None


def test_bearer_invalid_when_expired(store):
    s = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b",
        bearer_ttl_seconds=10, user_agent=None,
    )
    assert s.bearer_valid()
    s.mcp_bearer_expires_at = time.time() - 1
    assert not s.bearer_valid()


def test_delete_returns_bearer(store):
    s = store.create(
        client_id="c", client_secret="sk", mcp_bearer="bearer_to_revoke",
        bearer_ttl_seconds=60, user_agent=None,
    )
    bearer = store.delete(s.session_id)
    assert bearer == "bearer_to_revoke"
    assert store.load(s.session_id) is None
    assert store.delete(s.session_id) is None


def test_delete_all_for_client(store):
    sessions = [
        store.create(
            client_id="cA", client_secret="sk", mcp_bearer=f"b{i}",
            bearer_ttl_seconds=60, user_agent=None,
        )
        for i in range(3)
    ]
    other = store.create(
        client_id="cB", client_secret="sk", mcp_bearer="b_other",
        bearer_ttl_seconds=60, user_agent=None,
    )
    revoked = store.delete_all_for_client("cA")
    assert sorted(revoked) == sorted([s.mcp_bearer for s in sessions])
    assert store.load(other.session_id) is not None


def test_list_for_client_orders_by_last_seen(store):
    s_old = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b1",
        bearer_ttl_seconds=60, user_agent=None,
    )
    time.sleep(0.01)
    s_new = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b2",
        bearer_ttl_seconds=60, user_agent=None,
    )
    listed = store.list_for_client("c")
    assert [s.session_id for s in listed] == [s_new.session_id, s_old.session_id]


def test_session_ttl_is_90_days():
    assert SESSION_TTL_SECONDS == 90 * 24 * 3600


def test_cleanup_expired(store):
    s_old = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b1",
        bearer_ttl_seconds=60, user_agent=None,
    )
    s_new = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b2",
        bearer_ttl_seconds=60, user_agent=None,
    )
    store._db.conn().execute(
        "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
        (time.time() - 1, s_old.session_id),
    )
    deleted = store.cleanup_expired()
    assert deleted == 1
    assert store.load(s_old.session_id) is None
    assert store.load(s_new.session_id) is not None


def test_migration_v1_to_v2_renames_gemini_models(tmp_path):
    """A DB created under schema v1 with bare model names must be upgraded."""
    import sqlite3

    path = tmp_path / "legacy.db"
    # Hand-build a v1 database that predates the migration.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversations (
          id TEXT PRIMARY KEY, client_id TEXT NOT NULL, title TEXT,
          model TEXT NOT NULL DEFAULT 'gemini-3-flash',
          thinking_effort TEXT NOT NULL DEFAULT 'low',
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE messages (
          id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          role TEXT NOT NULL, content TEXT, tool_calls TEXT,
          thinking_summary TEXT, model TEXT, effort TEXT,
          created_at REAL NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO conversations VALUES ('c1', 'cli', NULL, 'gemini-3-flash', 'low', 0, 0)")
    conn.execute("INSERT INTO conversations VALUES ('c2', 'cli', NULL, 'gemini-3.1-pro', 'medium', 0, 0)")
    conn.execute("INSERT INTO messages VALUES ('m1', 'c1', 'assistant', 'hi', NULL, NULL, 'gemini-3-flash', 'low', 0)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Opening via Database() should run migration v2.
    db = Database(path)
    rows = db.conn().execute(
        "SELECT id, model FROM conversations ORDER BY id"
    ).fetchall()
    assert dict(rows[0]) == {"id": "c1", "model": "gemini-3-flash-preview"}
    assert dict(rows[1]) == {"id": "c2", "model": "gemini-3.1-pro-preview"}
    msg = db.conn().execute("SELECT model FROM messages WHERE id='m1'").fetchone()
    assert msg["model"] == "gemini-3-flash-preview"

    # user_version reflects the migration.
    ver = db.conn().execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2


def test_short_ciphertext_decryption_returns_none(store):
    s = store.create(
        client_id="c", client_secret="sk", mcp_bearer="b",
        bearer_ttl_seconds=60, user_agent=None,
    )
    store._db.conn().execute(
        "UPDATE sessions SET client_secret_enc = ? WHERE session_id = ?",
        (b"xx", s.session_id),
    )
    assert store.get_client_secret(s.session_id) is None
