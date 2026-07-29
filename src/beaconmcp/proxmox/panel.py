"""MCP Apps panel for a single VM/CT.

The MCP Apps extension (`io.modelcontextprotocol/ui`) lets a tool carry a
reference to an interactive UI: `_meta.ui.resourceUri` points at a `ui://`
resource served as `text/html;profile=mcp-app`, which the host renders in a
sandboxed iframe and talks to over JSON-RPC on `postMessage`.

The wire format is all this module needs, so it runs on mcp 1.x. The `Apps`
extension class that wraps it lives in mcp 2.0 and requires `MCPServer`; the
two knobs it sets -- `meta=` on the tool and `mime_type=` on the resource --
are already on `FastMCP`.

Hosts that did not negotiate Apps ignore `_meta.ui` and just show the tool's
return value, which is why `proxmox_vm_panel` returns the full snapshot as
data rather than a "see the panel" placeholder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ProxmoxClient

PANEL_URI = "ui://beaconmcp/vm-panel.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"

_PANEL_HTML = Path(__file__).parent / "vm_panel.html"

_MB = 1048576
_GB = 1073741824


def _snapshot(client: ProxmoxClient, node: str, vmid: int) -> dict[str, Any]:
    """Everything the panel renders, in one pass over both guest types."""
    for vm_type in ("qemu", "lxc"):
        data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
        if isinstance(data, dict) and "error" in data:
            if "does not exist" in str(data["error"]).lower():
                continue
            return data
        if not isinstance(data, dict) or not data.get("status"):
            continue

        # QEMU reports disk=0 unless the guest agent is answering, so a zero
        # here means "unknown", not "empty". LXC reports it for real.
        disk_used = data.get("disk") or 0
        conf = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/config")
        if not isinstance(conf, dict) or "error" in conf:
            conf = {}

        return {
            "node": node,
            "vmid": vmid,
            "type": vm_type,
            "name": data.get("name", ""),
            "status": data.get("status"),
            "cpu_pct": round(data.get("cpu", 0) * 100, 1),
            "cpus": data.get("cpus"),
            "mem_used_mb": round(data.get("mem", 0) / _MB),
            "mem_max_mb": round(data.get("maxmem", 0) / _MB),
            "disk_used_gb": round(disk_used / _GB, 1) if disk_used else None,
            "disk_max_gb": round(data.get("maxdisk", 0) / _GB, 1),
            "uptime_h": round(data.get("uptime", 0) / 3600, 1),
            "cores": conf.get("cores"),
            "memory_mb": conf.get("memory"),
        }

    return {"error": f"VM/CT {vmid} not found on node '{node}'. Check the VMID and node name."}


def register_panel_tools(mcp: FastMCP, client: ProxmoxClient) -> None:
    @mcp.tool(
        meta={"ui": {"resourceUri": PANEL_URI, "visibility": ["model", "app"]}},
    )
    def proxmox_vm_panel(node: str, vmid: int) -> dict[str, Any]:
        """Open an interactive control panel for one VM or container.

        Renders live CPU / RAM / disk state with buttons for start, stop and
        restart, and fields to change the CPU core count and memory. Use this
        instead of proxmox_vm_status when the user wants to *act* on a guest
        rather than just read its numbers, or when they ask to "manage",
        "control" or "open" a VM.

        The panel drives the ordinary tools (proxmox_vm_start / _stop /
        _restart / _config), so every action it takes goes through the same
        approval the client applies to any other tool call.

        Returns: {node, vmid, type, name, status, cpu_pct, cpus, mem_used_mb,
        mem_max_mb, disk_used_gb, disk_max_gb, uptime_h, cores, memory_mb}.
        ``disk_used_gb`` is null when the guest does not report it.
        """
        return _snapshot(client, node, vmid)

    @mcp.resource(
        PANEL_URI,
        name="vm-panel",
        title="VM control panel",
        description="Interactive panel for a single Proxmox VM or container.",
        mime_type=APP_MIME_TYPE,
    )
    def vm_panel_app() -> str:
        return _PANEL_HTML.read_text(encoding="utf-8")
