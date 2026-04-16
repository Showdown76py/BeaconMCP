from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ProxmoxClient


def register_monitoring_tools(mcp: FastMCP, client: ProxmoxClient) -> None:
    """Register all Proxmox monitoring and diagnostic tools."""

    @mcp.tool()
    def proxmox_list_nodes() -> dict[str, Any]:
        """List all Proxmox cluster nodes with their status (online/offline).

        Use this as the first step when diagnosing cluster health or checking which nodes are available.
        Returns a list of nodes with name, status, CPU usage, memory usage, and uptime.
        If a node appears offline, use ilo_health_status to check if it's a hardware issue,
        or ssh_exec_command to try reaching it directly.
        """
        results = []
        for node_name in client.configured_nodes:
            data = client.get(node_name, "nodes")
            if isinstance(data, dict) and "error" in data:
                results.append({"name": node_name, "status": "unreachable", "error": data["error"]})
            elif isinstance(data, list):
                for node in data:
                    results.append({
                        "name": node.get("node"),
                        "status": node.get("status", "unknown"),
                        "cpu": round(node.get("cpu", 0) * 100, 1),
                        "memory_used_gb": round(node.get("mem", 0) / 1073741824, 1),
                        "memory_total_gb": round(node.get("maxmem", 0) / 1073741824, 1),
                        "uptime_hours": round(node.get("uptime", 0) / 3600, 1),
                    })
            else:
                results.append({"name": node_name, "status": "unknown", "raw": str(data)})
        return {"nodes": results}

    @mcp.tool()
    def proxmox_node_status(node: str) -> dict[str, Any]:
        """Get detailed status of a specific Proxmox node: CPU, RAM, disk, uptime, kernel, PVE version.

        Use after proxmox_list_nodes to drill into a specific node.
        Provide the node name (e.g., 'pve1').
        Returns detailed resource usage and system information.
        """
        data = client.get(node, f"nodes/{node}/status")
        if isinstance(data, dict) and "error" in data:
            return data
        return {
            "node": node,
            "cpu_cores": data.get("cpuinfo", {}).get("cores"),
            "cpu_model": data.get("cpuinfo", {}).get("model"),
            "cpu_usage_pct": round(data.get("cpu", 0) * 100, 1),
            "memory_used_gb": round(data.get("memory", {}).get("used", 0) / 1073741824, 1),
            "memory_total_gb": round(data.get("memory", {}).get("total", 0) / 1073741824, 1),
            "swap_used_gb": round(data.get("swap", {}).get("used", 0) / 1073741824, 1),
            "swap_total_gb": round(data.get("swap", {}).get("total", 0) / 1073741824, 1),
            "rootfs_used_gb": round(data.get("rootfs", {}).get("used", 0) / 1073741824, 1),
            "rootfs_total_gb": round(data.get("rootfs", {}).get("total", 0) / 1073741824, 1),
            "uptime_hours": round(data.get("uptime", 0) / 3600, 1),
            "kernel_version": data.get("kversion"),
            "pve_version": data.get("pveversion"),
        }

    @mcp.tool()
    def proxmox_list_vms(node: str = "") -> dict[str, Any]:
        """List all VMs and containers with their status and resource usage.

        Use to get an overview of what's running on the cluster.
        Omit 'node' to list VMs across all configured nodes.
        Provide a node name (e.g., 'pve1') to list only that node's VMs.
        Returns VMID, name, status, type (qemu/lxc), CPU, and memory for each.
        """
        target_nodes = [node] if node else client.configured_nodes
        all_vms: list[dict[str, Any]] = []

        for n in target_nodes:
            for vm_type in ("qemu", "lxc"):
                data = client.get(n, f"nodes/{n}/{vm_type}")
                if isinstance(data, dict) and "error" in data:
                    all_vms.append({"node": n, "type": vm_type, "error": data["error"]})
                    continue
                if not isinstance(data, list):
                    continue
                for vm in data:
                    all_vms.append({
                        "node": n,
                        "vmid": vm.get("vmid"),
                        "name": vm.get("name", ""),
                        "status": vm.get("status"),
                        "type": vm_type,
                        "cpu_usage_pct": round(vm.get("cpu", 0) * 100, 1),
                        "memory_used_mb": round(vm.get("mem", 0) / 1048576, 0),
                        "memory_max_mb": round(vm.get("maxmem", 0) / 1048576, 0),
                        "disk_used_gb": round(vm.get("disk", 0) / 1073741824, 1),
                        "uptime_hours": round(vm.get("uptime", 0) / 3600, 1),
                    })

        all_vms.sort(key=lambda v: v.get("vmid", 0))
        return {"vms": all_vms, "total": len(all_vms)}

    @mcp.tool()
    def proxmox_vm_status(node: str, vmid: int) -> dict[str, Any]:
        """Get detailed status of a specific VM or container: CPU, RAM, disk I/O, network I/O, uptime.

        Use after proxmox_list_vms to drill into a specific VM.
        Provide both the node name and VMID.
        Auto-detects whether the target is a QEMU VM or LXC container.
        """
        # Try qemu first, then lxc
        for vm_type in ("qemu", "lxc"):
            data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
            if isinstance(data, dict) and "error" in data:
                if "does not exist" in str(data.get("error", "")).lower():
                    continue
                # Real error (network, auth)
                return data
            if isinstance(data, dict) and data.get("status"):
                config_data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/config")
                result: dict[str, Any] = {
                    "node": node,
                    "vmid": vmid,
                    "type": vm_type,
                    "name": data.get("name", ""),
                    "status": data.get("status"),
                    "cpu_usage_pct": round(data.get("cpu", 0) * 100, 1),
                    "cpus": data.get("cpus"),
                    "memory_used_mb": round(data.get("mem", 0) / 1048576, 0),
                    "memory_max_mb": round(data.get("maxmem", 0) / 1048576, 0),
                    "disk_read_mb": round(data.get("diskread", 0) / 1048576, 1),
                    "disk_write_mb": round(data.get("diskwrite", 0) / 1048576, 1),
                    "net_in_mb": round(data.get("netin", 0) / 1048576, 1),
                    "net_out_mb": round(data.get("netout", 0) / 1048576, 1),
                    "uptime_hours": round(data.get("uptime", 0) / 3600, 1),
                    "pid": data.get("pid"),
                }
                if isinstance(config_data, dict) and "error" not in config_data:
                    result["config_summary"] = {
                        "cores": config_data.get("cores"),
                        "memory_mb": config_data.get("memory"),
                        "description": config_data.get("description", ""),
                    }
                return result

        return {"error": f"VM/CT {vmid} not found on node '{node}'. Check the VMID and node name."}

    @mcp.tool()
    def proxmox_get_logs(node: str, source: str = "syslog", limit: int = 50) -> dict[str, Any]:
        """Retrieve system logs from a Proxmox node.

        Use to diagnose system-level issues, crashes, or service failures.
        Set 'source' to 'syslog' for system logs or 'tasks' for Proxmox task logs.
        Adjust 'limit' to control how many log lines to return (default 50, max 500).
        """
        limit = min(limit, 500)

        if source == "tasks":
            data = client.get(node, f"nodes/{node}/tasks", limit=limit)
            if isinstance(data, dict) and "error" in data:
                return data
            if isinstance(data, list):
                return {
                    "node": node,
                    "source": "tasks",
                    "entries": [
                        {
                            "upid": t.get("upid"),
                            "type": t.get("type"),
                            "status": t.get("status"),
                            "user": t.get("user"),
                            "starttime": t.get("starttime"),
                            "endtime": t.get("endtime"),
                        }
                        for t in data
                    ],
                }
            return {"node": node, "source": "tasks", "entries": [], "raw": str(data)}

        # syslog
        data = client.get(node, f"nodes/{node}/syslog", limit=limit)
        if isinstance(data, dict) and "error" in data:
            return data
        if isinstance(data, list):
            return {
                "node": node,
                "source": "syslog",
                "lines": [entry.get("t", "") for entry in data],
            }
        return {"node": node, "source": "syslog", "lines": [], "raw": str(data)}

    @mcp.tool()
    def proxmox_get_tasks(node: str = "", limit: int = 20) -> dict[str, Any]:
        """List recent Proxmox tasks across the cluster: migrations, backups, VM operations.

        Use to check what operations have been running or to investigate failed tasks.
        Omit 'node' to list tasks from all configured nodes.
        Returns task type, status, user, and timing for each.
        """
        target_nodes = [node] if node else client.configured_nodes
        all_tasks: list[dict[str, Any]] = []

        for n in target_nodes:
            data = client.get(n, f"nodes/{n}/tasks", limit=limit)
            if isinstance(data, dict) and "error" in data:
                all_tasks.append({"node": n, "error": data["error"]})
                continue
            if isinstance(data, list):
                for t in data:
                    all_tasks.append({
                        "node": n,
                        "upid": t.get("upid"),
                        "type": t.get("type"),
                        "status": t.get("status"),
                        "user": t.get("user"),
                        "starttime": t.get("starttime"),
                        "endtime": t.get("endtime"),
                    })

        return {"tasks": all_tasks, "total": len(all_tasks)}
