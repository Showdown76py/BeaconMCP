from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import SSHClient, SSHHostResolutionError, SSHNotConfiguredError


def register_ssh_tools(mcp: FastMCP, ssh_client: SSHClient) -> None:
    """Register SSH command execution tools."""

    @mcp.tool()
    async def ssh_exec_command(host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        """Execute a command on a host via SSH and wait for the result.

        Use as a fallback when the Proxmox API is unavailable, or to run commands
        directly on a Proxmox host (not inside a VM -- use proxmox_exec_command for that).
        'host' can be a node name (pve1), a VMID (101 -> 192.168.1.101), or a direct IP/hostname.
        Timeout defaults to 60s (max 300s). For long commands, use ssh_exec_command_async.
        """
        timeout = min(timeout, 300)
        try:
            return await ssh_client.exec_command(host, command, timeout)
        except (SSHNotConfiguredError, SSHHostResolutionError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ssh_exec_command_async(host: str, command: str) -> dict[str, Any]:
        """Start a long-running SSH command and return immediately with an exec_id.

        Use for commands that take more than 60 seconds (updates, large file operations, etc.).
        Returns an exec_id. Use ssh_exec_get_result to poll for completion.
        'host' can be a node name (pve1), a VMID (101), or a direct IP/hostname.
        """
        try:
            exec_id = await ssh_client.exec_command_async(host, command)
            return {
                "exec_id": exec_id,
                "status": "running",
                "host": host,
                "resolved": ssh_client.resolve_host(host),
                "command": command,
            }
        except (SSHNotConfiguredError, SSHHostResolutionError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def ssh_exec_get_result(exec_id: str) -> dict[str, Any]:
        """Get the result of an async SSH command started with ssh_exec_command_async.

        Provide the exec_id returned by ssh_exec_command_async.
        Returns status (running/completed/failed/timeout), stdout, stderr, and exit_code.
        Call repeatedly to poll for completion.
        """
        session = SSHClient.get_session(exec_id)
        if not session:
            return {"error": f"No SSH command found with exec_id '{exec_id}'."}
        return {
            "exec_id": exec_id,
            "host": session.host,
            "command": session.command,
            "status": session.status,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "elapsed_s": round(time.time() - session.started_at)
            if session.status == "running"
            else None,
        }

    @mcp.tool()
    def ssh_list_sessions() -> dict[str, Any]:
        """List all active and recent SSH command sessions.

        Use to check what SSH commands are running or have completed.
        Returns exec_id, host, command, status, and elapsed time for each session.
        """
        sessions = SSHClient.list_sessions()
        return {"sessions": sessions, "total": len(sessions)}
