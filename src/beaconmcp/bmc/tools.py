"""MCP tool wrappers for the BMC registry.

One tool per action (plus a ``bmc_list_devices`` discovery tool). Every
action tool takes a ``device_id`` parameter that names which backend to
dispatch to. When only one device is configured, ``device_id`` is
optional and defaults to that single device.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..utils import filter_fields
from .base import BMCClient, BMCNotConfiguredError, BMCTunnelError


def register_bmc_tools(
    mcp: FastMCP, registry: dict[str, BMCClient]
) -> None:
    """Register BMC management tools on the MCP server.

    No-op when the registry is empty (no ``bmc.devices`` in config).
    """
    if not registry:
        return

    device_ids = sorted(registry.keys())
    ids_hint = ", ".join(f"'{d}'" for d in device_ids)
    default_id: str | None = device_ids[0] if len(device_ids) == 1 else None

    def _resolve(device_id: str | None) -> BMCClient:
        if device_id is None:
            if default_id is None:
                raise BMCNotConfiguredError(
                    f"device_id is required (multiple BMC devices configured: {ids_hint}). "
                    "Call bmc_list_devices to see all configured devices."
                )
            return registry[default_id]
        backend = registry.get(device_id)
        if backend is None:
            raise BMCNotConfiguredError(
                f"Unknown device_id {device_id!r}. Valid values: {ids_hint}. "
                "Call bmc_list_devices to see all configured devices."
            )
        return backend

    @mcp.tool()
    async def bmc_list_devices() -> dict[str, Any]:
        """List all configured BMC devices (iLO / IPMI / iDRAC / Supermicro).

        Returns each device's stable ``id`` and ``type``. Use the ``id``
        values as the ``device_id`` parameter of the other bmc_* tools.
        """
        return {
            "devices": [
                {"id": cli.id, "type": cli.type} for cli in registry.values()
            ]
        }

    @mcp.tool()
    async def bmc_server_info(
        device_id: str | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get physical server information (model, serial, firmware) from a BMC.

        Use to identify the hardware behind a BMC and confirm firmware
        levels before issuing power actions. Pass ``fields=[...]`` to trim
        the response -- iLO in particular returns many keys.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
            fields: optional allow-list of top-level keys to keep.
        """
        try:
            return filter_fields(await _resolve(device_id).server_info(), fields)
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_health_status(
        device_id: str | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get hardware health from a BMC: temperatures, fans, power supplies, disks, memory.

        Most important diagnostic tool when a host becomes unresponsive —
        reveals sensor-level failures not visible over the OS. Pass
        ``fields=[...]`` to trim the response.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
            fields: optional allow-list of top-level keys to keep.
        """
        try:
            return filter_fields(await _resolve(device_id).health(), fields)
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_power_status(device_id: str | None = None) -> dict[str, Any]:
        """Get the current physical power state (on/off) of a server via its BMC.

        Use to confirm a host is actually powered on before deeper debugging.
        If the node is unreachable via the Proxmox API but BMC says power is
        on, the issue is software-level rather than hardware.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
        """
        try:
            return await _resolve(device_id).power_status()
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_power_on(device_id: str | None = None) -> dict[str, Any]:
        """Power on a physical server via its BMC.

        Use when the server is powered off and needs to boot. Confirm the
        current state with bmc_power_status first. After powering on, allow
        2–3 minutes before the node reappears in proxmox_list_nodes.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
        """
        try:
            return await _resolve(device_id).power_on()
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_power_off(
        device_id: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Power off a physical server via its BMC.

        Default (force=false) sends an ACPI shutdown (clean, like pressing
        the power button). force=true immediately cuts power — reserve for
        fully unresponsive hosts. Prefer ``proxmox_vm_stop`` and
        ``ssh_run(host=..., command='shutdown -h now')`` before forcing.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
            force: skip the ACPI graceful path and cut power immediately.
        """
        try:
            return await _resolve(device_id).power_off(force)
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_power_reset(device_id: str | None = None) -> dict[str, Any]:
        """Hard-reset a physical server via its BMC.

        Last-resort recovery when the host is completely frozen. Equivalent
        to pressing the physical reset button. Try ``proxmox_vm_restart``
        and ``ssh_run(host=..., command='reboot')`` first.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
        """
        try:
            return await _resolve(device_id).power_reset()
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def bmc_get_event_log(
        device_id: str | None = None,
        limit: int = 50,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch a BMC event log: hardware errors, reboots, power events, PSU/fan faults.

        Essential for post-mortem analysis of host crashes. Returns the most
        recent events (default 50, capped at 200). Pass ``fields=[...]`` to
        trim top-level keys of the response.

        Args:
            device_id: id of the target BMC. Optional when only one device
              is configured. Use bmc_list_devices to discover valid ids.
            limit: maximum number of events to return (1–200).
            fields: optional allow-list of top-level keys to keep.
        """
        limit = max(1, min(int(limit), 200))
        try:
            return filter_fields(await _resolve(device_id).event_log(limit), fields)
        except (BMCNotConfiguredError, BMCTunnelError) as exc:
            return {"error": str(exc)}
