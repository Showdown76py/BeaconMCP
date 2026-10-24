"""OAuth refresh tokens (rotation + reuse detection) and OAuth-token persistence.

The claude.ai connector used to drop to "needs authentication" on every
restart (OAuth bearers lived in memory only) and every time its 24 h access
token ran out. Refresh tokens fix the restart case and let a connector renew
its bearer inside the day without a prompt. The sign-in itself still ends
24 h after the authorization, by design: this server can run shell on the
hypervisors, so a session that never goes back through TOTP is not wanted.
These tests pin all three.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import RefreshTokenError, TokenStore


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _past_grace(store: TokenStore) -> None:
    """Age every rotated-out token past the retry grace window, so a replay
    is judged as reuse rather than as a retry of the same call."""
    for rt in store._refresh_tokens.values():
        if rt.used:
            rt.used_at -= store.REFRESH_REUSE_GRACE + 1


DAY = TokenStore.SESSION_LIFETIME


def _clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """Freeze the store's clock (``time.time``) at ``now``."""
    monkeypatch.setattr("beaconmcp.auth.time.time", lambda: now)


# --- issuance ----------------------------------------------------------------


def test_authorization_code_grant_returns_refresh_token() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.access_token and grant.refresh_token
    assert grant.access_token != grant.refresh_token
    assert grant.expires_in == TokenStore.TOKEN_TTL
    assert store.validate(grant.access_token) == "client-a"


def test_client_credentials_grant_has_no_refresh_token() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a")
    assert grant.refresh_token is None
    assert store.validate(grant.access_token) == "client-a"


def test_refresh_token_is_not_a_valid_bearer() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    assert store.validate(grant.refresh_token) is None


# --- refresh + rotation ------------------------------------------------------


def test_refresh_issues_new_access_and_refresh_token() -> None:
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    second = store.refresh(first.refresh_token, "client-a")
    assert second.refresh_token is not None
    assert second.access_token != first.access_token
    assert second.refresh_token != first.refresh_token
    assert store.validate(second.access_token) == "client-a"
    # Old access token is untouched: it simply runs out its own TTL.
    assert store.validate(first.access_token) == "client-a"


def test_refresh_rotates_old_refresh_token_out() -> None:
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    second = store.refresh(first.refresh_token, "client-a")
    assert second.refresh_token is not None
    # The rotated-out token can't be used again, the new one can.
    _past_grace(store)
    with pytest.raises(RefreshTokenError):
        store.refresh(first.refresh_token, "client-a")


def test_replayed_refresh_token_revokes_whole_family() -> None:
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    second = store.refresh(first.refresh_token, "client-a")
    assert second.refresh_token is not None

    _past_grace(store)
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(first.refresh_token, "client-a")
    assert exc.value.reason == "reused"

    # Every token of the family is dead: the legitimate holder has to
    # re-authenticate, and so does whoever replayed the stolen token.
    assert store.validate(first.access_token) is None
    assert store.validate(second.access_token) is None
    with pytest.raises(RefreshTokenError):
        store.refresh(second.refresh_token, "client-a")


def test_reuse_is_detected_after_a_short_refresh_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    # With refresh_token_ttl below the family lifetime, a rotated-out token
    # replayed after its own idle TTL must still count as reuse, not as an
    # unknown token that leaves the family alive.
    now = 1_000_000.0
    _clock(monkeypatch, now)
    store = TokenStore(refresh_token_ttl=3600)
    first = store.issue_oauth("client-a", refresh=True)
    _clock(monkeypatch, now + 600)
    second = store.refresh(first.refresh_token, "client-a")

    _clock(monkeypatch, now + 4200)  # past R1's own 1 h TTL, family still alive
    store._cleanup()
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(first.refresh_token, "client-a")
    assert exc.value.reason == "reused"
    assert store.validate(second.access_token) is None
    with pytest.raises(RefreshTokenError):
        store.refresh(second.refresh_token, "client-a")


def test_reuse_does_not_touch_other_families() -> None:
    store = TokenStore()
    a = store.issue_oauth("client-a", refresh=True)
    b = store.issue_oauth("client-a", refresh=True)
    assert a.refresh_token is not None and b.refresh_token is not None
    store.refresh(a.refresh_token, "client-a")
    _past_grace(store)
    with pytest.raises(RefreshTokenError):
        store.refresh(a.refresh_token, "client-a")
    assert store.validate(b.access_token) == "client-a"
    store.refresh(b.refresh_token, "client-a")


# --- retry grace ---------------------------------------------------------------


