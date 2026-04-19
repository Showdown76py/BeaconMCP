from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import SSHClient, SSHHostResolutionError, SSHNotConfiguredError


def _session_to_result(exec_id: str, session) -> dict[str, Any]:
    """Turn an SSH session row into a ``proxmox_run``-shaped response."""
    elapsed = round(time.time() - session.started_at, 1)
    if session.status == "running":
        return {
            "status": "running",
            "exec_id": exec_id,
            "host": session.host,
            "command": session.command,
            "elapsed_s": elapsed,
        }
    return {
        "status": "ok" if session.status == "completed" and session.exit_code == 0 else session.status,
        "exec_id": exec_id,
        "host": session.host,
        "command": session.command,
        "stdout": session.stdout,
        "stderr": session.stderr,
        "exit_code": session.exit_code,
        "duration_s": elapsed,
    }


def register_ssh_tools(mcp: FastMCP, ssh_client: SSHClient) -> None:
    """Register SSH command execution tools."""

    @mcp.tool()
    async def ssh_run(
        host: str = "",
        command: str = "",
        timeout: int = 60,
        wait: bool = True,
        exec_id: str = "",
    ) -> dict[str, Any]:
        """Run a command on a host via SSH. Handles sync + async in one tool.

        Three call patterns:

        - **Sync** (default): pass ``host`` + ``command``. Blocks up to
          ``timeout`` seconds (max 600). Completes -> returns
          ``stdout``/``stderr``/``exit_code``. Times out -> auto-switches to
          async and returns ``{status: "running", exec_id}``.
        - **Async start**: ``host`` + ``command`` + ``wait=False``.
          Returns ``{status: "running", exec_id}`` immediately.
        - **Poll existing**: pass ``exec_id`` only. Returns the current
          status/output for that session.

        ``host`` must resolve to a declared ``ssh.hosts[]`` entry. Accepts:
        an entry ``name``; a numeric VMID when ``ssh.vmid_to_ip`` is set
        (e.g. ``"110"`` -> ``"192.168.1.110"``); or a declared ``host``
        address. If ``ssh.inherit_proxmox_nodes: true``, every Proxmox node
        is auto-declared as an SSH host under its own name, so reaching the
        hypervisor reuses the same identifier as ``proxmox_run(node=…)``.

        To run **inside a VM or LXC** managed by Proxmox, prefer
        ``proxmox_run`` (QEMU Guest Agent / ``pct exec``) — no SSH is needed
        and it works even when the guest has no inbound network reachability.
        """
        if exec_id:
            session = SSHClient.get_session(exec_id)
            if not session:
                return {"status": "error", "error": f"No SSH command with exec_id {exec_id!r}."}
            return _session_to_result(exec_id, session)

        if not host or not command:
            return {"status": "error", "error": "`host` and `command` are required when `exec_id` is not provided."}

        max_timeout = min(max(timeout, 1), 600)

        try:
            new_id = await ssh_client.exec_command_async(host, command)
        except (SSHNotConfiguredError, SSHHostResolutionError) as e:
            return {"status": "error", "error": str(e)}

        if not wait:
            return {"status": "running", "exec_id": new_id, "host": host, "elapsed_s": 0}

        deadline = time.time() + max_timeout
        while time.time() < deadline:
            session = SSHClient.get_session(new_id)
            if session and session.status != "running":
                return _session_to_result(new_id, session)
            await asyncio.sleep(0.5)

        session = SSHClient.get_session(new_id)
        if session and session.status != "running":
            return _session_to_result(new_id, session)
        return {
            "status": "running",
            "exec_id": new_id,
            "host": host,
            "elapsed_s": int(time.time() - (session.started_at if session else time.time())),
            "hint": "Command still running. Call ssh_run(exec_id=...) to poll.",
        }

    @mcp.tool()
    def ssh_list_sessions() -> dict[str, Any]:
        """List all active and recent SSH command sessions.

        Returns exec_id, host, command, status, and elapsed time for each session.
        """
        sessions = SSHClient.list_sessions()
        return {"sessions": sessions, "total": len(sessions)}
