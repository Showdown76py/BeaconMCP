from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..utils import filter_fields
from .client import ProxmoxClient


@dataclass
class ExecSession:
    exec_id: str
    node: str
    vmid: int
    vm_type: str
    command: str
    status: str = "running"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    pid: int | None = None
    started_at: float = field(default_factory=time.time)


_exec_sessions: dict[str, ExecSession] = {}
# Drop finished sessions older than this (seconds) on each new insertion so the
# store can't grow unboundedly in a long-running server.
_EXEC_SESSION_TTL = 3600


def _prune_exec_sessions() -> None:
    now = time.time()
    stale = [
        eid
        for eid, s in _exec_sessions.items()
        if s.status != "running" and now - s.started_at > _EXEC_SESSION_TTL
    ]
    for eid in stale:
        del _exec_sessions[eid]


def _detect_vm_type(client: ProxmoxClient, node: str, vmid: int) -> str | None:
    for vm_type in ("qemu", "lxc"):
        data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
        if isinstance(data, dict) and "error" in data:
            continue
        if isinstance(data, dict) and data.get("status"):
            return vm_type
    return None


def _exec_lxc_unsupported(vmid: int) -> dict[str, Any]:
    """LXC exec is not exposed by the Proxmox API.

    Commands inside containers must be run via ``pct exec`` on the host, which
    requires SSH access to the node.
    """
    return {
        "error": (
            f"Proxmox API does not expose an exec endpoint for LXC containers. "
            f"Use ssh_run on the host node with "
            f"'pct exec {vmid} -- <command>' instead."
        )
    }