def test_retry_within_grace_gets_the_same_pair() -> None:
    # A client that timed out and resent the same refresh call must not be
    # treated as an attacker: it gets the pair the first call minted.
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    winner = store.refresh(first.refresh_token, "client-a")
    retry = store.refresh(first.refresh_token, "client-a")
    assert retry == winner
    # The family is intact and keeps rotating normally.
    assert store.validate(winner.access_token) == "client-a"
    assert winner.refresh_token is not None
    nxt = store.refresh(winner.refresh_token, "client-a")
    assert nxt.refresh_token not in (first.refresh_token, winner.refresh_token)


def test_replay_after_grace_revokes_family() -> None:
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    winner = store.refresh(first.refresh_token, "client-a")
    _past_grace(store)
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(first.refresh_token, "client-a")
    assert exc.value.reason == "reused"
    assert store.validate(winner.access_token) is None


def test_grace_pair_is_dropped_once_the_window_closes() -> None:
    store = TokenStore()
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    store.refresh(first.refresh_token, "client-a")
    rt = store._refresh_tokens[_sha(first.refresh_token)]
    assert rt.replay is not None
    _past_grace(store)
    store._cleanup()
    assert rt.replay is None


def test_retry_after_restart_is_strict(tmp_path: Path) -> None:
    # The minted pair is memory-only (never raw in tokens.db), so a replay
    # straight after a restart falls back to the RFC 9700 behaviour.
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    winner = store.refresh(first.refresh_token, "client-a")

    reborn = TokenStore(db_path=db)
    with pytest.raises(RefreshTokenError) as exc:
        reborn.refresh(first.refresh_token, "client-a")
    assert exc.value.reason == "reused"
    assert reborn.validate(winner.access_token) is None


def test_refresh_with_wrong_client_rejected() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(grant.refresh_token, "client-b")
    assert exc.value.reason == "invalid"
    # The rightful owner can still use it.
    store.refresh(grant.refresh_token, "client-a")


def test_unknown_refresh_token_rejected() -> None:
    store = TokenStore()
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh("not-a-token", "client-a")
    assert exc.value.reason == "invalid"


def test_expired_refresh_token_rejected() -> None:
    store = TokenStore(refresh_token_ttl=60)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    store._refresh_tokens[_sha(grant.refresh_token)].expires_at = time.time() - 1
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(grant.refresh_token, "client-a")
    assert exc.value.reason == "expired"


def test_refresh_token_ttl_is_configurable() -> None:
    store = TokenStore(refresh_token_ttl=3600)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    rt = store._refresh_tokens[_sha(grant.refresh_token)]
    assert 3590 < rt.expires_at - time.time() <= 3600


def test_refresh_token_ttl_defaults_to_24_hours() -> None:
    assert TokenStore.SESSION_LIFETIME == 86400
    assert TokenStore().refresh_token_ttl == TokenStore.SESSION_LIFETIME


@pytest.mark.parametrize("ttl", [0, -1, DAY + 1])
def test_refresh_token_ttl_out_of_range_rejected(ttl: int) -> None:
    # No "never expires", and nothing past the absolute session lifetime.
    with pytest.raises(ValueError, match="24 h"):
        TokenStore(refresh_token_ttl=ttl)


# --- absolute session lifetime -------------------------------------------------


def test_sign_in_never_outlives_24h(monkeypatch: pytest.MonkeyPatch) -> None:
    # A connector in constant use must still go back through TOTP once a
    # day: a refresh at 24 h + 1 s fails even though the token it presents
    # was rotated in a minute earlier.
    t0 = 1_800_000_000.0
    _clock(monkeypatch, t0)
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None

    _clock(monkeypatch, t0 + DAY - 60)
    late = store.refresh(grant.refresh_token, "client-a")
    assert late.refresh_token is not None
    # The new pair stops at the family end, not 24 h / ttl from now.
    assert late.expires_in == 60
    assert store._refresh_tokens[_sha(late.refresh_token)].expires_at == t0 + DAY

    _clock(monkeypatch, t0 + DAY + 1)
    with pytest.raises(RefreshTokenError) as exc:
        store.refresh(late.refresh_token, "client-a")
    assert exc.value.reason == "expired"
    assert store.validate(late.access_token) is None
    # Nothing of the sign-in is left, rotated-out tokens included.
    assert store._refresh_tokens == {}
    assert store._oauth_tokens == {}


