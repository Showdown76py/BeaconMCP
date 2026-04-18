"""Tests for the in-memory sliding-window rate limiter."""

from __future__ import annotations

import time

from beaconmcp.ratelimit import RateLimiter, client_ip


def test_allows_up_to_limit_then_blocks() -> None:
    rl = RateLimiter(limit=3, window_seconds=60.0)
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is True
    assert rl.check("1.2.3.4") is False


def test_keys_are_independent() -> None:
    rl = RateLimiter(limit=2, window_seconds=60.0)
    assert rl.check("a") is True
    assert rl.check("a") is True
    assert rl.check("a") is False
    # Different key: fresh budget.
    assert rl.check("b") is True


def test_window_expiry_frees_slots() -> None:
    rl = RateLimiter(limit=2, window_seconds=0.05)
    assert rl.check("k") is True
    assert rl.check("k") is True
    assert rl.check("k") is False
    time.sleep(0.08)
    # Old events aged out.
    assert rl.check("k") is True


def test_retry_after_nonzero_when_blocked() -> None:
    rl = RateLimiter(limit=1, window_seconds=60.0)
    assert rl.check("x") is True
    assert rl.check("x") is False
    assert rl.retry_after("x") > 0


def test_client_ip_prefers_forwarded_for() -> None:
    class _H:
        def __init__(self, fwd: str | None) -> None:
            self._fwd = fwd
        def get(self, k: str) -> str | None:
            if k.lower() == "x-forwarded-for":
                return self._fwd
            return None

    class _Client:
        host = "10.0.0.1"

    class _Req:
        def __init__(self, fwd: str | None) -> None:
            self.headers = _H(fwd)
            self.client = _Client()

    # Trusted proxy -> parse X-Forwarded-For
    assert client_ip(_Req("203.0.113.7, 10.0.0.1"), trusted_proxies=("10.0.0.1",)) == "203.0.113.7"
    assert client_ip(_Req(" 1.2.3.4 "), trusted_proxies=("10.0.0.1",)) == "1.2.3.4"
    # Untrusted proxy -> return peer IP directly
    assert client_ip(_Req("203.0.113.7"), trusted_proxies=("127.0.0.1",)) == "10.0.0.1"
