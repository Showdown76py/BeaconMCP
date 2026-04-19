"""High-level aggregator tools that collapse multi-call workflows into one.

Rationale
---------
A typical diagnostic session in an MCP client looks like:
``list_nodes`` -> ``list_vms`` -> ``storage_status`` -> ``node_status`` -> ``get_logs``.
That's five tool calls, five round-trips, and a lot of repeated JSON.

The helpers in this module return the same information in one call each, at
the cost of slightly larger payloads. Clients keep full access to the
fine-grained tools; these aggregators exist so the LLM can pick a shorter
path when it doesn't yet know what it's looking for.

All aggregators are careful to:
* gracefully downgrade if a capability is missing (no SSH -> no SSH facts;
  no BMC registry -> no hardware facts).
* report errors inline per node/VM rather than failing the whole call, so the
  caller can still work with the partial view.
"""

from __future__ import annotations

import asyncio
import fnmatch
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import Config
from ..utils import filter_fields
from .client import ProxmoxClient


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Cap on concurrent Proxmox/BMC fan-outs. Small homelab clusters (3-10 nodes)
# fit comfortably; bigger clusters still benefit from parallelism without
# hammering the API with hundreds of simultaneous TLS handshakes.
_MAX_PARALLEL = 8


def _parallel_map(fn: Any, items: list[Any]) -> list[Any]:
    """Run ``fn(item)`` for each item in parallel threads, preserving order.

    Keeps aggregator fan-out roughly linear in the slowest node rather than
    serial sum-of-all-nodes. Errors bubble back as the function's normal
    return shape (each helper already returns error dicts inline), so we
    don't catch here.
    """
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(items))) as ex:
        return list(ex.map(fn, items))


def _collect_node_summaries(client: ProxmoxClient) -> list[dict[str, Any]]:
    """One row per configured node with key health metrics.

    Mirrors ``proxmox_list_nodes`` but keeps only the fields cluster_overview
    actually needs to stay token-efficient.
    """
    def _one(node_name: str) -> dict[str, Any]:
        data = client.get(node_name, "nodes")
        if isinstance(data, dict) and "error" in data:
            return {"name": node_name, "status": "unreachable", "error": data["error"]}
        if not isinstance(data, list):
            return {"name": node_name, "status": "unknown"}
        for node in data:
            if node.get("node") != node_name:
                continue
            return {
                "name": node_name,
                "status": node.get("status", "unknown"),
                "cpu": round(node.get("cpu", 0) * 100, 1),
                "mem_used_gb": round(node.get("mem", 0) / 1073741824, 1),
                "mem_total_gb": round(node.get("maxmem", 0) / 1073741824, 1),
                "uptime_h": round(node.get("uptime", 0) / 3600, 1),
            }
        return {"name": node_name, "status": "unknown"}

    return _parallel_map(_one, list(client.configured_nodes))