def test_access_token_is_capped_at_the_session_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = 1_800_000_000.0
    _clock(monkeypatch, t0)
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.expires_in == TokenStore.TOKEN_TTL
    assert grant.refresh_token is not None

    _clock(monkeypatch, t0 + DAY - 3600)
    hourly = store.refresh(grant.refresh_token, "client-a")
    assert hourly.expires_in == 3600
    _clock(monkeypatch, t0 + DAY - 1)
    assert store.validate(hourly.access_token) == "client-a"
    _clock(monkeypatch, t0 + DAY + 1)
    assert store.validate(hourly.access_token) is None


def test_client_credentials_bearer_keeps_its_24h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No family, no refresh token: the plain 24 h bearer is unchanged.
    t0 = 1_800_000_000.0
    _clock(monkeypatch, t0)
    store = TokenStore()
    grant = store.issue_oauth("client-cli")
    assert grant.expires_in == TokenStore.TOKEN_TTL
    assert store._oauth_tokens[_sha(grant.access_token)].expires_at == t0 + DAY


def test_short_refresh_ttl_is_an_idle_timeout_inside_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = 1_800_000_000.0
    _clock(monkeypatch, t0)
    store = TokenStore(refresh_token_ttl=DAY - 600)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    assert store._refresh_tokens[_sha(grant.refresh_token)].expires_at == t0 + DAY - 600

    # A refresh restarts the idle clock but the family end still wins.
    _clock(monkeypatch, t0 + DAY - 1200)
    nxt = store.refresh(grant.refresh_token, "client-a")
    assert nxt.refresh_token is not None
    assert store._refresh_tokens[_sha(nxt.refresh_token)].expires_at == t0 + DAY

    # An idle expiry takes the refresh token out, nothing else: the bearer
    # runs to its own end and the family is not over yet.
    _clock(monkeypatch, t0)
    store2 = TokenStore(refresh_token_ttl=DAY - 600)
    idle = store2.issue_oauth("client-a", refresh=True)
    assert idle.refresh_token is not None
    _clock(monkeypatch, t0 + DAY - 500)
    with pytest.raises(RefreshTokenError) as exc:
        store2.refresh(idle.refresh_token, "client-a")
    assert exc.value.reason == "expired"
    assert store2.validate(idle.access_token) == "client-a"


def test_session_end_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "tokens.db"
    t0 = 1_800_000_000.0
    _clock(monkeypatch, t0)
    store = TokenStore(db_path=db)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    _clock(monkeypatch, t0 + DAY - 60)
    late = store.refresh(grant.refresh_token, "client-a")
    assert late.refresh_token is not None

    _clock(monkeypatch, t0 + DAY - 30)
    reborn = TokenStore(db_path=db)
    rt = reborn._refresh_tokens[_sha(late.refresh_token)]
    assert rt.family_expires_at == t0 + DAY
    last = reborn.refresh(late.refresh_token, "client-a")
    assert last.expires_in == 30
    assert last.refresh_token is not None

    _clock(monkeypatch, t0 + DAY + 1)
    again = TokenStore(db_path=db)
    # Rows past their expiry are purged while loading, so the dead sign-in
    # is simply unknown to the new process.
    with pytest.raises(RefreshTokenError) as exc:
        again.refresh(last.refresh_token, "client-a")
    assert exc.value.reason == "invalid"
    assert again.validate(last.access_token) is None
    assert again._db is not None
    assert again._db.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 0
    assert again._db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0


