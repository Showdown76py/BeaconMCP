from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import FastMCP

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


async def _exec_qemu_sync(client: ProxmoxClient, node: str, vmid: int, command: str, timeout: int) -> dict[str, Any]:
    """Execute a command in a QEMU VM via Guest Agent, polling until done."""
    import asyncio
    import shlex

    # Proxmox agent/exec endpoint expects `command` as an array (binary + args).
    # proxmoxer encodes list values with doseq=True, which PVE parses as an array.
    parts = shlex.split(command)

    # Start the command via QEMU Guest Agent
    result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/exec", command=parts)
    if isinstance(result, dict) and "error" in result:
        return result

    pid = result.get("pid") if isinstance(result, dict) else None
    if pid is None:
        return {"error": f"Failed to start command in VM {vmid}. QEMU Guest Agent may not be running."}

    # Poll for result (async-safe, does not block event loop)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_data = client.get(node, f"nodes/{node}/qemu/{vmid}/agent/exec-status", pid=pid)
        if isinstance(status_data, dict) and "error" in status_data:
            return status_data
        if isinstance(status_data, dict) and status_data.get("exited"):
            stdout = status_data.get("out-data", "")
            stderr = status_data.get("err-data", "")
            # Proxmox returns base64-encoded output
            if status_data.get("out-data-encoding") == "base64" and stdout:
                stdout = base64.b64decode(stdout).decode("utf-8", errors="replace")
            if status_data.get("err-data-encoding") == "base64" and stderr:
                stderr = base64.b64decode(stderr).decode("utf-8", errors="replace")
            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": status_data.get("exitcode", -1),
            }
        await asyncio.sleep(2)

    return {
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "status": "timeout",
        "error": f"Command timed out after {timeout}s. Use proxmox_exec_command_async for long-running commands.",
    }


def _exec_lxc_unsupported(vmid: int) -> dict[str, Any]:
    """LXC exec is not exposed by the Proxmox API.

    Commands inside containers must be run via `pct exec` on the host, which
    requires SSH access to the node.
    """
    return {
        "error": (
            f"Proxmox API does not expose an exec endpoint for LXC containers. "
            f"Use ssh_exec_command on the host node with "
            f"'pct exec {vmid} -- <command>' instead."
        )
    }