def _collect_vm_summaries(
    client: ProxmoxClient, target_nodes: list[str] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Flat list of VMs across one or more nodes + total count.

    Returns a flat list (not nested by node) because callers that use this
    helper want to filter/count across the whole set; the per-node nesting
    shape is already available via ``proxmox_list_vms``.
    """
    nodes = list(target_nodes or client.configured_nodes)

    # Each node needs a qemu + lxc fetch. Flatten to (node, vm_type) tuples
    # so the whole fan-out runs in parallel instead of 2 * N serial calls.
    tasks = [(n, t) for n in nodes for t in ("qemu", "lxc")]

    def _one(task: tuple[str, str]) -> list[dict[str, Any]]:
        n, vm_type = task
        data = client.get(n, f"nodes/{n}/{vm_type}")
        if not isinstance(data, list):
            return []
        return [{
            "node": n,
            "vmid": vm.get("vmid"),
            "name": vm.get("name", ""),
            "status": vm.get("status"),
            "type": vm_type,
            "cpu_pct": round(vm.get("cpu", 0) * 100, 1),
            "mem_used_mb": round(vm.get("mem", 0) / 1048576, 0),
        } for vm in data]

    rows: list[dict[str, Any]] = []
    for chunk in _parallel_map(_one, tasks):
        rows.extend(chunk)
    rows.sort(key=lambda v: (v.get("node", ""), v.get("vmid", 0)))
    return rows, len(rows)


def _collect_storage_summaries(client: ProxmoxClient) -> list[dict[str, Any]]:
    def _per_node(n: str) -> list[dict[str, Any]]:
        data = client.get(n, f"nodes/{n}/storage")
        if isinstance(data, dict) and "error" in data:
            return [{"node": n, "error": data["error"]}]
        if not isinstance(data, list):
            return []
        # Fan out the per-pool status queries within a node too -- 4+ pools
        # per node is common (local, zfs, nfs, cephfs).
        pools = [s for s in data if s.get("storage")]

        def _pool(s: dict[str, Any]) -> dict[str, Any]:
            name = s["storage"]
            status = client.get(n, f"nodes/{n}/storage/{name}/status")
            used = total = 0
            if isinstance(status, dict) and "error" not in status:
                used = status.get("used", 0)
                total = status.get("total", 0)
            return {
                "node": n,
                "name": name,
                "type": s.get("type"),
                "used_gb": round(used / 1073741824, 1),
                "total_gb": round(total / 1073741824, 1),
                "usage_pct": round(used / total * 100, 1) if total > 0 else 0,
            }

        return _parallel_map(_pool, pools)

    rows: list[dict[str, Any]] = []
    for chunk in _parallel_map(_per_node, list(client.configured_nodes)):
        rows.extend(chunk)
    return rows


def _find_vm_location(client: ProxmoxClient, vmid: int) -> tuple[str, str] | None:
    """Return (node, vm_type) for a VMID; None if not found."""
    for n in client.configured_nodes:
        for vm_type in ("qemu", "lxc"):
            data = client.get(n, f"nodes/{n}/{vm_type}/{vmid}/status/current")
            if isinstance(data, dict) and "error" in data:
                continue
            if isinstance(data, dict) and data.get("status"):
                return (n, vm_type)
    return None


async def _bmc_summary(bmc_registry: dict, config: Config, node: str) -> dict[str, Any] | None:
    """Return a short BMC status blurb for the given node, if one is mapped.

    Heuristic: a BMC device is "attached" to a node when its ``jump_host``
    matches the node name. That's how HP iLO setups tend to be declared and
    is the only mapping the config currently exposes.
    """
    if not bmc_registry:
        return None
    matches = [d for d in config.bmc_devices if d.jump_host == node]
    if not matches:
        return None
    device = matches[0]
    client = bmc_registry.get(device.id)
    if not client:
        return {"device_id": device.id, "error": "BMC device in config but not in registry"}
    try:
        power = await client.power_status()
        health = await client.health()
    except Exception as exc:  # noqa: BLE001  -- surface anything as a soft error
        return {"device_id": device.id, "error": str(exc)}
    return {
        "device_id": device.id,
        "type": device.type,
        "power": power.get("power_status") if isinstance(power, dict) else None,
        "health_summary": {
            k: v
            for k, v in (health.items() if isinstance(health, dict) else [])
            if k in ("overall", "fans", "temperatures", "power_supplies")
        },
    }


def _recent_errors(client: ProxmoxClient, node: str, limit: int = 20) -> list[dict[str, Any]]:
    """Pull the last ``limit`` failed tasks on ``node``.

    Proxmox exposes task exit status as a string: "OK" for success, anything
    else (including "unknown", actual error strings) means not-ok.
    """
    data = client.get(node, f"nodes/{node}/tasks", limit=limit)
    if not isinstance(data, list):
        return []
    errors = []
    for t in data:
        status = t.get("status", "")
        if status and status != "OK":
            errors.append({
                "upid": t.get("upid"),
                "type": t.get("type"),
                "status": status,
                "user": t.get("user"),
                "starttime": t.get("starttime"),
                "endtime": t.get("endtime"),
            })
    return errors


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_aggregator_tools(
    mcp: FastMCP,
    proxmox_client: ProxmoxClient,
    config: Config,
    bmc_registry: dict | None = None,
) -> None:
    """Register the four aggregator tools.

    ``bmc_registry`` is accepted as a plain dict (device_id -> BMCClient)
    rather than importing the type, so this module can be registered even
    when BMC support is disabled.
    """

    bmc_registry = bmc_registry or {}

    @mcp.tool()
    def cluster_overview(
        include_storage: bool = True,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return cluster state (nodes + VMs + optional storage) in one call.

        Use this as the first diagnostic step -- it replaces
        ``proxmox_list_nodes`` + ``proxmox_list_vms`` + ``proxmox_storage_status``.
        Set ``include_storage=False`` to skip storage (saves the per-pool status
        roundtrip on large clusters). Pass ``fields=[...]`` to trim each entry
        to only the keys you need (applied uniformly to nodes/vms/storage).
        """
        nodes = _collect_node_summaries(proxmox_client)
        vms, total_vms = _collect_vm_summaries(proxmox_client)
        out: dict[str, Any] = {
            "nodes": filter_fields(nodes, fields),
            "vms": filter_fields(vms, fields),
            "total_vms": total_vms,
        }
        if include_storage:
            out["storage"] = filter_fields(_collect_storage_summaries(proxmox_client), fields)
        return out

    @mcp.tool()
    async def cluster_health(node: str = "") -> dict[str, Any]:
        """Aggregate health signals for one node (or all nodes): metrics + BMC + recent errors.

        Collapses ``proxmox_node_status`` + ``bmc_health_status`` +
        ``proxmox_get_tasks`` into one call. When ``node`` is empty every
        configured node is scanned. BMC data is only attached for nodes that
        have a BMC device declared with ``jump_host: <node>``.
        """
        target_nodes = [node] if node else list(proxmox_client.configured_nodes)

        async def _one(n: str) -> dict[str, Any]:
            # Offload the blocking Proxmox calls to a thread so we can run the
            # BMC await + Proxmox fetch in parallel per node, and every node in
            # parallel overall via gather.
            loop = asyncio.get_running_loop()
            status_task = loop.run_in_executor(
                None, lambda: proxmox_client.get(n, f"nodes/{n}/status"),
            )
            errors_task = loop.run_in_executor(
                None, lambda: _recent_errors(proxmox_client, n, limit=20),
            )
            bmc_task = _bmc_summary(bmc_registry, config, n)
            status, errors, bmc = await asyncio.gather(status_task, errors_task, bmc_task)
            if isinstance(status, dict) and "error" in status:
                return {"node": n, "error": status["error"]}
            entry: dict[str, Any] = {
                "node": n,
                "cpu_pct": round(status.get("cpu", 0) * 100, 1),
                "mem_used_gb": round(status.get("memory", {}).get("used", 0) / 1073741824, 1),
                "mem_total_gb": round(status.get("memory", {}).get("total", 0) / 1073741824, 1),
                "uptime_h": round(status.get("uptime", 0) / 3600, 1),
                "kernel": status.get("kversion"),
                "pve_version": status.get("pveversion"),
            }
            if bmc is not None:
                entry["bmc"] = bmc
            entry["recent_errors"] = errors
            return entry

        results = list(await asyncio.gather(*[_one(n) for n in target_nodes]))
        if node:
            return results[0] if results else {"error": f"Node {node!r} not configured."}
        return {"nodes": results}

    @mcp.tool()
    def vm_find(pattern: str, node: str = "") -> dict[str, Any]:
        """Find VMs/CTs by name using glob (``web-*``) or substring (``db``).

        Returns a compact hit list so the caller can follow up with
        ``proxmox_vm_status`` or ``vm_bulk_action``. Omit ``node`` to search
        across every configured node.
        """
        target_nodes = [node] if node else None
        vms, _ = _collect_vm_summaries(proxmox_client, target_nodes)
        pat = pattern.strip()
        is_glob = any(ch in pat for ch in "*?[")
        hits: list[dict[str, Any]] = []
        for vm in vms:
            name = vm.get("name", "")
            if is_glob:
                if fnmatch.fnmatchcase(name, pat):
                    hits.append(vm)
            elif pat.lower() in name.lower():
                hits.append(vm)
        return {"pattern": pat, "total": len(hits), "vms": hits}

    @mcp.tool()
    def vm_bulk_action(
        vmids: list[int],
        action: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run ``start``/``stop``/``restart`` on many VMs/CTs in parallel.

        Locates each VMID across the cluster, fires the action, and collects
        per-VM UPIDs (or errors) in one response. ``force`` applies to stop
        and restart actions. Capped at 50 VMs per call to prevent runaway
        fan-out; split larger lists client-side.
        """
        valid_actions = {"start", "stop", "restart"}
        if action not in valid_actions:
            return {"error": f"Unsupported action {action!r}. Use one of {sorted(valid_actions)}."}

        # Dedupe while preserving order -- repeated VMIDs are almost always a
        # caller bug and doing the same stop/start twice is never what they want.
        seen: set[int] = set()
        unique_vmids = [v for v in vmids if not (v in seen or seen.add(v))]

        # Hard cap. A typo like `vm_bulk_action(range(1, 10000), "stop")` should
        # fail loud, not take down a cluster. 50 covers legit bulk ops on any
        # homelab-scale setup.
        _MAX_BULK = 50
        if len(unique_vmids) > _MAX_BULK:
            return {
                "error": f"Too many VMIDs ({len(unique_vmids)} unique); cap is {_MAX_BULK} per call. "
                "Split into multiple calls.",
            }

        def _one(vmid: int) -> dict[str, Any]:
            location = _find_vm_location(proxmox_client, vmid)
            if not location:
                return {"vmid": vmid, "error": "not found"}
            n, vm_type = location
            endpoint = f"nodes/{n}/{vm_type}/{vmid}/status/{action}"
            params: dict[str, Any] = {}
            if action in ("stop", "restart") and force:
                params["forceStop"] = 1
            resp = proxmox_client.post(n, endpoint, **params)
            if isinstance(resp, dict) and "error" in resp:
                return {"vmid": vmid, "node": n, "error": resp["error"]}
            upid = resp if isinstance(resp, str) else (
                resp.get("upid") if isinstance(resp, dict) else None
            )
            return {"vmid": vmid, "node": n, "type": vm_type, "upid": upid}

        results = _parallel_map(_one, unique_vmids)
        ok = sum(1 for r in results if "upid" in r)
        return {"action": action, "total": len(results), "ok": ok, "results": results}
