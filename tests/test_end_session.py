"""security_end_session: end the human session by default, revoke on request."""

from __future__ import annotations

import time

import pytest
from mcp.server.mcpserver import MCPServer

from beaconmcp import auth
from beaconmcp.auth import TokenStore
from beaconmcp.security.tools import register_security_tools


@pytest.fixture()
def tool(monkeypatch):
    store = TokenStore()
    monkeypatch.setattr(auth, "_active_token_store", store)
    monkeypatch.setattr(TokenStore, "REVOKE_GRACE_SECONDS", 0.0)
    captured = {}

    class Fake:
        def tool(self):
            def deco(fn):
                captured["fn"] = fn
                return fn
            return deco

    register_security_tools(Fake())  # type: ignore[arg-type]
    return store, captured["fn"]


def _as_current(token: str):
    return auth.current_bearer_token.set(token)


def test_default_ends_human_session_keeps_tokens(tool) -> None:
    store, end_session = tool
    token = store.issue_oauth("c1", refresh=True).access_token
    reset = _as_current(token)
    try:
        out = end_session()
    finally:
        auth.current_bearer_token.reset(reset)
    assert out["revoked"] is False and out["reverify_required"] is True
    assert store.validate(token) == "c1"
    assert store.get_interactive(token).verified_at == 0.0


def test_sign_out_revokes(tool) -> None:
    store, end_session = tool
    token = store.issue_oauth("c1", refresh=True).access_token
    reset = _as_current(token)
    try:
        out = end_session(sign_out=True)
    finally:
        auth.current_bearer_token.reset(reset)
    time.sleep(0.01)
    assert out["revoked"] is True
    assert store.validate(token) is None


def test_api_token_still_revoked(tool) -> None:
    store, end_session = tool
    token, _ = store.issue("c1", name="laptop")
    reset = _as_current(token)
    try:
        out = end_session()
    finally:
        auth.current_bearer_token.reset(reset)
    time.sleep(0.01)
    assert out["revoked"] is True
    assert store.validate(token) is None


def test_registers_on_real_server() -> None:
    mcp = MCPServer("t")
    register_security_tools(mcp)
