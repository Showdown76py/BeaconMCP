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


def _is_local_peer(ip_value: str) -> bool:
    """Loopback, RFC 1918, link-local, ULA or CGNAT: not routable from the internet."""
    try:
        return not ipaddress.ip_address(ip_value).is_global
    except ValueError:
        return False


def _peer_is_trusted(
    request: object, trusted_proxies: tuple[str, ...],
) -> tuple[str | None, bool]:
    """``(direct peer IP, is it a declared trusted proxy)`` for a request.

    The one place that decides whether a forwarded header may be honoured:
    :func:`client_ip`, :func:`forwarded_host` and :func:`forwarded_scheme` all
    key on the real TCP peer, which is why the server runs with uvicorn
    ``proxy_headers=False`` (see ``__main__``).
    """
    client = getattr(request, "client", None)
    direct_peer = getattr(client, "host", None) if client is not None else None
    direct_ip = _coerce_ip(str(direct_peer)) if direct_peer is not None else None
    return direct_ip, bool(direct_ip) and _is_trusted_proxy(direct_ip, trusted_proxies)


# Headers already warned about, so a misconfigured proxy costs one log line
# per header per process rather than one per request.
_warned_headers: set[str] = set()


def _warn_ignored_forwarded(header: str, peer: str | None, consequence: str) -> None:
    """Say once per header that a forwarding peer is not in ``trusted_proxies``.

    Fires with an empty list and with a non-empty one alike: a cloudflared
    tunnel connects from its own LAN address, which ``cloudflare`` (the edge
    ranges) does not cover, and nginx on the same host comes from loopback.
    Without this the symptom (shared rate-limit budget, http issuer, passkey
    that stopped matching) has no visible cause.
    """
    if header in _warned_headers:
        return
    _warned_headers.add(header)
    logging.getLogger("beaconmcp").warning(
        "%s received from %s, which is not in server.trusted_proxies: %s "
        "If %s is your reverse proxy or cloudflared tunnel, add it to "
        "server.trusted_proxies.",
        header, peer or "an unknown peer", consequence, peer or "it",
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
    direct_ip, peer_trusted = _peer_is_trusted(request, trusted_proxies)

    headers = getattr(request, "headers", None)
    if not peer_trusted and headers is not None and hasattr(headers, "get"):
        if headers.get("x-forwarded-for"):
            _warn_ignored_forwarded(
                "X-Forwarded-For", direct_ip,
                "every client behind that proxy shares one rate-limit and "
                "TOTP-lockout budget.",
            )
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

    Like :func:`client_ip`, this keys on the real TCP peer through
    :func:`_peer_is_trusted`. A forwarded value that is present but not
    honoured is logged once, because the visible symptom (issuer or passkey
    RP ID on the backend's own ``Host``) does not point at ``trusted_proxies``.

    Kept deliberately narrow -- it does NOT touch the scheme; that is
    :func:`forwarded_scheme`, which is trusted more widely (see its docstring).
    """
    headers = getattr(request, "headers", None)
    has_get = headers is not None and hasattr(headers, "get")

    def _hdr(name: str) -> str | None:
        return headers.get(name) if has_get else None

    host_header = _hdr("host") or default
    fwd = _hdr("x-forwarded-host")
    if not fwd:
        return host_header

    direct_ip, peer_trusted = _peer_is_trusted(request, trusted_proxies)
    if peer_trusted:
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

    _warn_ignored_forwarded(
        "X-Forwarded-Host", direct_ip,
        f"the OAuth issuer, the passkey RP ID and the dashboard URLs use the "
        f"Host header ({host_header}) instead, so a passkey enrolled under the "
        f"forwarded hostname will not match.",
    )
    return host_header


def forwarded_scheme(request: object, trusted_proxies: tuple[str, ...]) -> str:
    """Client-facing URL scheme for a Starlette ``Request``.

    ``X-Forwarded-Proto`` is honoured from three kinds of peer: any peer when
    no trusted proxy is declared (a TLS-terminating edge must still be able
    to report https), a declared trusted proxy, and a peer on a non-global
    address (loopback, RFC 1918, link-local, CGNAT) even when the list is
    set. The last case is deliberate: the usual layout is cloudflared or
    nginx on the same host or LAN with ``trusted_proxies: [cloudflare]``
    naming the edge rather than the tunnel, and ignoring the header there
    silently downgrades the OAuth issuer, the passkey origin, the dashboard
    URLs and the Secure cookie flag to http.

    The scheme is trusted more widely than ``X-Forwarded-For`` (rate-limit
    evasion) or ``X-Forwarded-Host`` (hostname a credential is bound to)
    because forging it buys an attacker nothing: the cookie and the URLs
    it affects are the forger's own. The one thing the gate still does is
    refuse an undeclared *internet* peer that claims https. Note what it
    does not do: a direct plain-HTTP request to the backend keeps its own
    ``http`` scheme and gets a non-Secure cookie on that connection, which
    is the connection's property, not a downgrade anyone else can trigger.
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
        direct_ip, peer_trusted = _peer_is_trusted(request, trusted_proxies)
        if not peer_trusted and not (direct_ip and _is_local_peer(direct_ip)):
            _warn_ignored_forwarded(
                "X-Forwarded-Proto", direct_ip,
                f"the scheme falls back to the backend's own ({own_scheme}), so "
                f"the OAuth issuer, the passkey origin and the Secure cookie "
                f"flag follow it.",
            )
            return own_scheme
    return fwd.split(",")[0].strip() or own_scheme
