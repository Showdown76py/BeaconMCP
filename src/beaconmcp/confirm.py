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

DEFAULT_CONFIRM_TOOLS: tuple[str, ...] = (
    "bmc_power_off",
    "bmc_power_reset",
    "proxmox_snapshot_rollback",
    "proxmox_snapshot_delete",
    "proxmox_backup_restore",
    "beaconmcp_self_update",
)


class ConfirmationDeclined(ToolError):
    """The user said no (or dismissed the dialog). Not an error."""


class ConfirmationUnavailable(ToolError):
    """The client declared elicitation but could not show the dialog."""


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


def _supports_form(ctx: Any) -> bool:
    """True when the client declared form elicitation for this session."""
    try:
        session = ctx.request_context.session
        # client_capabilities also covers stateless 2026-07-28 clients, which
        # declare them per request in _meta instead of at initialize
        caps = getattr(session, "client_capabilities", None)
        if caps is None:
            caps = session.client_params.capabilities
        elicitation = caps.elicitation
    except (AttributeError, ValueError, LookupError):
        return False
    if elicitation is None:
        return False
    # a bare {} is the 2025-06-18 form-only declaration
    return elicitation.form is not None or elicitation.url is None


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

    Raises :class:`ConfirmationDeclined` when the user declines or cancels,
    :class:`ConfirmationUnavailable` when the dialog could not be shown.
    """
    if ctx is None or not _needs_confirmation(tool, kwargs, tools):
        return kwargs
    try:
        ctx.request_context
    except (AttributeError, ValueError, LookupError):
        return kwargs
    if not _supports_form(ctx):
        return kwargs
    try:
        result = await ctx.elicit(message=_message(tool, kwargs), schema=_Confirm)
    except Exception as exc:  # noqa: BLE001 -- transport, timeout, client error
        audit.emit("tool.confirm.unavailable", tool=tool, error=str(exc)[:200])
        raise ConfirmationUnavailable(
            f"{tool} needs a confirmation but the client could not show it "
            f"({exc.__class__.__name__}). Nothing was run."
        ) from exc
    data = getattr(result, "data", None)
    if getattr(result, "action", "") != "accept" or not getattr(data, "confirm", False):
        audit.emit("tool.confirm.declined", tool=tool)
        raise ConfirmationDeclined("Cancelled by the user.")
    audit.emit("tool.confirm.ok", tool=tool)
    if tool == "beaconmcp_self_update":
        # the dialog is the confirmation, no need for the model to pass confirm=True
        return {**kwargs, "confirm": True}
    return kwargs
