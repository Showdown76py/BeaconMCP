"""Best-effort human confirmation for a few destructive tools.

When the client supports form elicitation, the user gets a yes/no dialog
before the tool runs. When it doesn't, the tool runs as it always did unless
``server.confirm_strict`` is on, in which case it is refused. A client that
declares elicitation but cannot be sent a request (no back channel: MCP
2026-07-28 forbids server-initiated requests) counts as one that doesn't.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import NoBackChannelError
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


def _can_send_request(ctx: Any) -> bool:
    """False when this request has no channel to send the client a request.

    Declaring elicitation is not enough: a 2026-07-28 client, or streamable
    HTTP in JSON-response or stateless mode, leaves the server no way to send
    ``elicitation/create``. Unknown (older or fake session) counts as True;
    ``elicit`` then raises NoBackChannelError, handled the same way.
    """
    try:
        return bool(getattr(ctx.request_context.session, "can_send_request", True))
    except (AttributeError, ValueError, LookupError):
        return True


def _without_dialog(tool: str, kwargs: dict, strict: bool, reason: str) -> dict:
    """The client cannot show a dialog: refused when strict, else run as before."""
    if strict:
        audit.emit("tool.confirm.unavailable", tool=tool, error=reason)
        raise ConfirmationUnavailable(
            f"{tool} needs a confirmation but this client cannot show a "
            "confirmation and server.confirm_strict is on. Nothing was run."
        )
    return kwargs


# Config keys that reach the host or other guests' disks. PVE already refuses
# several of them to API tokens (bind mounts, devN passthrough, hookscript,
# most features), so for those this filter is defense in depth: it keeps
# working if the server runs with a more privileged credential. What PVE does
# let a token do is attach an EXISTING volume of another guest (``mp1:
# local:102/vm-102-disk-9.raw``), which gives one guest the disk of another.
# Disk keys are therefore sensitive unless the value is clearly harmless: a
# freshly allocated volume, ``none`` or an ISO in a CD-ROM drive.
_DISK_KEY = re.compile(r"^(mp|scsi|virtio|sata|ide|efidisk|tpmstate|unused)\d+$")
_NEW_VOLUME = re.compile(r"^[\w.-]+:\d+(\.\d+)?$")  # <storage>:<size>
_ISO_VOLUME = re.compile(r"^[\w.-]+:iso/[^/]+\.(iso|img)$", re.IGNORECASE)
# Host passthrough: devices (devN, hostpciN, usbN, parallelN) and host
# directories (virtiofsN). serialN is a host device too unless it is a socket.
_SENSITIVE_PATTERN = re.compile(r"^(dev|hostpci|usb|parallel|virtiofs)\d+$")
_SERIAL_KEY = re.compile(r"^serial\d+$")
_SENSITIVE_PREFIXES = ("lxc.",)
# Sensitive whenever set to something non falsy. ``cicustom`` points cloud-init
# at snippet files read from a storage on the host.
_SENSITIVE_KEYS = frozenset({"archive", "hookscript", "args", "restore", "force", "cicustom"})


def _falsy(value: Any) -> bool:
    return value is None or str(value).strip().lower() in ("", "0", "false", "no", "off")


def _sensitive_disk(value: Any) -> bool:
    """True unless a disk value is a new volume, ``none`` or an ISO CD-ROM."""
    parts = str(value).strip().split(",")
    first = parts[0].strip()
    opts = [p.strip().lower() for p in parts[1:]]
    if any(o.startswith("import-from=") for o in opts):
        return True
    if _NEW_VOLUME.match(first) or first.lower() == "none":
        return False
    if "media=cdrom" in opts and (first.lower() == "cdrom" or _ISO_VOLUME.match(first)):
        return False
    return True


def _sensitive_features(value: Any) -> bool:
    """True when an LXC ``features`` value enables anything besides nesting.

    keyctl, fuse, mknod, force_rw_sys and mount= widen what the container can
    do against the host kernel; PVE refuses them to API tokens, this keeps the
    gate if the server runs with a stronger credential.
    """
    for part in str(value).split(","):
        name, sep, flag = part.partition("=")
        name = name.strip().lower()
        if not name or name == "nesting":
            continue
        if not sep or not _falsy(flag):
            return True
    return False


def _sensitive_config(cfg: Any) -> bool:
    """True when a Proxmox guest config dict carries a host-reaching key."""
    if not isinstance(cfg, dict):
        return False
    for key, value in cfg.items():
        k = str(key).strip().lower()
        if k in _SENSITIVE_KEYS:
            if not _falsy(value):
                return True
        elif k == "unprivileged":
            if _falsy(value):
                return True
        elif k == "rootfs" or _DISK_KEY.match(k):
            if _sensitive_disk(value):
                return True
        elif k == "features":
            if _sensitive_features(value):
                return True
        elif _SERIAL_KEY.match(k):
            if str(value).strip().lower() != "socket":
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


# The tool list in force for this process. server.py sets it from the config
# at import; the dashboard (same process) reads it so that its approval card
# covers whatever the server would ask.
_active_tools: frozenset[str] = frozenset(DEFAULT_CONFIRM_TOOLS)


def set_active_tools(configured: Collection[str] | None) -> frozenset[str]:
    """Set the tool list from ``server.confirm_tools`` (None = defaults)."""
    global _active_tools
    _active_tools = frozenset(DEFAULT_CONFIRM_TOOLS if configured is None else configured)
    return _active_tools


def active_tools() -> frozenset[str]:
    return _active_tools


def requires_confirmation(
    tool: str, kw: dict, tools: Collection[str] | None = None,
) -> bool:
    """Public form of the server rule: would this call raise the dialog?"""
    return _needs_confirmation(tool, kw, _active_tools if tools is None else tools)


async def confirm_destructive(
    ctx: Any, tool: str, kwargs: dict, tools: Collection[str] = DEFAULT_CONFIRM_TOOLS,
    strict: bool = False,
) -> dict:
    """Ask the user before a destructive tool runs. Returns the kwargs to use.

    Raises :class:`ConfirmationDeclined` when the user declines or cancels,
    :class:`ConfirmationUnavailable` when the dialog could not be shown, or,
    with ``strict``, when the client cannot show one at all (no form
    elicitation, or no back channel to send it the request).
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
        return _without_dialog(tool, kwargs, strict, "no form elicitation")
    try:
        if not _can_send_request(ctx):
            raise NoBackChannelError("elicitation/create")
        result = await ctx.elicit(message=_message(tool, kwargs), schema=_Confirm)
    except NoBackChannelError:
        # raised before anything reaches the client: it cannot show a dialog
        audit.emit("tool.confirm.no_back_channel", tool=tool, strict=strict)
        return _without_dialog(tool, kwargs, strict, "no back channel")
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
