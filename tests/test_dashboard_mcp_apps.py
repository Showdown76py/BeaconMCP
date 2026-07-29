"""MCP Apps support in the dashboard chat (#35).

The dashboard is both the MCP client that negotiates the extension and the
host that renders the ``ui://`` documents. These tests pin the parts a
refactor could quietly break without any visible symptom: the capability on
the wire, the headers that decide whether a panel can be framed at all and
what it may reach from inside the frame, and the allow-list that says which
tool calls a panel may make on its own.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.dashboard import mcp_bridge
from beaconmcp.dashboard.app import DashboardDeps, build_dashboard_routes
from beaconmcp.dashboard.chat import (
    FakeChatEngine,
    FakeScript,
    ToolCallEnd,
    ToolCallStart,
    assemble_assistant_message,
    format_app_context,
    panel_call_allowed,
)
from beaconmcp.dashboard.confirmations import ConfirmationStore
from beaconmcp.dashboard.conversations import ConversationStore
from beaconmcp.dashboard.csrf import CSRF_COOKIE
from beaconmcp.dashboard.db import Database
from beaconmcp.dashboard.session import SessionStore

from test_dashboard_chat import FakeClientStore, FakeTokenStore  # noqa: E402


PANEL_URI = "ui://beaconmcp/vm-panel.html"
PANEL_HTML = "<!doctype html><title>vm</title><script>1</script>"


# ---------------------------------------------------------------------------
# Client half: capability negotiation and _meta.ui plumbing
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_initialize_declares_the_apps_extension(monkeypatch):
    """The extension rides on ``ClientCapabilities`` even on mcp 1.x.

    ``extensions`` only becomes a typed field in mcp 2.0, which is what made
    this look blocked behind the ``<2`` pin. It is not: the model allows
    extras, so the field serialises under the name the spec gives it. If a
    future SDK bump makes ``ClientCapabilities`` strict, this fails here
    rather than silently dropping the capability at runtime.
    """
    from mcp import types
    from mcp.client.session import ClientSession

    captured: dict = {}

    async def fake_send_request(self, request, result_type, **kwargs):
        captured["request"] = request
        return None

    monkeypatch.setattr(ClientSession, "send_request", fake_send_request)

    session = object.__new__(mcp_bridge.AppsClientSession)
    request = types.ClientRequest(
        types.InitializeRequest(
            params=types.InitializeRequestParams(
                protocolVersion=types.LATEST_PROTOCOL_VERSION,
                capabilities=types.ClientCapabilities(),
                clientInfo=types.Implementation(name="t", version="1"),
            ),
        )
    )
    await session.send_request(request, types.InitializeResult)

    wire = captured["request"].model_dump(by_alias=True, exclude_none=True)
    extensions = wire["params"]["capabilities"]["extensions"]
    assert extensions == {
        "io.modelcontextprotocol/ui": {
            "mimeTypes": ["text/html;profile=mcp-app"],
        }
    }


@pytest.mark.anyio
async def test_non_initialize_requests_pass_through_untouched(monkeypatch):
    """Only the handshake is rewritten; every other request goes as-is."""
    from mcp import types
    from mcp.client.session import ClientSession

    captured: dict = {}

    async def fake_send_request(self, request, result_type, **kwargs):
        captured["request"] = request
        return None

    monkeypatch.setattr(ClientSession, "send_request", fake_send_request)

    session = object.__new__(mcp_bridge.AppsClientSession)
    original = types.ClientRequest(types.ListToolsRequest(method="tools/list"))
    await session.send_request(original, types.ListToolsResult)

    assert captured["request"] is original
    assert captured["request"].model_dump(by_alias=True, exclude_none=True) == {
        "method": "tools/list",
    }


def test_ui_resource_uri_extraction():
    assert mcp_bridge.ui_resource_uri({"ui": {"resourceUri": PANEL_URI}}) == PANEL_URI
    assert mcp_bridge.ui_resource_uri(None) is None
    assert mcp_bridge.ui_resource_uri({"ui": {}}) is None
    # A non-ui:// target is not a panel. Honouring one would let a tool
    # point the host's iframe at an arbitrary URL.
    assert mcp_bridge.ui_resource_uri({"ui": {"resourceUri": "https://evil/x"}}) is None
    assert mcp_bridge.ui_resource_uri({"ui": {"resourceUri": "file:///etc/passwd"}}) is None


def test_ui_resource_uris_by_tool():
    class _Tool:
        def __init__(self, name, meta):
            self.name = name
            self.meta = meta

    mapping = mcp_bridge.ui_resource_uris_by_tool([
        _Tool("proxmox_vm_panel", {"ui": {"resourceUri": PANEL_URI}}),
        _Tool("proxmox_list_vms", None),
    ])
    assert mapping == {"proxmox_vm_panel": PANEL_URI}


class _FakeResourceContent:
    def __init__(self, text, mime):
        self.text = text
        self.mimeType = mime


class _FakeReadResult:
    def __init__(self, contents):
        self.contents = contents


class _FakeSession:
    def __init__(self, contents):
        self._contents = contents

    async def read_resource(self, uri):
        return _FakeReadResult(self._contents)


@pytest.mark.anyio
async def test_read_ui_resource_accepts_an_app_document():
    session = _FakeSession([_FakeResourceContent(PANEL_HTML, "text/html;profile=mcp-app")])
    assert await mcp_bridge.read_ui_resource(session, PANEL_URI) == PANEL_HTML


@pytest.mark.anyio
async def test_read_ui_resource_refuses_a_plain_resource():
    """The MIME check is what stops this being a generic resource proxy."""
    session = _FakeSession([_FakeResourceContent("nodes: []", "text/plain")])
    with pytest.raises(mcp_bridge.UiResourceError):
        await mcp_bridge.read_ui_resource(session, PANEL_URI)


@pytest.mark.anyio
async def test_read_ui_resource_refuses_a_non_ui_scheme():
    session = _FakeSession([_FakeResourceContent("x", "text/html;profile=mcp-app")])
    with pytest.raises(mcp_bridge.UiResourceError):
        await mcp_bridge.read_ui_resource(session, "beaconmcp://infrastructure")


# ---------------------------------------------------------------------------
# The panel allow-list -- the decision #35 asked to be made explicitly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,args", [
    ("proxmox_vm_panel", {"node": "pve1", "vmid": 104}),
    ("cluster_overview_interactive", {}),
    ("proxmox_vm_start", {"node": "pve1", "vmid": 104}),
    ("proxmox_vm_stop", {"node": "pve1", "vmid": 104}),
    ("proxmox_vm_restart", {"node": "pve1", "vmid": 104}),
    ("proxmox_vm_config", {"node": "pve1", "vmid": 104, "updates": {"cores": 4}}),
])
def test_panel_may_drive_guest_lifecycle(name, args):
    """A labelled button on one guest is the click; a modal would restate it."""
    assert panel_call_allowed(name, args) is True


@pytest.mark.parametrize("name,args", [
    ("ssh_run", {"host": "pve1", "command": "rm -rf /"}),
    ("proxmox_run", {"node": "pve1", "vmid": 104, "command": "id"}),
    ("proxmox_write_file", {"node": "pve1", "vmid": 104, "path": "/root/.ssh/authorized_keys"}),
    ("vm_bulk_action", {"vmids": [1, 2, 3], "action": "stop"}),
    ("proxmox_snapshot_rollback", {"node": "pve1", "vmid": 104, "snapname": "s"}),
    ("proxmox_backup_restore", {"node": "pve1", "vmid": 104}),
    ("bmc_power_off", {"device_id": "rack1"}),
    # updates is an open-ended guest config. Exempting the tool wholesale
    # would exempt hookscript, raw QEMU args and device passthrough with it.
    ("proxmox_vm_config", {"node": "pve1", "vmid": 104,
                           "updates": {"hookscript": "local:snippets/x.sh"}}),
    ("proxmox_vm_config", {"node": "pve1", "vmid": 104,
                           "updates": {"cores": 4, "args": "-device x"}}),
    # Pulls new code, reinstalls dependencies and restarts the service.
    ("beaconmcp_self_update", {"confirm": True}),
])
def test_panel_may_not_reach_the_gated_tools(name, args):
    """The exemption is a closed list, not "iframe calls skip the gate".

    A ui:// document is HTML the server wrote. Letting one through the gate
    by virtue of being in a frame would hand every connected MCP server a
    way around the approval it is documented to be subject to.
    """
    assert panel_call_allowed(name, args) is False


# ---------------------------------------------------------------------------
# Model context from panels
# ---------------------------------------------------------------------------

def test_format_app_context_labels_the_source():
    block = format_app_context([
        {"tool": "proxmox_vm_panel", "text": "VM 104 is now stopped.", "structured": {"status": "stopped"}},
    ])
    assert "proxmox_vm_panel" in block
    assert "VM 104 is now stopped." in block
    assert '"status": "stopped"' in block
    # The model has to know this did not come from the operator's keyboard.
    assert "did not go through you" in block


def test_format_app_context_empty():
    assert format_app_context([]) == ""
    assert format_app_context(None) == ""
    assert format_app_context([{"tool": "x"}]) == ""


def test_format_app_context_truncates():
    block = format_app_context([
        {"tool": "cluster", "text": "x" * 10_000, "structured": None},
    ])
    assert len(block) < 5_000


def test_assemble_persists_only_the_panel_uri():
    """The snapshot is not stored -- reopening refetches instead."""
    content, tool_calls, _ = assemble_assistant_message([
        ToolCallStart(id="1", name="proxmox_vm_panel", args={"node": "pve1", "vmid": 104}),
        ToolCallEnd(
            id="1", status="ok", preview="{}", duration_ms=12,
            ui={"resourceUri": PANEL_URI, "result": {"structuredContent": {"vmid": 104}}},
        ),
    ])
    assert tool_calls[0].ui_resource_uri == PANEL_URI
    assert "result" not in tool_calls[0].to_json()


# ---------------------------------------------------------------------------
# Host routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    return FakeChatEngine(FakeScript(events=[
        ToolCallStart(id="fc_0", name="proxmox_vm_panel", args={"node": "pve1", "vmid": 104}),
        ToolCallEnd(
            id="fc_0", status="ok", preview='{"vmid": 104}', duration_ms=7,
            ui={
                "resourceUri": PANEL_URI,
                "result": {"content": [], "isError": False,
                           "structuredContent": {"vmid": 104, "status": "running"}},
            },
        ),
    ]))


@pytest.fixture()
def deps(tmp_path, engine):
    db = Database(tmp_path / "dashboard.db")
    return DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
        confirmations=ConfirmationStore(),
    )


@pytest.fixture()
def client(deps):
    mcp_bridge.cache_clear()
    app = Starlette(routes=build_dashboard_routes(deps))
    return TestClient(app, follow_redirects=False)


def _login(client):
    r = client.get("/app/login")
    csrf = r.cookies.get(CSRF_COOKIE)
    r = client.post("/app/login", data={
        "csrf_token": csrf, "client_id": "c",
        "client_secret": "s", "totp": "123456", "remember": "on",
    })
    assert r.status_code == 303
    return client.cookies.get(CSRF_COOKIE)


class _StubSession:
    """Stands in for a live MCP session in the two host routes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))

        class _Result:
            content = []
            isError = False
            structuredContent = {"ok": True}

        return _Result()


