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


def test_unwrap_exception_single():
    from tarkamcp.dashboard.chat import _unwrap_exception

    err = ValueError("boom")
    assert _unwrap_exception(err) is err


def test_unwrap_exception_simple_group():
    from tarkamcp.dashboard.chat import _unwrap_exception

    inner = RuntimeError("real cause")
    group = ExceptionGroup("task group", [inner])
    assert _unwrap_exception(group) is inner


def test_unwrap_exception_nested_groups():
    from tarkamcp.dashboard.chat import _unwrap_exception

    inner = ConnectionError("network")
    nested = ExceptionGroup("inner", [inner])
    outer = ExceptionGroup("outer", [nested])
    assert _unwrap_exception(outer) is inner


def test_unwrap_exception_prefers_leaf_over_group():
    from tarkamcp.dashboard.chat import _unwrap_exception

    leaf = TypeError("t")
    sibling_group = ExceptionGroup("sibling", [RuntimeError("deep")])
    outer = ExceptionGroup("outer", [sibling_group, leaf])
    assert _unwrap_exception(outer) is leaf


def test_classify_error_preview_model_permission_denied():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception(
        "403 PERMISSION_DENIED. The caller does not have permission"
    )
    code, msg = _classify_error(err, "gemini-3-flash-preview")
    assert code == "model_access_denied"
    assert "gemini-2.5" in msg
    assert "gemini-3-flash-preview" in msg


def test_classify_error_stable_model_permission_denied():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception("403 PERMISSION_DENIED. caller issue")
    code, msg = _classify_error(err, "gemini-2.5-flash")
    assert code == "permission_denied"
    assert "gemini-2.5-flash" in msg


def test_classify_error_model_not_found():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception("404 NOT_FOUND. models/foo is not found")
    code, msg = _classify_error(err, "foo")
    assert code == "model_not_found"


def test_classify_error_rate_limit():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded")
    code, _msg = _classify_error(err, "gemini-2.5-flash")
    assert code == "rate_limited"


def test_classify_error_generic():
    from tarkamcp.dashboard.chat import _classify_error

    err = RuntimeError("boom")
    code, msg = _classify_error(err, "gemini-2.5-flash")
    assert code == "gemini_error"
    assert "RuntimeError" in msg


def test_classify_error_upstream_internal():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception(
        "500 INTERNAL. {'error': {'code': 500, 'message': 'Internal error encountered.', 'status': 'INTERNAL'}}"
    )
    code, msg = _classify_error(err, "gemini-3-flash-preview")
    assert code == "upstream_internal"
    assert "500" in msg
    assert "gemini-3-flash-preview" in msg


def test_classify_error_upstream_unavailable():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception("503 UNAVAILABLE. The service is temporarily unavailable")
    code, _msg = _classify_error(err, "gemini-2.5-flash")
    assert code == "upstream_unavailable"


def test_classify_error_upstream_timeout():
    from tarkamcp.dashboard.chat import _classify_error

    err = Exception("504 DEADLINE_EXCEEDED")
    code, _msg = _classify_error(err, "gemini-2.5-flash")
    assert code == "upstream_timeout"


def test_mcp_tool_to_declaration_passes_input_schema():
    from google.genai import types

    from tarkamcp.dashboard.chat import _mcp_tool_to_declaration

    class FakeMCPTool:
        name = "proxmox_list_vms"
        description = "List VMs on a Proxmox node"
        inputSchema = {
            "type": "object",
            "properties": {"node": {"type": "string"}},
            "required": ["node"],
        }

    decl = _mcp_tool_to_declaration(FakeMCPTool(), types)
    assert decl.name == "proxmox_list_vms"
    assert decl.description == "List VMs on a Proxmox node"
    assert decl.parameters_json_schema["required"] == ["node"]


def test_mcp_tool_to_declaration_defaults_schema_when_missing():
    from google.genai import types

    from tarkamcp.dashboard.chat import _mcp_tool_to_declaration

    class FakeMCPTool:
        name = "ping"
        description = ""
        inputSchema = None

    decl = _mcp_tool_to_declaration(FakeMCPTool(), types)
    # Gemini rejects empty/missing schemas; helper must substitute an
    # empty object schema so the tool still registers.
    assert decl.parameters_json_schema == {"type": "object", "properties": {}}


def test_mcp_call_result_to_response_flattens_text_content():
    from tarkamcp.dashboard.chat import _mcp_call_result_to_response

    class FakeText:
        text = "hello"

    class FakeResult:
        content = [FakeText()]
        isError = False
        structuredContent = None

    payload = _mcp_call_result_to_response(FakeResult())
    assert payload == {"content": [{"type": "text", "text": "hello"}]}


def test_mcp_call_result_to_response_marks_error():
    from tarkamcp.dashboard.chat import _mcp_call_result_to_response

    class FakeText:
        text = "boom"

    class FakeResult:
        content = [FakeText()]
        isError = True
        structuredContent = None

    payload = _mcp_call_result_to_response(FakeResult())
    assert payload["error"] is True