def test_rows_written_before_the_session_end_existed_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Earlier builds of this branch let a family live as long as it kept
    # refreshing and had no family_expires_at column. Opening such a db
    # adds the column, dates every family from its first token and caps
    # each access or refresh token at that family's 24 h.
    import sqlite3

    db = tmp_path / "tokens.db"
    t0 = 1_800_000_000.0
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE oauth_tokens (token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
        " family_id TEXT, expires_at REAL NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE refresh_tokens (token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
        " family_id TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,"
        " used INTEGER NOT NULL DEFAULT 0)"
    )
    month = 30 * DAY
    # Family "old": signed in 20 days ago, refreshed daily since.
    conn.execute(
        "INSERT INTO refresh_tokens VALUES (?, 'client-a', 'old', ?, ?, 1)",
        (_sha("old-first"), t0 + month, t0),
    )
    conn.execute(
        "INSERT INTO refresh_tokens VALUES (?, 'client-a', 'old', ?, ?, 0)",
        (_sha("old-live"), t0 + 20 * DAY + month, t0 + 20 * DAY),
    )
    conn.execute(
        "INSERT INTO oauth_tokens VALUES (?, 'client-a', 'old', ?, ?)",
        (_sha("old-bearer"), t0 + 21 * DAY, t0 + 20 * DAY),
    )
    # Family "new": signed in an hour ago.
    now = t0 + 20 * DAY + 3600
    conn.execute(
        "INSERT INTO refresh_tokens VALUES (?, 'client-a', 'new', ?, ?, 0)",
        (_sha("new-live"), now - 3600 + month, now - 3600),
    )
    conn.execute(
        "INSERT INTO oauth_tokens VALUES (?, 'client-a', 'new', ?, ?)",
        (_sha("new-bearer"), now - 3600 + DAY, now - 3600),
    )
    # A bearer whose family rows are gone keeps its own expiry.
    conn.execute(
        "INSERT INTO oauth_tokens VALUES (?, 'client-a', 'orphan', ?, ?)",
        (_sha("orphan-bearer"), now + 600, now - DAY + 600),
    )
    conn.commit()
    conn.close()

    _clock(monkeypatch, now)
    store = TokenStore(db_path=db)
    # The 20-day-old sign-in is over, bearer included.
    assert store.validate("old-bearer") is None
    with pytest.raises(RefreshTokenError):
        store.refresh("old-live", "client-a")
    # The hour-old one runs to its first token + 24 h and no further.
    rt = store._refresh_tokens[_sha("new-live")]
    assert rt.family_expires_at == now - 3600 + DAY
    assert rt.expires_at == rt.family_expires_at
    assert store._oauth_tokens[_sha("new-bearer")].expires_at == rt.family_expires_at
    assert store.validate("orphan-bearer") == "client-a"
    assert store._oauth_tokens[_sha("orphan-bearer")].expires_at == now + 600
    assert store._db is not None
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(refresh_tokens)")}
    assert "family_expires_at" in cols


# --- revocation --------------------------------------------------------------


