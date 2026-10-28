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
import logging
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


class TotpLockout:
    """TOTP brute-force lockout keyed on ``(client_id, source IP)``.

    ``client_id`` is not a secret (it travels in authorize URLs, browser
    history, proxy logs), so a lockout keyed on it alone let anyone who knew
    it lock the owner out of every 2FA path with a handful of wrong codes.
    Strikes are counted per source instead: ``per_source`` wrong codes from
    one IP lock that IP out for that client. A second, higher per-client cap
    (``per_client`` wrong codes from any mix of IPs) still stops a guesser
    who spreads attempts over many addresses.

    Every failure pushes the cooldown of its counters forward; a counter
    whose cooldown has passed starts again from zero. A success clears the
    caller's own per-source counter only, so a distributed guesser can't
    reset the per-client cap by having the owner log in.

    A source that entered a valid code recently is exempt from the
    per-client cap (its own per-source cap still applies). Otherwise
    anyone with two addresses could spend the per-client budget every
    cooldown and keep the owner locked out of every TOTP path for good.
    """

    def __init__(
        self,
        *,
        per_source: int = 5,
        per_client: int = 10,
        lockout_seconds: float = 300.0,
        known_good_seconds: float = 30 * 86400.0,
    ) -> None:
        self._per_source = per_source
        self._per_client = per_client
        self._lockout = lockout_seconds
        self._known_good_ttl = known_good_seconds
        # key -> (failures, cooldown_until). Keys are ("src", client_id, ip)
        # and ("client", client_id).
        self._strikes: dict[tuple[str, ...], tuple[int, float]] = {}
        # ("src", client_id, ip) -> until: sources that passed TOTP lately.
        self._known_good: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _src_key(client_id: str, ip: str | None) -> tuple[str, ...]:
        return ("src", client_id, ip or "-")

    def _count_locked(self, key: tuple[str, ...], now: float) -> int:
        entry = self._strikes.get(key)
        if entry is None:
            return 0
        count, until = entry
        if now >= until:
            del self._strikes[key]
            return 0
        return count

    def _is_known_good(self, key: tuple[str, ...], now: float) -> bool:
        until = self._known_good.get(key)
        if until is None:
            return False
        if now >= until:
            del self._known_good[key]
            return False
        return True

    def locked(self, client_id: str, ip: str | None = None) -> bool:
        now = time.time()
        src = self._src_key(client_id, ip)
        with self._lock:
            if self._count_locked(src, now) >= self._per_source:
                return True
            if self._is_known_good(src, now):
                return False
            return self._count_locked(("client", client_id), now) >= self._per_client

    def record_failure(self, client_id: str, ip: str | None = None) -> None:
        now = time.time()
        until = now + self._lockout
        with self._lock:
            for key in (("client", client_id), self._src_key(client_id, ip)):
                self._strikes[key] = (self._count_locked(key, now) + 1, until)
            if len(self._strikes) > 4096:
                for key, (_, key_until) in list(self._strikes.items()):
                    if now >= key_until:
                        del self._strikes[key]

    def record_success(self, client_id: str, ip: str | None = None) -> None:
        now = time.time()
        src = self._src_key(client_id, ip)
        with self._lock:
            self._strikes.pop(src, None)
            self._known_good[src] = now + self._known_good_ttl
            if len(self._known_good) > 4096:
                for key, until in list(self._known_good.items()):
                    if now >= until:
                        del self._known_good[key]
                # Still full: forget the oldest, the cap only bounds memory.
                while len(self._known_good) > 4096:
                    del self._known_good[min(self._known_good, key=self._known_good.__getitem__)]


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


_forwarding_warned = False


