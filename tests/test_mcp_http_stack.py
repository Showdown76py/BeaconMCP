"""End-to-end test of the real /mcp Starlette stack built by ``_run_http``.

Everything between the socket and the tools is exercised in-process: the
auth middleware, the ``/mcp/c/<slug>`` rewrite shim, the streamable HTTP
session manager (started by the parent app's lifespan), and the SDK's
DNS-rebinding guard. No socket is opened: ``uvicorn.run`` is replaced by a
fake that captures the ASGI app, which httpx then drives via ASGITransport.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

ROOT = Path(__file__).parent.parent
HOST = "mcp.example.com"
ACCEPT = "application/json, text/event-stream"

# Same throwaway values as the `config` job in .github/workflows/ci.yml.
_CI_ENV = {
    "BEACONMCP_SESSION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "GEMINI_API_KEY": "ci-dummy",
    "PVE1_TOKEN_SECRET": "ci-dummy",
    "PVE2_TOKEN_SECRET": "ci-dummy",
    "RACK1_ILO_PASSWORD": "ci-dummy",
    "RACK2_IPMI_PASSWORD": "ci-dummy",
    "RACK3_IDRAC_PASSWORD": "ci-dummy",
    "VPS2_PW": "ci-dummy",
}


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build the app exactly as `_cmd_serve` does and return it with a bearer."""
    return _build_stack(tmp_path, monkeypatch)


def _build_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, yaml_origins: list[str] | None = None,
) -> dict[str, Any]:
    for key, value in _CI_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BEACONMCP_CONFIG", str(ROOT / "beaconmcp.yaml.example"))
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("BEACONMCP_TOKENS_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setenv("BEACONMCP_CLIENTS_FILE", str(tmp_path / "clients.json"))

    import uvicorn

    from beaconmcp import __main__ as entry
    from beaconmcp import auth
    from beaconmcp.server import config, mcp, transport_security

    if yaml_origins is not None:
        # What server.py computes when the YAML list is empty: its built-in
        # fallback goes to the SDK, the YAML value stays empty.
        monkeypatch.setattr(config.server, "allowed_origins", yaml_origins)
        monkeypatch.setattr(transport_security, "allowed_origins", ["https://claude.ai"])

    captured: dict[str, Any] = {}
    # `_run_http` does `import uvicorn` locally, so patching the attribute on
    # the module is enough; the fake keeps the server from binding a port.
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, uvicorn_kw=kw))

    # Seed a client before the build: `_run_http` loads clients.json itself.
    client_id, _secret, _totp = auth.ClientStore(tmp_path / "clients.json").create("t")

    entry._run_http(mcp, "127.0.0.1", 8420)

    # `_run_http` registers its TokenStore with `auth.register_token_store`
    # so tools can revoke the caller's bearer; that module-level handle is the
    # least invasive way to mint a token that the middleware will accept.
    token, _ttl = auth._active_token_store.issue(client_id)
    captured["token"] = token
    captured["client_id"] = client_id
    return captured


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}",
    )


def _headers(token: str | None, **extra: str) -> dict[str, str]:
    h = {"Accept": ACCEPT, "Content-Type": "application/json", "Host": HOST}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(extra)
    return h


