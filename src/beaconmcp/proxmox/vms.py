from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..utils import filter_fields
from .client import ProxmoxClient


def _detect_vm_type(client: ProxmoxClient, node: str, vmid: int) -> str | None:
    """Detect whether a VMID is a QEMU VM or LXC container."""
    for vm_type in ("qemu", "lxc"):
        data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
        if isinstance(data, dict) and "error" in data:
            continue
        if isinstance(data, dict) and data.get("status"):
            return vm_type
    return None


def register_vm_tools(mcp: FastMCP, client: ProxmoxClient) -> None:
    """Register all Proxmox VM/CT lifecycle management tools."""

    @mcp.tool()
    def proxmox_vm_start(node: str, vmid: int) -> dict[str, Any]:
        """Start a stopped VM or container.

        Use when a VM/CT needs to be powered on.
        Provide the node name and VMID. Auto-detects VM vs container.
        Returns the task UPID on success for tracking the operation.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/status/start")
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "start", "upid": result}

    @mcp.tool()
    def proxmox_vm_stop(node: str, vmid: int, force: bool = False) -> dict[str, Any]:
        """Stop a running VM or container.

        Use to shut down a VM/CT. Set force=true for an immediate hard stop
        (equivalent to pulling the power cord -- use only when a clean shutdown fails).
        Default is a clean ACPI shutdown for VMs or clean stop for containers.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        endpoint = "stop" if force else "shutdown"
        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/status/{endpoint}")
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": endpoint, "force": force, "upid": result}

    @mcp.tool()
    def proxmox_vm_restart(node: str, vmid: int) -> dict[str, Any]:
        """Restart a running VM or container (clean reboot).

        Use when a VM/CT needs to be rebooted. Sends an ACPI reboot signal for VMs
        or a clean restart for containers. If the VM is unresponsive, stop it with force=true first,
        then start it again.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        # Both QEMU and LXC use /status/reboot (PVE 7+). LXC does not expose /status/restart.
        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/status/reboot")
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "restart", "upid": result}

    @mcp.tool()
    def proxmox_vm_create(node: str, vmid: int, vm_type: str = "qemu", config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a new VM or container on a Proxmox node.

        Use to provision new virtual machines or containers.
        Set vm_type to 'qemu' for a VM or 'lxc' for a container.
        Pass configuration as a dict (e.g., {"cores": 2, "memory": 4096, "net0": "virtio,bridge=vmbr0"}).
        Refer to Proxmox API docs for available config options per VM type.
        """
        if vm_type not in ("qemu", "lxc"):
            return {"error": f"Invalid vm_type '{vm_type}'. Use 'qemu' for VMs or 'lxc' for containers."}

        create_params = config or {}
        result = client.post(node, f"nodes/{node}/{vm_type}", vmid=vmid, **create_params)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "type": vm_type, "action": "create", "upid": result}

    @mcp.tool()
    def proxmox_vm_clone(node: str, vmid: int, newid: int, name: str = "") -> dict[str, Any]:
        """Clone an existing VM or container to create a copy.

        Use to duplicate a VM/CT. Provide the source VMID, the new VMID for the clone,
        and optionally a name. The clone inherits the source configuration.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        kwargs: dict[str, Any] = {"newid": newid}
        if name:
            # PVE uses `name` for QEMU VMs and `hostname` for LXC containers.
            kwargs["name" if vm_type == "qemu" else "hostname"] = name

        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/clone", **kwargs)
        if isinstance(result, dict) and "error" in result:
            return result
        return {
            "src_vmid": vmid,
            "new_vmid": newid,
            "name": name,
            "node": node,
            "action": "clone",
            "upid": result,
        }

    @mcp.tool()
    def proxmox_vm_migrate(node: str, vmid: int, target_node: str) -> dict[str, Any]:
        """Migrate a VM or container to another Proxmox node.

        Use to move a VM/CT from one node to another (e.g., for maintenance or load balancing).
        The VM can be running (live migration) or stopped.
        Provide the current node, VMID, and the target node name.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/migrate", target=target_node)
        if isinstance(result, dict) and "error" in result:
            return result
        return {
            "vmid": vmid,
            "src_node": node,
            "dst_node": target_node,
            "action": "migrate",
            "upid": result,
        }

    @mcp.tool()
    def proxmox_vm_config(
        node: str,
        vmid: int,
        updates: dict[str, Any] | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read or modify the configuration of a VM or container.

        Without 'updates': returns the full current configuration.
        With 'updates': applies the provided config changes (e.g., {"memory": 4096, "cores": 4}).
        Use to inspect or change VM settings like memory, CPU cores, network, disks, etc.
        Pass ``fields=[...]`` (read-only mode) to trim the returned ``config``
        blob -- helpful because full VM configs can be large (dozens of
        disk/net/hostpci keys). Ignored when ``updates`` is given.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        if updates is None:
            data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/config")
            if isinstance(data, dict) and "error" in data:
                return data
            return {
                "vmid": vmid,
                "node": node,
                "type": vm_type,
                "config": filter_fields(data, fields),
            }

        result = client.put(node, f"nodes/{node}/{vm_type}/{vmid}/config", **updates)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "config_update", "applied": updates}

    @mcp.tool()
    def proxmox_snapshot_list(node: str, vmid: int) -> dict[str, Any]:
        """List all snapshots for a VM or container.
        
        Returns the snapshot hierarchy including names, descriptions, and creation times.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        result = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/snapshot")
        if isinstance(result, dict) and "error" in result:
            return result
            
        return {"vmid": vmid, "node": node, "snapshots": result}

    @mcp.tool()
    def proxmox_snapshot_create(
        node: str, 
        vmid: int, 
        snapname: str, 
        description: str = "", 
        vmstate: bool = False,
        dry_run: bool = False
    ) -> dict[str, Any]:
        """Create a new snapshot of a VM or container.
        
        Use this before risky operations (OS upgrades, risky commands) to establish a safe checkpoint.
        Set vmstate=true to capture RAM (slower, only for running QEMU VMs).
        """
        if dry_run: return {"status": "dry_run", "message": f"Would create snapshot {snapname!r} for VM/CT {vmid} on {node}."}
        
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        params = {"snapname": snapname}
        if description:
            params["description"] = description
        if vmstate and vm_type == "qemu":
            params["vmstate"] = 1
            
        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/snapshot", **params)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "snapshot_create", "snapname": snapname, "upid": result}

    @mcp.tool()
    def proxmox_snapshot_rollback(node: str, vmid: int, snapname: str, dry_run: bool = False) -> dict[str, Any]:
        """Roll back a VM or container to a previous snapshot.
        
        Restores the guest to the exact state of the named snapshot.
        Use proxmox_snapshot_list to find the correct snapname.
        """
        if dry_run: return {"status": "dry_run", "message": f"Would roll back VM/CT {vmid} on {node} to snapshot {snapname!r}."}
        
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        result = client.post(node, f"nodes/{node}/{vm_type}/{vmid}/snapshot/{snapname}/rollback")
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "snapshot_rollback", "snapname": snapname, "upid": result}

    @mcp.tool()
    def proxmox_snapshot_delete(node: str, vmid: int, snapname: str, dry_run: bool = False) -> dict[str, Any]:
        """Delete a VM or container snapshot.
        
        Removes the snapshot from the storage backing the VM.
        """
        if dry_run: return {"status": "dry_run", "message": f"Would delete snapshot {snapname!r} from VM/CT {vmid} on {node}."}
        
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        result = client.delete(node, f"nodes/{node}/{vm_type}/{vmid}/snapshot/{snapname}")
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "snapshot_delete", "snapname": snapname, "upid": result}
