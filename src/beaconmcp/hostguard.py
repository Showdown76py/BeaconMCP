"""Host-header allowlist for the whole ASGI stack.

The MCP SDK's ``TransportSecurityMiddleware`` validates ``Host`` against
``server.allowed_hosts``, but it is attached to the streamable-HTTP app only,
so it covers ``/mcp`` and nothing else. Every other route BeaconMCP mounts
next to it derives a hostname from the request and trusts it: the OAuth
issuer and the endpoint URLs in ``/.well-known/oauth-authorization-server``,
the WebAuthn RP ID and origin a passkey is bound to, the host shown on the
authorize page as the anti-phishing cue, and the dashboard session cookies.

Without this guard a DNS-rebinding page (or any proxy that relays an
arbitrary ``Host``) gets a full ``/app`` session under an attacker-chosen
hostname while ``/mcp`` correctly answers 421. This middleware sits at the
top of the stack and applies the same allowlist, with the same matching
rules as the SDK, to every HTTP request.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence

from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .ratelimit import forwarded_host

__all__ = ["HostGuardMiddleware", "host_allowed"]

logger = logging.getLogger("beaconmcp")


def host_allowed(host: str | None, allowed_hosts: Iterable[str]) -> bool:
    """Match a ``Host`` value the way the SDK does.

    An entry matches exactly, or, when it ends in ``:*``, matches any port on
    that host (``127.0.0.1:*`` accepts ``127.0.0.1:8420`` but not a bare
    ``127.0.0.1``). A missing or empty host never matches.
    """
    if not host:
        return False
    for allowed in allowed_hosts:
        if host == allowed:
            return True
        if allowed.endswith(":*") and host.startswith(allowed[:-2] + ":"):
            return True
    return False


class HostGuardMiddleware:
    """Reject every HTTP request whose host is not in ``allowed_hosts``.

    Two values are checked. The raw ``Host`` header always is, exactly as the
    SDK checks it on ``/mcp``. When the direct peer is a declared trusted
    proxy and sends ``X-Forwarded-Host``, that value is what the application
    will treat as the client-facing host (see :func:`forwarded_host`), so it
    must be allowlisted as well. From any other peer the forwarded header is
    ignored downstream and therefore not validated here.

    ``/health`` is exempt: it is the liveness probe that Docker, systemd and
    ``beaconmcp check-startup`` hit by IP, its answer is a constant that
    derives nothing from the request, and a 421 there would make an
    incomplete allowlist look like a dead server.

    Pure ASGI on purpose: it runs before any routing or body handling and
    costs nothing on the hot path.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Sequence[str],
        trusted_proxies: Sequence[str] = (),
        enabled: bool = True,
        exempt_paths: Collection[str] = ("/health",),
    ) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.trusted_proxies = tuple(trusted_proxies)
        self.enabled = enabled
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.enabled
            or scope.get("path") in self.exempt_paths
        ):
            await self.app(scope, receive, send)
            return

        # HTTPConnection is the Request without a body: ``.headers`` and
        # ``.client`` are exactly what forwarded_host() reads.
        conn = HTTPConnection(scope)
        raw_host = conn.headers.get("host")
        if not host_allowed(raw_host, self.allowed_hosts):
            await self._reject(conn, "Host", raw_host, scope, receive, send)
            return

        if self.trusted_proxies and conn.headers.get("x-forwarded-host"):
            effective = forwarded_host(conn, self.trusted_proxies)
            if not host_allowed(effective, self.allowed_hosts):
                await self._reject(
                    conn, "X-Forwarded-Host", effective, scope, receive, send,
                )
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        conn: HTTPConnection,
        header: str,
        value: str | None,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        # Same wording as the SDK's TransportSecurityMiddleware on /mcp, with
        # the value and the peer: a 421 on every route is otherwise a blind
        # spot when allowed_hosts is one entry short.
        logger.warning(
            "Invalid Host header: %s (%s, from %s, %s %s)",
            value or "<missing>",
            header,
            conn.client.host if conn.client else "unknown peer",
            scope.get("method", "?"),
            scope.get("path", "?"),
        )
        response = Response("Invalid Host header", status_code=421)
        await response(scope, receive, send)
