"""Host-header guard applied to every route, not only the SDK's /mcp.

The SDK's ``TransportSecurityMiddleware`` only wraps the streamable-HTTP
endpoint. Everything BeaconMCP mounts next to it (dashboard, OAuth, metadata,
health) derives hostnames from the request -- the OAuth issuer, the WebAuthn
RP ID, the host shown on the authorize page -- so the same allowlist has to
hold at the top of the stack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.hostguard import HostGuardMiddleware, host_allowed  # noqa: E402

ALLOWED = ["beacon.example", "127.0.0.1:*", "[::1]:*"]


# ---------------------------------------------------------------------------
# Pure matcher: same semantics as the SDK (exact, or ``base:*`` port wildcard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["beacon.example", "127.0.0.1:8420", "[::1]:8420"])
def test_host_allowed_accepts_listed_hosts(host: str) -> None:
    assert host_allowed(host, ALLOWED)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "attacker.test",
        "beacon.example:8420",  # exact entry has no port wildcard
        "127.0.0.1",  # wildcard entry demands a port
        "beacon.example.attacker.test",
        "xbeacon.example",
    ],
)
def test_host_allowed_rejects_everything_else(host: str | None) -> None:
    assert not host_allowed(host, ALLOWED)


# ---------------------------------------------------------------------------
# Middleware, on a toy app
# ---------------------------------------------------------------------------

async def _echo(request: Request) -> JSONResponse:
    return JSONResponse({"host": request.headers.get("host")})


def _app(*, allowed=ALLOWED, trusted_proxies=(), enabled=True) -> Starlette:
    return Starlette(
        routes=[Route("/{path:path}", _echo)],
        middleware=[
            Middleware(
                HostGuardMiddleware,
                allowed_hosts=allowed,
                trusted_proxies=trusted_proxies,
                enabled=enabled,
            ),
        ],
    )


def _client(app: Starlette, peer: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 54321)),
        base_url="http://beacon.example",
    )


@pytest.mark.parametrize("path", ["/", "/metrics", "/app/login", "/oauth/authorize"])
async def test_foreign_host_gets_421_on_any_path(path: str) -> None:
    async with _client(_app()) as c:
        resp = await c.get(path, headers={"Host": "attacker.test"})
    assert resp.status_code == 421
    assert "Invalid Host header" in resp.text


async def test_health_probe_is_exempt() -> None:
    # Liveness probes (Docker, systemd, check-startup) hit it by IP, and its
    # constant answer derives nothing from the request.
    async with _client(_app()) as c:
        resp = await c.get("/health", headers={"Host": "attacker.test"})
    assert resp.status_code == 200


async def test_listed_host_passes() -> None:
    async with _client(_app()) as c:
        resp = await c.get("/app/login", headers={"Host": "beacon.example"})
    assert resp.status_code == 200
    assert resp.json() == {"host": "beacon.example"}


async def test_forwarded_host_from_untrusted_peer_is_ignored() -> None:
    # Nothing to validate: forwarded_host() will not honour it either.
    async with _client(_app(trusted_proxies=("10.0.0.0/8",))) as c:
        resp = await c.get(
            "/", headers={"Host": "beacon.example", "X-Forwarded-Host": "attacker.test"},
        )
    assert resp.status_code == 200


async def test_forwarded_host_from_trusted_peer_must_be_listed_too() -> None:
    # Behind a declared proxy the forwarded value becomes the effective host
    # (issuer, RP ID), so it is held to the same allowlist as Host.
    app = _app(trusted_proxies=("10.0.0.0/8",))
    async with _client(app, peer="10.1.2.3") as c:
        bad = await c.get(
            "/", headers={"Host": "beacon.example", "X-Forwarded-Host": "attacker.test"},
        )
        good = await c.get(
            "/", headers={"Host": "127.0.0.1:8420", "X-Forwarded-Host": "beacon.example"},
        )
    assert bad.status_code == 421
    assert good.status_code == 200


async def test_raw_host_still_checked_behind_trusted_proxy() -> None:
    app = _app(trusted_proxies=("10.0.0.0/8",))
    async with _client(app, peer="10.1.2.3") as c:
        resp = await c.get(
            "/", headers={"Host": "attacker.test", "X-Forwarded-Host": "beacon.example"},
        )
    assert resp.status_code == 421


async def test_disabled_guard_lets_everything_through() -> None:
    async with _client(_app(enabled=False)) as c:
        resp = await c.get("/", headers={"Host": "attacker.test"})
    assert resp.status_code == 200


async def test_non_http_scopes_are_passed_through() -> None:
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    guard = HostGuardMiddleware(inner, allowed_hosts=ALLOWED)
    await guard({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
