"""proxmox_run on QEMU guests goes through the guest's shell.

The guest agent's ``agent/exec`` runs an argv without a shell, so the
command line has to be wrapped (``/bin/sh -c`` or ``cmd.exe /c``) for pipes,
redirections and ``&&`` to mean what they mean for LXC containers.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.proxmox.system import _guest_argv, register_system_tools  # noqa: E402


class FakeClient:
    """Answers just enough of the PVE API for a QEMU proxmox_run."""

    def __init__(self, osinfo):
        self.osinfo = osinfo
        self.exec_commands: list[list[str]] = []

    def get(self, node, path, **kwargs):
        if path.endswith("/qemu/100/status/current"):
            return {"status": "running"}
        if path.endswith("/status/current"):
            return {"error": "not found"}
        if path.endswith("/agent/get-osinfo"):
            return self.osinfo
        if path.endswith("/agent/exec-status"):
            return {
                "exited": 1, "exitcode": 0,
                "out-data": base64.b64encode(b"b\n").decode(),
                "out-data-encoding": "base64",
            }
        raise AssertionError(path)

    def post(self, node, path, **kwargs):
        assert path.endswith("/agent/exec")
        self.exec_commands.append(kwargs["command"])
        return {"pid": 42}


def _tools(client):
    mcp = MagicMock()
    tools = {}

    def tool(*a, **k):
        def deco(fn):
            tools[fn.__name__] = fn
            return fn
        return deco

    mcp.tool = tool
    register_system_tools(mcp, client)
    return tools


LINUX = {"result": {"id": "debian", "name": "Debian GNU/Linux"}}
WINDOWS = {"result": {"id": "mswindows", "name": "Microsoft Windows"}}


@pytest.mark.parametrize(
    "osinfo, expected_prefix",
    [
        (LINUX, ["/bin/sh", "-c"]),
        (WINDOWS, ["cmd.exe", "/c"]),
        # Agent can't report its OS: assume POSIX.
        ({"error": "guest-get-osinfo has been disabled"}, ["/bin/sh", "-c"]),
        ({"result": {}}, ["/bin/sh", "-c"]),
        (None, ["/bin/sh", "-c"]),
    ],
)
def test_guest_argv_picks_the_guest_shell(osinfo, expected_prefix) -> None:
    client = FakeClient(osinfo)
    cmd = "echo a | tr a b"
    assert _guest_argv(client, "pve1", 100, cmd)[0] == expected_prefix + [cmd]


async def test_qemu_command_is_passed_whole_to_the_shell() -> None:
    client = FakeClient(LINUX)
    run = _tools(client)["proxmox_run"]
    res = await run(node="pve1", vmid=100, command="echo a | tr a b > /tmp/out && cat /tmp/out")
    assert res["status"] == "ok"
    assert client.exec_commands == [
        ["/bin/sh", "-c", "echo a | tr a b > /tmp/out && cat /tmp/out"]
    ]
    # The session keeps the command as the caller wrote it.
    assert res["command"] == "echo a | tr a b > /tmp/out && cat /tmp/out"


async def test_windows_paths_keep_their_backslashes() -> None:
    client = FakeClient(WINDOWS)
    run = _tools(client)["proxmox_run"]
    await run(node="pve1", vmid=100, command=r"type C:\Windows\System32\drivers\etc\hosts")
    assert client.exec_commands == [
        ["cmd.exe", "/c", r"type C:\Windows\System32\drivers\etc\hosts"]
    ]


async def test_unbalanced_quote_no_longer_raises() -> None:
    # shlex.split used to raise here after the session was registered.
    client = FakeClient(LINUX)
    run = _tools(client)["proxmox_run"]
    res = await run(node="pve1", vmid=100, command="echo 'oops")
    assert res["status"] == "ok"
    assert client.exec_commands == [["/bin/sh", "-c", "echo 'oops"]]


class FailingExecClient(FakeClient):
    def post(self, node, path, **kwargs):
        self.exec_commands.append(kwargs["command"])
        return {"error": "Failed to execute child process \u201c/bin/sh\u201d (No such file or directory)"}


async def test_exec_error_names_the_shell_fallback_when_the_os_is_unknown() -> None:
    client = FailingExecClient({"error": "guest-get-osinfo has been disabled"})
    res = await _tools(client)["proxmox_run"](node="pve1", vmid=100, command="dir")
    assert res["status"] == "failed"
    assert "/bin/sh" in res["error"] and "guest-get-osinfo" in res["error"]


async def test_exec_error_has_no_hint_when_the_os_is_known() -> None:
    client = FailingExecClient(LINUX)
    res = await _tools(client)["proxmox_run"](node="pve1", vmid=100, command="dir")
    assert res["status"] == "failed"
    assert "guest-get-osinfo" not in res["error"]
