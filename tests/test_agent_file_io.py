"""proxmox_read_file / proxmox_write_file against PVE's agent/file-* semantics.

The fake client mirrors qemu-server's API2/Qemu/Agent.pm: file-read decodes
the guest agent's base64 unless ``decode=0``, file-write base64-encodes
``content`` itself unless ``encode=0``, and ``content`` is capped at 60 KiB.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.proxmox import system  # noqa: E402

NODE, VM = "pve1", 100


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakePVE:
    def __init__(self, *, has_decode_param: bool = True) -> None:
        self.files: dict[str, bytes] = {}
        self.has_decode_param = has_decode_param

    def get(self, node: str, path: str, **params: Any) -> Any:
        if path == f"nodes/{node}/qemu/{VM}/status/current":
            return {"status": "running"}
        if path == f"nodes/{node}/qemu/{VM}/agent/file-read":
            if not self.has_decode_param:
                # Older qemu-server: neither `decode` nor `count` in the schema.
                for name in ("decode", "count"):
                    if name in params:
                        return {"error": f"400 Parameter verification failed: {name}: property is not defined in schema"}
            data = self.files[params["file"]][: params.get("count", 16 * 1024 * 1024)]
            if params.get("decode", 1):
                # Perl hands JSON the decoded bytes, one code point each.
                return {"content": data.decode("latin-1"), "bytes-read": len(data)}
            return {"content": base64.b64encode(data).decode(), "bytes-read": len(data)}
        return {"error": "not found"}

    def post(self, node: str, path: str, **params: Any) -> Any:
        if path == f"nodes/{node}/qemu/{VM}/agent/file-write":
            if len(params["content"]) > 60 * 1024:
                return {"error": "400 Parameter verification failed: content: value may only be 61440 characters long"}
            encode = params.get("encode", 1)
            buf = base64.b64encode(params["content"].encode()).decode() if encode else params["content"]
            self.files[params["file"]] = base64.b64decode(buf)
            return {}
        return {"error": "not found"}

    def exec_stat(self, path: str) -> int | None:
        return len(self.files.get(path, b""))


@pytest.fixture
def pve(monkeypatch: pytest.MonkeyPatch):
    fake = FakePVE()
    monkeypatch.setattr(system, "_qemu_file_size", lambda c, n, v, p: fake.exec_stat(p))
    mcp = FakeMCP()
    system.register_system_tools(mcp, fake, None)
    fake.tools = mcp.tools
    return fake


async def test_write_stores_the_content_not_its_base64(pve) -> None:
    res = await pve.tools["proxmox_write_file"](node=NODE, vmid=VM, path="/etc/motd", content="héllo\n")
    assert res["status"] == "success", res
    assert pve.files["/etc/motd"] == "héllo\n".encode()


async def test_write_over_the_agent_cap_points_to_transfers(pve) -> None:
    res = await pve.tools["proxmox_write_file"](node=NODE, vmid=VM, path="/x", content="a" * (46 * 1024))
    assert res["status"] == "error"
    assert "proxmox_upload_link" in res["error"]
    ok = await pve.tools["proxmox_write_file"](node=NODE, vmid=VM, path="/x", content="a" * (45 * 1024))
    assert ok["status"] == "success", ok


async def test_read_text_round_trips(pve) -> None:
    pve.files["/etc/hostname"] = "web01\n".encode()
    res = await pve.tools["proxmox_read_file"](node=NODE, vmid=VM, path="/etc/hostname")
    assert res == {"status": "success", "vmid": VM, "node": NODE, "path": "/etc/hostname", "content": "web01\n"}


async def test_read_binary_returns_real_base64(pve) -> None:
    blob = bytes(range(256))
    pve.files["/bin/blob"] = blob
    res = await pve.tools["proxmox_read_file"](node=NODE, vmid=VM, path="/bin/blob", binary=True)
    assert base64.b64decode(res["content_base64"]) == blob
    text = await pve.tools["proxmox_read_file"](node=NODE, vmid=VM, path="/bin/blob")
    assert text["status"] == "error" and "binary=True" in text["error"]


async def test_read_works_on_pve_without_the_decode_param(pve) -> None:
    pve.has_decode_param = False
    pve.files["/etc/os"] = "Ubuntu é\n".encode()
    res = await pve.tools["proxmox_read_file"](node=NODE, vmid=VM, path="/etc/os")
    assert res["content"] == "Ubuntu é\n"


async def test_read_refuses_more_than_the_limit(pve, monkeypatch: pytest.MonkeyPatch) -> None:
    # stat unavailable: the post-read guard must still catch it.
    monkeypatch.setattr(system, "_qemu_file_size", lambda c, n, v, p: None)
    pve.files["/big"] = b"x" * (system._FILE_READ_MAX_BYTES + 10)
    res = await pve.tools["proxmox_read_file"](node=NODE, vmid=VM, path="/big")
    assert res["status"] == "error"
