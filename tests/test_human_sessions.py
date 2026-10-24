"""Human sessions on OAuth refresh families: the last passkey/TOTP check,
which reauth.py compares against the 24 h human-session TTL."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from beaconmcp.auth import TokenStore, token_hash


def _sign_in(store: TokenStore, client_id: str = "client-a") -> str:
    return store.issue_oauth(client_id, refresh=True).access_token


def test_sign_in_opens_a_verified_session(tmp_path: Path) -> None:
    store = TokenStore(db_path=tmp_path / "tokens.db")
    token = _sign_in(store)
    session = store.get_interactive(token)
    assert session is not None and session.interactive
    assert session.client_id == "client-a"
    assert time.time() - session.verified_at < 5


def test_session_survives_refresh() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    key = store.get_interactive(grant.access_token).key
    renewed = store.refresh(grant.refresh_token, "client-a")
    assert store.get_interactive(renewed.access_token).key == key


def test_session_survives_restart_and_is_hashed(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    token = _sign_in(store)
    store.mark_verified(store.get_interactive(token).key, at=1234.0)

    reborn = TokenStore(db_path=db)
    assert reborn.get_interactive(token).verified_at == 1234.0
    assert token.encode() not in db.read_bytes()
    rows = sqlite3.connect(db).execute("SELECT family_id FROM oauth_families").fetchall()
    assert rows == [(store.get_interactive(token).key,)]


def test_non_oauth_tokens_have_no_session() -> None:
    store = TokenStore()
    named, _ = store.issue("client-a", name="laptop")
    plain, _ = store.issue("client-a")
    no_family = store.issue_oauth("client-a", refresh=False).access_token
    for t in (named, plain, no_family, "nope"):
        assert store.get_interactive(t) is None


def test_session_lifetime_outlives_a_day() -> None:
    store = TokenStore(session_lifetime=30 * 86400)
    grant = store.issue_oauth("client-a", refresh=True)
    # access token still rotates daily, the sign-in itself lasts 30 days
    assert grant.expires_in == TokenStore.TOKEN_TTL
    assert store.refresh_token_ttl == 30 * 86400


def test_default_lifetime_unchanged() -> None:
    assert TokenStore().session_lifetime == TokenStore.SESSION_LIFETIME == 86400


def test_revoke_session_kills_the_family(monkeypatch) -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    key = store.get_interactive(grant.access_token).key
    assert store.revoke_session(key)
    assert store.validate(grant.access_token) is None
    assert store.get_interactive(grant.access_token) is None
    assert not store.revoke_session(key)


def test_mark_verified_unknown_key() -> None:
    assert not TokenStore().mark_verified("nope")


def test_token_hash_is_sha256() -> None:
    assert token_hash("x") == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
