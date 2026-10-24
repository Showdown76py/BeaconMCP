"""One-shot upload links (``proxmox_upload_link``) and ``proxmox_fetch_url``.

The Proxmox and SSH clients are fakes: the fake SSH client keeps what was
"pushed" in memory and answers ``sha256sum`` from it, so the checksum path
runs for real without a guest.
"""

from __future__ import annotations

import hashlib
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp import transfers  # noqa: E402
from beaconmcp.proxmox import system  # noqa: E402

NODE = "pve1"
CT = 201
STOPPED_CT = 202
VM = 300  # a QEMU VM reached over SSH


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeProxmox:
    """Knows a running LXC container, ``CT``, and a stopped one, ``STOPPED_CT``."""

    def __init__(self, tmp_path: Path, max_mb: int = 1, public_url: str | None = "https://beacon.example") -> None:
        self._config = SimpleNamespace(
            server=SimpleNamespace(transfers_dir=tmp_path / "staging", transfers_max_mb=max_mb),
            features=SimpleNamespace(dashboard=SimpleNamespace(public_url=public_url)),
        )

        self.stopped = {STOPPED_CT}

    def get(self, node: str, path: str, **_: Any) -> dict[str, Any]:
        for vmid in (CT, STOPPED_CT):
            if path == f"nodes/{node}/lxc/{vmid}/status/current":
                return {"status": "stopped" if vmid in self.stopped else "running"}
        if path == f"nodes/{node}/qemu/{VM}/status/current":
            return {"status": "running"}
        return {"error": "not found"}