def register_system_tools(mcp: FastMCP, client: ProxmoxClient) -> None:
    """Register Proxmox system administration and command execution tools."""

    @mcp.tool()
    def proxmox_storage_status(
        node: str = "",
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get storage status across the cluster: usage, type, content types.

        Use to check disk space, storage health, or find available storage.
        Omit 'node' to list storage from all configured nodes.
        Pass ``fields=[...]`` to trim each entry to a subset of keys
        (e.g. ``["name", "usage_pct"]``).
        Returns: {"storage": {"<node>": [{name, type, content, enabled, used_gb,
        total_gb, usage_pct}]}}. Per-node errors appear as {"error": "..."} entries.
        """
        target_nodes = [node] if node else client.configured_nodes
        by_node: dict[str, list[dict[str, Any]]] = {}

        for n in target_nodes:
            entries: list[dict[str, Any]] = []
            data = client.get(n, f"nodes/{n}/storage")
            if isinstance(data, dict) and "error" in data:
                entries.append({"error": data["error"]})
                by_node[n] = entries
                continue
            if not isinstance(data, list):
                by_node[n] = entries
                continue
            for s in data:
                storage_name = s.get("storage")
                if not storage_name:
                    continue
                status = client.get(n, f"nodes/{n}/storage/{storage_name}/status")
                used = 0
                total = 0
                if isinstance(status, dict) and "error" not in status:
                    used = status.get("used", 0)
                    total = status.get("total", 0)

                entries.append({
                    "name": storage_name,
                    "type": s.get("type"),
                    "content": s.get("content"),
                    "enabled": s.get("enabled", 1) == 1,
                    "used_gb": round(used / 1073741824, 1),
                    "total_gb": round(total / 1073741824, 1),
                    "usage_pct": round(used / total * 100, 1) if total > 0 else 0,
                })
            by_node[n] = filter_fields(entries, fields)

        return {"storage": by_node}

    @mcp.tool()
    def proxmox_network_config(node: str) -> dict[str, Any]:
        """Get network interface configuration of a Proxmox node.

        Use to inspect network setup: bridges, bonds, VLANs, IP addresses.
        Returns all network interfaces with their type, address, and configuration.
        """
        data = client.get(node, f"nodes/{node}/network")
        if isinstance(data, dict) and "error" in data:
            return data
        if not isinstance(data, list):
            return {"node": node, "interfaces": [], "raw": str(data)}

        interfaces = []
        for iface in data:
            interfaces.append({
                "name": iface.get("iface"),
                "type": iface.get("type"),
                "address": iface.get("address"),
                "netmask": iface.get("netmask"),
                "gateway": iface.get("gateway"),
                "bridge_ports": iface.get("bridge_ports"),
                "active": iface.get("active", False),
                "method": iface.get("method"),
                "cidr": iface.get("cidr"),
            })

        return {"node": node, "interfaces": interfaces}

    # -----------------------------------------------------------------------
    # proxmox_run: unified sync + async QEMU exec
    # -----------------------------------------------------------------------

    def _start_async_qemu(node: str, vmid: int, command: str) -> dict[str, Any]:
        """Kick off a QEMU guest-agent command and track it in the session store."""
        import shlex

        _prune_exec_sessions()
        exec_id = str(uuid.uuid4())[:8]
        session = ExecSession(
            exec_id=exec_id,
            node=node,
            vmid=vmid,
            vm_type="qemu",
            command=command,
        )
        _exec_sessions[exec_id] = session

        parts = shlex.split(command)
        result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/exec", command=parts)
        if isinstance(result, dict) and "error" in result:
            session.status = "failed"
            session.stderr = str(result["error"])
            return {"exec_id": exec_id, "status": "failed", "error": result["error"]}
        session.pid = result.get("pid") if isinstance(result, dict) else None
        return {"exec_id": exec_id, "status": "running"}

    def _poll_session(exec_id: str) -> dict[str, Any]:
        """Advance a tracked session if possible, return a status-shape dict."""
        session = _exec_sessions.get(exec_id)
        if not session:
            return {"status": "error", "error": f"No command found with exec_id {exec_id!r}."}

        if session.status == "running" and session.vm_type == "qemu" and session.pid is not None:
            status_data = client.get(
                session.node,
                f"nodes/{session.node}/qemu/{session.vmid}/agent/exec-status",
                pid=session.pid,
            )
            if isinstance(status_data, dict) and status_data.get("exited"):
                stdout = status_data.get("out-data", "")
                stderr = status_data.get("err-data", "")
                if status_data.get("out-data-encoding") == "base64" and stdout:
                    stdout = base64.b64decode(stdout).decode("utf-8", errors="replace")
                if status_data.get("err-data-encoding") == "base64" and stderr:
                    stderr = base64.b64decode(stderr).decode("utf-8", errors="replace")
                session.status = "completed"
                session.stdout = stdout
                session.stderr = stderr
                session.exit_code = status_data.get("exitcode", -1)

            if session.status == "running" and time.time() - session.started_at > 600:
                session.status = "timeout"

        elapsed = round(time.time() - session.started_at, 1)
        if session.status == "running":
            return {
                "status": "running",
                "exec_id": exec_id,
                "command": session.command,
                "elapsed_s": elapsed,
            }
        return {
            "status": "ok" if session.status == "completed" and session.exit_code == 0 else session.status,
            "exec_id": exec_id,
            "command": session.command,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "duration_s": elapsed,
        }

    @mcp.tool()
    def proxmox_run(
        node: str = "",
        vmid: int = 0,
        command: str = "",
        timeout: int = 60,
        wait: bool = True,
        exec_id: str = "",
    ) -> dict[str, Any]:
        """Run a command inside a QEMU VM via the Guest Agent. Handles sync + async in one tool.

        Three call patterns:

        - **Sync** (default): pass ``node``, ``vmid``, ``command``. Blocks up to
          ``timeout`` seconds (max 600). Completes -> returns
          ``stdout``/``stderr``/``exit_code``. Times out -> auto-switches to
          async and returns ``{status: "running", exec_id}``.
        - **Async start**: pass ``node``, ``vmid``, ``command``, ``wait=False``.
          Returns ``{status: "running", exec_id}`` immediately.
        - **Poll existing**: pass ``exec_id`` only. Returns the current
          status/output for that session.

        LXC containers have no Guest Agent -- use ``ssh_run`` with
        ``pct exec <vmid> -- <cmd>`` instead. For commands on the Proxmox host
        itself (not inside a VM), use ``ssh_run`` directly.
        """
        if exec_id:
            return _poll_session(exec_id)

        if not command:
            return {"status": "error", "error": "`command` is required when `exec_id` is not provided."}
        if not node or not vmid:
            return {"status": "error", "error": "`node` and `vmid` are required to start a command."}

        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
        if vm_type == "lxc":
            return _exec_lxc_unsupported(vmid) | {"status": "error"}

        started = _start_async_qemu(node, vmid, command)
        if started.get("status") == "failed":
            return started
        new_id = started["exec_id"]

        if not wait:
            return {"status": "running", "exec_id": new_id, "elapsed_s": 0}

        max_timeout = min(max(timeout, 1), 600)
        deadline = time.time() + max_timeout
        while time.time() < deadline:
            result = _poll_session(new_id)
            if result["status"] != "running":
                return result
            time.sleep(1)
        # Timed out; hand back the session handle so caller can keep polling.
        return {
            "status": "running",
            "exec_id": new_id,
            "elapsed_s": int(time.time() - _exec_sessions[new_id].started_at),
            "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
        }
