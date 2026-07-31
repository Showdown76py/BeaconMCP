"""Tests for the in-memory sliding-window rate limiter."""

from __future__ import annotations

import time

from beaconmcp.ratelimit import RateLimiter, client_ip, forwarded_host


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


def test_forwarded_host_only_trusts_declared_proxy() -> None:
    class _H:
        def __init__(self, host: str | None, xfh: str | None) -> None:
            self._host = host
            self._xfh = xfh

        def get(self, k: str) -> str | None:
            k = k.lower()
            if k == "host":
                return self._host
            if k == "x-forwarded-host":
                return self._xfh
            return None

    class _Client:
        host = "10.0.0.1"

    class _Req:
        def __init__(
            self, host: str | None, xfh: str | None, *, peer: str = "10.0.0.1",
        ) -> None:
            self.headers = _H(host, xfh)
            c = _Client()
            c.host = peer
            self.client = c

    # No trusted proxies configured -> X-Forwarded-Host is ignored, even when
    # the peer looks internal. The request's own Host header wins.
    assert (
        forwarded_host(_Req("real.example", "evil.attacker"), trusted_proxies=())
        == "real.example"
    )

    # Trusted direct proxy -> the forwarded host is believed.
    assert (
        forwarded_host(
            _Req("internal:8420", "public.example", peer="10.0.0.1"),
            trusted_proxies=("10.0.0.1",),
        )
        == "public.example"
    )

    # A spoofed X-Forwarded-Host from an UNtrusted peer is dropped; Host wins.
    assert (
        forwarded_host(
            _Req("real.example", "evil.attacker", peer="192.0.2.8"),
            trusted_proxies=("10.0.0.0/8",),
        )
        == "real.example"
    )

    # Proxy chain: a client-supplied prefix must not win. A proxy that appends
    # its own value puts it last, so the last entry is returned -- symmetric
    # with client_ip's right-to-left walk.
    assert (
        forwarded_host(
            _Req("internal", "evil.attacker, public.example", peer="10.0.0.1"),
            trusted_proxies=("10.0.0.1",),
        )
        == "public.example"
    )

    # Nothing usable -> default.
    assert forwarded_host(_Req(None, None), trusted_proxies=()) == "localhost"


def test_forwarded_host_through_a_real_starlette_request() -> None:
    # Guards against the hand-built _Req above drifting from runtime: build an
    # actual Starlette Request from an ASGI scope and confirm the trusted-proxy
    # branch opens. This is the shape the app sees once uvicorn is told not to
    # rewrite scope["client"] (proxy_headers=False), so request.client.host is
    # the real TCP peer -- the proxy -- not the X-Forwarded-For client.
    from starlette.requests import Request

    def _req(host: str, xfh: str | None, peer: str) -> Request:
        headers = [(b"host", host.encode())]
        if xfh is not None:
            headers.append((b"x-forwarded-host", xfh.encode()))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 44444),
            "scheme": "http",
            "server": ("app", 80),
        }
        return Request(scope)

    # Proxy peer is trusted -> the forwarded host is believed.
    assert (
        forwarded_host(
            _req("127.0.0.1:8420", "beacon.example.com", "127.0.0.1"),
            trusted_proxies=("127.0.0.1",),
        )
        == "beacon.example.com"
    )

    # Direct (untrusted) peer -> forwarded host dropped, own Host wins.
    assert (
        forwarded_host(
            _req("real.example", "evil.attacker", "203.0.113.5"),
            trusted_proxies=("127.0.0.1",),
        )
        == "real.example"
    )
