"""Tests for the MCP Apps panels.

The value here is the wire format: a host only renders a panel if the tool
carries `_meta.ui.resourceUri` and the resource comes back under the mcp-app
MIME type, so these assert what goes out on the wire rather than the Python
objects behind it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.proxmox.panel import (
    APP_MIME_TYPE,
    CLUSTER_PANEL_URI,
    LOGS_PANEL_URI,
    VM_PANEL_URI,
    _syslog_level,
    _vm_snapshot,
    register_panel_tools,
)

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

_PANELS = [
    ("proxmox_vm_panel", VM_PANEL_URI),
    ("proxmox_logs_panel", LOGS_PANEL_URI),
    ("cluster_overview_interactive", CLUSTER_PANEL_URI),
]


class _Client:
    """Minimal ProxmoxClient stand-in keyed on the path suffix."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.configured_nodes = ["pve1"]

    def get(self, node: str, path: str, **kwargs: Any) -> Any:
        self.calls.append(path)
        self.kwargs.append(kwargs)
        for suffix, value in self.responses.items():
            if path.endswith(suffix):
                return value
        return _NOT_FOUND


def _server(client: Any) -> FastMCP:
    mcp = FastMCP("test")
    register_panel_tools(mcp, client)
    return mcp


async def _tool(mcp: FastMCP, name: str) -> Any:
    return next(t for t in await mcp.list_tools() if t.name == name)


# --- wire format ------------------------------------------------------------


@pytest.mark.parametrize("name,uri", _PANELS)
async def test_tool_points_at_its_panel(name: str, uri: str) -> None:
    tool = await _tool(_server(_Client()), name)
    assert tool.meta == {"ui": {"resourceUri": uri, "visibility": ["model", "app"]}}


@pytest.mark.parametrize("name,uri", _PANELS)
async def test_advertised_resource_resolves_as_an_app(name: str, uri: str) -> None:
    """A resourceUri that 404s on resources/read renders as a blank frame."""
    mcp = _server(_Client())
    listed = next(r for r in await mcp.list_resources() if str(r.uri) == uri)
    assert listed.mimeType == APP_MIME_TYPE

    contents = list(await mcp.read_resource(uri))
    assert contents, f"{uri} is advertised but reads back empty"


@pytest.mark.parametrize("name,uri", _PANELS)
async def test_shared_runtime_is_spliced_in(name: str, uri: str) -> None:
    """The marker must be gone and both halves of the runtime present.

    A panel that ships with the literal <!--mcp-runtime--> still in it has no
    bridge and no styles: it loads, does nothing, and never speaks to the host.
    """
    html = list(await _server(_Client()).read_resource(uri))[0].content

    assert "<!--mcp-runtime-->" not in html
    assert "const MCPApp" in html, "bridge.js missing"
    assert "--font-sans" in html, "panel.css missing"


async def test_handshake_params_are_flat() -> None:
    """ui/initialize takes appInfo / appCapabilities / protocolVersion, flat.

    Nesting them under `capabilities`, or sending `clientInfo` instead of
    `appInfo`, fails the host's schema check -- and a rejected handshake is
    silent: the host just never replies, so the app never sends `initialized`
    and never receives the tool result. The panel then sits on "Loading..."
    with nothing in the console to explain it.
    """
    html = list(await _server(_Client()).read_resource(VM_PANEL_URI))[0].content
    params = html.split('request("ui/initialize", {', 1)[1].split("})", 1)[0]

    assert "appInfo:" in params
    assert "appCapabilities:" in params
    assert "protocolVersion:" in params
    assert "clientInfo" not in params
    assert "capabilities: {" not in params


# --- VM snapshot ------------------------------------------------------------