@pytest.fixture()
def stub_mcp(monkeypatch):
    stub = _StubSession()

    @asynccontextmanager
    async def fake_open_session(url, bearer, **kwargs):
        yield stub

    async def fake_read(session, uri):
        if uri != PANEL_URI:
            raise mcp_bridge.UiResourceError("unknown resource")
        return PANEL_HTML

    monkeypatch.setattr(mcp_bridge, "open_session", fake_open_session)
    monkeypatch.setattr(mcp_bridge, "read_ui_resource", fake_read)
    return stub


# --- panel document ---------------------------------------------------------

def test_panel_route_requires_a_session(client, stub_mcp):
    r = client.get(f"/app/api/mcp/panel?uri={PANEL_URI}")
    assert r.status_code == 401


def test_panel_route_rejects_a_non_ui_uri(client, stub_mcp):
    _login(client)
    r = client.get("/app/api/mcp/panel?uri=beaconmcp://infrastructure")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_uri"


def test_panel_route_serves_the_document_with_its_own_csp(client, stub_mcp):
    """Framable by us, inline script allowed, and no way out of the frame.

    The default dashboard headers are the opposite on both counts --
    ``X-Frame-Options: DENY`` would stop the panel rendering at all and
    ``script-src 'self'`` would kill the inline bridge -- so this route
    setting its own is load-bearing, not tidiness.
    """
    _login(client)
    r = client.get(f"/app/api/mcp/panel?uri={PANEL_URI}")
    assert r.status_code == 200
    assert r.text == PANEL_HTML

    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "script-src 'unsafe-inline'" in csp
    # The panel reaches the cluster through its parent, never directly.
    assert "connect-src 'none'" in csp
    assert "default-src 'none'" in csp
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "DENY" not in r.headers["x-frame-options"]


