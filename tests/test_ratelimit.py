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


def test_gc_reclaims_stale_buckets() -> None:
    rl = RateLimiter(limit=1, window_seconds=0.01)
    for i in range(1100):
        assert rl.check(f"k{i}") is True
    time.sleep(0.03)
    # Any new check past 1024 buckets triggers stale-bucket collection.
    assert rl.check("fresh") is True
    assert len(rl._buckets) == 1


def test_client_ip_uses_rightmost_untrusted_hop() -> None:
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
        def __init__(self, fwd: str | None, *, peer: str = "10.0.0.1") -> None:
            self.headers = _H(fwd)
            c = _Client()
            c.host = peer
            self.client = c

    # Trusted direct proxy + spoofed left-most value:
    # proxy appends the real client to XFF, so we must not return the spoof.
    assert (
        client_ip(
            _Req("198.51.100.66, 203.0.113.7"),
            trusted_proxies=("10.0.0.1",),
        )
        == "203.0.113.7"
    )

    # CIDR rules are accepted for trusted proxies.
    assert (
        client_ip(
            _Req("203.0.113.9", peer="10.1.2.3"),
            trusted_proxies=("10.0.0.0/8",),
        )
        == "203.0.113.9"
    )

    # Untrusted direct peer -> ignore XFF entirely.
    assert (
        client_ip(
            _Req("203.0.113.7", peer="192.0.2.8"),
            trusted_proxies=("127.0.0.1",),
        )
        == "192.0.2.8"
    )

    # Direct peer with no trust config -> use peer IP.
    assert client_ip(_Req(None, peer="203.0.113.10"), trusted_proxies=()) == "203.0.113.10"
