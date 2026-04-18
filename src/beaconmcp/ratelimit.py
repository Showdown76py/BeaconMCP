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

    def check(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[key] = bucket
            # Drop expired events.
            while bucket.events and bucket.events[0] < cutoff:
                bucket.events.popleft()
            if len(bucket.events) >= self._limit:
                return False
            bucket.events.append(now)
            # Opportunistic GC: if the bucket map grows large, drop any
            # bucket whose deque is now empty. Cheap enough to run inline.
            if len(self._buckets) > 1024:
                empty = [k for k, b in self._buckets.items() if not b.events]
                for k in empty:
                    del self._buckets[k]
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` can make another request (0 if allowed now).

        Used to populate the ``Retry-After`` response header.
        """
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or not bucket.events:
                return 0
            oldest = bucket.events[0]
            return max(0, int(self._window - (time.monotonic() - oldest)) + 1)


def client_ip(request: object, trusted_proxies: tuple[str, ...] = ()) -> str:
    """Best-effort client IP for a Starlette ``Request``.

    Honors ``X-Forwarded-For`` (takes the first entry) only when the direct
    peer is in ``trusted_proxies``. Otherwise, falls back to the direct peer.
    This prevents a direct client from spoofing their IP to bypass limiters.
    """
    client = getattr(request, "client", None)
    direct_peer = getattr(client, "host", None) if client is not None else None
    
    headers = getattr(request, "headers", None)
    if headers is not None and direct_peer in trusted_proxies:
        fwd = headers.get("x-forwarded-for") if hasattr(headers, "get") else None
        if fwd:
            # Take the left-most entry (original client, per RFC 7239 common usage).
            return fwd.split(",")[0].strip()

    if direct_peer:
        return str(direct_peer)
    return "unknown"
