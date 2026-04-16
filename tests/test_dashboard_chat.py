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
)
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
    def issue(self, cid):
        self._n += 1
        return f"b_{self._n}", BEARER_TTL_SECONDS
    def validate(self, token): return None
    def revoke(self, token):
        self.revoked.append(token)
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
        content=json.dumps({"model": "gemini-3-flash", "effort": "medium"}),
    )
    assert r.status_code == 201
    conv = r.json()["conversation"]
    assert conv["model"] == "gemini-3-flash"
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
    assert turn.mcp_url == "https://mcp.example/mcp"
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


def test_chat_page_auth_required(app_and_client):
    _, client = app_and_client
    r = client.get("/app/chat")
    assert r.status_code == 302


def test_chat_page_renders_after_login(app_and_client):
    _, client = app_and_client
    _login(client)
    r = client.get("/app/chat")
    assert r.status_code == 200
    assert "chat-root" in r.text
    assert "gemini-3-flash" in r.text
    assert "gemini-3.1-pro" in r.text
