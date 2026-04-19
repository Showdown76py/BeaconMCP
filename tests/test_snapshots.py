import pytest
from unittest.mock import MagicMock
from beaconmcp.proxmox.vms import register_vm_tools

def test_snapshot_tools():
    mcp_mock = MagicMock()
    # Capture the registered tools
    tools = {}
    
    def tool_decorator(*args, **kwargs):
        def wrapper(func):
            tools[func.__name__] = func
            return func
        return wrapper
        
    mcp_mock.tool = tool_decorator
    
    client_mock = MagicMock()
    # Mock _detect_vm_type behavior by returning a dummy status
    client_mock.get.side_effect = lambda node, path: {"status": "running"} if "status/current" in path else [{"name": "snap1"}]
    
    # Mock post/delete for snapshot actions
    client_mock.post.return_value = "UPID:node:123:snapshot_create"
    client_mock.delete.return_value = "UPID:node:123:snapshot_delete"
    
    register_vm_tools(mcp_mock, client_mock)
    
    # Test proxmox_snapshot_list
    assert "proxmox_snapshot_list" in tools
    res = tools["proxmox_snapshot_list"]("pve1", 100)
    assert "snapshots" in res
    
    # Test proxmox_snapshot_create with vmstate on running QEMU
    assert "proxmox_snapshot_create" in tools
    res = tools["proxmox_snapshot_create"]("pve1", 100, "snap1", "Test", True)
    assert res.get("action") == "snapshot_create"
    
    # Test proxmox_snapshot_rollback
    assert "proxmox_snapshot_rollback" in tools
    res = tools["proxmox_snapshot_rollback"]("pve1", 100, "snap1")
    assert res.get("action") == "snapshot_rollback"
    
    # Test proxmox_snapshot_delete
    assert "proxmox_snapshot_delete" in tools
    res = tools["proxmox_snapshot_delete"]("pve1", 100, "snap1")
    assert res.get("action") == "snapshot_delete"
