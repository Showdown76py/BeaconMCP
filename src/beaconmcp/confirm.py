"""Best-effort human confirmation for a few destructive tools.

When the client supports form elicitation, the user gets a yes/no dialog
before the tool runs. When it doesn't, the tool runs as it always did unless
``server.confirm_strict`` is on, in which case it is refused.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
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
    "proxmox_vm_create",
    "proxmox_vm_config",
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


# Config keys that reach the host or other guests' disks (hook scripts, raw
# QEMU args, bind mounts, device passthrough, raw LXC keys, privileged
# containers) or that turn a create into a restore over an existing guest.
_SENSITIVE_PATTERN = re.compile(r"^(mp|dev|hostpci|usb)\d+$")
_SENSITIVE_PREFIXES = ("lxc.",)
_PRESENT_KEYS = frozenset({"archive", "hookscript", "args"})
_TRUTHY_KEYS = frozenset({"restore", "force"})


def _falsy(value: Any) -> bool:
    return value is None or str(value).strip().lower() in ("", "0", "false", "no", "off")


def _sensitive_config(cfg: Any) -> bool:
    """True when a Proxmox guest config dict carries a host-reaching key."""
    if not isinstance(cfg, dict):
        return False
    for key, value in cfg.items():
        k = str(key).strip().lower()
        if k in _PRESENT_KEYS:
            if not _falsy(value):
                return True
        elif k in _TRUTHY_KEYS:
            if not _falsy(value):
                return True
        elif k == "unprivileged":
            if _falsy(value):
                return True
        elif _SENSITIVE_PATTERN.match(k) or k.startswith(_SENSITIVE_PREFIXES):
            return True
    return False


def _runs_command(kw: dict) -> bool:
    # polling a running command (exec_id without command) executes nothing
    return bool(kw.get("command")) or not kw.get("exec_id")


# Tools that are only dangerous in some call shapes: tool -> kw -> needs a dialog.
# A listed tool without an entry here always needs one.
_CONDITIONS: dict[str, Callable[[dict], bool]] = {
    "proxmox_backup_restore": lambda kw: bool(kw.get("force")),
    "proxmox_vm_create": lambda kw: _sensitive_config(kw.get("config")),
    "proxmox_vm_config": lambda kw: _sensitive_config(kw.get("updates")),
    "ssh_run": _runs_command,
    "proxmox_run": _runs_command,
}


def _needs_confirmation(tool: str, kw: dict, tools: Collection[str]) -> bool:
    if tool not in tools or kw.get("dry_run"):
        return False
    condition = _CONDITIONS.get(tool)
    return condition(kw) if condition else True


async def confirm_destructive(
    ctx: Any, tool: str, kwargs: dict, tools: Collection[str] = DEFAULT_CONFIRM_TOOLS,
    strict: bool = False,
) -> dict:
    """Ask the user before a destructive tool runs. Returns the kwargs to use.

    Raises :class:`ConfirmationDeclined` when the user declines or cancels,
    :class:`ConfirmationUnavailable` when the dialog could not be shown, or,
    with ``strict``, when the client cannot show one at all.
    """
    if not _needs_confirmation(tool, kwargs, tools):
        return kwargs
    has_context = True
    if ctx is None:
        has_context = False
    else:
        try:
            ctx.request_context
        except (AttributeError, ValueError, LookupError):
            has_context = False
    if not has_context or not _supports_form(ctx):
        if strict:
            audit.emit("tool.confirm.unavailable", tool=tool, error="no form elicitation")
            raise ConfirmationUnavailable(
                f"{tool} needs a confirmation but this client cannot show a "
                "confirmation and server.confirm_strict is on. Nothing was run."
            )
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