def test_qemu_disk_usage_is_null_not_zero() -> None:
    """0 from QEMU means "no guest agent", which must not render as an empty bar."""
    client = _Client({"qemu/100/status/current": _QEMU_STATUS, "qemu/100/config": {"cores": 4, "memory": 4096}})

    snap = _vm_snapshot(client, "pve1", 100)

    assert snap["disk_used_gb"] is None
    assert snap["disk_max_gb"] == 32.0


def test_qemu_snapshot_maps_status_and_config() -> None:
    client = _Client({"qemu/100/status/current": _QEMU_STATUS, "qemu/100/config": {"cores": 4, "memory": 4096}})

    assert _vm_snapshot(client, "pve1", 100) == {
        "node": "pve1", "vmid": 100, "type": "qemu", "name": "web-101",
        "status": "running", "cpu_pct": 12.3, "cpus": 4,
        "mem_used_mb": 2048, "mem_max_mb": 4096,
        "disk_used_gb": None, "disk_max_gb": 32.0, "uptime_h": 2.0,
        "cores": 4, "memory_mb": 4096,
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

    snap = _vm_snapshot(client, "pve1", 200)

    assert snap["type"] == "lxc"
    assert snap["disk_used_gb"] == 5.0


def test_missing_guest_reports_an_error() -> None:
    assert "not found" in _vm_snapshot(_Client(), "pve1", 999)["error"]


def test_unreadable_config_still_renders_live_state() -> None:
    """A config read can fail on its own; the panel should degrade, not blank out."""
    client = _Client({
        "qemu/100/status/current": _QEMU_STATUS,
        "qemu/100/config": {"error": "connection refused"},
    })

    snap = _vm_snapshot(client, "pve1", 100)

    assert snap["status"] == "running"
    assert snap["cores"] is None


def test_transport_error_is_not_mistaken_for_a_missing_guest() -> None:
    """Only a "does not exist" error means "try the other guest type"."""
    client = _Client({"qemu/100/status/current": {"error": "connection refused"}})

    snap = _vm_snapshot(client, "pve1", 100)

    assert snap["error"] == "connection refused"
    assert not any("lxc" in call for call in client.calls)


# --- logs panel -------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("kernel: EXT4-fs error (device sda1)", "error"),
        ("corosync: connection refused to node pve2", "error"),
        ("pvedaemon: deprecated option 'foo'", "warn"),
        ("systemd[1]: Started Daily apt upgrade.", "info"),
    ],
)
def test_syslog_lines_are_classified(line: str, expected: str) -> None:
    assert _syslog_level(line) == expected


async def test_logs_panel_labels_syslog_lines() -> None:
    client = _Client({"syslog": [{"t": "kernel: I/O error"}, {"t": "systemd: Started thing"}]})
    tool = await _server(client).call_tool("proxmox_logs_panel", {"node": "pve1"})

    entries = tool[1]["entries"]
    assert [e["level"] for e in entries] == ["error", "info"]
    assert entries[0]["text"] == "kernel: I/O error"


async def test_logs_panel_flags_failed_tasks() -> None:
    """Proxmox writes "OK" on success; anything else is the failure message."""
    client = _Client({"tasks": [
        {"upid": "UPID:1", "type": "vzdump", "status": "OK", "user": "root@pam"},
        {"upid": "UPID:2", "type": "qmstart", "status": "unable to start VM", "user": "root@pam"},
        {"upid": "UPID:3", "type": "qmigrate", "status": None, "user": "root@pam"},
    ]})

    result = await _server(client).call_tool(
        "proxmox_logs_panel", {"node": "pve1", "source": "tasks"},
    )

    # A running task has no status yet and must not be painted as a failure.
    assert [e["level"] for e in result[1]["entries"]] == ["info", "error", "info"]


async def test_logs_panel_caps_the_line_count() -> None:
    """The Proxmox API rejects anything above 500, and the panel's box has no max."""
    client = _Client({"syslog": []})
    await _server(client).call_tool("proxmox_logs_panel", {"node": "pve1", "limit": 9000})

    assert client.kwargs[0]["limit"] == 500