def _body(resp: httpx.Response) -> dict[str, Any]:
    """The SDK may answer plain JSON or a one-event SSE stream."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no data line in SSE body: {resp.text!r}")
    return resp.json()


def _rpc(method: str, id_: int | None = None, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


_INIT = _rpc("initialize", 1, {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
})


async def _handshake(c: httpx.AsyncClient, token: str, path: str = "/mcp") -> dict[str, str]:
    resp = await c.post(path, json=_INIT, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    sid = resp.headers["mcp-session-id"]
    h = _headers(token, **{"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18"})
    done = await c.post(path, json=_rpc("notifications/initialized"), headers=h)
    assert done.status_code == 202, done.text
    return h


async def test_authorize_trusts_claude_without_yaml_origins(tmp_path, monkeypatch) -> None:
    # With server.allowed_origins left out of beaconmcp.yaml the SDK falls back
    # to its built-in list for CORS; the OAuth redirect check must use that
    # same list, or the Claude connector gets a 400 on a default install.
    stack = _build_stack(tmp_path, monkeypatch, yaml_origins=[])
    app = stack["app"]
    params = {
        "response_type": "code",
        "client_id": stack["client_id"],
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "state": "s",
        "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    }
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.get("/oauth/authorize", params=params)
    assert resp.status_code == 200, resp.text


def test_stop_does_not_wait_forever_on_open_requests(stack) -> None:
    # uvicorn waits for every connection to close by default; one stalled
    # request then held restarts until systemd SIGKILLed the process at 90 s.
    grace = stack["uvicorn_kw"].get("timeout_graceful_shutdown")
    assert grace is not None and 0 < grace < 90


async def test_missing_bearer_is_401_with_resource_metadata(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.post("/mcp", json=_INIT, headers=_headers(None))
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


async def test_initialize_returns_session_and_protocol_version(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.post("/mcp", json=_INIT, headers=_headers(stack["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.headers["mcp-session-id"]
    # The client proposed 2025-06-18 and mcp 2.3.0 supports it, so the server
    # echoes it back instead of downgrading.
    assert _body(resp)["result"]["protocolVersion"] == "2025-06-18"


async def test_tools_list_schemas_are_objects(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        h = await _handshake(c, stack["token"])
        resp = await c.post("/mcp", json=_rpc("tools/list", 2), headers=h)
    assert resp.status_code == 200, resp.text
    tools = _body(resp)["result"]["tools"]
    assert len(tools) > 40
    bad = [t["name"] for t in tools if t["inputSchema"].get("type") != "object"]
    assert not bad, f"inputSchema is not an object schema for: {bad}"


async def test_tool_call_answers_from_config(stack) -> None:
    """security_end_session needs no network: it only touches the token store."""
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        h = await _handshake(c, stack["token"])
        resp = await c.post(
            "/mcp",
            json=_rpc("tools/call", 3, {"name": "security_end_session", "arguments": {}}),
            headers=h,
        )
    assert resp.status_code == 200, resp.text
    result = _body(resp)["result"]
    assert result["isError"] is False
    # A bare `dict` return has no output schema, so the payload is the text block.
    payload = json.loads(result["content"][0]["text"])
    assert payload["revoked"] is True


async def test_slug_path_is_rewritten_to_mcp(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.post("/mcp/c/some-slug", json=_INIT, headers=_headers(stack["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.headers["mcp-session-id"]
    assert "protocolVersion" in _body(resp)["result"]


async def test_unknown_host_is_rejected_with_421(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.post(
            "/mcp", json=_INIT, headers=_headers(stack["token"], Host="evil.example.net"),
        )
    assert resp.status_code == 421


async def test_upload_link_route_skips_bearer_auth(stack) -> None:
    # The token in the path is the credential: no bearer, and an unknown
    # token is the handler's 404, not the middleware's 401.
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.put("/transfers/upload/not-a-real-token", content=b"x")
        metrics = await c.get("/metrics")
    assert resp.status_code == 404
    assert "not-a-real-token" not in metrics.text


async def test_upload_link_body_streams_through_middleware(stack, tmp_path) -> None:
    from beaconmcp import transfers

    received: dict[str, bytes] = {}

    async def pusher(node, vmid, path, dest, verify):
        received[dest] = path.read_bytes()
        return {"status": "success"}

    transfers.register_pusher(pusher)
    link = transfers.get_store().issue(
        node="pve1", vmid=100, dest="/root/f", max_bytes=1 << 20, staging_dir=tmp_path,
    )
    payload = b"\x00\x01binary\xff" * 5000
    app = stack["app"]
    try:
        async with app.router.lifespan_context(app), _client(app) as c:
            resp = await c.put(transfers.UPLOAD_PATH_PREFIX + link.token, content=payload)
    finally:
        transfers.register_pusher(None)
    assert resp.status_code == 200, resp.text
    assert received["/root/f"] == payload


# ---------------------------------------------------------------------------
# Host allowlist on every route (not only the SDK's /mcp guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/metrics"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/oauth/authorize"),
        ("GET", "/app/login"),
        ("POST", "/app/api/mcp/call"),
    ],
)
async def test_foreign_host_is_421_on_every_route(stack, method: str, path: str) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.request(method, path, headers={"Host": "attacker.test"})
    assert resp.status_code == 421, (path, resp.status_code)


async def test_health_probe_ignores_host(stack) -> None:
    # check-startup and the Docker healthcheck probe it by IP.
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.get("/health", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 200


async def test_oauth_metadata_issuer_is_never_a_foreign_host(stack) -> None:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        resp = await c.get("/.well-known/oauth-authorization-server", headers={"Host": HOST})
    assert resp.status_code == 200
    assert resp.json()["issuer"] == f"http://{HOST}"


async def test_forwarded_host_from_trusted_proxy_must_be_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from beaconmcp.server import config

    # httpx's ASGITransport reports 127.0.0.1 as the TCP peer.
    monkeypatch.setattr(config.server, "trusted_proxies", ["127.0.0.1"])
    stack = _build_stack(tmp_path, monkeypatch)
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        bad = await c.get(
            "/.well-known/oauth-authorization-server",
            headers={"Host": HOST, "X-Forwarded-Host": "attacker.test"},
        )
        good = await c.get(
            "/.well-known/oauth-authorization-server",
            headers={"Host": "127.0.0.1:8420", "X-Forwarded-Host": HOST},
        )
    assert bad.status_code == 421
    assert good.status_code == 200
    assert good.json()["issuer"] == f"http://{HOST}"


async def test_forwarded_proto_trust_when_proxies_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from beaconmcp.server import config

    # trusted_proxies names Cloudflare's edge, as docs/cloudflare.md shows,
    # but cloudflared itself connects from loopback: https must survive that
    # (it did before forwarded_scheme existed). An internet peer that is not
    # the declared proxy still cannot claim https.
    monkeypatch.setattr(config.server, "trusted_proxies", ["173.245.48.0/20"])
    stack = _build_stack(tmp_path, monkeypatch)
    app = stack["app"]
    metadata = "/.well-known/oauth-authorization-server"
    headers = {"Host": HOST, "X-Forwarded-Proto": "https"}
    async with app.router.lifespan_context(app):
        async with _client(app) as c:  # peer 127.0.0.1
            local = await c.get(metadata, headers=headers)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("8.8.8.8", 4321)),
            base_url=f"http://{HOST}",
        ) as c:
            public = await c.get(metadata, headers=headers)
    assert local.json()["issuer"] == f"https://{HOST}"
    assert public.json()["issuer"] == f"http://{HOST}"