def register_system_tools(mcp: FastMCP, client: ProxmoxClient) -> None:
    """Register Proxmox system administration and command execution tools."""


    @mcp.tool()
    async def proxmox_read_file(node: str, vmid: int, path: str, binary: bool = False) -> dict[str, Any]:
        """Read a file from a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely (file must be < 1MB).
        For containers, this requires SSH to be configured.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        if vm_type == "qemu":
            result = client.get(node, f"nodes/{node}/qemu/{vmid}/agent/file-read", file=path)
            if isinstance(result, dict) and "error" in result:
                return {"status": "error", "error": result["error"]}
            # QEMU Guest Agent returns file content base64-encoded
            try:
                if isinstance(result, dict) and "content" in result:
                    import base64
                    raw_b64 = result["content"]
                    if len(raw_b64) > 1398101: # ~1MB limit in base64
                        return {"status": "error", "error": "File exceeds 1MB limit. Use SSH to download large files."}
                    
                    if binary:
                        return {"status": "success", "vmid": vmid, "node": node, "path": path, "content_base64": raw_b64}
                        
                    try:
                        content = base64.b64decode(raw_b64).decode("utf-8")
                        return {"status": "success", "vmid": vmid, "node": node, "path": path, "content": content}
                    except UnicodeDecodeError:
                        return {"status": "error", "error": "Binary data detected. Pass binary=True to retrieve as base64."}
                return {"status": "success", "vmid": vmid, "node": node, "path": path, "content": str(result)}
            except Exception as e:
                return {"status": "error", "error": f"Failed to decode file content: {e}"}
        
        return {"status": "error", "error": "LXC file reading is currently unsupported via API. Please use ssh_run to cat the file."}

    @mcp.tool()
    async def proxmox_write_file(node: str, vmid: int, path: str, content: str) -> dict[str, Any]:
        """Write a file to a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely to avoid shell escaping issues.
        For containers, this requires SSH to be configured.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'."}
            
        if vm_type == "qemu":
            import base64
            encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
            result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/file-write", file=path, content=encoded, encode=1)
            if isinstance(result, dict) and "error" in result:
                return result
            return {"vmid": vmid, "node": node, "path": path, "action": "file_write", "status": "success"}
            
        return {"error": "LXC file writing is currently unsupported via API. Please use ssh_run to write the file."}

    @mcp.tool()
    def proxmox_storage_status(node: str = "") -> dict[str, Any]:
        """Get storage status across the cluster: usage, type, content types.

        Use to check disk space, storage health, or find available storage.
        Omit 'node' to list storage from all configured nodes.
        Returns: {"storage": {"<node>": [{name, type, content, enabled, used_gb,
        total_gb, usage_pct}]}}. The storage pool name is in the 'name' field
        of each entry. Per-node errors appear as {"error": "..."} entries.
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
            by_node[n] = entries

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

    @mcp.tool()
    async def proxmox_exec_command(node: str, vmid: int, command: str, timeout: int = 60) -> dict[str, Any]:
        """Execute a command inside a QEMU VM (via QEMU Guest Agent) and wait for the result.

        Use for short-lived commands that complete within the timeout (default 60s, max 300s).
        Returns stdout, stderr, and exit_code.
        For long-running commands (apt upgrade, backups, etc.), use proxmox_exec_command_async instead.
        For commands on the Proxmox host itself, use ssh_exec_command.
        LXC containers have no API exec endpoint: use ssh_exec_command with 'pct exec <vmid> -- <cmd>'.
        """
        timeout = min(timeout, 300)
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        if vm_type == "qemu":
            return await _exec_qemu_sync(client, node, vmid, command, timeout)
        return _exec_lxc_unsupported(vmid)

    @mcp.tool()
    def proxmox_exec_command_async(node: str, vmid: int, command: str) -> dict[str, Any]:
        """Start a long-running command inside a VM or container and return immediately.

        Use for commands that take more than 60 seconds (apt upgrade, database dumps, file transfers).
        Returns an exec_id to track the command. Use proxmox_exec_get_result with that exec_id
        to poll for completion and retrieve output.
        """
        import shlex

        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        if vm_type == "lxc":
            # LXC has no API exec endpoint; surface the actionable error up-front.
            return _exec_lxc_unsupported(vmid)

        _prune_exec_sessions()
        exec_id = str(uuid.uuid4())[:8]
        session = ExecSession(
            exec_id=exec_id,
            node=node,
            vmid=vmid,
            vm_type=vm_type,
            command=command,
        )
        _exec_sessions[exec_id] = session

        # Start via guest agent (command is an array: binary + args)
        parts = shlex.split(command)
        result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/exec", command=parts)
        if isinstance(result, dict) and "error" in result:
            session.status = "failed"
            session.stderr = str(result["error"])
            return {"exec_id": exec_id, "status": "failed", "error": result["error"]}
        session.pid = result.get("pid") if isinstance(result, dict) else None

        return {"exec_id": exec_id, "status": "running", "vmid": vmid, "command": command}

    @mcp.tool()
    def proxmox_exec_get_result(exec_id: str) -> dict[str, Any]:
        """Get the result of an async command started with proxmox_exec_command_async.

        Provide the exec_id returned by proxmox_exec_command_async.
        Returns status (running/completed/failed/timeout), stdout, stderr, and exit_code when done.
        Call repeatedly to poll for completion.
        """
        session = _exec_sessions.get(exec_id)
        if not session:
            return {"error": f"No command found with exec_id '{exec_id}'. It may have expired or never existed."}

        if session.status != "running":
            return {
                "exec_id": exec_id,
                "status": session.status,
                "stdout": session.stdout,
                "stderr": session.stderr,
                "exit_code": session.exit_code,
                "command": session.command,
            }

        # Poll QEMU guest agent
        if session.vm_type == "qemu" and session.pid is not None:
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

        # Check for timeout (10 min max for async)
        if time.time() - session.started_at > 600:
            session.status = "timeout"

        return {
            "exec_id": exec_id,
            "status": session.status,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "command": session.command,
            "elapsed_s": round(time.time() - session.started_at),
        }