def test_panel_route_reports_an_unreadable_resource(client, stub_mcp):
    _login(client)
    r = client.get("/app/api/mcp/panel?uri=ui://beaconmcp/nope.html")
    assert r.status_code == 502
    assert r.json()["error"] == "resource_unavailable"


def test_panel_documents_are_cached(client, stub_mcp, monkeypatch):
    _login(client)
    reads: list[str] = []

    async def counting_read(session, uri):
        reads.append(uri)
        return PANEL_HTML

    monkeypatch.setattr(mcp_bridge, "read_ui_resource", counting_read)
    for _ in range(3):
        assert client.get(f"/app/api/mcp/panel?uri={PANEL_URI}").status_code == 200
    assert len(reads) == 1


# --- tools/call relay -------------------------------------------------------

def test_relay_requires_a_session(client, stub_mcp):
    r = client.post(
        "/app/api/mcp/call",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"name": "proxmox_vm_stop", "arguments": {}}),
    )
    assert r.status_code == 401


def test_relay_requires_csrf(client, stub_mcp):
    _login(client)
    r = client.post(
        "/app/api/mcp/call",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"name": "proxmox_vm_stop", "arguments": {}}),
    )
    assert r.status_code == 403
    assert r.json()["error"] == "csrf"


def test_relay_runs_an_allowed_tool(client, stub_mcp):
    csrf = _login(client)
    r = client.post(
        "/app/api/mcp/call",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({
            "name": "proxmox_vm_stop", "arguments": {"node": "pve1", "vmid": 104},
        }),
    )
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"] == {"ok": True}
    assert stub_mcp.calls == [("proxmox_vm_stop", {"node": "pve1", "vmid": 104})]