class FakeSSH:
    """Records node uploads and ``pct push`` targets, and SFTP into ``VM``."""

    def __init__(self) -> None:
        self.node_files: dict[str, bytes] = {}
        self.guest_files: dict[str, bytes] = {}
        self.vm_files: dict[str, bytes] = {}
        self.vm_dirs: set[str] = {"/opt/app"}
        self.sha256sum_exit: int | None = None  # force a sha256sum exit code

    def resolve(self, host: str) -> str:
        if host != str(VM):
            raise KeyError(host)
        return "192.0.2.30"

    async def sftp_put(self, host: str, local_path: str, remote_path: str) -> None:
        data = Path(local_path).read_bytes()
        if host == str(VM):
            self.vm_files[remote_path] = data
        else:
            self.node_files[remote_path] = data

    async def sftp_remove(self, host: str, remote_path: str) -> None:
        self.node_files.pop(remote_path, None)

    async def exec_command(self, host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        argv = shlex.split(command)
        if argv[:2] == ["pct", "push"]:
            self.guest_files[argv[4]] = self.node_files[argv[3]]
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if argv[:2] == ["pct", "exec"] and argv[4] == "sha256sum":
            if self.sha256sum_exit is not None:
                return {"exit_code": self.sha256sum_exit, "stdout": "", "stderr": "sha256sum: failed"}
            data = self.guest_files[argv[5]]
            return {"exit_code": 0, "stdout": f"{hashlib.sha256(data).hexdigest()}  {argv[5]}\n"}
        if host == str(VM) and argv[:2] == ["test", "-d"]:
            return {"exit_code": 0 if argv[2] in self.vm_dirs else 1, "stdout": ""}
        if host == str(VM) and argv[0] == "sha256sum":
            data = self.vm_files[argv[1]]
            return {"exit_code": 0, "stdout": f"{hashlib.sha256(data).hexdigest()}  {argv[1]}\n"}
        return {"exit_code": 1, "stderr": f"unexpected command: {command}"}


@pytest.fixture
def env(tmp_path: Path):
    transfers._store = transfers.UploadLinkStore()
    mcp, proxmox, ssh = FakeMCP(), FakeProxmox(tmp_path), FakeSSH()
    system.register_system_tools(mcp, proxmox, ssh)
    app = Starlette(routes=[
        Route(
            transfers.UPLOAD_PATH_PREFIX + "{token}",
            transfers.make_upload_endpoint(),
            methods=["PUT", "POST"],
        ),
    ])
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://beacon.example")
    yield SimpleNamespace(
        tools=mcp.tools, ssh=ssh, http=http, staging=tmp_path / "staging", proxmox=proxmox,
    )
    transfers.register_pusher(None)


async def _mint(env, **kw: Any) -> dict[str, Any]:
    args = {"node": NODE, "vmid": CT, "dest": "/root/blob.bin", **kw}
    return await env.tools["proxmox_upload_link"](**args)


# --- link store --------------------------------------------------------------


def test_claim_is_single_use(tmp_path: Path) -> None:
    store = transfers.UploadLinkStore()
    link = store.issue(node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path)
    assert store.claim(link.token) is link
    assert store.claim(link.token) is None


def test_expired_link_cannot_be_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = transfers.UploadLinkStore()
    link = store.issue(node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path, ttl=60)
    later = time.time() + 61
    monkeypatch.setattr(transfers.time, "time", lambda: later)
    assert store.claim(link.token) is None
    assert store.get(link.link_id).status == "expired"


def test_ttl_is_clamped(tmp_path: Path) -> None:
    store = transfers.UploadLinkStore()
    link = store.issue(node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path, ttl=10**9)
    assert link.expires_at - link.created_at <= transfers.MAX_TTL + 1


def test_pending_links_are_capped(tmp_path: Path) -> None:
    store = transfers.UploadLinkStore()
    for _ in range(transfers.MAX_PENDING_LINKS):
        store.issue(node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path)
    with pytest.raises(RuntimeError):
        store.issue(node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path)


def test_public_view_never_leaks_the_token(tmp_path: Path) -> None:
    link = transfers.UploadLinkStore().issue(
        node=NODE, vmid=CT, dest="/x", max_bytes=10, staging_dir=tmp_path,
    )
    assert link.token not in repr(link.public())


# --- proxmox_upload_link + HTTP route ---------------------------------------


async def test_mint_returns_absolute_url(env) -> None:
    res = await _mint(env)
    assert res["status"] == "success"
    assert res["url"].startswith("https://beacon.example/transfers/upload/")
    assert res["vm_type"] == "lxc"


async def test_mint_falls_back_to_request_base(tmp_path: Path) -> None:
    mcp = FakeMCP()
    system.register_system_tools(mcp, FakeProxmox(tmp_path, public_url=None), FakeSSH())
    var = transfers.current_public_base.set("https://seen.example")
    try:
        res = await mcp.tools["proxmox_upload_link"](node=NODE, vmid=CT, dest="/x")
    finally:
        transfers.current_public_base.reset(var)
        transfers.register_pusher(None)
    assert res["url"].startswith("https://seen.example/transfers/upload/")


async def test_mint_rejects_relative_dest_and_unknown_guest(env) -> None:
    assert (await _mint(env, dest="root/x"))["status"] == "error"
    assert (await _mint(env, vmid=999))["status"] == "error"


async def test_upload_lands_in_guest_and_link_is_spent(env) -> None:
    payload = bytes(range(256)) * 100  # binary, not valid UTF-8
    link = await _mint(env)
    resp = await env.http.put(link["url"], content=payload)
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["status"] == "success"
    assert body["checksum_verified"] is True
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert env.ssh.guest_files["/root/blob.bin"] == payload
    # Nothing left behind in the staging dir or on the node.
    assert list(env.staging.iterdir()) == []
    assert env.ssh.node_files == {}
    # Single use, and the outcome is readable afterwards.
    assert (await env.http.put(link["url"], content=b"again")).status_code == 404
    status = await env.tools["proxmox_upload_link"](link_id=link["link_id"])
    assert status["status"] == "success"
    assert status["result"]["bytes"] == len(payload)


async def test_upload_over_cap_is_refused(env) -> None:
    link = await _mint(env)
    resp = await env.http.put(link["url"], content=b"x" * (1024 * 1024 + 1))
    assert resp.status_code == 413
    assert env.ssh.guest_files == {}
    assert list(env.staging.iterdir()) == []


async def test_multipart_upload_is_refused(env) -> None:
    link = await _mint(env)
    resp = await env.http.post(link["url"], files={"f": ("a.txt", b"hi")})
    assert resp.status_code == 415


async def test_mint_refuses_a_stopped_guest(env) -> None:
    res = await _mint(env, vmid=STOPPED_CT)
    assert res["status"] == "error"
    assert "stopped" in res["error"]


async def test_guest_stopped_after_mint_is_409(env) -> None:
    link = await _mint(env)
    env.proxmox.stopped.add(CT)
    resp = await env.http.put(link["url"], content=b"data")
    assert resp.status_code == 409
    assert "stopped" in resp.json()["error"]
    assert env.ssh.guest_files == {} and env.ssh.node_files == {}
    assert list(env.staging.iterdir()) == []


async def test_failed_push_is_500_not_502(env, monkeypatch: pytest.MonkeyPatch) -> None:
    # A 502 from the origin is replaced by Cloudflare's own error page,
    # which would hide the reason from the uploader.
    async def failing_exec(host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        return {"exit_code": 1, "stdout": "", "stderr": "no such directory"}

    monkeypatch.setattr(env.ssh, "exec_command", failing_exec)
    link = await _mint(env)
    resp = await env.http.put(link["url"], content=b"data")
    assert resp.status_code == 500
    assert "no such directory" in resp.json()["error"]


async def test_node_copy_is_removed_after_a_failed_sftp(env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def partial_put(host: str, local_path: str, remote_path: str) -> None:
        env.ssh.node_files[remote_path] = b"partial"
        raise OSError("No space left on device")

    monkeypatch.setattr(env.ssh, "sftp_put", partial_put)
    link = await _mint(env)
    resp = await env.http.put(link["url"], content=b"data")
    assert resp.status_code == 500
    assert env.ssh.node_files == {}


async def test_cancelled_upload_cleans_up_and_settles_the_link(env, monkeypatch: pytest.MonkeyPatch) -> None:
    # A restart cancels in-flight requests (uvicorn's graceful-shutdown cap).
    import asyncio

    started = asyncio.Event()

    async def hanging_exec(host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(3600)
        return {}

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(transfers.audit, "emit", lambda event, **kw: events.append({"event": event, **kw}))
    monkeypatch.setattr(env.ssh, "exec_command", hanging_exec)
    link = await _mint(env)
    task = asyncio.create_task(env.http.put(link["url"], content=b"data"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # let the shielded cleanup run

    assert env.ssh.node_files == {}
    assert list(env.staging.iterdir()) == []
    status = await env.tools["proxmox_upload_link"](link_id=link["link_id"])
    assert status["status"] == "error"
    assert [e["event"] for e in events] == ["transfer.upload"]


@pytest.mark.parametrize("code, ok", [(127, True), (1, False)])
async def test_checksum_failure_is_not_reported_as_unverified_success(env, code: int, ok: bool) -> None:
    env.ssh.sha256sum_exit = code
    link = await _mint(env)
    resp = await env.http.put(link["url"], content=b"data")
    body = resp.json()
    if ok:  # sha256sum missing: success, flagged as unverified
        assert body["status"] == "success" and body["checksum_verified"] is False
    else:  # anything else: the file is not where asked
        assert body["status"] == "error" and "Checksum check" in body["error"]


async def test_qemu_push_lands_at_dest(env) -> None:
    link = await _mint(env, vmid=VM, dest="/opt/app/tool.bin")
    resp = await env.http.put(link["url"], content=b"bin")
    assert resp.json()["status"] == "success", resp.json()
    assert env.ssh.vm_files == {"/opt/app/tool.bin": b"bin"}


async def test_qemu_push_refuses_a_directory_dest(env) -> None:
    link = await _mint(env, vmid=VM, dest="/opt/app")
    resp = await env.http.put(link["url"], content=b"bin")
    assert resp.json()["status"] == "error"
    assert "directory" in resp.json()["error"]
    assert env.ssh.vm_files == {}


async def test_loopback_request_base_is_not_used_for_links(tmp_path: Path) -> None:
    # The dashboard chat reaches the MCP endpoint over 127.0.0.1.
    mcp = FakeMCP()
    system.register_system_tools(mcp, FakeProxmox(tmp_path, public_url=None), FakeSSH())
    var = transfers.current_public_base.set("http://127.0.0.1:8420")
    try:
        res = await mcp.tools["proxmox_upload_link"](node=NODE, vmid=CT, dest="/x")
    finally:
        transfers.current_public_base.reset(var)
        transfers.register_pusher(None)
    assert res["url"].startswith(transfers.UPLOAD_PATH_PREFIX)
    assert "warning" in res


async def test_unknown_token_is_404(env) -> None:
    resp = await env.http.put(transfers.UPLOAD_PATH_PREFIX + "nope", content=b"x")
    assert resp.status_code == 404


# --- proxmox_fetch_url ------------------------------------------------------


_DNS = {
    "files.example": ["93.184.216.34"],
    "localhost": ["127.0.0.1", "::1"],
    "metadata.example": ["169.254.169.254"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "lan.example": ["192.168.1.50"],
}


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch):
    files = {
        "https://files.example/tool.bin": b"\x00\xffbinary\x00",
        "http://lan.example/f": b"lan",
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == "https://files.example/to-metadata":
            return httpx.Response(302, headers={"location": "http://metadata.example/latest"})
        data = files.get(str(request.url))
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)

    async def fake_dns(host: str) -> list[str]:
        if host not in _DNS:
            raise OSError("unknown host")
        return _DNS[host]

    real = httpx.AsyncClient
    monkeypatch.setattr(
        system.httpx, "AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(system, "_host_addresses", fake_dns)
    return SimpleNamespace(files=files, requested=requested)


async def test_fetch_url_pushes_into_guest(env, remote) -> None:
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=CT, url="https://files.example/tool.bin", dest="/usr/local/bin/tool",
    )
    assert res["status"] == "success", res
    assert env.ssh.guest_files["/usr/local/bin/tool"] == remote.files["https://files.example/tool.bin"]
    assert list(env.staging.iterdir()) == []


async def test_fetch_url_reports_http_errors(env, remote) -> None:
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=CT, url="https://files.example/missing", dest="/x",
    )
    assert res["status"] == "error"
    assert "404" in res["error"]


async def test_fetch_url_rejects_other_schemes(env) -> None:
    res = await env.tools["proxmox_fetch_url"](node=NODE, vmid=CT, url="file:///etc/passwd", dest="/x")
    assert res["status"] == "error"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8420/metrics",
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://mapped.example/x",
        "http://metadata.example/latest",
        # A public URL that redirects to a link-local one.
        "https://files.example/to-metadata",
    ],
)
async def test_fetch_url_refuses_local_targets(env, remote, url: str) -> None:
    res = await env.tools["proxmox_fetch_url"](node=NODE, vmid=CT, url=url, dest="/x")
    assert res["status"] == "error"
    assert "not allowed" in res["error"]
    assert env.ssh.guest_files == {}
    # Refused before any byte left for a local target.
    reached = {httpx.URL(u).host for u in remote.requested}
    assert reached <= {"files.example"}


async def test_fetch_url_allows_private_lan(env, remote) -> None:
    res = await env.tools["proxmox_fetch_url"](node=NODE, vmid=CT, url="http://lan.example/f", dest="/x")
    assert res["status"] == "success", res


async def test_fetch_url_refuses_a_stopped_guest_before_downloading(env, remote) -> None:
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=STOPPED_CT, url="https://files.example/tool.bin", dest="/x",
    )
    assert res["status"] == "error"
    assert "stopped" in res["error"]
    assert remote.requested == []


async def test_fetch_url_checks_target_before_downloading(env, remote) -> None:
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=999, url="https://files.example/tool.bin", dest="/x",
    )
    assert res["status"] == "error"
    assert "999" in res["error"]
    assert remote.requested == []


# --- the connect-time guard (real sockets) -----------------------------------


@pytest.fixture
def loopback_server():
    """A real HTTP server on 127.0.0.1 holding a "secret" only reachable locally."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"LOOPBACK-ONLY-SECRET " + self.headers.get("Host", "").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _dns_sequence(monkeypatch: pytest.MonkeyPatch, answers: list) -> None:
    """Each lookup of a ``.test`` name pops the next answer (an exception is
    raised). Patched at socket.getaddrinfo, so the guard's lookups and the
    connect's own lookup (if any) both see the sequence."""
    import socket

    real = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)  # anyio passes IDNA bytes
        if not name.endswith(".test"):
            return real(host, port, *args, **kwargs)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise socket.gaierror(str(answer))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in answer
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "answers",
    [
        # DNS rebinding: public for the early check, loopback for the connect.
        [["93.184.216.34"], ["127.0.0.1"]],
        # Lookup error first: the early check lets it through, the connect
        # must not.
        [OSError("SERVFAIL"), ["127.0.0.1"]],
    ],
)
async def test_fetch_url_connects_only_to_the_checked_address(
    env, loopback_server, monkeypatch: pytest.MonkeyPatch, answers,
) -> None:
    _dns_sequence(monkeypatch, answers)
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=CT, url=f"http://rebind.test:{loopback_server}/", dest="/x",
    )
    assert res["status"] == "error", res
    assert env.ssh.guest_files == {}


