"""Interactive (authorization_code) bearers: long-lived, hashed on disk, with a
``verified_at`` stamp the human-session gate reads."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from beaconmcp.auth import TokenStore, token_hash


def test_issue_interactive_validates_and_is_hashed_on_disk(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    token, _ = store.issue_interactive("client-a")

    assert store.validate(token) == "client-a"
    rows = sqlite3.connect(db).execute(
        "SELECT token_hash, client_id FROM interactive_tokens"
    ).fetchall()
    assert rows == [(token_hash(token), "client-a")]
    assert token.encode() not in db.read_bytes()


def test_interactive_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    token, _ = store.issue_interactive("client-a")
    verified = store.get_interactive(token).verified_at

    reborn = TokenStore(db_path=db)
    assert reborn.validate(token) == "client-a"
    at = reborn.get_interactive(token)
    assert at is not None and at.interactive
    assert at.verified_at == verified


def test_mark_verified_persists(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    token, _ = store.issue_interactive("client-a")
    later = time.time() + 1000
    assert store.mark_verified(token_hash(token), at=later)

    assert TokenStore(db_path=db).get_interactive(token).verified_at == later


def test_interactive_ttl_default_30_days() -> None:
    store = TokenStore()
    _, expires_in = store.issue_interactive("client-a")
    assert expires_in == 2592000


def test_interactive_ttl_configurable() -> None:
    store = TokenStore(interactive_token_ttl=120)
    _, expires_in = store.issue_interactive("client-a")
    assert expires_in == 120


def test_revoke_interactive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(TokenStore, "REVOKE_GRACE_SECONDS", 0.0)
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    token, _ = store.issue_interactive("client-a")
    assert store.revoke(token)
    time.sleep(0.01)
    assert store.validate(token) is None
    assert TokenStore(db_path=db).validate(token) is None


def test_revoke_hash(monkeypatch) -> None:
    monkeypatch.setattr(TokenStore, "REVOKE_GRACE_SECONDS", 0.0)
    store = TokenStore()
    token, _ = store.issue_interactive("client-a")
    assert store.revoke_hash(token_hash(token))
    time.sleep(0.01)
    assert store.validate(token) is None
    assert not store.revoke_hash("unknown")


def test_get_interactive_ignores_named_and_plain() -> None:
    store = TokenStore()
    named, _ = store.issue("client-a", name="laptop")
    plain, _ = store.issue("client-a")
    assert store.get_interactive(named) is None
    assert store.get_interactive(plain) is None
    assert store.get_interactive("nope") is None


def test_expired_interactive_rejected() -> None:
    store = TokenStore(interactive_token_ttl=1)
    token, _ = store.issue_interactive("client-a")
    store.get_interactive(token).expires_at = time.time() - 1
    assert store.validate(token) is None
    assert store.get_interactive(token) is None


def test_drop_interactive_unless(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    keep, _ = store.issue_interactive("alive")
    gone, _ = store.issue_interactive("revoked")
    assert store.drop_interactive_unless(lambda cid: cid == "alive") == 1
    assert store.validate(gone) is None and store.validate(keep) == "alive"
    assert TokenStore(db_path=db).validate(gone) is None
