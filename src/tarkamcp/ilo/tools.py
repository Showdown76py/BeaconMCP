from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ILOClient, ILONotConfiguredError, ILOTunnelError


def register_ilo_tools(mcp: FastMCP, ilo_client: ILOClient) -> None:
    """Register HP iLO hardware management tools."""

    @mcp.tool()
    async def ilo_server_info() -> dict[str, Any]:
        """Get physical server information: model, serial number, firmware versions.

        Use to identify the hardware and check firmware levels.
        Connects to iLO 4 through an SSH tunnel via pve1.
        If this fails, pve1 may be unreachable -- check with proxmox_list_nodes first.
        """
        try:
            return await ilo_client.get_server_info()
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_health_status() -> dict[str, Any]:
        """Get full hardware health: temperatures, fans, power supplies, disks, memory status.

        Use when diagnosing hardware issues -- overheating, fan failures, disk errors, PSU problems.
        This is the most important iLO tool for crash investigation.
        Returns detailed sensor readings and component health status.
        """
        try:
            return await ilo_client.get_health()
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_power_status() -> dict[str, Any]:
        """Get the current physical power state of the server (ON/OFF).

        Use to check if the server is physically powered on.
        If a Proxmox node is unreachable but power is ON, the issue is likely software.
        If power is OFF, use ilo_power_on to start it.
        """
        try:
            return await ilo_client.get_power_status()
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_power_on() -> dict[str, Any]:
        """Power on the physical server via iLO.

        Use when the server is physically powered off and needs to be started.
        Check ilo_power_status first to confirm it's actually off.
        After powering on, wait 2-3 minutes then check proxmox_list_nodes for the node to appear.
        """
        try:
            return await ilo_client.power_on()
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_power_off(force: bool = False) -> dict[str, Any]:
        """Power off the physical server via iLO.

        Default (force=false): sends an ACPI shutdown signal (clean shutdown, like pressing the power button).
        With force=true: immediately cuts power (use only when the server is completely unresponsive).
        Try proxmox_vm_stop and ssh_exec_command 'shutdown -h now' before using force power off.
        """
        try:
            return await ilo_client.power_off(force)
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_power_reset() -> dict[str, Any]:
        """Hard reset the physical server via iLO.

        Use as a last resort when the server is completely frozen and doesn't respond to
        any software-level reboot commands. Equivalent to pressing the physical reset button.
        Try proxmox_vm_restart and ssh_exec_command 'reboot' before using this.
        """
        try:
            return await ilo_client.power_reset()
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}

    @mcp.tool()
    async def ilo_get_event_log(limit: int = 50) -> dict[str, Any]:
        """Get the iLO event log: hardware errors, reboots, power events, component failures.

        Use to investigate past hardware events and find root causes of crashes.
        Returns the most recent events (default 50, max 200).
        Events include timestamps, severity, and descriptions.
        """
        limit = min(limit, 200)
        try:
            return await ilo_client.get_event_log(limit)
        except (ILONotConfiguredError, ILOTunnelError) as e:
            return {"error": str(e)}