def _warn_untrusted_forwarding(forwarded_for: str | None, peer: str | None) -> None:
    """Say once that a proxy is forwarding but is not in ``trusted_proxies``.

    Behind a reverse proxy every request then comes from the proxy's own
    address, so the per-IP limits (login, authorize, TOTP lockout) become
    one budget shared by every visitor, and one attacker can spend it for
    everyone. Also fires with a non-empty list: a cloudflared tunnel
    connects from its own LAN address, which ``cloudflare`` (the edge
    ranges) does not cover.
    """
    global _forwarding_warned
    if _forwarding_warned or not forwarded_for:
        return
    _forwarding_warned = True
    logging.getLogger("beaconmcp").warning(
        "X-Forwarded-For received from %s, which is not in server.trusted_proxies: "
        "every client behind that proxy shares one rate-limit and TOTP-lockout "
        "budget. If %s is your reverse proxy or cloudflared tunnel, add it to "
        "server.trusted_proxies.",
        peer or "an unknown peer", peer or "it",
    )


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
    peer_trusted = bool(direct_ip) and _is_trusted_proxy(direct_ip, trusted_proxies)
    if not peer_trusted and headers is not None and hasattr(headers, "get"):
        _warn_untrusted_forwarding(headers.get("x-forwarded-for"), direct_ip)
    if headers is not None and peer_trusted:
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


def forwarded_host(
    request: object,
    trusted_proxies: tuple[str, ...] = (),
    *,
    default: str = "localhost",
) -> str:
    """Client-facing Host for a Starlette ``Request``.

    Mirrors :func:`client_ip`'s trust model for the *host* dimension: the
    ``X-Forwarded-Host`` header is attacker-controlled on a direct request, so
    it is honored only when the direct peer is a declared trusted proxy.
    Otherwise the request's own ``Host`` header is used (then ``default``).

    Like :func:`client_ip`, this keys on ``request.client.host`` being the real
    TCP peer, so the server must run with uvicorn ``proxy_headers=False`` (see
    ``__main__``): the default ``ProxyHeadersMiddleware`` would rewrite the peer
    to the ``X-Forwarded-For`` client and the trusted-proxy branch below could
    never open.

    Kept deliberately narrow -- it does NOT touch the scheme; that is
    :func:`forwarded_scheme`, which has to stay permissive when
    ``trusted_proxies`` is unset (see its docstring).
    """
    headers = getattr(request, "headers", None)
    has_get = headers is not None and hasattr(headers, "get")

    def _hdr(name: str) -> str | None:
        return headers.get(name) if has_get else None

    host_header = _hdr("host") or default
    if not trusted_proxies:
        return host_header

    client = getattr(request, "client", None)
    direct_peer = getattr(client, "host", None) if client is not None else None
    direct_ip = _coerce_ip(str(direct_peer)) if direct_peer is not None else None
    if direct_ip and _is_trusted_proxy(direct_ip, trusted_proxies):
        fwd = _hdr("x-forwarded-host")
        if fwd:
            # Take the last entry, not the first. A proxy that appends rather
            # than overwrites puts its own value last, so the last entry is the
            # one the nearest trusted proxy wrote; returning the first would
            # hand back a client-supplied prefix. Symmetric with client_ip's
            # right-to-left walk. Proxies that overwrite (the common case:
            # nginx ``proxy_set_header X-Forwarded-Host $host``) leave a single
            # entry, so first and last coincide.
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return host_header


def forwarded_scheme(request: object, trusted_proxies: tuple[str, ...] = ()) -> str:
    """Client-facing URL scheme for a Starlette ``Request``.

    ``X-Forwarded-Proto`` is honoured in two cases: when no trusted proxy is
    declared at all (a TLS-terminating edge must still be able to report
    https, otherwise the Secure-cookie flag and the OAuth issuer silently
    downgrade to http), and when the direct peer is a declared trusted proxy.
    Once ``server.trusted_proxies`` is set, a direct caller can no longer strip
    the header's effect by speaking to the backend itself.
    """
    headers = getattr(request, "headers", None)
    url = getattr(request, "url", None)
    own_scheme = str(getattr(url, "scheme", "http") or "http")
    if headers is None or not hasattr(headers, "get"):
        return own_scheme
    fwd = headers.get("x-forwarded-proto")
    if not fwd:
        return own_scheme
    if trusted_proxies:
        client = getattr(request, "client", None)
        direct_peer = getattr(client, "host", None) if client is not None else None
        direct_ip = _coerce_ip(str(direct_peer)) if direct_peer is not None else None
        if not direct_ip or not _is_trusted_proxy(direct_ip, trusted_proxies):
            return own_scheme
    return fwd.split(",")[0].strip() or own_scheme
