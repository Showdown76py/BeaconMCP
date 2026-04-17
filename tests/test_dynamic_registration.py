"""Tests for OAuth Dynamic Client Registration (ChatGPT connector flow).

Covers the two correctness-critical paths:

1. :class:`DynamicSlugStore.consume` is atomic & single-use — concurrent
   claims deterministically produce exactly one winner.
2. Derived :class:`Client` rows delegate TOTP verification to their
   owner, and owner revocation cascades to all derived rows.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pyotp
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import ClientStore
from beaconmcp.dashboard.db import Database
from beaconmcp.dashboard.dyn_reg import (
    SLUG_TTL_SECONDS,
    DynamicSlugStore,
    SlugAlreadyConsumed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "dash.db")


@pytest.fixture()
def slug_store(db: Database) -> DynamicSlugStore:
    return DynamicSlugStore(db)


@pytest.fixture()
def clients(tmp_path: Path) -> ClientStore:
    return ClientStore(tmp_path / "clients.json")


# ---------------------------------------------------------------------------
# Slug lifecycle
# ---------------------------------------------------------------------------


def test_mint_then_consume_records_resulting_client(slug_store: DynamicSlugStore) -> None:
    row = slug_store.mint(owner_client_id="owner_1", label="ChatGPT iPhone")
    assert row.used_at is None

    claimed = slug_store.consume(row.slug, resulting_client_id="new_client_1")
    assert claimed.used_at is not None
    assert claimed.resulting_client_id == "new_client_1"

    reloaded = slug_store.load(row.slug)
    assert reloaded is not None and reloaded.resulting_client_id == "new_client_1"


def test_consume_twice_raises(slug_store: DynamicSlugStore) -> None:
    row = slug_store.mint(owner_client_id="owner_1", label="x")
    slug_store.consume(row.slug, resulting_client_id="c1")
    with pytest.raises(SlugAlreadyConsumed):
        slug_store.consume(row.slug, resulting_client_id="c2")


def test_consume_expired_slug_rejected(
    slug_store: DynamicSlugStore, db: Database,
) -> None:
    row = slug_store.mint(owner_client_id="owner_1", label="x")
    # Fast-forward expiry by rewriting the row directly.
    db.conn().execute(
        "UPDATE oauth_dynamic_slugs SET expires_at = ? WHERE slug = ?",
        (time.time() - 1, row.slug),
    )
    with pytest.raises(SlugAlreadyConsumed):
        slug_store.consume(row.slug, resulting_client_id="c1")


def test_consume_unknown_slug_rejected(slug_store: DynamicSlugStore) -> None:
    with pytest.raises(SlugAlreadyConsumed):
        slug_store.consume("not-a-real-slug", resulting_client_id="c1")


def test_concurrent_consume_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    """Two threads race to claim the same slug; exactly one wins."""
    # Each thread uses its own Database instance (fresh connection) to
    # mimic the real HTTP server where handlers run on a thread pool.
    db_path = tmp_path / "dash.db"
    bootstrap = Database(db_path)
    store = DynamicSlugStore(bootstrap)
    row = store.mint(owner_client_id="owner_1", label="race")

    barrier = threading.Barrier(10)
    wins: list[str] = []
    losses: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        local_store = DynamicSlugStore(Database(db_path))
        barrier.wait()
        try:
            local_store.consume(row.slug, resulting_client_id=f"c{i}")
            with lock:
                wins.append(f"c{i}")
        except SlugAlreadyConsumed:
            with lock:
                losses.append(f"c{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1, f"expected exactly one winner, got {wins}"
    assert len(losses) == 9


def test_delete_unused_only_removes_unconsumed(
    slug_store: DynamicSlugStore,
) -> None:
    row = slug_store.mint(owner_client_id="owner_1", label="pending")
    assert slug_store.delete_unused(row.slug, "owner_1") is True
    # Idempotent: second call is a no-op, returns False.
    assert slug_store.delete_unused(row.slug, "owner_1") is False

    # Consumed slugs survive as audit trail.
    row2 = slug_store.mint(owner_client_id="owner_1", label="used")
    slug_store.consume(row2.slug, resulting_client_id="c1")
    assert slug_store.delete_unused(row2.slug, "owner_1") is False


def test_delete_unused_scoped_to_owner(slug_store: DynamicSlugStore) -> None:
    row = slug_store.mint(owner_client_id="owner_A", label="x")
    assert slug_store.delete_unused(row.slug, "owner_B") is False
    assert slug_store.load(row.slug) is not None


def test_prune_expired_drops_only_unused_expired_rows(
    slug_store: DynamicSlugStore, db: Database,
) -> None:
    # 1 fresh, 1 expired-unused, 1 expired-used.
    fresh = slug_store.mint(owner_client_id="o", label="fresh")
    expired_unused = slug_store.mint(owner_client_id="o", label="exp-unused")
    expired_used = slug_store.mint(owner_client_id="o", label="exp-used")

    slug_store.consume(expired_used.slug, resulting_client_id="c1")

    now = time.time()
    db.conn().execute(
        "UPDATE oauth_dynamic_slugs SET expires_at = ? WHERE slug IN (?, ?)",
        (now - 1, expired_unused.slug, expired_used.slug),
    )

    removed = slug_store.prune_expired()
    assert removed == 1
    assert slug_store.load(fresh.slug) is not None
    assert slug_store.load(expired_unused.slug) is None
    assert slug_store.load(expired_used.slug) is not None


def test_ttl_matches_design(slug_store: DynamicSlugStore) -> None:
    """Guard against accidental TTL changes — the dashboard UI copy depends
    on this value, and a sudden bump would surprise users."""
    row = slug_store.mint(owner_client_id="o", label="x")
    delta = row.expires_at - row.created_at
    assert abs(delta - SLUG_TTL_SECONDS) < 1.0


# ---------------------------------------------------------------------------
# Dynamic-client TOTP delegation
# ---------------------------------------------------------------------------


def test_dynamic_client_totp_delegates_to_owner(clients: ClientStore) -> None:
    owner_id, _, owner_seed = clients.create("human")
    derived_id, derived_secret = clients.create_dynamic(
        owner_client_id=owner_id,
        name="ChatGPT (derived)",
        registration_source="chatgpt:slug1",
    )

    # Derived client authenticates with its own secret but NOT its own TOTP.
    assert clients.verify(derived_id, derived_secret) is True
    now_code = pyotp.TOTP(owner_seed).now()
    assert clients.verify_totp(derived_id, now_code) is True
    # Wrong codes still fail.
    assert clients.verify_totp(derived_id, "000000") is False


def test_dynamic_client_without_owner_secret_cannot_verify_totp(
    clients: ClientStore,
) -> None:
    """If the owner is revoked after the derived client is created, TOTP
    verification fails closed — a derived client can't outlive its owner."""
    owner_id, _, owner_seed = clients.create("human")
    derived_id, _ = clients.create_dynamic(
        owner_client_id=owner_id,
        name="derived",
        registration_source="chatgpt:slug1",
    )

    # Revoke owner; derived row cascades away (verify_totp returns False
    # because the client is gone).
    clients.revoke(owner_id)
    assert clients.verify_totp(derived_id, pyotp.TOTP(owner_seed).now()) is False


