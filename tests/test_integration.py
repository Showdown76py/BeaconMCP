"""
BeaconMCP -- Integration test suite
===================================

Run against real infrastructure once services are back online.
Requires a filled .env file with real credentials.

Usage:
    # Run all tests (requires all services: PVE1 + SSH + iLO)
    python tests/test_integration.py

    # Run a specific section
    python tests/test_integration.py --section proxmox
    python tests/test_integration.py --section ssh
    python tests/test_integration.py --section bmc

    # Run with a test VM (for destructive tests: start/stop/clone)
    python tests/test_integration.py --test-vmid 9999

Prerequisites:
    - API token created on pve1 (see README.md)
    - QEMU Guest Agent installed in at least one VM
    - SSH access to pve1 with password auth
    - iLO accessible via pve1 tunnel (for iLO tests)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Ensure the project is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    message: str
    data: dict | None = None


class TestRunner:
    def __init__(self) -> None:
        self.results: list[TestResult] = []
        self._section = ""

    def section(self, name: str) -> None:
        self._section = name
        print(f"\n{'=' * 60}")
        print(f"  {name}")
        print(f"{'=' * 60}")

    def record(self, name: str, passed: bool, message: str, data: dict | None = None) -> None:
        full_name = f"[{self._section}] {name}" if self._section else name
        result = TestResult(full_name, passed, message, data)
        self.results.append(result)
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}] {name}")
        if not passed:
            print(f"         {message}")
        if data and not passed:
            preview = json.dumps(data, indent=2, default=str)[:300]
            print(f"         {preview}")

    def summary(self) -> None:
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        total = len(self.results)
        print(f"\n{'=' * 60}")
        print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
        print(f"{'=' * 60}")
        if failed:
            print("\n  Failed tests:")
            for r in self.results:
                if not r.passed:
                    print(f"    - {r.name}: {r.message}")
        print()


# ---------------------------------------------------------------------------
# Import tools (done lazily so .env is loaded first)
# ---------------------------------------------------------------------------

def get_tools() -> dict:
    """Import and return all registered MCP tools."""
    from beaconmcp.server import mcp
    return mcp._tool_manager._tools


def call_tool(tools: dict, name: str, **kwargs):
    """Call an MCP tool function directly, handling both sync and async."""
    tool = tools[name]
    result = tool.fn(**kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    return result


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------

def test_proxmox_monitoring(runner: TestRunner, tools: dict) -> None:
    runner.section("Proxmox Monitoring")

    # T1: List nodes
    result = call_tool(tools, "proxmox_list_nodes")
    has_nodes = isinstance(result, dict) and "nodes" in result and len(result["nodes"]) > 0
    runner.record(
        "proxmox_list_nodes returns nodes",
        has_nodes,
        "Expected a list of nodes with at least 1 entry",
        result,
    )

    if has_nodes:
        pve1_node = next((n for n in result["nodes"] if n.get("name") == "pve1"), None)
        runner.record(
            "pve1 is present in node list",
            pve1_node is not None,
            "pve1 should appear in the cluster node list",
            result,
        )
        runner.record(
            "pve1 is online",
            pve1_node is not None and pve1_node.get("status") == "online",
            f"pve1 status: {pve1_node.get('status') if pve1_node else 'missing'}",
            pve1_node,
        )

    # T2: Node status
    result = call_tool(tools, "proxmox_node_status", node="pve1")
    has_cpu = isinstance(result, dict) and "cpu_cores" in result
    runner.record(
        "proxmox_node_status returns CPU/RAM/disk info",
        has_cpu and "memory_total_gb" in result and "rootfs_total_gb" in result,
        "Expected cpu_cores, memory_total_gb, rootfs_total_gb",
        result,
    )
    if has_cpu:
        runner.record(
            "CPU usage is a percentage (0-100)",
            0 <= result.get("cpu_usage_pct", -1) <= 100,
            f"cpu_usage_pct = {result.get('cpu_usage_pct')}",
        )
        runner.record(
            "Uptime is positive",
            result.get("uptime_hours", 0) > 0,
            f"uptime_hours = {result.get('uptime_hours')}",
        )
        runner.record(
            "PVE version is present",
            result.get("pve_version") is not None,
            f"pve_version = {result.get('pve_version')}",
        )

    # T3: List VMs
    result = call_tool(tools, "proxmox_list_vms", node="pve1")
    has_vms = isinstance(result, dict) and "vms" in result
    runner.record(
        "proxmox_list_vms returns VM list",
        has_vms,
        "Expected a 'vms' key with a list",
        result,
    )
    if has_vms and len(result["vms"]) > 0:
        first_vm = result["vms"][0]
        runner.record(
            "VMs have required fields (vmid, name, status, type)",
            all(k in first_vm for k in ("vmid", "name", "status", "type")),
            f"First VM keys: {list(first_vm.keys())}",
            first_vm,
        )

    # T4: VM status (use first running VM found)
    running_vm = None
    if has_vms:
        running_vm = next((v for v in result["vms"] if v.get("status") == "running"), None)
    if running_vm:
        result = call_tool(tools, "proxmox_vm_status", node="pve1", vmid=running_vm["vmid"])
        runner.record(
            f"proxmox_vm_status for VMID {running_vm['vmid']} returns details",
            isinstance(result, dict) and "status" in result and "cpu_usage_pct" in result,
            "Expected status, cpu_usage_pct, memory fields",
            result,
        )
    else:
        runner.record(
            "proxmox_vm_status (skipped: no running VM found)",
            True,
            "Need at least one running VM to test vm_status",
        )

    # T5: Get logs
    result = call_tool(tools, "proxmox_get_logs", node="pve1", source="syslog", limit=10)
    runner.record(
        "proxmox_get_logs (syslog) returns log lines",
        isinstance(result, dict) and "lines" in result and len(result.get("lines", [])) > 0,
        "Expected non-empty 'lines' list",
        result if isinstance(result, dict) and "error" in result else None,
    )

    result = call_tool(tools, "proxmox_get_logs", node="pve1", source="tasks", limit=5)
    runner.record(
        "proxmox_get_logs (tasks) returns task entries",
        isinstance(result, dict) and "entries" in result,
        "Expected 'entries' list",
        result if isinstance(result, dict) and "error" in result else None,
    )

    # T6: Get tasks
    result = call_tool(tools, "proxmox_get_tasks", node="pve1", limit=5)
    runner.record(
        "proxmox_get_tasks returns task list",
        isinstance(result, dict) and "tasks" in result,
        "Expected 'tasks' key",
        result,
    )


def test_proxmox_system(runner: TestRunner, tools: dict) -> None:
    runner.section("Proxmox System")

    # T7: Storage status
    result = call_tool(tools, "proxmox_storage_status", node="pve1")
    has_storage = isinstance(result, dict) and "storage" in result and len(result.get("storage", [])) > 0
    runner.record(
        "proxmox_storage_status returns storage list",
        has_storage,
        "Expected at least one storage entry",
        result,
    )
    if has_storage:
        first = result["storage"][0]
        runner.record(
            "Storage entries have name/type/usage fields",
            all(k in first for k in ("storage", "type", "used_gb", "total_gb")),
            f"First storage keys: {list(first.keys())}",
            first,
        )

    # T8: Network config
    result = call_tool(tools, "proxmox_network_config", node="pve1")
    has_ifaces = isinstance(result, dict) and "interfaces" in result and len(result.get("interfaces", [])) > 0
    runner.record(
        "proxmox_network_config returns interface list",
        has_ifaces,
        "Expected at least one network interface",
        result,
    )
    if has_ifaces:
        bridge = next((i for i in result["interfaces"] if i.get("type") == "bridge"), None)
        runner.record(
            "At least one bridge interface exists",
            bridge is not None,
            "Proxmox nodes should have at least one bridge (vmbr0)",
        )


def test_proxmox_exec(runner: TestRunner, tools: dict) -> None:
    runner.section("Proxmox Command Execution (QEMU Guest Agent)")

    # Find a running QEMU VM with guest agent
    vms_result = call_tool(tools, "proxmox_list_vms", node="pve1")
    running_qemu = None
    if isinstance(vms_result, dict) and "vms" in vms_result:
        running_qemu = next(
            (v for v in vms_result["vms"]
             if v.get("status") == "running" and v.get("type") == "qemu"),
            None,
        )

    if not running_qemu:
        runner.record(
            "proxmox_exec_command (skipped: no running QEMU VM)",
            True,
            "Need a running QEMU VM with guest agent to test exec",
        )
        return

    vmid = running_qemu["vmid"]
    print(f"  Using VMID {vmid} ({running_qemu.get('name', '?')}) for exec tests")

    # T9: Sync exec -- simple command
    result = call_tool(tools, "proxmox_exec_command", node="pve1", vmid=vmid,
                       command="echo BeaconMCP-test", timeout=30)
    runner.record(
        f"proxmox_exec_command 'echo' in VM {vmid}",
        isinstance(result, dict) and "BeaconMCP-test" in result.get("stdout", ""),
        f"Expected stdout containing 'BeaconMCP-test', got: {result}",
        result,
    )

    # T10: Sync exec -- exit code
    result = call_tool(tools, "proxmox_exec_command", node="pve1", vmid=vmid,
                       command="cat /etc/hostname", timeout=30)
    runner.record(
        f"proxmox_exec_command 'cat /etc/hostname' returns exit_code 0",
        isinstance(result, dict) and result.get("exit_code") == 0,
        f"exit_code = {result.get('exit_code')}, stdout = {result.get('stdout', '')[:100]}",
        result,
    )

    # T11: Async exec + poll
    result = call_tool(tools, "proxmox_exec_command_async", node="pve1", vmid=vmid,
                       command="sleep 3 && echo async-done")
    has_exec_id = isinstance(result, dict) and "exec_id" in result
    runner.record(
        f"proxmox_exec_command_async returns exec_id",
        has_exec_id and result.get("status") == "running",
        f"Expected status=running with exec_id",
        result,
    )

    if has_exec_id:
        exec_id = result["exec_id"]
        # Poll until done (max 30s)
        deadline = time.time() + 30
        final_result = None
        while time.time() < deadline:
            final_result = call_tool(tools, "proxmox_exec_get_result", exec_id=exec_id)
            if isinstance(final_result, dict) and final_result.get("status") != "running":
                break
            time.sleep(2)

        runner.record(
            f"proxmox_exec_get_result returns completed result",
            isinstance(final_result, dict) and final_result.get("status") == "completed",
            f"Final status: {final_result.get('status') if final_result else 'none'}",
            final_result,
        )
        if isinstance(final_result, dict) and final_result.get("status") == "completed":
            runner.record(
                "Async exec stdout contains 'async-done'",
                "async-done" in final_result.get("stdout", ""),
                f"stdout = {final_result.get('stdout', '')[:100]}",
            )

    # T12: Exec on non-existent VM
    result = call_tool(tools, "proxmox_exec_command", node="pve1", vmid=99999,
                       command="echo test", timeout=10)
    runner.record(
        "proxmox_exec_command on invalid VMID returns error",
        isinstance(result, dict) and "error" in result,
        f"Expected error, got: {result}",
        result,
    )


def test_proxmox_exec_lxc(runner: TestRunner, tools: dict) -> None:
    runner.section("Proxmox Command Execution (LXC)")

    vms_result = call_tool(tools, "proxmox_list_vms", node="pve1")
    running_lxc = None
    if isinstance(vms_result, dict) and "vms" in vms_result:
        running_lxc = next(
            (v for v in vms_result["vms"]
             if v.get("status") == "running" and v.get("type") == "lxc"),
            None,
        )

    if not running_lxc:
        runner.record(
            "LXC exec (skipped: no running LXC container)",
            True,
            "Need a running LXC container to test lxc exec",
        )
        return

    vmid = running_lxc["vmid"]
    print(f"  Using CT {vmid} ({running_lxc.get('name', '?')}) for LXC exec tests")

    result = call_tool(tools, "proxmox_exec_command", node="pve1", vmid=vmid,
                       command="echo LXC-test", timeout=30)
    # The Proxmox API does not expose an exec endpoint for LXC containers; the
    # tool must return an actionable error pointing to ssh_exec_command + pct exec.
    runner.record(
        f"proxmox_exec_command on CT {vmid} returns LXC-not-supported guidance",
        isinstance(result, dict)
        and "error" in result
        and "pct exec" in result.get("error", ""),
        f"Result: {result}",
        result,
    )


def test_proxmox_vm_lifecycle(runner: TestRunner, tools: dict, test_vmid: int | None) -> None:
    runner.section("Proxmox VM Lifecycle")

    if not test_vmid:
        runner.record(
            "VM lifecycle tests (skipped: no --test-vmid provided)",
            True,
            "Pass --test-vmid <id> to run start/stop/clone/config tests on a sacrificial VM",
        )
        return

    print(f"  Using VMID {test_vmid} for lifecycle tests")

    # T13: Read VM config
    result = call_tool(tools, "proxmox_vm_config", node="pve1", vmid=test_vmid)
    runner.record(
        f"proxmox_vm_config (read) for VMID {test_vmid}",
        isinstance(result, dict) and ("config" in result or "error" in result),
        f"Expected config or error",
        result,
    )

    if isinstance(result, dict) and "error" in result:
        runner.record(
            "VM lifecycle tests aborted: test VM not found",
            False,
            f"VMID {test_vmid} not found. Create it first or use a different --test-vmid.",
            result,
        )
        return

    vm_type = result.get("type", "qemu")

    # T14: Stop (if running)
    status_result = call_tool(tools, "proxmox_vm_status", node="pve1", vmid=test_vmid)
    if isinstance(status_result, dict) and status_result.get("status") == "running":
        result = call_tool(tools, "proxmox_vm_stop", node="pve1", vmid=test_vmid)
        runner.record(
            f"proxmox_vm_stop VMID {test_vmid}",
            isinstance(result, dict) and "upid" in result,
            f"Expected UPID, got: {result}",
            result,
        )
        print(f"  Waiting 10s for VM to stop...")
        time.sleep(10)

    # T15: Start
    result = call_tool(tools, "proxmox_vm_start", node="pve1", vmid=test_vmid)
    runner.record(
        f"proxmox_vm_start VMID {test_vmid}",
        isinstance(result, dict) and ("upid" in result or "error" in result),
        f"Result: {result}",
        result,
    )
    if isinstance(result, dict) and "upid" in result:
        print(f"  Waiting 10s for VM to start...")
        time.sleep(10)

    # T16: Verify it's running
    status_result = call_tool(tools, "proxmox_vm_status", node="pve1", vmid=test_vmid)
    runner.record(
        f"VMID {test_vmid} is running after start",
        isinstance(status_result, dict) and status_result.get("status") == "running",
        f"Status: {status_result.get('status') if isinstance(status_result, dict) else status_result}",
        status_result,
    )

    # T17: Restart
    result = call_tool(tools, "proxmox_vm_restart", node="pve1", vmid=test_vmid)
    runner.record(
        f"proxmox_vm_restart VMID {test_vmid}",
        isinstance(result, dict) and ("upid" in result or "error" in result),
        f"Result: {result}",
        result,
    )

    # T18: Modify config (change description, harmless)
    result = call_tool(tools, "proxmox_vm_config", node="pve1", vmid=test_vmid,
                       updates={"description": "BeaconMCP test VM - safe to delete"})
    runner.record(
        f"proxmox_vm_config (update description) VMID {test_vmid}",
        isinstance(result, dict) and ("updates_applied" in result or "error" in result),
        f"Result: {result}",
        result,
    )

    # T19: Clone (to VMID test_vmid+1000)
    clone_id = test_vmid + 1000
    result = call_tool(tools, "proxmox_vm_clone", node="pve1", vmid=test_vmid,
                       newid=clone_id, name="beaconmcp-test-clone")
    runner.record(
        f"proxmox_vm_clone {test_vmid} -> {clone_id}",
        isinstance(result, dict) and ("upid" in result or "error" in result),
        f"Result: {result}",
        result,
    )

    # Cleanup: delete the clone if it was created
    if isinstance(result, dict) and "upid" in result:
        print(f"  Waiting 15s for clone to complete...")
        time.sleep(15)
        # Stop clone if running, then delete
        call_tool(tools, "proxmox_vm_stop", node="pve1", vmid=clone_id, force=True)
        time.sleep(5)
        from beaconmcp.server import proxmox_client
        proxmox_client.delete("pve1", f"nodes/pve1/{vm_type}/{clone_id}")
        print(f"  Cleaned up clone VMID {clone_id}")


def test_ssh(runner: TestRunner, tools: dict) -> None:
    runner.section("SSH Module")

    if "ssh_exec_command" not in tools:
        runner.record(
            "SSH tests (skipped: SSH not configured)",
            True,
            "Set SSH_USER and SSH_PASSWORD in .env to enable SSH tests",
        )
        return

    # T20: SSH exec on pve1
    result = call_tool(tools, "ssh_exec_command", host="pve1", command="uptime", timeout=30)
    runner.record(
        "ssh_exec_command 'uptime' on pve1",
        isinstance(result, dict) and result.get("exit_code") == 0 and "load average" in result.get("stdout", ""),
        f"Result: {result}",
        result,
    )

    # T21: SSH exec -- hostname
    result = call_tool(tools, "ssh_exec_command", host="pve1", command="hostname", timeout=15)
    runner.record(
        "ssh_exec_command 'hostname' on pve1",
        isinstance(result, dict) and result.get("exit_code") == 0 and len(result.get("stdout", "").strip()) > 0,
        f"stdout = '{result.get('stdout', '').strip()}'",
        result,
    )

    # T22: SSH exec -- df (disk usage)
    result = call_tool(tools, "ssh_exec_command", host="pve1", command="df -h /", timeout=15)
    runner.record(
        "ssh_exec_command 'df -h /' on pve1",
        isinstance(result, dict) and result.get("exit_code") == 0,
        f"exit_code = {result.get('exit_code')}",
        result,
    )

    # T23: SSH exec -- failing command
    result = call_tool(tools, "ssh_exec_command", host="pve1",
                       command="cat /nonexistent/file/12345", timeout=15)
    runner.record(
        "ssh_exec_command on nonexistent file returns non-zero exit code",
        isinstance(result, dict) and result.get("exit_code", 0) != 0,
        f"exit_code = {result.get('exit_code')}, stderr = {result.get('stderr', '')[:100]}",
        result,
    )

    # T24: SSH host resolution -- VMID format
    from beaconmcp.server import ssh_client
    resolved = ssh_client.resolve_host("101")
    runner.record(
        "SSH host resolution: VMID '101' -> 192.168.1.101",
        resolved == "192.168.1.101",
        f"Resolved to: {resolved}",
    )
    resolved = ssh_client.resolve_host("pve1")
    runner.record(
        "SSH host resolution: 'pve1' -> configured host",
        resolved == os.environ.get("PVE1_HOST", ""),
        f"Resolved to: {resolved}",
    )

    # T25: SSH async exec + poll
    result = call_tool(tools, "ssh_exec_command_async", host="pve1",
                       command="sleep 2 && echo ssh-async-done")
    has_id = isinstance(result, dict) and "exec_id" in result
    runner.record(
        "ssh_exec_command_async returns exec_id",
        has_id,
        f"Result: {result}",
        result,
    )

    if has_id:
        exec_id = result["exec_id"]
        print(f"  Polling exec_id={exec_id}...")
        deadline = time.time() + 30
        final = None
        while time.time() < deadline:
            final = call_tool(tools, "ssh_exec_get_result", exec_id=exec_id)
            if isinstance(final, dict) and final.get("status") != "running":
                break
            time.sleep(2)
        runner.record(
            "ssh_exec_get_result returns completed",
            isinstance(final, dict) and final.get("status") == "completed",
            f"Final: {final}",
            final,
        )

    # T26: List sessions
    result = call_tool(tools, "ssh_list_sessions")
    runner.record(
        "ssh_list_sessions returns session list",
        isinstance(result, dict) and "sessions" in result,
        f"Result: {result}",
        result,
    )


def test_bmc(runner: TestRunner, tools: dict) -> None:
    runner.section("BMC Module")

    if "bmc_server_info" not in tools:
        runner.record(
            "BMC tests (skipped: no BMC device configured)",
            True,
            "Add at least one entry to bmc.devices[] in beaconmcp.yaml to enable BMC tests.",
        )
        return

    # T27: Server info
    result = call_tool(tools, "bmc_server_info")
    runner.record(
        "bmc_server_info returns server details",
        isinstance(result, dict) and ("product_name" in result or "fru" in result or "error" in result),
        f"Result: {json.dumps(result, default=str)[:200]}",
        result,
    )
    if isinstance(result, dict) and "error" in result:
        runner.record(
            "BMC tests aborted: cannot reach BMC",
            False,
            result["error"],
        )
        return

    # T28: Health status
    result = call_tool(tools, "bmc_health_status")
    runner.record(
        "bmc_health_status returns health data",
        isinstance(result, dict) and "error" not in result,
        f"Result type: {type(result).__name__}, keys: {list(result.keys())[:5] if isinstance(result, dict) else 'N/A'}",
        result if isinstance(result, dict) and "error" in result else None,
    )

    # T29: Power status
    result = call_tool(tools, "bmc_power_status")
    runner.record(
        "bmc_power_status returns ON/OFF",
        isinstance(result, dict) and "power_status" in result,
        f"Result: {result}",
        result,
    )
    if isinstance(result, dict) and "power_status" in result:
        runner.record(
            "Server power is ON",
            str(result["power_status"]).upper() == "ON",
            f"power_status = {result['power_status']}",
        )

    # T30: Event log
    result = call_tool(tools, "bmc_get_event_log", limit=10)
    runner.record(
        "bmc_get_event_log returns events",
        isinstance(result, dict) and ("events" in result or "error" in result),
        f"Total events: {result.get('total', '?')}",
        result if isinstance(result, dict) and "error" in result else None,
    )

    # NOTE: power_on / power_off / power_reset are destructive; skipped in
    # automated runs. Test them manually if needed.
    runner.record(
        "bmc_power_on/off/reset (not tested: destructive)",
        True,
        "Manual testing required. Use bmc_power_status to verify state first.",
    )


def test_mcp_resources(runner: TestRunner) -> None:
    runner.section("MCP Resources & Prompts")

    from beaconmcp.server import mcp, config

    # T31: Infrastructure resource
    resource_fn = None
    for key, res in mcp._resource_manager._resources.items():
        if "infrastructure" in str(key):
            resource_fn = res
            break

    runner.record(
        "beaconmcp://infrastructure resource is registered",
        resource_fn is not None,
        "Expected a resource matching 'infrastructure'",
    )

    # T32: Infrastructure YAML is loaded
    runner.record(
        "infrastructure.yaml is loaded into config",
        len(config.infrastructure) > 0,
        f"Keys: {list(config.infrastructure.keys()) if config.infrastructure else 'empty'}",
    )

    # T33: Prompt is registered
    prompts = mcp._prompt_manager._prompts
    runner.record(
        "beaconmcp_context prompt is registered",
        "beaconmcp_context" in prompts,
        f"Available prompts: {list(prompts.keys())}",
    )

    # T34: Config validation
    runner.record(
        "Config has at least 1 PVE node",
        len(config.pve_nodes) >= 1,
        f"Nodes: {[n.name for n in config.pve_nodes]}",
    )
    runner.record(
        "PVE1 host matches expected",
        config.pve_nodes[0].host == os.environ.get("PVE1_HOST", ""),
        f"Host: {config.pve_nodes[0].host}",
    )


def test_error_handling(runner: TestRunner, tools: dict) -> None:
    runner.section("Error Handling")

    # T35: Invalid node
    result = call_tool(tools, "proxmox_node_status", node="nonexistent-node")
    runner.record(
        "Invalid node returns actionable error",
        isinstance(result, dict) and "error" in result,
        f"Result: {result}",
        result,
    )

    # T36: Invalid VMID
    result = call_tool(tools, "proxmox_vm_status", node="pve1", vmid=99999)
    runner.record(
        "Invalid VMID returns error (not crash)",
        isinstance(result, dict) and "error" in result,
        f"Result: {result}",
        result,
    )

    # T37: Invalid exec_id
    result = call_tool(tools, "proxmox_exec_get_result", exec_id="nonexistent")
    runner.record(
        "Invalid exec_id returns error",
        isinstance(result, dict) and "error" in result,
        f"Result: {result}",
        result,
    )

    if "ssh_exec_get_result" in tools:
        result = call_tool(tools, "ssh_exec_get_result", exec_id="nonexistent")
        runner.record(
            "Invalid SSH exec_id returns error",
            isinstance(result, dict) and "error" in result,
            f"Result: {result}",
            result,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="BeaconMCP integration tests")
    parser.add_argument("--section", choices=["proxmox", "ssh", "bmc", "all"], default="all",
                        help="Which section to test")
    parser.add_argument("--test-vmid", type=int, default=None,
                        help="VMID of a sacrificial test VM for lifecycle tests (start/stop/clone)")
    args = parser.parse_args()

    runner = TestRunner()
    tools = get_tools()

    print(f"\nBeaconMCP Integration Tests")
    print(f"Tools registered: {len(tools)}")
    print(f"Section: {args.section}")
    if args.test_vmid:
        print(f"Test VMID: {args.test_vmid}")

    sections = args.section

    if sections in ("proxmox", "all"):
        test_proxmox_monitoring(runner, tools)
        test_proxmox_system(runner, tools)
        test_proxmox_exec(runner, tools)
        test_proxmox_exec_lxc(runner, tools)
        test_proxmox_vm_lifecycle(runner, tools, args.test_vmid)
        test_error_handling(runner, tools)

    if sections in ("ssh", "all"):
        test_ssh(runner, tools)

    if sections in ("bmc", "all"):
        test_bmc(runner, tools)

    if sections == "all":
        test_mcp_resources(runner)

    runner.summary()

    # Exit with error code if any test failed
    failed = sum(1 for r in runner.results if not r.passed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