async def test_fetch_url_unresolvable_host_fails_closed(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _dns_sequence(monkeypatch, [OSError("NXDOMAIN")])
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=CT, url="http://nowhere.test/", dest="/x",
    )
    assert res["status"] == "error"
    assert env.ssh.guest_files == {}


async def test_guarded_connect_keeps_the_hostname(
    env, loopback_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend 127.0.0.1 is a public address to exercise the success path:
    # the connection goes to the resolved IP, the Host header keeps the name.
    _dns_sequence(monkeypatch, [["127.0.0.1"]])
    monkeypatch.setattr(system, "_is_local_address", lambda addr: False)
    res = await env.tools["proxmox_fetch_url"](
        node=NODE, vmid=CT, url=f"http://files.test:{loopback_server}/f", dest="/f",
    )
    assert res["status"] == "success", res
    assert env.ssh.guest_files["/f"] == f"LOOPBACK-ONLY-SECRET files.test:{loopback_server}".encode()


async def test_fetch_tries_each_checked_address(monkeypatch) -> None:
    # An unreachable first answer (an AAAA on an IPv4-only box) must not end
    # the fetch: every address was checked, so the next one is still safe.
    import httpcore

    async def fake_dns(host):
        return ["2001:db8::1", "198.51.100.7"]

    monkeypatch.setattr(system, "_host_addresses", fake_dns)
    backend = system._GuardedNetworkBackend()
    tried: list[str] = []

    async def connect(addr, port, **kw):
        tried.append(addr)
        if addr == "2001:db8::1":
            raise httpcore.ConnectError("network unreachable")
        return "stream"

    monkeypatch.setattr(backend._inner, "connect_tcp", connect)
    assert await backend.connect_tcp("example.com", 443) == "stream"
    assert tried == ["2001:db8::1", "198.51.100.7"]
