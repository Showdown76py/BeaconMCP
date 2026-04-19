from __future__ import annotations

import base64
import asyncio
import time
import uuid
import shlex
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


def register_system_tools(mcp: FastMCP, client: ProxmoxClient, ssh_client: Any = None) -> None:
    """Register Proxmox system administration and command execution tools."""

    @mcp.tool()
    def proxmox_storage_status(node: str = "") -> dict[str, Any]:
        """Get storage status across the cluster: usage, type, content types.

        Use to check disk space, storage health, or find available storage.
        Omit 'node' to list storage from all configured nodes.
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
                used = status.get("used", 0) if isinstance(status, dict) and "error" not in status else 0
                total = status.get("total", 0) if isinstance(status, dict) and "error" not in status else 0

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
        """Get network interface configuration of a Proxmox node."""
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

    def _start_async_qemu(node: str, vmid: int, command: str) -> dict[str, Any]:
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
        session = _exec_sessions.get(exec_id)
        if not session:
            return {"status": "error", "error": f"No command found with exec_id '{exec_id}'."}

        if session.status != "running":
            return {
                "exec_id": exec_id,
                "status": "ok" if session.status == "completed" and session.exit_code == 0 else session.status,
                "stdout": session.stdout,
                "stderr": session.stderr,
                "exit_code": session.exit_code,
                "command": session.command,
                "elapsed_s": round(time.time() - session.started_at, 1),
            }

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

        if time.time() - session.started_at > 600:
            session.status = "timeout"

        elapsed = round(time.time() - session.started_at, 1)
        if session.status == "running":
            return {"status": "running", "exec_id": exec_id, "command": session.command, "elapsed_s": elapsed}
        
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
    async def proxmox_run(
        node: str = "",
        vmid: int = 0,
        command: str = "",
        timeout: int = 60,
        wait: bool = True,
        exec_id: str = "",
    ) -> dict[str, Any]:
        """Run a command inside a VM (QEMU Guest Agent) or container (LXC pct exec).

        Three call patterns:
        - **Sync** (default): pass ``node``, ``vmid``, ``command``. Blocks up to ``timeout`` seconds (max 600).
        - **Async start**: pass ``node``, ``vmid``, ``command``, ``wait=False``. Returns immediately.
        - **Poll existing**: pass ``exec_id`` only. Returns the current status/output for that session.
        """
        loop = asyncio.get_running_loop()

        def _format_ssh_result(xid: str, s) -> dict[str, Any]:
            elapsed = round(time.time() - s.started_at, 1)
            if s.status == "running":
                return {"status": "running", "exec_id": xid, "command": s.command, "elapsed_s": elapsed}
            return {
                "status": "ok" if s.status == "completed" and s.exit_code == 0 else s.status,
                "exec_id": xid,
                "command": s.command,
                "stdout": s.stdout,
                "stderr": s.stderr,
                "exit_code": s.exit_code,
                "duration_s": elapsed,
            }

        if exec_id:
            if exec_id in _exec_sessions:
                return await loop.run_in_executor(None, _poll_session, exec_id)
            if ssh_client:
                ssh_sess = ssh_client.get_session(exec_id)
                if ssh_sess:
                    return _format_ssh_result(exec_id, ssh_sess)
            return {"status": "error", "error": f"No command found with exec_id {exec_id!r}."}

        if not command:
            return {"status": "error", "error": "`command` is required when `exec_id` is not provided."}
        if not node or not vmid:
            return {"status": "error", "error": "`node` and `vmid` are required to start a command."}

        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
        
        max_timeout = min(max(timeout, 1), 600)
        
        if vm_type == "lxc":
            if not ssh_client:
                return {"status": "error", "error": "LXC execution requires SSH access to the Proxmox node, but SSH is not configured."}
            try:
                # Safe escaping to prevent shell injection (fixing the owner's feedback)
                escaped_cmd = shlex.quote(command)
                lxc_cmd = f"pct exec {vmid} -- sh -c {escaped_cmd}"
                new_id = await ssh_client.exec_command_async(node, lxc_cmd)
            except Exception as e:
                return {"status": "error", "error": str(e)}

            if not wait:
                return {"status": "running", "exec_id": new_id, "elapsed_s": 0}

            deadline = time.time() + max_timeout
            while time.time() < deadline:
                session = ssh_client.get_session(new_id)
                if session and session.status != "running":
                    return _format_ssh_result(new_id, session)
                await asyncio.sleep(1)
                
            session = ssh_client.get_session(new_id)
            if session and session.status != "running":
                return _format_ssh_result(new_id, session)
            return {
                "status": "running",
                "exec_id": new_id,
                "elapsed_s": int(time.time() - (session.started_at if session else time.time())),
                "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
            }
        else:
            started = await loop.run_in_executor(None, _start_async_qemu, node, vmid, command)
            if started.get("status") == "failed":
                return started
            new_id = started["exec_id"]

            if not wait:
                return {"status": "running", "exec_id": new_id, "elapsed_s": 0}

            deadline = time.time() + max_timeout
            while time.time() < deadline:
                result = await loop.run_in_executor(None, _poll_session, new_id)
                if result["status"] != "running":
                    return result
                await asyncio.sleep(1)
            
            return {
                "status": "running",
                "exec_id": new_id,
                "elapsed_s": int(time.time() - _exec_sessions[new_id].started_at),
                "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
            }
