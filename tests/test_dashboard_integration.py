"""Dashboard integration tests using Starlette's TestClient.

Mocks ClientStore + TokenStore so we don't need the real auth backend.
Run with::

    pytest tests/test_dashboard_integration.py -v
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.dashboard.app import (
    BEARER_TTL_SECONDS,
    DashboardDeps,
    SESSION_COOKIE,
    build_dashboard_routes,
)
from beaconmcp.dashboard.csrf import CSRF_COOKIE
from beaconmcp.dashboard.db import Database
from beaconmcp.dashboard.session import SessionStore


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class FakeClientStore:
    def __init__(self):
        self.clients = {
            "beaconmcp_test": {
                "secret": "sk_test",
                "name": "Test Client",
                "totp": "123456",
            }
        }

    def verify(self, client_id, secret):
        c = self.clients.get(client_id)
        return bool(c and c["secret"] == secret)

    def verify_totp(self, client_id, code):
        c = self.clients.get(client_id)
        return bool(c and c["totp"] == code)

    def get_name(self, client_id):
        c = self.clients.get(client_id)
        return c["name"] if c else None


class FakeTokenStore:
    def __init__(self):
        self._tokens: dict[str, str] = {}
        self.next_ttl = BEARER_TTL_SECONDS
        self.revoked: list[str] = []
        self._counter = 0

    def issue(self, client_id):
        self._counter += 1
        token = f"bearer_{client_id}_{self._counter}"
        self._tokens[token] = client_id
        return token, self.next_ttl

    def validate(self, token):
        return self._tokens.get(token)

    def revoke(self, token):
        self.revoked.append(token)
        self._tokens.pop(token, None)
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def deps(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    db = Database(tmp_path / "dashboard.db")
    session_store = SessionStore(db, key=os.urandom(32))
    failures: dict[str, tuple[int, float]] = {}

    def totp_locked(cid):
        e = failures.get(cid)
        if not e:
            return False
        count, until = e
        return count >= 5 and time.time() < until

    def totp_record_failure(cid):
        c, _ = failures.get(cid, (0, 0.0))
        failures[cid] = (c + 1, time.time() + 300)

    def totp_record_success(cid):
        failures.pop(cid, None)

    # A sentinel non-None engine so the post-login landing stays /app/chat.
    # These integration tests exercise chat-mode routing; the tokens-only
    # mode (engine=None) is covered separately.
    return DashboardDeps(
        database=db,
        session_store=session_store,
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=totp_locked,
        totp_record_failure=totp_record_failure,
        totp_record_success=totp_record_success,
        engine=object(),  # type: ignore[arg-type]
    )


@pytest.fixture()
def client(deps):
    app = Starlette(routes=build_dashboard_routes(deps))
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def tokens_only_client(tmp_path):
    """A fixture mirroring ``deps``/``client`` but with engine=None.

    Exercises the tokens-only mode of the dashboard: no Gemini key set,
    so ``/app/chat`` redirects to ``/app/tokens`` and post-login lands
    there directly.
    """
    db = Database(tmp_path / "dashboard-tokens-only.db")
    session_store = SessionStore(db, key=os.urandom(32))
    failures: dict[str, tuple[int, float]] = {}

    deps_local = DashboardDeps(
        database=db,
        session_store=session_store,
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: (
            failures.get(cid, (0, 0.0))[0] >= 5
            and time.time() < failures.get(cid, (0, 0.0))[1]
        ),
        totp_record_failure=lambda cid: failures.__setitem__(
            cid,
            (failures.get(cid, (0, 0.0))[0] + 1, time.time() + 300),
        ),
        totp_record_success=lambda cid: (failures.pop(cid, None), None)[1],
        engine=None,
    )
    app = Starlette(routes=build_dashboard_routes(deps_local))
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _login_form(csrf_token: str, **overrides) -> dict:
    data = {
        "csrf_token": csrf_token,
        "client_id": "beaconmcp_test",
        "client_secret": "sk_test",
        "totp": "123456",
        "remember": "on",
    }
    data.update(overrides)
    return data


def _csrf(client: TestClient) -> str:
    """Hit the login page to obtain a CSRF cookie."""
    r = client.get("/app/login")
    assert r.status_code == 200
    token = r.cookies.get(CSRF_COOKIE)
    assert token, "CSRF cookie missing"
    return token


def test_index_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/login"


def test_login_page_renders(client):
    r = client.get("/app/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert "Client ID" in r.text
    assert r.cookies.get(CSRF_COOKIE)


def test_login_post_csrf_required(client):
    r = client.post(
        "/app/login",
        data={
            "client_id": "beaconmcp_test",
            "client_secret": "sk_test",
            "totp": "123456",
        },
    )
    assert r.status_code == 403
    assert r.json() == {"error": "csrf"}


def test_login_post_wrong_credentials(client):
    token = _csrf(client)
    r = client.post(
        "/app/login",
        data=_login_form(token, client_secret="wrong"),
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_post_wrong_totp(client):
    token = _csrf(client)
    r = client.post(
        "/app/login",
        data=_login_form(token, totp="000000"),
    )
    assert r.status_code == 401
    assert "Invalid 2FA code" in r.text


def test_login_post_success(client, deps):
    token = _csrf(client)
    r = client.post("/app/login", data=_login_form(token))
    assert r.status_code == 303
    assert r.headers["location"] == "/app/chat"
    assert r.cookies.get(SESSION_COOKIE)
    # Session persisted
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    assert len(sessions) == 1
    assert sessions[0].mcp_bearer.startswith("bearer_beaconmcp_test_")


def test_chat_requires_session(client):
    r = client.get("/app/chat")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/login"


def test_chat_accessible_after_login(client):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    r = client.get("/app/chat")
    assert r.status_code == 200
    assert "beaconmcp_test" in r.text


def test_logout_revokes_bearer(client, deps):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    bearer = sessions[0].mcp_bearer

    # CSRF cookie is rotated on login, fetch the fresh one.
    new_token = client.cookies.get(CSRF_COOKIE)
    r = client.post("/app/logout", data={"csrf_token": new_token})
    assert r.status_code == 303
    assert r.headers["location"] == "/app/login"
    assert bearer in deps.token_store.revoked
    assert deps.session_store.list_for_client("beaconmcp_test") == []


def test_refresh_requires_session(client):
    r = client.get("/app/refresh")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/login"


def test_refresh_when_bearer_still_valid_redirects_to_chat(client):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    r = client.get("/app/refresh")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/chat"


def test_refresh_when_bearer_expired_renders_form(client, deps):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    deps.session_store._db.conn().execute(
        "UPDATE sessions SET mcp_bearer_expires_at = ? WHERE session_id = ?",
        (0, sessions[0].session_id),
    )

    r = client.get("/app/refresh")
    assert r.status_code == 200
    assert "Test Client" in r.text
    # New UI replaces the "2FA code" label with the 6-digit boxes + copy.
    assert "6-digit code" in r.text


def test_refresh_post_re_issues_bearer(client, deps):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    sid = sessions[0].session_id
    old_bearer = sessions[0].mcp_bearer
    deps.session_store._db.conn().execute(
        "UPDATE sessions SET mcp_bearer_expires_at = ? WHERE session_id = ?",
        (0, sid),
    )

    new_token = client.cookies.get(CSRF_COOKIE)
    r = client.post(
        "/app/refresh",
        data={"csrf_token": new_token, "totp": "123456"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/chat"

    refreshed = deps.session_store.load(sid)
    assert refreshed is not None
    assert refreshed.mcp_bearer != old_bearer
    assert refreshed.bearer_valid()


def test_refresh_wrong_totp(client, deps):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    deps.session_store._db.conn().execute(
        "UPDATE sessions SET mcp_bearer_expires_at = ? WHERE session_id = ?",
        (0, sessions[0].session_id),
    )

    new_token = client.cookies.get(CSRF_COOKIE)
    r = client.post(
        "/app/refresh",
        data={"csrf_token": new_token, "totp": "999999"},
    )
    assert r.status_code == 401
    assert "Invalid 2FA code" in r.text


def test_login_after_5_failed_totp_locks_out(client, deps):
    token = _csrf(client)
    for _ in range(5):
        r = client.post("/app/login", data=_login_form(token, totp="000000"))
        assert r.status_code == 401

    r = client.post("/app/login", data=_login_form(token))
    assert r.status_code == 429
    assert "Too many attempts" in r.text


def test_logout_csrf_required(client):
    r = client.post("/app/logout", data={})
    assert r.status_code == 403


def test_existing_session_skips_login_page(client):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    r = client.get("/app/login")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/chat"


def test_existing_session_with_stale_bearer_redirects_to_refresh(client, deps):
    token = _csrf(client)
    client.post("/app/login", data=_login_form(token))
    sessions = deps.session_store.list_for_client("beaconmcp_test")
    deps.session_store._db.conn().execute(
        "UPDATE sessions SET mcp_bearer_expires_at = ? WHERE session_id = ?",
        (0, sessions[0].session_id),
    )

    r = client.get("/app/login")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/refresh"


def test_security_headers_present(client):
    r = client.get("/app/login")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Referrer-Policy" in r.headers
    assert "Content-Security-Policy" in r.headers


# ---------------------------------------------------------------------------
# Tokens-only mode (engine=None): chat redirects to tokens
# ---------------------------------------------------------------------------


def test_tokens_only_login_lands_on_tokens(tokens_only_client):
    r = tokens_only_client.get("/app/login")
    token = r.cookies.get(CSRF_COOKIE)
    assert token
    r = tokens_only_client.post(
        "/app/login",
        data={
            "csrf_token": token,
            "client_id": "beaconmcp_test",
            "client_secret": "sk_test",
            "totp": "123456",
            "remember": "on",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/tokens"


def test_tokens_only_chat_redirects_to_tokens(tokens_only_client):
    r = tokens_only_client.get("/app/login")
    token = r.cookies.get(CSRF_COOKIE)
    tokens_only_client.post(
        "/app/login",
        data={
            "csrf_token": token,
            "client_id": "beaconmcp_test",
            "client_secret": "sk_test",
            "totp": "123456",
            "remember": "on",
        },
    )
    r = tokens_only_client.get("/app/chat")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/tokens"


def test_tokens_only_login_page_redirects_when_authenticated(tokens_only_client):
    r = tokens_only_client.get("/app/login")
    token = r.cookies.get(CSRF_COOKIE)
    tokens_only_client.post(
        "/app/login",
        data={
            "csrf_token": token,
            "client_id": "beaconmcp_test",
            "client_secret": "sk_test",
            "totp": "123456",
            "remember": "on",
        },
    )
    # Already authenticated: /app/login should bounce to the tokens page
    # since there is no chat engine configured.
    r = tokens_only_client.get("/app/login")
    assert r.status_code == 302
    assert r.headers["location"] == "/app/tokens"
