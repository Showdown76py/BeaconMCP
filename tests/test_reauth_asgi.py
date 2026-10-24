"""ASGI gate in front of the MCP app."""

from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from beaconmcp import reauth
from beaconmcp.auth import TokenStore, token_hash
from beaconmcp.reauth import (
    ClientInfoRegistry,
    ClientSessionInfo,
    McpGateApp,
    PendingReverifyStore,
    ReauthRuntime,
)

META = "https://beacon.test/.well-known/oauth-protected-resource"


async def _echo(scope, receive, send):
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"echo":' + json.dumps(body.decode()).encode() + b"}"})


@pytest.fixture()
def env():
    store = TokenStore()
    rt = ReauthRuntime(
        token_store=store, registry=ClientInfoRegistry(),
        pending=PendingReverifyStore(), session_ttl=60, hold_timeout=1,
        issuer_for=lambda req: "https://beacon.test",
    )
    reauth.configure(rt)
    client = TestClient(McpGateApp(_echo, resource_metadata_for=lambda req: META))
    yield client, store, rt
    reauth.configure(None)


def _rpc(method: str, params: dict | None = None, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def _lapse(store: TokenStore, token: str) -> None:
    store.get_interactive(token).verified_at = time.time() - 3600


def _post(client, token, payload):
    return client.post("/mcp", json=payload, headers={
        "authorization": f"Bearer {token}", "user-agent": "claude-code/2.1",
    })


def _init(caps: dict) -> dict:
    return _rpc("initialize", {"capabilities": caps,
                               "clientInfo": {"name": "claude-code", "version": "2.1"}})


def test_initialize_records_capabilities(env) -> None:
    client, store, rt = env
    token, _ = store.issue_interactive("c1")
    res = _post(client, token, _init({"elicitation": {"url": {}}}))
    assert res.status_code == 200
    info = rt.registry.get(token_hash(token))
    assert info.elicit_url and info.name == "claude-code"
    assert info.user_agent == "claude-code/2.1"


def test_lapsed_without_elicitation_gets_401_with_header(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({}))
    _lapse(store, token)
    res = _post(client, token, _rpc("tools/call", {"name": "x"}))
    assert res.status_code == 401
    www = res.headers["www-authenticate"]
    assert 'error="invalid_token"' in www and f'resource_metadata="{META}"' in www


def test_lapsed_unknown_capabilities_gets_401(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _lapse(store, token)
    assert _post(client, token, _rpc("tools/call", {"name": "x"})).status_code == 401


def test_lapsed_tools_call_with_url_passes_through(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({"elicitation": {"url": {}}}))
    _lapse(store, token)
    res = _post(client, token, _rpc("tools/call", {"name": "x"}))
    assert res.status_code == 200 and "echo" in res.json()


def test_lapsed_resources_read_url_gets_32042(env) -> None:
    client, store, rt = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({"elicitation": {"url": {}, "form": {}}}))
    _lapse(store, token)
    res = _post(client, token, _rpc("resources/read", {"uri": "beaconmcp://infrastructure"}, rid=7))
    body = res.json()
    assert res.status_code == 200 and body["id"] == 7
    assert body["error"]["code"] == -32042
    el = body["error"]["data"]["elicitations"][0]
    assert el["mode"] == "url"
    assert el["url"] == f"https://beacon.test/oauth/reverify?id={el['elicitationId']}"
    assert rt.pending.get(el["elicitationId"]).tool == "resources/read"


def test_lapsed_resources_read_form_only_gets_link_error(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({"elicitation": {}}))
    _lapse(store, token)
    body = _post(client, token, _rpc("prompts/get", {"name": "p"})).json()
    assert body["error"]["code"] == -32001
    assert "https://beacon.test/oauth/reverify?id=" in body["error"]["message"]


def test_fresh_session_untouched(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    res = _post(client, token, _rpc("tools/call", {"name": "x"}))
    assert res.status_code == 200 and "echo" in res.json()


def test_named_token_never_gated(env) -> None:
    client, store, _ = env
    token, _ = store.issue("c1", name="laptop")
    res = _post(client, token, _rpc("tools/call", {"name": "x"}))
    assert res.status_code == 200


def test_lapsed_list_methods_allowed(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _lapse(store, token)
    assert _post(client, token, _rpc("tools/list")).status_code == 200


def test_capabilities_recorded_on_reinitialize(env) -> None:
    client, store, rt = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({}))
    assert not rt.registry.get(token_hash(token)).elicit_url
    _post(client, token, _init({"elicitation": {"url": {}}}))
    assert rt.registry.get(token_hash(token)).elicit_url


def test_body_replayed_intact(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    payload = _rpc("tools/call", {"name": "x", "arguments": {"a": "é" * 50}})
    res = _post(client, token, payload)
    assert json.loads(res.json()["echo"]) == payload


def test_batch_with_gated_method(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _lapse(store, token)
    res = _post(client, token, [_rpc("tools/list"), _rpc("tools/call", {"name": "x"}, 2)])
    assert res.status_code == 401


def test_no_runtime_passthrough(env) -> None:
    client, store, _ = env
    reauth.configure(None)
    token, _ = store.issue_interactive("c1")
    _lapse(store, token)
    assert _post(client, token, _rpc("tools/call")).status_code == 200


def test_session_info_dataclass_defaults() -> None:
    assert not ClientSessionInfo().can_elicit


def _chunked(token: str, chunks: list[bytes]):
    """Drive the app directly with a body split like uvicorn does."""
    import asyncio

    sent: list[dict] = []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
             for i, c in enumerate(chunks)]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "raw_path": b"/mcp",
             "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode())],
             "client": ("127.0.0.1", 1), "server": ("t", 80), "scheme": "http"}
    asyncio.run(McpGateApp(_echo, resource_metadata_for=lambda r: META)(scope, receive, send))
    return sent[0]["status"]


def test_oversized_body_on_lapsed_session_fails_closed(env) -> None:
    _, store, _ = env
    token, _ = store.issue_interactive("c1")
    _lapse(store, token)
    rpc = json.dumps(_rpc("resources/read", {"uri": "beaconmcp://infrastructure"})).encode()
    padded = rpc + b" " * (2 * 1024 * 1024)
    chunks = [padded[i:i + 65536] for i in range(0, len(padded), 65536)]
    assert _chunked(token, chunks) == 401


def test_oversized_body_on_fresh_session_passes(env) -> None:
    _, store, _ = env
    token, _ = store.issue_interactive("c1")
    body = b" " * (2 * 1024 * 1024)
    assert _chunked(token, [body[i:i + 65536] for i in range(0, len(body), 65536)]) == 200


def test_unparseable_body_on_lapsed_session_fails_closed(env) -> None:
    client, store, _ = env
    token, _ = store.issue_interactive("c1")
    _post(client, token, _init({"elicitation": {"url": {}}}))
    _lapse(store, token)
    res = client.post("/mcp", content=b"{not json", headers={"authorization": f"Bearer {token}"})
    assert res.status_code == 401
