"""Tests for the MCP Apps VM panel.

The value here is the wire format: a host only renders the panel if the tool
carries `_meta.ui.resourceUri` and the resource comes back under the mcp-app
MIME type, so these assert what goes out on the wire rather than the Python
objects behind it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.proxmox.panel import APP_MIME_TYPE, PANEL_URI, _snapshot, register_panel_tools

_NOT_FOUND = {"error": "Configuration file 'nodes/pve1/qemu/100.conf' does not exist"}

_QEMU_STATUS = {
    "status": "running",
    "name": "web-101",
    "cpu": 0.1234,
    "cpus": 4,
    "mem": 2 * 1048576 * 1024,
    "maxmem": 4 * 1048576 * 1024,
    # QEMU reports 0 unless the guest agent answers.
    "disk": 0,
    "maxdisk": 32 * 1073741824,
    "uptime": 7200,
}


class _Client:
    """Minimal ProxmoxClient stand-in keyed on the path suffix."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, node: str, path: str) -> Any:
        self.calls.append(path)
        for suffix, value in self.responses.items():
            if path.endswith(suffix):
                return value
        return _NOT_FOUND


def _server(client: Any) -> FastMCP:
    mcp = FastMCP("test")
    register_panel_tools(mcp, client)
    return mcp


# --- wire format ------------------------------------------------------------


async def test_tool_points_at_the_panel_resource() -> None:
    tools = await _server(_Client({})).list_tools()
    panel = next(t for t in tools if t.name == "proxmox_vm_panel")
    assert panel.meta == {"ui": {"resourceUri": PANEL_URI, "visibility": ["model", "app"]}}


async def test_resource_is_served_as_an_mcp_app() -> None:
    resources = await _server(_Client({})).list_resources()
    panel = next(r for r in resources if str(r.uri) == PANEL_URI)
    assert panel.mimeType == APP_MIME_TYPE


async def test_advertised_resource_uri_actually_resolves() -> None:
    """A tool whose resourceUri 404s on resources/read renders as a blank frame."""
    mcp = _server(_Client({}))
    tools = await mcp.list_tools()
    uri = next(t for t in tools if t.name == "proxmox_vm_panel").meta["ui"]["resourceUri"]

    contents = list(await mcp.read_resource(uri))

    assert contents, f"{uri} is advertised but reads back empty"
    assert "ui/initialize" in contents[0].content, "panel HTML is missing the host handshake"


async def test_handshake_params_are_flat() -> None:
    """ui/initialize takes appInfo / appCapabilities / protocolVersion, flat.

    Nesting them under `capabilities`, or sending `clientInfo` instead of
    `appInfo`, fails the host's schema check -- and a rejected handshake is
    silent: the host just never replies, so the app never sends `initialized`
    and never receives the tool result. The panel then sits on "Loading..."
    with nothing in the console to explain it.
    """
    mcp = _server(_Client({}))
    html = list(await mcp.read_resource(PANEL_URI))[0].content
    params = html.split('request("ui/initialize", {', 1)[1].split("})", 1)[0]

    assert "appInfo:" in params
    assert "appCapabilities:" in params
    assert "protocolVersion:" in params
    assert "clientInfo" not in params
    assert "capabilities: {" not in params


# --- snapshot mapping -------------------------------------------------------


def test_qemu_disk_usage_is_null_not_zero() -> None:
    """0 from QEMU means "no guest agent", which must not render as an empty bar."""
    client = _Client({"qemu/100/status/current": _QEMU_STATUS, "qemu/100/config": {"cores": 4, "memory": 4096}})

    snap = _snapshot(client, "pve1", 100)

    assert snap["disk_used_gb"] is None
    assert snap["disk_max_gb"] == 32.0


def test_qemu_snapshot_maps_status_and_config() -> None:
    client = _Client({"qemu/100/status/current": _QEMU_STATUS, "qemu/100/config": {"cores": 4, "memory": 4096}})

    snap = _snapshot(client, "pve1", 100)

    assert snap == {
        "node": "pve1",
        "vmid": 100,
        "type": "qemu",
        "name": "web-101",
        "status": "running",
        "cpu_pct": 12.3,
        "cpus": 4,
        "mem_used_mb": 2048,
        "mem_max_mb": 4096,
        "disk_used_gb": None,
        "disk_max_gb": 32.0,
        "uptime_h": 2.0,
        "cores": 4,
        "memory_mb": 4096,
    }


def test_falls_through_to_lxc_when_no_qemu_guest() -> None:
    client = _Client({
        "lxc/200/status/current": {
            "status": "running", "name": "ct", "cpu": 0, "cpus": 1,
            "mem": 0, "maxmem": 536870912,
            "disk": 5 * 1073741824, "maxdisk": 10 * 1073741824, "uptime": 0,
        },
        "lxc/200/config": {"cores": 1, "memory": 512},
    })

    snap = _snapshot(client, "pve1", 200)

    assert snap["type"] == "lxc"
    assert snap["disk_used_gb"] == 5.0


def test_missing_guest_reports_an_error() -> None:
    snap = _snapshot(_Client({}), "pve1", 999)

    assert "not found" in snap["error"]


def test_unreadable_config_still_renders_live_state() -> None:
    """A config read can fail on its own; the panel should degrade, not blank out."""
    client = _Client({
        "qemu/100/status/current": _QEMU_STATUS,
        "qemu/100/config": {"error": "connection refused"},
    })

    snap = _snapshot(client, "pve1", 100)

    assert snap["status"] == "running"
    assert snap["cores"] is None


def test_transport_error_is_not_mistaken_for_a_missing_guest() -> None:
    """Only a "does not exist" error means "try the other guest type"."""
    client = _Client({"qemu/100/status/current": {"error": "connection refused"}})

    snap = _snapshot(client, "pve1", 100)

    assert snap["error"] == "connection refused"
    assert not any("lxc" in call for call in client.calls)
