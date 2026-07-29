"""MCP Apps panels.

The MCP Apps extension (`io.modelcontextprotocol/ui`) lets a tool carry a
reference to an interactive UI: `_meta.ui.resourceUri` points at a `ui://`
resource served as `text/html;profile=mcp-app`, which the host renders in a
sandboxed iframe and talks to over JSON-RPC on `postMessage`.

The wire format is all this module needs, so it runs on mcp 1.x. The `Apps`
extension class that wraps it lives in mcp 2.0 and requires `MCPServer`; the
two knobs it sets -- `meta=` on the tool and `mime_type=` on the resource --
are already on `FastMCP`.

Hosts that did not negotiate Apps ignore `_meta.ui` and just show the tool's
return value, which is why every panel tool returns its full snapshot as data
rather than a "see the panel" placeholder.

Each panel is one HTML file under ``apps/``. They share ``bridge.js`` (the
JSON-RPC client) and ``panel.css`` (the look), spliced in at the
``<!--mcp-runtime-->`` marker so what ships to the host stays a single
self-contained document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .aggregators import (
    _collect_node_summaries,
    _collect_storage_summaries,
    _collect_vm_summaries,
)
from .client import ProxmoxClient

APP_MIME_TYPE = "text/html;profile=mcp-app"

VM_PANEL_URI = "ui://beaconmcp/vm-panel.html"
LOGS_PANEL_URI = "ui://beaconmcp/logs-panel.html"
CLUSTER_PANEL_URI = "ui://beaconmcp/cluster-panel.html"

_APPS_DIR = Path(__file__).parent / "apps"
_RUNTIME_MARKER = "<!--mcp-runtime-->"

_MB = 1048576
_GB = 1073741824


def _read_app(name: str) -> str:
    """Load a panel document with the shared CSS and bridge spliced in."""
    html = (_APPS_DIR / name).read_text(encoding="utf-8")
    runtime = (
        f"<style>{(_APPS_DIR / 'panel.css').read_text(encoding='utf-8')}</style>"
        f"<script>{(_APPS_DIR / 'bridge.js').read_text(encoding='utf-8')}</script>"
    )
    return html.replace(_RUNTIME_MARKER, runtime)


def _vm_snapshot(client: ProxmoxClient, node: str, vmid: int) -> dict[str, Any]:
    """Everything the VM panel renders, in one pass over both guest types."""
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
        meta={"ui": {"resourceUri": VM_PANEL_URI, "visibility": ["model", "app"]}},
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
        return _vm_snapshot(client, node, vmid)

    @mcp.tool(
        meta={"ui": {"resourceUri": LOGS_PANEL_URI, "visibility": ["model", "app"]}},
    )
    def proxmox_logs_panel(node: str, source: str = "syslog", limit: int = 200) -> dict[str, Any]:
        """Open a scrollable, filterable log and task viewer for one node.

        Prefer this over proxmox_get_logs whenever the user wants to *read*
        logs rather than have them summarised: the panel keeps every line,
        highlights errors and warnings, filters as you type, and can switch
        between the syslog and the Proxmox task list without another turn.

        Args:
            source: 'syslog' for system logs, 'tasks' for the task history.
            limit: Lines to fetch, capped at 500 by the Proxmox API.

        Returns: {node, source, entries: [...]}. For syslog each entry is
        {text, level} where level is error/warn/info, guessed from the line.
        For tasks each entry is {upid, type, status, user, starttime, endtime,
        level}.
        """
        limit = min(limit, 500)

        if source == "tasks":
            data = client.get(node, f"nodes/{node}/tasks", limit=limit)
            if isinstance(data, dict) and "error" in data:
                return data
            entries = [
                {
                    "upid": t.get("upid"),
                    "type": t.get("type"),
                    "status": t.get("status"),
                    "user": t.get("user"),
                    "starttime": t.get("starttime"),
                    "endtime": t.get("endtime"),
                    # Proxmox writes "OK" for success and a message otherwise;
                    # a still-running task has no status yet.
                    "level": "info" if t.get("status") in ("OK", None) else "error",
                }
                for t in (data if isinstance(data, list) else [])
            ]
            return {"node": node, "source": "tasks", "entries": entries}

        data = client.get(node, f"nodes/{node}/syslog", limit=limit)
        if isinstance(data, dict) and "error" in data:
            return data
        entries = [
            {"text": line, "level": _syslog_level(line)}
            for line in (entry.get("t", "") for entry in (data if isinstance(data, list) else []))
        ]
        return {"node": node, "source": "syslog", "entries": entries}

    @mcp.tool(
        meta={"ui": {"resourceUri": CLUSTER_PANEL_URI, "visibility": ["model", "app"]}},
    )
    def cluster_overview_interactive(include_storage: bool = True) -> dict[str, Any]:
        """Open an interactive cluster dashboard: nodes, guests and storage.

        The same data as cluster_overview, but rendered as a browsable panel:
        nodes with CPU and memory pressure, a searchable guest table with
        per-row start/stop, and storage pools with usage bars. Use it when the
        user wants to look around the cluster rather than ask one question
        about it.

        Returns: {nodes: [...], vms: [...], total_vms, storage: [...]}.
        """
        nodes = _collect_node_summaries(client)
        vms, total_vms = _collect_vm_summaries(client)
        out: dict[str, Any] = {"nodes": nodes, "vms": vms, "total_vms": total_vms}
        if include_storage:
            out["storage"] = _collect_storage_summaries(client)
        return out

    _register_app_resource(mcp, VM_PANEL_URI, "vm-panel", "VM control panel", "vm_panel.html")
    _register_app_resource(mcp, LOGS_PANEL_URI, "logs-panel", "Log viewer", "logs_panel.html")
    _register_app_resource(
        mcp, CLUSTER_PANEL_URI, "cluster-panel", "Cluster dashboard", "cluster_panel.html",
    )


# Cheap keyword scan. Proxmox hands us raw journald text with no severity
# field, so the alternative to guessing is showing every line flat -- which is
# the thing this panel exists to fix. Over-flagging a line is harmless; the
# filter box is there for when it gets noisy.
_ERROR_WORDS = ("error", "fail", "fatal", "critical", "panic", "segfault", "refused", "timeout")
_WARN_WORDS = ("warn", "deprecat", "retry", "degraded", "unable")


def _syslog_level(line: str) -> str:
    lowered = line.lower()
    if any(word in lowered for word in _ERROR_WORDS):
        return "error"
    if any(word in lowered for word in _WARN_WORDS):
        return "warn"
    return "info"


def _register_app_resource(
    mcp: FastMCP, uri: str, name: str, title: str, filename: str,
) -> None:
    @mcp.resource(uri, name=name, title=title, mime_type=APP_MIME_TYPE)
    def _app() -> str:
        return _read_app(filename)
