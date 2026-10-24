"""Tool-call gate: hold the call until the human session is re-verified."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitationCapability, FormElicitationCapability, UrlElicitationCapability

from beaconmcp import reauth
from beaconmcp.auth import TokenStore
from beaconmcp.reauth import (
    ClientInfoRegistry,
    PendingReverifyStore,
    ReauthRuntime,
    gate_tool_call,
)

def _sign_in(store, client_id="c1"):
    return store.issue_oauth(client_id, refresh=True).access_token



class FakeCtx:
    def __init__(self, token: str, elicitation, *, on_elicit=None, result="accept"):
        caps = SimpleNamespace(elicitation=elicitation)
        self.session = SimpleNamespace(
            client_params=SimpleNamespace(capabilities=caps),
            send_elicit_complete=self._complete,
        )
        self.request_context = SimpleNamespace(
            request=SimpleNamespace(
                headers={"authorization": f"Bearer {token}", "user-agent": "ua/1"},
                client=SimpleNamespace(host="203.0.113.9"),
            ),
            session=self.session,
            request_id=7,
        )
        self.request_id = 7
        self.url_calls: list[dict] = []
        self.form_calls: list[str] = []
        self.completed: list[str] = []
        self._on_elicit = on_elicit
        self._result = result

    async def _complete(self, pid, related_request_id=None):
        self.completed.append((pid, related_request_id))

    async def elicit_url(self, message, url, elicitation_id):
        self.url_calls.append({"message": message, "url": url, "id": elicitation_id})
        if self._on_elicit:
            self._on_elicit(elicitation_id)
        return SimpleNamespace(action=self._result)

    async def elicit(self, message, schema):
        self.form_calls.append(message)
        if self._on_elicit:
            pid = message.split("id=")[1].split()[0]
            self._on_elicit(pid)
        return SimpleNamespace(action=self._result, data=None)


URL = ElicitationCapability(url=UrlElicitationCapability())
FORM_LEGACY = ElicitationCapability()
BOTH = ElicitationCapability(form=FormElicitationCapability(), url=UrlElicitationCapability())


@pytest.fixture()
def env():
    store = TokenStore()
    rt = ReauthRuntime(
        token_store=store, registry=ClientInfoRegistry(),
        pending=PendingReverifyStore(), session_ttl=60, hold_timeout=0.3,
        issuer_for=lambda req: "https://beacon.test",
    )
    reauth.configure(rt)
    token = _sign_in(store)
    yield rt, store, token
    reauth.configure(None)


def _lapse(store, token):
    store.get_interactive(token).verified_at = time.time() - 3600


def _verifier(rt, store, token):
    def verify(pid):
        store.mark_verified(store.get_interactive(token).key)
        rt.pending.complete(pid)
    return verify


async def test_fresh_session_runs_without_elicitation(env) -> None:
    rt, store, token = env
    ctx = FakeCtx(token, URL)
    await gate_tool_call(ctx, "proxmox_vm_start", {"vmid": 1})
    assert ctx.url_calls == [] and ctx.form_calls == []


async def test_url_client_elicits_then_runs_after_complete(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, BOTH, on_elicit=_verifier(rt, store, token))
    await gate_tool_call(ctx, "proxmox_vm_start", {"node": "pve-02", "vmid": 104})
    call = ctx.url_calls[0]
    assert call["url"] == f"https://beacon.test/oauth/reverify?id={call['id']}"
    assert "proxmox_vm_start" in call["message"]
    assert ctx.completed == [(call["id"], 7)]
    assert ctx.form_calls == []


async def test_form_client_gets_link_in_form(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, FORM_LEGACY, on_elicit=_verifier(rt, store, token))
    await gate_tool_call(ctx, "proxmox_vm_start", {})
    assert ctx.url_calls == []
    assert "https://beacon.test/oauth/reverify?id=" in ctx.form_calls[0]


async def test_hold_timeout_raises_tool_error_with_link(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, URL)
    with pytest.raises(ToolError, match=r"https://beacon\.test/oauth/reverify\?id="):
        await gate_tool_call(ctx, "proxmox_vm_start", {})


async def test_no_elicitation_capability_errors_with_link(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, None)
    with pytest.raises(ToolError, match="oauth/reverify"):
        await gate_tool_call(ctx, "proxmox_vm_start", {})
    assert ctx.url_calls == [] and ctx.form_calls == []


async def test_concurrent_calls_share_pending(env) -> None:
    rt, store, token = env
    rt.hold_timeout = 2
    _lapse(store, token)
    ctx = FakeCtx(token, URL)
    first = asyncio.create_task(gate_tool_call(ctx, "a", {}))
    second = asyncio.create_task(gate_tool_call(ctx, "b", {}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ctx.url_calls:
            break
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(ctx.url_calls) == 2:
            break
    # each call prompts, both with the same link
    assert len(ctx.url_calls) == 2
    assert ctx.url_calls[0]["id"] == ctx.url_calls[1]["id"]
    _verifier(rt, store, token)(ctx.url_calls[0]["id"])
    await asyncio.gather(first, second)


async def test_late_verify_then_retry_runs(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, URL)
    with pytest.raises(ToolError):
        await gate_tool_call(ctx, "a", {})
    _verifier(rt, store, token)(ctx.url_calls[0]["id"])
    retry = FakeCtx(token, URL)
    await gate_tool_call(retry, "a", {})
    assert retry.url_calls == []


async def test_decline_errors_immediately(env) -> None:
    rt, store, token = env
    rt.hold_timeout = 5
    _lapse(store, token)
    ctx = FakeCtx(token, URL, result="decline")
    start = time.monotonic()
    with pytest.raises(ToolError, match="oauth/reverify"):
        await gate_tool_call(ctx, "a", {})
    assert time.monotonic() - start < 1


async def test_retry_after_timeout_prompts_again(env) -> None:
    rt, store, token = env
    _lapse(store, token)
    ctx = FakeCtx(token, URL)
    with pytest.raises(ToolError):
        await gate_tool_call(ctx, "a", {})
    with pytest.raises(ToolError):
        await gate_tool_call(ctx, "a", {})
    assert len(ctx.url_calls) == 2
    assert ctx.url_calls[0]["id"] == ctx.url_calls[1]["id"]


async def test_verify_while_elicitation_pending_is_seen(env) -> None:
    rt, store, token = env
    rt.hold_timeout = 2
    _lapse(store, token)

    class Hanging(FakeCtx):
        async def elicit_url(self, message, url, elicitation_id):
            self.url_calls.append({"id": elicitation_id})
            await asyncio.sleep(10)

    ctx = Hanging(token, URL)
    task = asyncio.create_task(gate_tool_call(ctx, "a", {}))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if ctx.url_calls:
            break
    _verifier(rt, store, token)(ctx.url_calls[0]["id"])
    await asyncio.wait_for(task, 1)


async def test_named_token_untouched(env) -> None:
    rt, store, _ = env
    named, _ = store.issue("c1", name="x")
    await gate_tool_call(FakeCtx(named, URL), "a", {})


async def test_no_request_context_fails_closed(env) -> None:
    class NoReq:
        @property
        def request_context(self):
            raise ValueError("outside request")

    with pytest.raises(ToolError):
        await gate_tool_call(NoReq(), "a", {})
    with pytest.raises(ToolError):
        await gate_tool_call(None, "a", {})


async def test_no_runtime_is_noop(env) -> None:
    reauth.configure(None)
    await gate_tool_call(None, "a", {})
