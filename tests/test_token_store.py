"""Tests for :class:`TokenStore` behavior and thread-safety.

The single-threaded contract (issue / validate / revoke / named-token cap)
must be unchanged by the locking work, and a concurrent issue+validate
storm must not raise ``RuntimeError: dictionary changed size during
iteration`` -- which is exactly what the unlocked version did once tools
started running on a worker-thread pool.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import TokenCapExceeded, TokenStore


def test_issue_then_validate_returns_client_id() -> None:
    store = TokenStore()
    token, ttl = store.issue("client_a")
    assert ttl == TokenStore.TOKEN_TTL
    assert store.validate(token) == "client_a"


def test_validate_unknown_token_is_none() -> None:
    assert TokenStore().validate("nope") is None


def test_expired_token_is_rejected_and_dropped() -> None:
    store = TokenStore()
    token, _ = store.issue("client_a")
    # Force expiry in the past.
    store._tokens[token].expires_at = time.time() - 1
    assert store.validate(token) is None
    assert token not in store._tokens


def test_revoke_applies_grace_then_rejects() -> None:
    store = TokenStore()
    token, _ = store.issue("client_a")
    assert store.revoke(token) is True
    # Within the grace window it is still valid.
    assert store.validate(token) == "client_a"
    # After the grace deadline it is rejected.
    store._tokens[token].expires_at = time.time() - 1
    assert store.validate(token) is None


def test_revoke_unknown_token_returns_false() -> None:
    assert TokenStore().revoke("nope") is False


def test_named_token_cap_enforced() -> None:
    store = TokenStore()
    for i in range(TokenStore.NAMED_TOKEN_CAP):
        store.issue("client_a", name=f"tok{i}")
    assert store.count_named("client_a") == TokenStore.NAMED_TOKEN_CAP
    with pytest.raises(TokenCapExceeded):
        store.issue("client_a", name="one-too-many")
    # Unnamed (dashboard-session) tokens are not capped.
    store.issue("client_a")  # must not raise


def test_revoke_named_by_prefix() -> None:
    store = TokenStore()
    token, _ = store.issue("client_a", name="laptop")
    assert store.revoke_named(token[:12], "client_a") is True
    # Revoked named token starts its grace countdown.
    store._tokens[token].expires_at = time.time() - 1
    assert store.validate(token) is None


def test_revoke_named_wrong_client_rejected() -> None:
    store = TokenStore()
    token, _ = store.issue("client_a", name="laptop")
    assert store.revoke_named(token[:12], "client_b") is False


def test_concurrent_issue_and_validate_no_corruption() -> None:
    """Issue + validate + list from many threads at once.

    Without locking the in-memory dict, the iterating readers (count_named,
    list_named, validate's _cleanup) would intermittently raise
    "dictionary changed size during iteration". This must complete cleanly.
    """
    store = TokenStore()
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(30)
    issued: list[str] = []
    issued_lock = threading.Lock()

    def issuer(i: int) -> None:
        barrier.wait()
        try:
            for j in range(50):
                token, _ = store.issue(f"client_{i % 5}")
                with issued_lock:
                    issued.append(token)
        except Exception as e:  # pragma: no cover - failure path
            with lock:
                errors.append(e)

    def reader(i: int) -> None:
        barrier.wait()
        try:
            for _ in range(50):
                store.count_named(f"client_{i % 5}")
                store.list_named(f"client_{i % 5}")
                with issued_lock:
                    sample = issued[-1] if issued else None
                if sample:
                    store.validate(sample)
        except Exception as e:  # pragma: no cover - failure path
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=issuer, args=(i,)) for i in range(15)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors[:3]}"
    # Every issued token is retrievable.
    assert len(issued) == 15 * 50
    assert store.validate(issued[0]) is not None
