"""Best-effort human confirmation for a few destructive tools.

When the client supports form elicitation, the user gets a yes/no dialog
before the tool runs. When it doesn't, the tool runs as it always did: the
user chose not to break automation, so this is not a hard gate.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import audit
from .reauth import elicit_modes

DEFAULT_CONFIRM_TOOLS: tuple[str, ...] = (
    "bmc_power_off",
    "bmc_power_reset",
    "proxmox_snapshot_rollback",
    "proxmox_snapshot_delete",
    "proxmox_backup_restore",
    "beaconmcp_self_update",
)


class _Confirm(BaseModel):
    confirm: bool = Field(default=False, description="Yes, do it")


def _message(tool: str, kw: dict) -> str:
    device = kw.get("device_id") or "the server"
    vm = f"VM {kw.get('vmid')} on {kw.get('node')}"
    if tool == "bmc_power_off":
        how = " (hard power cut)" if kw.get("force") else ""
        return f"Power off {device} via its BMC{how}?"
    if tool == "bmc_power_reset":
        return f"Hard reset {device} via its BMC?"
    if tool == "proxmox_snapshot_rollback":
        return f"Roll back {vm} to snapshot {kw.get('snapname')}? Changes since then are lost."
    if tool == "proxmox_snapshot_delete":
        return f"Delete snapshot {kw.get('snapname')} of {vm}?"
    if tool == "proxmox_backup_restore":
        return f"Restore {kw.get('archive')} over {vm}? This overwrites it."
    if tool == "beaconmcp_self_update":
        return "Update BeaconMCP" + (" and restart the server?" if kw.get("restart", True) else "?")
    return f"Run {tool}?"


def _needs_confirmation(tool: str, kw: dict, tools: Collection[str]) -> bool:
    if tool not in tools or kw.get("dry_run"):
        return False
    if tool == "proxmox_backup_restore" and not kw.get("force"):
        return False
    return True


async def confirm_destructive(
    ctx: Any, tool: str, kwargs: dict, tools: Collection[str] = DEFAULT_CONFIRM_TOOLS,
) -> dict:
    """Ask the user before a destructive tool runs. Returns the kwargs to use.

    Raises :class:`ToolError` when the user declines or cancels.
    """
    if ctx is None or not _needs_confirmation(tool, kwargs, tools):
        return kwargs
    try:
        ctx.request_context
    except (AttributeError, ValueError, LookupError):
        return kwargs
    form, _ = elicit_modes(ctx, None)
    if not form:
        return kwargs
    result = await ctx.elicit(message=_message(tool, kwargs), schema=_Confirm)
    data = getattr(result, "data", None)
    if getattr(result, "action", "") != "accept" or not getattr(data, "confirm", False):
        audit.emit("tool.confirm.declined", tool=tool)
        raise ToolError("Cancelled by the user.")
    audit.emit("tool.confirm.ok", tool=tool)
    if tool == "beaconmcp_self_update":
        # the dialog is the confirmation, no need for the model to pass confirm=True
        return {**kwargs, "confirm": True}
    return kwargs
