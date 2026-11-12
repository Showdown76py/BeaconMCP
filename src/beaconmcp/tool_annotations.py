"""MCP tool annotations, declared once for every tool.

Annotations are hints for the client (approval prompts, grouping of read-only
tools); they are not a security boundary. Enforcement stays server-side, see
``confirm.DEFAULT_CONFIRM_TOOLS`` and the bearer/TOTP layers.

Rules applied to each entry (MCP spec, ``ToolAnnotations``):

- ``readOnlyHint`` is true only when the tool changes nothing in any call mode
  (``proxmox_vm_config`` reads without ``updates`` but writes with it, so it is
  not read-only). Read-only entries leave destructive/idempotent unset: the
  spec ignores them there.
- ``destructiveHint`` is true when the tool can delete, overwrite, stop or run
  arbitrary code; false only for a pure addition without loss.
- ``idempotentHint`` is true only when repeating the same call has no further
  effect; when in doubt it is false.
- ``openWorldHint`` is true only for tools that reach beyond the declared
  infrastructure (Proxmox, declared SSH hosts, BMCs): arbitrary URLs and the
  remote git repository used for updates.

Every registered tool must have an entry; ``tests/test_tool_annotations.py``
fails on a tool without one and on an entry without a tool.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


def _read(*, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(read_only_hint=True, open_world_hint=open_world)


def _write(
    *, destructive: bool, idempotent: bool, open_world: bool = False,
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    # Aggregators and monitoring
    "cluster_overview": _read(),
    "cluster_overview_interactive": _read(),
    "cluster_health": _read(),
    "vm_find": _read(),
    "proxmox_list_nodes": _read(),
    "proxmox_node_status": _read(),
    "proxmox_list_vms": _read(),
    "proxmox_vm_status": _read(),
    "proxmox_get_logs": _read(),
    "proxmox_get_tasks": _read(),
    "proxmox_vm_panel": _read(),
    "proxmox_logs_panel": _read(),
    "proxmox_storage_status": _read(),
    "proxmox_network_config": _read(),
    "proxmox_snapshot_list": _read(),
    "proxmox_backup_list": _read(),
    "proxmox_list_transfers": _read(),
    "proxmox_read_file": _read(),
    "ssh_list_sessions": _read(),
    "bmc_list_devices": _read(),
    "bmc_server_info": _read(),
    "bmc_health_status": _read(),
    "bmc_power_status": _read(),
    "bmc_get_event_log": _read(),
    "beaconmcp_check_update": _read(open_world=True),
    # Guest lifecycle
    "proxmox_vm_start": _write(destructive=False, idempotent=True),
    "proxmox_vm_stop": _write(destructive=True, idempotent=True),
    "proxmox_vm_restart": _write(destructive=True, idempotent=False),
    "proxmox_vm_create": _write(destructive=True, idempotent=False),
    "proxmox_vm_clone": _write(destructive=False, idempotent=False),
    "proxmox_vm_migrate": _write(destructive=True, idempotent=False),
    "proxmox_vm_config": _write(destructive=True, idempotent=True),
    "vm_bulk_action": _write(destructive=True, idempotent=False),
    # Snapshots and backups
    "proxmox_snapshot_create": _write(destructive=False, idempotent=False),
    "proxmox_snapshot_rollback": _write(destructive=True, idempotent=False),
    "proxmox_snapshot_delete": _write(destructive=True, idempotent=True),
    "proxmox_backup_create": _write(destructive=False, idempotent=False),
    "proxmox_backup_restore": _write(destructive=True, idempotent=False),
    # Commands and files
    "proxmox_run": _write(destructive=True, idempotent=False),
    "ssh_run": _write(destructive=True, idempotent=False),
    "proxmox_write_file": _write(destructive=True, idempotent=True),
    "proxmox_upload_file": _write(destructive=True, idempotent=True),
    "proxmox_download_file": _write(destructive=True, idempotent=True),
    # Only mints a link, but its holder can overwrite a file in the guest.
    "proxmox_upload_link": _write(destructive=True, idempotent=False),
    "proxmox_fetch_url": _write(destructive=True, idempotent=True, open_world=True),
    "proxmox_delete_transfer": _write(destructive=True, idempotent=True),
    # Hardware
    "bmc_power_on": _write(destructive=False, idempotent=True),
    "bmc_power_off": _write(destructive=True, idempotent=True),
    "bmc_power_reset": _write(destructive=True, idempotent=False),
    # Server itself
    "security_end_session": _write(destructive=True, idempotent=True),
    "beaconmcp_self_update": _write(destructive=True, idempotent=False, open_world=True),
}