def test_revoke_client_drops_access_and_refresh_tokens(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    a = store.issue_oauth("client-a", refresh=True)
    b = store.issue_oauth("client-b", refresh=True)
    assert a.refresh_token is not None and b.refresh_token is not None

    assert store.revoke_client("client-a") == 1
    assert store.validate(a.access_token) is None
    with pytest.raises(RefreshTokenError):
        store.refresh(a.refresh_token, "client-a")
    assert store.validate(b.access_token) == "client-b"

    reborn = TokenStore(db_path=db)
    assert reborn.validate(a.access_token) is None
    assert reborn.validate(b.access_token) == "client-b"


def test_end_session_revoke_kills_refresh_family() -> None:
    store = TokenStore()
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    assert store.revoke(grant.access_token) is True
    # Grace window keeps the in-flight response alive...
    assert store.validate(grant.access_token) == "client-a"
    # ...but the client can't silently mint a fresh bearer afterwards.
    with pytest.raises(RefreshTokenError):
        store.refresh(grant.refresh_token, "client-a")


def test_prune_unknown_clients(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    keep = store.issue_oauth("client-a", refresh=True)
    gone = store.issue_oauth("client-gone", refresh=True)

    reborn = TokenStore(db_path=db)
    reborn.prune_unknown_clients({"client-a"})
    assert reborn.validate(keep.access_token) == "client-a"
    assert reborn.validate(gone.access_token) is None


# --- persistence -------------------------------------------------------------


def test_oauth_access_token_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    grant = store.issue_oauth("client-a", refresh=True)
    cc = store.issue_oauth("client-cli")

    reborn = TokenStore(db_path=db)
    assert reborn.validate(grant.access_token) == "client-a"
    assert reborn.validate(cc.access_token) == "client-cli"


def test_refresh_token_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None

    reborn = TokenStore(db_path=db)
    fresh = reborn.refresh(grant.refresh_token, "client-a")
    assert reborn.validate(fresh.access_token) == "client-a"


def test_reuse_detected_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    first = store.issue_oauth("client-a", refresh=True)
    assert first.refresh_token is not None
    second = store.refresh(first.refresh_token, "client-a")

    reborn = TokenStore(db_path=db)
    with pytest.raises(RefreshTokenError) as exc:
        reborn.refresh(first.refresh_token, "client-a")
    assert exc.value.reason == "reused"
    assert reborn.validate(second.access_token) is None

    # The family revocation itself is durable too.
    again = TokenStore(db_path=db)
    assert again.validate(second.access_token) is None


def test_oauth_tokens_stored_hashed(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    grant = store.issue_oauth("client-a", refresh=True)
    assert grant.refresh_token is not None
    raw = db.read_bytes()
    assert grant.access_token.encode() not in raw
    assert grant.refresh_token.encode() not in raw
    assert store._db is not None
    hashes = {
        r[0] for r in store._db.execute("SELECT token_hash FROM oauth_tokens")
    } | {r[0] for r in store._db.execute("SELECT token_hash FROM refresh_tokens")}
    assert _sha(grant.access_token) in hashes
    assert _sha(grant.refresh_token) in hashes


def test_expired_oauth_rows_purged_on_load(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    grant = store.issue_oauth("client-a", refresh=True)
    assert store._db is not None
    store._db.execute("UPDATE oauth_tokens SET expires_at = 1.0")
    store._db.execute("UPDATE refresh_tokens SET expires_at = 1.0")
    store._db.commit()

    reborn = TokenStore(db_path=db)
    assert reborn.validate(grant.access_token) is None
    assert reborn._db is not None
    assert reborn._db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0
    assert reborn._db.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 0


def test_dashboard_session_bearer_still_memory_only(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = TokenStore(db_path=db)
    session, _ = store.issue("client-a")
    reborn = TokenStore(db_path=db)
    assert reborn.validate(session) is None


def test_existing_named_tokens_db_upgrades_in_place(tmp_path: Path) -> None:
    # A tokens.db written by a release without OAuth persistence only has
    # the named_tokens table; opening it must add the new tables and keep
    # the named tokens.
    db = tmp_path / "tokens.db"
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE named_tokens (token TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
        " name TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO named_tokens VALUES (?, ?, ?, ?, ?)",
        ("legacy-token", "client-a", "laptop", time.time() + 3600, time.time()),
    )
    conn.commit()
    conn.close()

    store = TokenStore(db_path=db)
    assert store.validate("legacy-token") == "client-a"
    grant = store.issue_oauth("client-a", refresh=True)
    assert TokenStore(db_path=db).validate(grant.access_token) == "client-a"


# --- metadata ----------------------------------------------------------------


def test_metadata_advertises_refresh_token_grant() -> None:
    from beaconmcp.__main__ import _oauth_server_metadata

    meta = _oauth_server_metadata("https://beacon.example")
    assert "refresh_token" in meta["grant_types_supported"]
    assert "authorization_code" in meta["grant_types_supported"]
    assert "client_credentials" in meta["grant_types_supported"]
    assert meta["token_endpoint"] == "https://beacon.example/oauth/token"


# --- config ------------------------------------------------------------------


def test_refresh_token_ttl_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beaconmcp.config import Config, ConfigError

    monkeypatch.setenv("VPS_PW", "pw")
    base = """
version: 1
server:
  refresh_token_ttl: {value}
ssh:
  hosts:
    - name: vps1
      host: 198.51.100.10
      user: root
      password: ${{VPS_PW}}
"""

    def load(value: int) -> Config:
        path = tmp_path / f"cfg{value}.yaml"
        path.write_text(base.format(value=value), encoding="utf-8")
        return Config.load(config_path=path)

    assert load(86400).server.refresh_token_ttl == 86400
    assert load(3600).server.refresh_token_ttl == 3600
    # No "never expires", and nothing past the 24 h session lifetime.
    for bad in (0, -1, 86401, 2592000):
        with pytest.raises(ConfigError, match="refresh_token_ttl"):
            load(bad)


def test_refresh_token_ttl_round_trips_through_wizard(tmp_path: Path) -> None:
    from beaconmcp.wizard import load_yaml_into_draft, render_yaml

    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text("version: 1\nserver:\n  refresh_token_ttl: 3600\n", encoding="utf-8")
    draft = load_yaml_into_draft(cfg)
    assert draft.server.refresh_token_ttl == "3600"
    assert "refresh_token_ttl: 3600" in render_yaml(draft)


def test_wizard_input_rejects_out_of_range_refresh_ttl() -> None:
    from types import SimpleNamespace

    from beaconmcp.wizard import MAX_REFRESH_TOKEN_TTL, _ServerPanel

    panel = _ServerPanel.__new__(_ServerPanel)
    panel.draft = SimpleNamespace(server=SimpleNamespace(refresh_token_ttl=""))
    panel.on_change = lambda: None
    assert MAX_REFRESH_TOKEN_TTL == 86400

    def typed(raw: str) -> str:
        event = SimpleNamespace(input=SimpleNamespace(id="srv-refresh-ttl"), value=raw)
        _ServerPanel.on_input_changed(panel, event)
        return panel.draft.server.refresh_token_ttl

    assert typed("3600") == "3600"
    assert typed("86400") == "86400"
    assert typed("0") == ""
    assert typed("86401") == ""
    assert typed("2592000") == ""