def test_is_transient_error_matches_5xx():
    from tarkamcp.dashboard.chat import _is_transient_error

    assert _is_transient_error(Exception("500 INTERNAL. Internal error"))
    assert _is_transient_error(Exception("503 UNAVAILABLE"))
    assert _is_transient_error(Exception("504 DEADLINE_EXCEEDED"))
    assert not _is_transient_error(Exception("403 PERMISSION_DENIED"))
    assert not _is_transient_error(RuntimeError("boom"))


def _run_retry_scenario(monkeypatch, fake_run):
    """Helper: swap in ``fake_run`` on a GeminiChatEngine and drain events."""
    import asyncio as _asyncio
    from tarkamcp.dashboard import chat as chat_mod
    from tarkamcp.dashboard.chat import GeminiChatEngine, TurnInput

    async def _noop_sleep(_):
        return None

    monkeypatch.setattr(chat_mod.asyncio, "sleep", _noop_sleep)
    engine = GeminiChatEngine(api_key="test")
    engine._run = fake_run  # type: ignore[assignment]
    turn = TurnInput(
        history=[], user_text="x", model="gemini-2.5-flash",
        effort="low", bearer="b", mcp_url="http://localhost/mcp",
    )

    async def _drain():
        return [e async for e in engine.run(turn)]

    return _asyncio.run(_drain())


def test_gemini_retry_recovers_from_transient_500(monkeypatch):
    """run() retries transient 5xx errors before surfacing them."""
    from tarkamcp.dashboard.chat import TextDelta

    attempts = {"n": 0}

    async def fake_run(_turn):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception(
                "500 INTERNAL. {'error': {'code': 500, 'status': 'INTERNAL'}}"
            )
        yield TextDelta(text="ok after retries")

    events = _run_retry_scenario(monkeypatch, fake_run)
    assert attempts["n"] == 3
    assert len(events) == 1
    assert isinstance(events[0], TextDelta)
    assert events[0].text == "ok after retries"


def test_gemini_retry_surfaces_after_max_attempts(monkeypatch):
    from tarkamcp.dashboard.chat import ErrorEvent

    attempts = {"n": 0}

    async def fake_run(_turn):
        attempts["n"] += 1
        raise Exception("500 INTERNAL. persistent")
        yield  # pragma: no cover -- makes fake_run an async generator

    events = _run_retry_scenario(monkeypatch, fake_run)
    # initial attempt + 2 retries = 3 total
    assert attempts["n"] == 3
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "upstream_internal"


def test_gemini_retry_skips_on_non_transient(monkeypatch):
    from tarkamcp.dashboard.chat import ErrorEvent

    attempts = {"n": 0}

    async def fake_run(_turn):
        attempts["n"] += 1
        raise Exception("403 PERMISSION_DENIED. caller")
        yield  # pragma: no cover

    events = _run_retry_scenario(monkeypatch, fake_run)
    assert attempts["n"] == 1  # no retries for 403
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "permission_denied"


def test_thinking_config_for_gemini_3():
    from tarkamcp.dashboard.chat import GeminiChatEngine

    cfg = GeminiChatEngine._build_thinking_config("gemini-3-flash-preview", "high")
    assert cfg.thinking_level is not None
    assert cfg.thinking_budget is None
    assert cfg.include_thoughts is False


def test_thinking_config_for_gemini_2_5():
    from tarkamcp.dashboard.chat import GeminiChatEngine

    cfg = GeminiChatEngine._build_thinking_config("gemini-2.5-flash", "medium")
    assert cfg.thinking_level is None
    assert cfg.thinking_budget == 4096


def test_thinking_config_clamps_gemini_2_5_pro_minimum():
    """2.5 Pro cannot disable thinking; budget must clamp to 128+."""
    from tarkamcp.dashboard.chat import GeminiChatEngine

    cfg = GeminiChatEngine._build_thinking_config("gemini-2.5-pro", "minimal")
    assert cfg.thinking_budget == 128  # clamped up from 0

    # 2.5 Flash has no minimum -- "minimal" means disable thinking.
    cfg_flash = GeminiChatEngine._build_thinking_config("gemini-2.5-flash", "minimal")
    assert cfg_flash.thinking_budget == 0


def test_thinking_config_unknown_effort_defaults_to_low():
    from tarkamcp.dashboard.chat import GeminiChatEngine

    cfg3 = GeminiChatEngine._build_thinking_config("gemini-3.1-pro-preview", "nonsense")
    # Enum repr contains LOW
    assert "LOW" in str(cfg3.thinking_level)
    cfg25 = GeminiChatEngine._build_thinking_config("gemini-2.5-pro", "nonsense")
    assert cfg25.thinking_budget == 1024  # low


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
