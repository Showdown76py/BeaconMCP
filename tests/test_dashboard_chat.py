"""Integration tests for Stage 2: conversations + chat stream with FakeChatEngine."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tarkamcp.dashboard.app import BEARER_TTL_SECONDS, DashboardDeps, build_dashboard_routes
from tarkamcp.dashboard.chat import (
    ErrorEvent,
    FakeChatEngine,
    FakeScript,
    TextDelta,
    ThinkingDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolConfirmRequired,
)
from tarkamcp.dashboard.confirmations import ConfirmationStore
from tarkamcp.dashboard.conversations import ConversationStore
from tarkamcp.dashboard.csrf import CSRF_COOKIE
from tarkamcp.dashboard.db import Database
from tarkamcp.dashboard.session import SessionStore


class FakeClientStore:
    def verify(self, cid, sec): return cid == "c" and sec == "s"
    def verify_totp(self, cid, code): return code == "123456"
    def get_name(self, cid): return "Test"


class FakeTokenStore:
    def __init__(self):
        self.revoked = []
        self._n = 0
        self._live: dict[str, str] = {}
    def issue(self, cid):
        self._n += 1
        token = f"b_{self._n}"
        self._live[token] = cid
        return token, BEARER_TTL_SECONDS
    def validate(self, token):
        # Mirrors the real TokenStore: return client_id if the token
        # was issued by this store instance. Tests can subclass to
        # simulate a wiped store (e.g. after a service restart).
        return self._live.get(token)
    def revoke(self, token):
        self.revoked.append(token)
        self._live.pop(token, None)
        return True


@pytest.fixture()
def engine():
    return FakeChatEngine(FakeScript(events=[TextDelta(text="pong")], title_text="Un titre"))


@pytest.fixture()
def deps(tmp_path, engine):
    db = Database(tmp_path / "dashboard.db")
    return DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
        confirmations=ConfirmationStore(),
        mcp_public_url="https://mcp.example/",
    )


@pytest.fixture()
def app_and_client(deps):
    app = Starlette(routes=build_dashboard_routes(deps))
    return app, TestClient(app, follow_redirects=False)


def _login(client):
    r = client.get("/app/login")
    csrf = r.cookies.get(CSRF_COOKIE)
    r = client.post("/app/login", data={
        "csrf_token": csrf, "client_id": "c",
        "client_secret": "s", "totp": "123456", "remember": "on",
    })
    assert r.status_code == 303
    return client.cookies.get(CSRF_COOKIE)


# ---------------------------------------------------------------------------
# Conversations API
# ---------------------------------------------------------------------------

def test_conv_list_empty(app_and_client):
    _, client = app_and_client
    _login(client)
    r = client.get("/app/api/conversations")
    assert r.status_code == 200
    assert r.json() == {"conversations": []}


def test_conv_list_requires_auth(app_and_client):
    _, client = app_and_client
    r = client.get("/app/api/conversations")
    assert r.status_code == 401


def test_conv_create_and_list(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"model": "gemini-3-flash-preview", "effort": "medium"}),
    )
    assert r.status_code == 201
    conv = r.json()["conversation"]
    assert conv["model"] == "gemini-3-flash-preview"
    assert conv["thinking_effort"] == "medium"

    r = client.get("/app/api/conversations")
    assert r.status_code == 200
    assert len(r.json()["conversations"]) == 1


def test_conv_create_csrf(app_and_client):
    _, client = app_and_client
    _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"Content-Type": "application/json"},
        content="{}",
    )
    assert r.status_code == 403


def test_conv_patch(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content="{}",
    )
    cid = r.json()["conversation"]["id"]
    r = client.patch(
        f"/app/api/conversations/{cid}",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"title": "Nouveau titre", "effort": "high"}),
    )
    assert r.status_code == 200
    conv = r.json()["conversation"]
    assert conv["title"] == "Nouveau titre"
    assert conv["thinking_effort"] == "high"


def test_conv_patch_rejects_invalid_effort(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]
    r = client.patch(
        f"/app/api/conversations/{cid}",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"effort": "ULTRA"}),
    )
    assert r.status_code == 200
    assert r.json()["conversation"]["thinking_effort"] == "low"  # unchanged


def test_conv_delete(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]
    r = client.delete(f"/app/api/conversations/{cid}",
                      headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    r = client.get(f"/app/api/conversations/{cid}")
    assert r.status_code == 404


def test_conv_scoped_to_client(deps, app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    # Create a conversation for client c
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    # Another client's conversation -- should not be visible
    other = deps.conversations.create(client_id="otherclient", model="gemini-3-flash", effort="low")

    r = client.get("/app/api/conversations")
    ids = [c["id"] for c in r.json()["conversations"]]
    assert cid in ids
    assert other.id not in ids


# ---------------------------------------------------------------------------
# Chat stream (SSE) with FakeChatEngine
# ---------------------------------------------------------------------------

def _parse_sse(text):
    events = []
    for frame in text.strip().split("\n\n"):
        ev = None
        data = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        payload = json.loads("\n".join(data)) if data else {}
        events.append((ev, payload))
    return events


def test_chat_stream_simple_text(app_and_client, engine, deps):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "ping"}),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    names = [e[0] for e in events]
    assert "text_delta" in names
    assert "done" in names
    assert "title_updated" in names

    text_events = [e[1] for e in events if e[0] == "text_delta"]
    assert text_events[0]["text"] == "pong"

    # Engine saw the call
    assert len(engine.calls) == 1
    turn = engine.calls[0]
    assert turn.user_text == "ping"
    assert turn.bearer.startswith("b_")
    # Local mode (default) ignores mcp_public_url and uses loopback so
    # the dashboard never round-trips through its own reverse proxy.
    assert turn.mcp_url == "http://127.0.0.1:8420/mcp"
    assert turn.history == []

    # Message persisted in DB
    msgs = deps.conversations.list_messages(cid)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "ping"
    assert msgs[1].content == "pong"


def test_chat_stream_tool_call(app_and_client, engine, deps):
    _, client = app_and_client
    engine.script = FakeScript(events=[
        ToolCallStart(id="tc1", name="proxmox_list_vms", args={"node": "pve1"}),
        ToolCallEnd(id="tc1", status="ok", preview="2 VMs", duration_ms=42),
        TextDelta(text="Voici la liste."),
    ])
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "liste"}),
    )
    events = _parse_sse(r.text)
    names = [e[0] for e in events]
    assert "tool_call" in names
    assert "tool_result" in names

    msgs = deps.conversations.list_messages(cid)
    assistant = msgs[1]
    assert len(assistant.tool_calls) == 1
    tc = assistant.tool_calls[0]
    assert tc.name == "proxmox_list_vms"
    assert tc.status == "ok"
    assert tc.preview == "2 VMs"
    assert tc.duration_ms == 42


def test_refresh_page_shows_totp_when_token_wiped(tmp_path, engine):
    """After a restart, /app/refresh must render the TOTP form instead
    of redirecting back to /app/chat and creating a redirect loop.
    """
    class WipedTokenStore(FakeTokenStore):
        def validate(self, token):
            return None  # TokenStore wiped by restart

    db = Database(tmp_path / "dashboard.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=WipedTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)

    _login(client)
    r = client.get("/app/refresh")
    # Must render the TOTP form, NOT 302 back to /app/chat.
    assert r.status_code == 200
    assert "totp" in r.text.lower()


def test_chat_page_redirects_to_refresh_when_token_wiped(tmp_path, engine):
    """/app/chat must send the user to /app/refresh when the bearer
    is wiped, to break the refresh<->chat redirect loop.
    """
    class WipedTokenStore(FakeTokenStore):
        def validate(self, token):
            return None

    db = Database(tmp_path / "dashboard.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=WipedTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)

    _login(client)
    r = client.get("/app/chat")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/refresh"


def test_chat_stream_detects_token_wiped_after_restart(tmp_path, engine):
    """After a service restart TokenStore is empty but the session's
    bearer is still there; we must emit session_expired before reaching
    the MCP server with a stale token.

    We simulate the "restart mid-session" flow: login + create
    conversation succeed while the token is live, then we flip the
    store into its post-restart state and try to stream.
    """
    class SwitchableTokenStore(FakeTokenStore):
        wiped = False
        def validate(self, token):
            if self.wiped:
                return None
            return super().validate(token)

    db = Database(tmp_path / "dashboard.db")
    token_store = SwitchableTokenStore()
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=token_store,
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)

    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    # Simulate `systemctl restart tarkamcp` wiping every issued token.
    token_store.wiped = True

    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "ping"}),
    )
    events = _parse_sse(r.text)
    assert events[0][0] == "session_expired"
    # Engine must NOT have been called with a doomed bearer.
    assert len(engine.calls) == 0


def test_chat_stream_session_expired(app_and_client, deps):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    # Expire the bearer
    sessions = deps.session_store.list_for_client("c")
    deps.session_store._db.conn().execute(
        "UPDATE sessions SET mcp_bearer_expires_at = 0 WHERE session_id = ?",
        (sessions[0].session_id,),
    )

    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "ping"}),
    )
    events = _parse_sse(r.text)
    assert events[0][0] == "session_expired"


def test_chat_stream_invalid_body(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({}),
    )
    assert r.status_code == 400


def test_chat_stream_not_found(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": "nope", "content": "x"}),
    )
    assert r.status_code == 404


def test_chat_stream_error_event(app_and_client, engine):
    _, client = app_and_client
    engine.script = FakeScript(events=[
        TextDelta(text="partial "),
        ErrorEvent(code="boom", message="tool failure"),
    ])
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]
    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "ping"}),
    )
    events = _parse_sse(r.text)
    codes = [e[0] for e in events]
    assert "error" in codes
    # Stream should still emit a done event so client state is coherent.
    assert "done" in codes


def test_chat_persists_history_for_second_turn(app_and_client, engine, deps):
    _, client = app_and_client
    engine.script = FakeScript(events=[TextDelta(text="ack")], title_text="t")
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "first"}),
    )
    client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "second"}),
    )
    # Second turn: history should contain the first user + first assistant.
    history = engine.calls[1].history
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[0].content == "first"
    assert history[1].content == "ack"


def test_chat_stream_ssh_tool_emits_confirm_event(app_and_client, engine, deps):
    """ssh_exec_command triggers a tool_confirm_required SSE frame and the
    engine must wait for a decision via /app/api/chat/confirm.
    """
    import threading, time as _t

    engine.script = FakeScript(events=[
        ToolCallStart(id="tc1", name="ssh_exec_command", args={"host": "pve1", "command": "ls"}),
        ToolConfirmRequired(id="tc1", name="ssh_exec_command", args={"host": "pve1", "command": "ls"}),
        ToolCallEnd(id="tc1", status="ok", preview="ok", duration_ms=50),
        TextDelta(text="done"),
    ])

    _, client = app_and_client
    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]

    # Approve the confirmation from a parallel thread so the SSE stream
    # is free to unblock and finish.
    def approve_when_ready():
        deadline = _t.time() + 5
        while _t.time() < deadline:
            pending = deps.confirmations.pending_for(
                deps.session_store.list_for_client("c")[0].session_id
            )
            if pending:
                client.post(
                    "/app/api/chat/confirm",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content=json.dumps({"call_id": pending[0], "approve": True}),
                )
                return
            _t.sleep(0.05)

    t = threading.Thread(target=approve_when_ready)
    t.start()
    try:
        r = client.post(
            "/app/api/chat/stream",
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
            content=json.dumps({"conversation_id": cid, "content": "run ls"}),
        )
    finally:
        t.join(timeout=5)

    events = _parse_sse(r.text)
    names = [e[0] for e in events]
    assert "tool_confirm_required" in names
    assert "tool_result" in names
    # done must still fire so the client state settles.
    assert "done" in names


def test_confirm_endpoint_requires_csrf(app_and_client):
    _, client = app_and_client
    _login(client)
    r = client.post(
        "/app/api/chat/confirm",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"call_id": "x", "approve": True}),
    )
    assert r.status_code == 403


def test_confirm_endpoint_rejects_unknown_call_id(app_and_client):
    _, client = app_and_client
    csrf = _login(client)
    r = client.post(
        "/app/api/chat/confirm",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"call_id": "nope", "approve": True}),
    )
    assert r.status_code == 404


def test_chat_page_auth_required(app_and_client):
    _, client = app_and_client
    r = client.get("/app/chat")
    assert r.status_code == 302


def test_chat_stream_remote_mode_still_routes_public_url(tmp_path, engine):
    """In remote mode the public URL is still the resolved target.

    The engine itself refuses to drive a remote turn (the SDK's
    backend-driven MCP mode is broken under our auth), but the URL
    resolution logic is independent and continues to honour the
    configured public hostname. That lets us keep remote-mode
    configuration around for a future re-enablement without changing
    the routing layer.
    """
    db = Database(tmp_path / "dashboard.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
        mcp_public_url="https://mcp.example/",
        mcp_mode="remote",
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)

    csrf = _login(client)
    r = client.post("/app/api/conversations",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                    content="{}")
    cid = r.json()["conversation"]["id"]
    client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": cid, "content": "ping"}),
    )
    assert engine.calls[-1].mcp_url == "https://mcp.example/mcp"
    assert engine.calls[-1].mcp_mode == "remote"


def test_gemini_engine_rejects_remote_mode():
    """GeminiChatEngine yields an ErrorEvent instead of calling the SDK."""
    import asyncio
    from tarkamcp.dashboard.chat import (
        ErrorEvent,
        GeminiChatEngine,
        TurnInput,
    )

    engine_real = GeminiChatEngine(api_key="test")
    turn = TurnInput(
        history=[], user_text="x", model="gemini-3-flash-preview",
        effort="low", bearer="b",
        mcp_url="https://mcp.example/mcp", mcp_mode="remote",
    )

    async def _drain():
        return [e async for e in engine_real.run(turn)]

    events = asyncio.run(_drain())
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "remote_mode_disabled"


def test_chat_page_renders_after_login(app_and_client):
    _, client = app_and_client
    _login(client)
    r = client.get("/app/chat")
    assert r.status_code == 200
    assert "chat-root" in r.text
    assert "gemini-3-flash-preview" in r.text
    assert "gemini-3.1-pro-preview" in r.text
