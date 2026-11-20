from unittest.mock import MagicMock

import pytest

from beaconmcp.proxmox.vms import register_vm_tools


def _clone_tool(vm_type="qemu"):
    tools = {}
    mcp = MagicMock()

    def deco(*a, **k):
        def wrap(fn):
            tools[fn.__name__] = fn
            return fn
        return wrap

    mcp.tool = deco
    client = MagicMock()

    def get(node, path, **kw):
        if f"/{vm_type}/" in path and path.endswith("status/current"):
            return {"status": "stopped"}
        return {"error": "nope"}

    client.get.side_effect = get
    client.post.return_value = "UPID:pve1:1:clone"
    register_vm_tools(mcp, client)
    return tools["proxmox_vm_clone"], client


def test_clone_defaults_send_only_newid_and_name():
    tool, client = _clone_tool("qemu")
    res = tool("pve1", 100, 200, "copy")
    client.post.assert_called_once_with("pve1", "nodes/pve1/qemu/100/clone", newid=200, name="copy")
    assert res["upid"] == "UPID:pve1:1:clone"


def test_clone_defaults_lxc_unchanged():
    tool, client = _clone_tool("lxc")
    tool("pve1", 100, 200, "ct")
    client.post.assert_called_once_with("pve1", "nodes/pve1/lxc/100/clone", newid=200, hostname="ct")


@pytest.mark.parametrize("vm_type", ["qemu", "lxc"])
def test_clone_full_storage_target_forwarded(vm_type):
    tool, client = _clone_tool(vm_type)
    tool("pve1", 100, 200, full=True, storage="local-lvm", target="pve2")
    client.post.assert_called_once_with(
        "pve1", f"nodes/pve1/{vm_type}/100/clone",
        newid=200, full=1, storage="local-lvm", target="pve2",
    )


def test_clone_full_false_sends_zero():
    tool, client = _clone_tool("qemu")
    tool("pve1", 100, 200, full=False)
    assert client.post.call_args.kwargs == {"newid": 200, "full": 0}
