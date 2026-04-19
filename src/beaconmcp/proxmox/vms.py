from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

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
    def proxmox_vm_config(node: str, vmid: int, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read or modify the configuration of a VM or container.

        Without 'updates': returns the full current configuration.
        With 'updates': applies the provided config changes (e.g., {"memory": 4096, "cores": 4}).
        Use to inspect or change VM settings like memory, CPU cores, network, disks, etc.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}

        if updates is None:
            data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/config")
            if isinstance(data, dict) and "error" in data:
                return data
            return {"vmid": vmid, "node": node, "type": vm_type, "config": data}

        result = client.put(node, f"nodes/{node}/{vm_type}/{vmid}/config", **updates)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "config_update", "applied": updates}

    @mcp.tool()
    def proxmox_backup_create(node: str, vmid: int, storage: str, mode: str = "snapshot", compress: str = "zstd", notes: str = "") -> dict[str, Any]:
        """Create a new backup of a VM or container.
        
        Args:
            storage: The storage pool where the backup will be saved (e.g. 'local', 'pbs', 'nfs').
            mode: 'stop', 'suspend', or 'snapshot' (default is 'snapshot' for zero downtime).
            compress: Compression algorithm ('zstd', 'lzo', 'gzip', or '0' for none).
            notes: Optional description for the backup.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"error": f"VM/CT {vmid} not found on node '{node}'. Check VMID and node name."}
            
        params = {
            "vmid": vmid,
            "storage": storage,
            "mode": mode,
            "compress": compress
        }
        if notes:
            params["notes"] = notes
            
        result = client.post(node, "nodes/{node}/vzdump", **params)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "backup_create", "storage": storage, "upid": result}

    @mcp.tool()
    def proxmox_backup_list(node: str, storage: str, vmid: int | None = None) -> dict[str, Any]:
        """List available backups (vzdump archives) on a specific storage pool.
        
        Args:
            storage: The storage pool to query (e.g. 'local', 'pbs', 'nfs').
            vmid: Filter backups by a specific VM/CT.
        """
        # GET /nodes/{node}/storage/{storage}/content?content=backup
        params = {"content": "backup"}
        if vmid is not None:
            params["vmid"] = vmid
            
        data = client.get(node, f"nodes/{node}/storage/{storage}/content", **params)
        if isinstance(data, dict) and "error" in data:
            return data
            
        if not isinstance(data, list):
            return {"backups": []}
            
        # Standardize the output for the LLM
        backups = []
        for b in data:
            backups.append({
                "volid": b.get("volid"),
                "vmid": b.get("vmid"),
                "format": b.get("format"),
                "size_gb": round(b.get("size", 0) / 1073741824, 2),
                "ctime": b.get("ctime"),
                "notes": b.get("notes", "")
            })
            
        return {"node": node, "storage": storage, "backups": backups}

    @mcp.tool()
    def proxmox_backup_restore(node: str, vmid: int, archive: str, force: bool = False, storage: str = "local-lvm") -> dict[str, Any]:
        """Restore a VM or container from a backup archive.
        
        Args:
            archive: The full volume ID of the backup archive (e.g. 'local:backup/vzdump-qemu-100-2023_10_25-00_00_00.vma.zst').
                     Use proxmox_backup_list to find the correct 'volid'.
            force: If True, overwrites an existing VM/CT if it already exists.
            storage: Target storage for the restored disks (default 'local-lvm').
        """
        # Determine if archive is for QEMU or LXC
        if "qemu" in archive:
            endpoint = f"nodes/{node}/qemu"
            vm_type = "qemu"
        elif "lxc" in archive:
            endpoint = f"nodes/{node}/lxc"
            vm_type = "lxc"
        else:
            return {"error": "Could not determine if backup is for 'qemu' or 'lxc' from archive name. Archive must contain 'qemu' or 'lxc'."}
            
        params = {
            "vmid": vmid,
            "archive": archive,
            "force": 1 if force else 0,
            "storage": storage
        }
            
        result = client.post(node, endpoint, **params)
        if isinstance(result, dict) and "error" in result:
            return result
        return {"vmid": vmid, "node": node, "action": "backup_restore", "archive": archive, "upid": result}
