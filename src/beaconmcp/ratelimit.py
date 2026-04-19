"""Tiny in-memory sliding-window rate limiter.

Covers the auth-adjacent endpoints (``/oauth/token``, ``/app/login``) so a
compromised or malicious client can't brute-force ``client_secret`` / TOTP
at line speed. The existing per-client TOTP lockout only triggers after a
valid-client-bad-TOTP pattern; this limiter fires earlier, on the *IP*,
regardless of which client_id is being tried.

The bucket lives in-process: if you run multiple BeaconMCP instances
behind a load balancer each instance gets its own count. That's fine for
the single-host homelab target; deploy a real limiter (nginx, Traefik) in
front if you need global state.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    events: deque[float] = field(default_factory=deque)


class RateLimiter:
    """Sliding-window limiter: N events per ``window_seconds`` per key.

    ``check(key)`` returns True if the event is allowed (and records it),
    False if it should be rejected. Keys are opaque strings -- we use the
    client IP for auth endpoints.
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_gc = 0.0

    @staticmethod
    def _prune_bucket(bucket: _Bucket, cutoff: float) -> None:
        while bucket.events and bucket.events[0] <= cutoff:
            bucket.events.popleft()

    def _collect_stale_buckets_locked(self, cutoff: float) -> None:
        for key, bucket in list(self._buckets.items()):
            self._prune_bucket(bucket, cutoff)
            if not bucket.events:
                del self._buckets[key]

    def check(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[key] = bucket
            # Drop expired events.
            self._prune_bucket(bucket, cutoff)
            if len(bucket.events) >= self._limit:
                return False
            bucket.events.append(now)
            # Opportunistic GC: once the map is large, reclaim stale keys whose
            # events are all outside the window.
            if len(self._buckets) > 1024 or (now - self._last_gc) >= self._window:
                self._collect_stale_buckets_locked(cutoff)
                self._last_gc = now
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` can make another request (0 if allowed now).

        Used to populate the ``Retry-After`` response header.
        """
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or not bucket.events:
                return 0
            cutoff = time.monotonic() - self._window
            self._prune_bucket(bucket, cutoff)
            if not bucket.events:
                del self._buckets[key]
                return 0
            oldest = bucket.events[0]
            return max(0, int(self._window - (time.monotonic() - oldest)) + 1)


def _coerce_ip(value: object) -> str | None:
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        pass
    if raw.startswith("[") and "]" in raw:
        try:
            return str(ipaddress.ip_address(raw[1 : raw.index("]")]))
        except ValueError:
            pass
    if raw.count(":") == 1:
        host, _, port = raw.rpartition(":")
        if host and port.isdigit():
            try:
                return str(ipaddress.ip_address(host))
            except ValueError:
                pass
    return None


def _is_trusted_proxy(ip_value: str, trusted_proxies: tuple[str, ...]) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_value)
    except ValueError:
        return False

    for raw_rule in trusted_proxies:
        rule = raw_rule.strip()
        if not rule:
            continue
        if "/" in rule:
            try:
                if ip_obj in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                continue
            continue
        rule_ip = _coerce_ip(rule)
        if rule_ip is None:
            continue
        if ip_obj == ipaddress.ip_address(rule_ip):
            return True
    return False


def client_ip(request: object, trusted_proxies: tuple[str, ...] = ()) -> str:
    """Best-effort client IP for a Starlette ``Request``.

    Honors ``X-Forwarded-For`` only when the direct peer is trusted. In that
    case we walk the chain from right to left and return the first untrusted
    hop, which avoids left-most spoofing when proxies append to the header.
    """
    client = getattr(request, "client", None)
    direct_peer = getattr(client, "host", None) if client is not None else None
    direct_peer_raw = str(direct_peer) if direct_peer is not None else ""
    direct_ip = _coerce_ip(direct_peer_raw)

    headers = getattr(request, "headers", None)
    if headers is not None and direct_ip and _is_trusted_proxy(direct_ip, trusted_proxies):
        fwd = headers.get("x-forwarded-for") if hasattr(headers, "get") else None
        if fwd:
            chain: list[str] = [
                ip for ip in (_coerce_ip(part) for part in fwd.split(",")) if ip is not None
            ]
            chain.append(direct_ip)
            for hop in reversed(chain):
                if not _is_trusted_proxy(hop, trusted_proxies):
                    return hop

    if direct_ip:
        return direct_ip
    if direct_peer_raw:
        return direct_peer_raw
    return "unknown"