def test_revoke_owner_cascades_to_derived(clients: ClientStore) -> None:
    owner_id, _, _ = clients.create("human")
    d1, _ = clients.create_dynamic(
        owner_client_id=owner_id, name="chatgpt-1", registration_source="chatgpt:s1",
    )
    d2, _ = clients.create_dynamic(
        owner_client_id=owner_id, name="chatgpt-2", registration_source="chatgpt:s2",
    )

    assert clients.exists(d1) and clients.exists(d2)
    clients.revoke(owner_id)
    assert not clients.exists(d1)
    assert not clients.exists(d2)


def test_revoke_derived_leaves_owner_intact(clients: ClientStore) -> None:
    owner_id, _, _ = clients.create("human")
    d1, _ = clients.create_dynamic(
        owner_client_id=owner_id, name="chatgpt", registration_source="chatgpt:s1",
    )
    clients.revoke(d1)
    assert clients.exists(owner_id)
    assert not clients.exists(d1)


def test_list_derived_scoped_to_owner(clients: ClientStore) -> None:
    a_id, _, _ = clients.create("owner_A")
    b_id, _, _ = clients.create("owner_B")
    clients.create_dynamic(
        owner_client_id=a_id, name="A-chatgpt", registration_source="chatgpt:s1",
    )
    clients.create_dynamic(
        owner_client_id=b_id, name="B-chatgpt", registration_source="chatgpt:s2",
    )
    a_derived = clients.list_derived(a_id)
    assert len(a_derived) == 1 and a_derived[0].name == "A-chatgpt"


def test_create_dynamic_requires_existing_owner(clients: ClientStore) -> None:
    with pytest.raises(ValueError):
        clients.create_dynamic(
            owner_client_id="does-not-exist",
            name="x",
            registration_source="chatgpt:s1",
        )


def test_derived_client_round_trips_through_disk(
    tmp_path: Path,
) -> None:
    """Owner + derived survive a ClientStore reopen (JSON serialization)."""
    path = tmp_path / "clients.json"
    first = ClientStore(path)
    owner_id, _, owner_seed = first.create("human")
    derived_id, derived_secret = first.create_dynamic(
        owner_client_id=owner_id,
        name="chatgpt",
        registration_source="chatgpt:s1",
    )

    second = ClientStore(path)
    derived = second.get(derived_id)
    assert derived is not None
    assert derived.owner_client_id == owner_id
    assert derived.registration_source == "chatgpt:s1"
    # TOTP delegation still works after reload.
    assert second.verify_totp(derived_id, pyotp.TOTP(owner_seed).now()) is True
    assert second.verify(derived_id, derived_secret) is True