def test_relay_refuses_a_gated_tool_without_calling_it(client, stub_mcp):
    csrf = _login(client)
    r = client.post(
        "/app/api/mcp/call",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({
            "name": "ssh_run", "arguments": {"host": "pve1", "command": "id"},
        }),
    )
    assert r.status_code == 403
    assert r.json()["error"] == "confirmation_required"
    assert stub_mcp.calls == []


def test_relay_rejects_a_malformed_body(client, stub_mcp):
    csrf = _login(client)
    r = client.post(
        "/app/api/mcp/call",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"name": "", "arguments": {}}),
    )
    assert r.status_code == 400


# --- turn plumbing ----------------------------------------------------------

def _parse_sse(text):
    events = []
    for frame in text.strip().split("\n\n"):
        ev, data = None, []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        events.append((ev, json.loads("\n".join(data)) if data else {}))
    return events


def test_tool_result_frame_carries_the_panel(client, deps):
    """Without this the browser never learns there is a frame to mount."""
    csrf = _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content="{}",
    )
    conv_id = r.json()["conversation"]["id"]

    r = client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({"conversation_id": conv_id, "content": "open vm 104"}),
    )
    results = [d for e, d in _parse_sse(r.text) if e == "tool_result"]
    assert results and results[0]["ui"]["resourceUri"] == PANEL_URI
    assert results[0]["ui"]["result"]["structuredContent"]["status"] == "running"

    stored = deps.conversations.list_messages(conv_id)[1].tool_calls[0]
    assert stored.ui_resource_uri == PANEL_URI


def test_app_context_reaches_the_turn(client, engine):
    csrf = _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content="{}",
    )
    conv_id = r.json()["conversation"]["id"]

    client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({
            "conversation_id": conv_id,
            "content": "et maintenant ?",
            "app_context": [
                {"tool": "proxmox_vm_panel", "text": "VM 104 is now stopped.",
                 "structured": {"status": "stopped"}},
                "junk",
                {"tool": "x"},
            ],
        }),
    )
    assert engine.calls[0].app_context == [
        {"tool": "proxmox_vm_panel", "text": "VM 104 is now stopped.",
         "structured": {"status": "stopped"}},
    ]


def test_app_context_entry_count_is_capped(client, engine):
    csrf = _login(client)
    r = client.post(
        "/app/api/conversations",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content="{}",
    )
    conv_id = r.json()["conversation"]["id"]
    client.post(
        "/app/api/chat/stream",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content=json.dumps({
            "conversation_id": conv_id,
            "content": "ping",
            "app_context": [
                {"tool": f"p{i}", "text": "x"} for i in range(50)
            ],
        }),
    )
    assert len(engine.calls[0].app_context) == 8
