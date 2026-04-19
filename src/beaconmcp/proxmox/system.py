from __future__ import annotations

import base64
import asyncio
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



    @mcp.tool()
    async def proxmox_run(
        node: str = "",
        vmid: int = 0,
        command: str = "",
        timeout: int = 60,
        wait: bool = True,
        exec_id: str = "",
    ) -> dict[str, Any]:
        """Run a command inside a VM (QEMU Guest Agent) or container (LXC pct exec). Handles sync + async in one tool.

        Three call patterns:

        - **Sync** (default): pass ``node``, ``vmid``, ``command``. Blocks up to
          ``timeout`` seconds (max 600). Completes -> returns
          ``stdout``/``stderr``/``exit_code``. Times out -> auto-switches to
          async and returns ``{status: "running", exec_id}``.
        - **Async start**: pass ``node``, ``vmid``, ``command``, ``wait=False``.
          Returns ``{status: "running", exec_id}`` immediately.
        - **Poll existing**: pass ``exec_id`` only. Returns the current
          status/output for that session.

        Automatically uses QEMU Guest Agent for VMs. For LXC containers, uses ``pct exec`` over SSH
        (requires SSH capability configured and the Proxmox node reachable).
        """
        loop = asyncio.get_running_loop()

        def _format_ssh_result(exec_id: str, s) -> dict[str, Any]:
            elapsed = round(time.time() - s.started_at, 1)
            if s.status == "running":
                return {"status": "running", "exec_id": exec_id, "command": s.command, "elapsed_s": elapsed}
            return {
                "status": "ok" if s.status == "completed" and s.exit_code == 0 else s.status,
                "exec_id": exec_id,
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
                # pct exec doesn't quote correctly if we just pass a string without care, but ssh_client.exec_command_async passes it verbatim to the shell.
                lxc_cmd = f"pct exec {vmid} -- {command}"
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
            # Timed out; hand back the session handle so caller can keep polling.
            return {
                "status": "running",
                "exec_id": new_id,
                "elapsed_s": int(time.time() - _exec_sessions[new_id].started_at),
                "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
            }
