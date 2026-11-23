"""One-shot download links (``proxmox_download_link``).

Mirror image of ``test_upload_links.py``: the fake SSH client serves "guest"
files from memory, so the pull path (``pct pull`` + SFTP for a CT, SFTP for a
VM), the size cap and the checksum run for real without a guest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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

from beaconmcp import audit, transfers  # noqa: E402
from beaconmcp.proxmox import system  # noqa: E402

NODE = "pve1"
CT = 201
STOPPED_CT = 202
VM = 300  # a QEMU VM reached over SSH
SRC = "/root/shot.png"
PAYLOAD = bytes(range(256)) * 40  # 10 KiB, every byte value


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeProxmox:
    def __init__(self, tmp_path: Path, max_mb: int = 1, public_url: str | None = "https://beacon.example") -> None:
        self._config = SimpleNamespace(
            server=SimpleNamespace(transfers_dir=tmp_path / "staging", transfers_max_mb=max_mb),
            features=SimpleNamespace(dashboard=SimpleNamespace(public_url=public_url)),
        )

    def get(self, node: str, path: str, **_: Any) -> dict[str, Any]:
        if path == f"nodes/{node}/lxc/{CT}/status/current":
            return {"status": "running"}
        if path == f"nodes/{node}/lxc/{STOPPED_CT}/status/current":
            return {"status": "stopped"}
        if path == f"nodes/{node}/qemu/{VM}/status/current":
            return {"status": "running"}
        return {"error": "not found"}


class FakeSSH:
    """Serves files out of ``guest_files`` (CT) and ``vm_files`` (VM)."""

    def __init__(self) -> None:
        self.guest_files: dict[str, bytes] = {SRC: PAYLOAD}
        self.vm_files: dict[str, bytes] = {SRC: PAYLOAD}
        self.node_files: dict[str, bytes] = {}
        self.pulls = 0  # data movements: pct pull + SFTP out of a VM
        self.sftp_gets = 0

    def resolve(self, host: str) -> str:
        if host != str(VM):
            raise KeyError(host)
        return "192.0.2.30"

    async def sftp_get(self, host: str, remote_path: str, local_path: str) -> None:
        self.sftp_gets += 1
        data = self.vm_files[remote_path] if host == str(VM) else self.node_files[remote_path]
        Path(local_path).write_bytes(data)

    async def sftp_remove(self, host: str, remote_path: str) -> None:
        self.node_files.pop(remote_path, None)

    async def exec_command(self, host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        argv = shlex.split(command)
        if argv[:2] == ["pct", "pull"]:
            self.pulls += 1
            self.node_files[argv[4]] = self.guest_files[argv[3]]
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if argv[:2] == ["pct", "exec"]:
            files, rest = self.guest_files, argv[4:]
        elif host == str(VM):
            files, rest = self.vm_files, argv
        else:
            return {"exit_code": 1, "stderr": f"unexpected command: {command}"}
        if rest[:3] == ["stat", "-c", "%s"]:
            if rest[3] not in files:
                return {"exit_code": 1, "stderr": "No such file"}
            return {"exit_code": 0, "stdout": f"{len(files[rest[3]])}\n"}
        if rest[0] == "sha256sum":
            return {"exit_code": 0, "stdout": f"{hashlib.sha256(files[rest[1]]).hexdigest()}  {rest[1]}\n"}
        return {"exit_code": 1, "stderr": f"unexpected command: {command}"}


def _app(trusted: tuple[str, ...] = ()) -> Starlette:
    return Starlette(routes=[
        Route(
            transfers.DOWNLOAD_PATH_PREFIX + "{token}",
            transfers.make_download_endpoint(trusted),
            methods=transfers.DOWNLOAD_METHODS,
        ),
    ])


@pytest.fixture
def make_env(tmp_path: Path):
    created: list[SimpleNamespace] = []

    def build(max_mb: int = 1) -> SimpleNamespace:
        transfers._download_store = transfers.DownloadLinkStore()
        mcp, proxmox, ssh = FakeMCP(), FakeProxmox(tmp_path, max_mb=max_mb), FakeSSH()
        system.register_system_tools(mcp, proxmox, ssh)
        app = _app()
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://beacon.example")
        env = SimpleNamespace(
            tools=mcp.tools, ssh=ssh, http=http, app=app,
            staging=tmp_path / "staging", proxmox=proxmox,
        )
        created.append(env)
        return env

    yield build
    transfers.register_pusher(None)


@pytest.fixture
def env(make_env):
    return make_env()


def _staged(env) -> list[str]:
    d = env.staging / transfers.DOWNLOAD_SUBDIR
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


async def _mint(env, **kw: Any) -> dict[str, Any]:
    args = {"node": NODE, "vmid": CT, "source": SRC, **kw}
    return await env.tools["proxmox_download_link"](**args)


def _token_of(res: dict[str, Any]) -> str:
    return res["url"].rsplit("/", 1)[1]


# --- link store --------------------------------------------------------------


def _issue(store, tmp_path: Path, **kw: Any):
    staged = tmp_path / f"dlink-{len(list(tmp_path.iterdir())):012x}"
    staged.write_bytes(b"x" * kw.pop("size", 10))
    args = dict(
        node=NODE, vmid=CT, source=SRC, filename="a.png", staged_path=staged,
        size=staged.stat().st_size, sha256=None, max_total_bytes=1 << 20,
    )
    args.update(kw)
    return store.issue(**args)


def test_claim_is_single_use(tmp_path: Path) -> None:
    store = transfers.DownloadLinkStore()
    link = _issue(store, tmp_path)
    assert store.claim(link.token) is link
    assert store.claim(link.token) is None


def test_expired_link_cannot_be_claimed_and_its_file_goes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = transfers.DownloadLinkStore()
    link = _issue(store, tmp_path, ttl=60)
    assert link.staged_path.exists()
    later = time.time() + 61
    monkeypatch.setattr(transfers.time, "time", lambda: later)
    assert store.claim(link.token) is None
    assert store.get(link.link_id).status == "expired"
    assert not link.staged_path.exists()


def test_sweep_removes_expired_files_without_any_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The timer armed at mint time calls sweep(): an unused link must not
    # keep its file past the TTL even if nobody ever touches the store again.
    store = transfers.DownloadLinkStore()
    link = _issue(store, tmp_path, ttl=60)
    assert link._timer is not None and link._timer.daemon
    link._timer.cancel()
    later = time.time() + 61
    monkeypatch.setattr(transfers.time, "time", lambda: later)
    store.sweep()
    assert not link.staged_path.exists()
    assert store.get(link.link_id).status == "expired"


def test_ttl_is_clamped(tmp_path: Path) -> None:
    store = transfers.DownloadLinkStore()
    link = _issue(store, tmp_path, ttl=10**9)
    assert link.expires_at - link.created_at <= transfers.MAX_TTL + 1
    link2 = _issue(store, tmp_path, ttl=1)
    assert link2.expires_at - link2.created_at >= transfers.MIN_TTL - 1


def test_pending_links_are_capped(tmp_path: Path) -> None:
    store = transfers.DownloadLinkStore()
    for _ in range(transfers.MAX_PENDING_LINKS):
        _issue(store, tmp_path)
    with pytest.raises(RuntimeError, match="pending download links"):
        _issue(store, tmp_path)
    with pytest.raises(RuntimeError, match="pending download links"):
        store.check_capacity()


def test_pending_bytes_are_capped(tmp_path: Path) -> None:
    store = transfers.DownloadLinkStore()
    _issue(store, tmp_path, size=600, max_total_bytes=1000)
    assert store.pending_bytes() == 600
    with pytest.raises(RuntimeError, match="staged"):
        _issue(store, tmp_path, size=600, max_total_bytes=1000)


def test_public_view_never_leaks_the_token(tmp_path: Path) -> None:
    link = _issue(transfers.DownloadLinkStore(), tmp_path)
    assert link.token not in repr(link.public())
    assert link.token not in repr(link)


# --- filename sanitizing ------------------------------------------------------


def test_safe_filename_strips_hostile_characters() -> None:
    name = transfers.safe_filename('a"\r\nX: y.png')
    assert name.isascii() and '"' not in name and "\r" not in name and "\n" not in name
    assert name.endswith("y.png")


@pytest.mark.parametrize("raw", ["", ".", "..", "/", "...", "\x00\x01"])
def test_safe_filename_never_empty_or_dots(raw: str) -> None:
    assert transfers.safe_filename(raw) == "download"


def test_safe_filename_drops_directories() -> None:
    assert transfers.safe_filename("../../etc/passwd") == "passwd"
    assert transfers.safe_filename("C:\\x\\shot.png") == "shot.png"


# --- proxmox_download_link + HTTP route ---------------------------------------


async def test_mint_then_get_returns_exact_bytes_once(env) -> None:
    res = await _mint(env)
    assert res["status"] == "success"
    assert res["url"].startswith("https://beacon.example/transfers/download/")
    assert res["size"] == len(PAYLOAD)
    assert res["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert res["filename"] == "shot.png"
    assert res["link_id"] and res["expires_at"]
    assert len(_staged(env)) == 1  # staged under the link, not the source name

    resp = await env.http.get(res["url"])
    assert resp.status_code == 200
    assert resp.content == PAYLOAD
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-length"] == str(len(PAYLOAD))
    assert resp.headers["content-disposition"].startswith('attachment; filename="shot.png"')
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"

    # Spent: a second request gets the same 404 as an unknown token.
    again = await env.http.get(res["url"])
    assert again.status_code == 404
    assert _staged(env) == []  # the staged copy is gone after the send

    status = await env.tools["proxmox_download_link"](link_id=res["link_id"])
    assert status["status"] == "success"
    assert status["result"]["bytes"] == len(PAYLOAD)


async def test_get_handles_a_multi_chunk_file(make_env) -> None:
    env = make_env(max_mb=4)
    big = b"\xab\xcd" * (1_300_000)  # 2.6 MB, several chunks
    env.ssh.guest_files[SRC] = big
    res = await _mint(env)
    resp = await env.http.get(res["url"])
    assert resp.status_code == 200
    assert resp.content == big
    assert resp.headers["content-length"] == str(len(big))


async def test_qemu_vm_download(env) -> None:
    res = await _mint(env, vmid=VM)
    assert res["status"] == "success"
    resp = await env.http.get(res["url"])
    assert resp.content == PAYLOAD


async def test_head_does_not_consume_the_token(env) -> None:
    res = await _mint(env)
    head = await env.http.head(res["url"])
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(PAYLOAD))
    assert head.headers["content-disposition"].startswith("attachment;")
    # Still pending, still downloadable.
    assert (await env.tools["proxmox_download_link"](link_id=res["link_id"]))["status"] == "pending"
    assert (await env.http.get(res["url"])).content == PAYLOAD
    # HEAD on a spent token is the same 404.
    assert (await env.http.head(res["url"])).status_code == 404


async def test_other_methods_are_405_and_keep_the_token(env) -> None:
    res = await _mint(env)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        resp = await env.http.request(method, res["url"], content=b"x")
        assert resp.status_code == 405, method
    assert (await env.http.get(res["url"])).content == PAYLOAD


async def test_unknown_token_is_404_without_detail(env) -> None:
    resp = await env.http.get(transfers.DOWNLOAD_PATH_PREFIX + "nope")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"
    assert "nope" not in resp.text
    unknown = resp.text
    # Spent and expired links answer with the very same body.
    res = await _mint(env)
    await env.http.get(res["url"])
    assert (await env.http.get(res["url"])).text == unknown


async def test_expired_token_is_404_and_the_staged_file_is_gone(env, monkeypatch: pytest.MonkeyPatch) -> None:
    res = await _mint(env, expires_in=60)
    assert len(_staged(env)) == 1
    later = time.time() + 61
    monkeypatch.setattr(transfers.time, "time", lambda: later)
    resp = await env.http.get(res["url"])
    assert resp.status_code == 404
    assert _staged(env) == []
    status = await env.tools["proxmox_download_link"](link_id=res["link_id"])
    assert status["status"] == "expired"


async def test_too_big_source_is_refused_before_any_transfer(env) -> None:
    env.ssh.guest_files[SRC] = b"x" * (2 * 1024 * 1024)  # cap is 1 MB
    res = await _mint(env)
    assert res["status"] == "error"
    assert "transfers_max_mb" in res["error"]
    assert env.ssh.pulls == 0 and env.ssh.sftp_gets == 0
    assert _staged(env) == []


async def test_too_big_vm_source_is_refused_before_any_transfer(env) -> None:
    env.ssh.vm_files[SRC] = b"x" * (2 * 1024 * 1024)
    res = await _mint(env, vmid=VM)
    assert res["status"] == "error"
    assert env.ssh.sftp_gets == 0
    assert _staged(env) == []


async def test_pending_link_cap_refuses_before_any_transfer(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfers, "MAX_PENDING_LINKS", 2)
    for _ in range(2):
        assert (await _mint(env))["status"] == "success"
    pulls = env.ssh.pulls
    res = await _mint(env)
    assert res["status"] == "error"
    assert "pending download links" in res["error"]
    assert env.ssh.pulls == pulls
    assert len(_staged(env)) == 2


async def test_cumulative_staged_bytes_are_capped(env) -> None:
    env.ssh.guest_files[SRC] = b"y" * 600_000  # cap is 1 MiB
    assert (await _mint(env))["status"] == "success"
    pulls = env.ssh.pulls
    res = await _mint(env)  # 600 KB more would exceed 1 MiB in total
    assert res["status"] == "error"
    assert "staged" in res["error"]
    assert env.ssh.pulls == pulls  # refused before moving the data
    assert len(_staged(env)) == 1


async def test_budget_comes_back_once_a_link_is_used(env) -> None:
    env.ssh.guest_files[SRC] = b"y" * 600_000
    first = await _mint(env)
    assert (await _mint(env))["status"] == "error"
    await env.http.get(first["url"])
    assert (await _mint(env))["status"] == "success"


async def test_status_by_link_id_has_no_token(env) -> None:
    res = await _mint(env)
    status = await env.tools["proxmox_download_link"](link_id=res["link_id"])
    assert status["status"] == "pending"
    assert status["link_id"] == res["link_id"]
    assert _token_of(res) not in json.dumps(status)
    unknown = await env.tools["proxmox_download_link"](link_id="nope")
    assert unknown["status"] == "error"


async def test_hostile_filename_is_sanitized_in_the_header(env) -> None:
    hostile = '/tmp/a"\r\nX: y.png'
    env.ssh.guest_files[hostile] = PAYLOAD
    res = await _mint(env, source=hostile)
    assert res["status"] == "success"
    assert '"' not in res["filename"] and "\r" not in res["filename"] and "\n" not in res["filename"]
    resp = await env.http.get(res["url"])
    assert resp.status_code == 200
    disp = resp.headers["content-disposition"]
    assert "\r" not in disp and "\n" not in disp
    assert "x" not in {k.lower() for k in resp.headers}  # no injected header
    assert disp.count('"') == 2
    assert disp.startswith("attachment; filename=")


async def test_mint_validates_arguments(env) -> None:
    assert (await _mint(env, source="relative.png"))["status"] == "error"
    assert (await env.tools["proxmox_download_link"](node="", vmid=0, source=SRC))["status"] == "error"
    assert (await _mint(env, vmid=999))["status"] == "error"
    stopped = await _mint(env, vmid=STOPPED_CT)
    assert stopped["status"] == "error" and "stopped" in stopped["error"]
    missing = await _mint(env, source="/nope")
    assert missing["status"] == "error"
    assert _staged(env) == []


async def test_mint_needs_ssh(tmp_path: Path) -> None:
    transfers._download_store = transfers.DownloadLinkStore()
    mcp = FakeMCP()
    system.register_system_tools(mcp, FakeProxmox(tmp_path), None)
    res = await mcp.tools["proxmox_download_link"](node=NODE, vmid=CT, source=SRC)
    assert res["status"] == "error" and "SSH" in res["error"]


async def test_mint_falls_back_to_a_relative_url_with_a_warning(tmp_path: Path) -> None:
    transfers._download_store = transfers.DownloadLinkStore()
    mcp, ssh = FakeMCP(), FakeSSH()
    system.register_system_tools(mcp, FakeProxmox(tmp_path, public_url=None), ssh)
    res = await mcp.tools["proxmox_download_link"](node=NODE, vmid=CT, source=SRC)
    assert res["url"].startswith("/transfers/download/")
    assert "Public URL unknown" in res["warning"]


async def test_checksum_can_be_skipped(env) -> None:
    res = await _mint(env, verify_checksum=False)
    assert res["status"] == "success"
    assert res["sha256"] is None
    assert (await env.http.get(res["url"])).content == PAYLOAD


async def test_interrupted_send_spends_the_link_and_cleans_up(make_env) -> None:
    env = make_env(max_mb=4)
    env.ssh.guest_files[SRC] = b"z" * 3_000_000
    res = await _mint(env)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)  # nothing more from the client; no busy loop
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and len(sent) > 2:
            raise OSError("client went away")

    scope = {
        "type": "http", "method": "GET", "path": "/transfers/download/" + _token_of(res),
        "raw_path": b"", "query_string": b"", "headers": [], "scheme": "https",
        "server": ("beacon.example", 443), "client": ("203.0.113.9", 4444),
        "http_version": "1.1", "root_path": "",
    }
    try:  # a vanished client ends the ASGI call one way or another
        await env.app(scope, receive, send)
    except Exception:  # noqa: BLE001
        pass

    status = await env.tools["proxmox_download_link"](link_id=res["link_id"])
    assert status["status"] == "error"
    assert _staged(env) == []
    assert (await env.http.get(res["url"])).status_code == 404


async def test_audit_event_has_no_token(env, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(audit, "emit", lambda event, **kw: events.append((event, kw)))
    res = await _mint(env)
    await env.http.get(res["url"])
    dl = [kw for ev, kw in events if ev == "transfer.download"]
    assert len(dl) == 1
    assert dl[0]["status"] == "success" and dl[0]["bytes"] == len(PAYLOAD)
    assert dl[0]["link_id"] == res["link_id"] and dl[0]["source"] == SRC
    assert _token_of(res) not in json.dumps(events, default=str)


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_startup_sweep_removes_only_old_orphans_and_spares_live_files(tmp_path: Path) -> None:
    # check-startup imports the server module (and so runs this sweep) next to
    # a live server: a recent file may belong to a pending link and must live.
    d = tmp_path / transfers.DOWNLOAD_SUBDIR
    d.mkdir()
    old = d / "dlink-0123456789ab"
    old_part = d / "dlink-0123456789ac.part"
    fresh = d / "dlink-0123456789ad"
    fresh_part = d / "dlink-0123456789ae.part"
    odd = d / "dlink-notes.txt"  # not our naming: never touched
    for f in (old, old_part, fresh, fresh_part, odd):
        f.write_bytes(b"x")
    for f in (old, old_part, odd):
        _age(f, transfers.ORPHAN_AGE + 60)
    assert transfers.sweep_orphan_downloads(tmp_path) == 2
    assert sorted(p.name for p in d.iterdir()) == sorted([fresh.name, fresh_part.name, odd.name])
    assert transfers.sweep_orphan_downloads(tmp_path / "missing") == 0


async def test_registering_the_tools_spares_a_live_staged_file(tmp_path: Path) -> None:
    d = tmp_path / "staging" / transfers.DOWNLOAD_SUBDIR
    d.mkdir(parents=True)
    live, stale = d / "dlink-0123456789ab", d / "dlink-0123456789ac"
    live.write_bytes(b"x")
    stale.write_bytes(b"x")
    _age(stale, transfers.ORPHAN_AGE + 60)
    system.register_system_tools(FakeMCP(), FakeProxmox(tmp_path), FakeSSH())
    assert live.exists() and not stale.exists()


async def test_staged_files_live_out_of_reach_of_the_transit_tools(env) -> None:
    res = await _mint(env)
    name = _staged(env)[0]
    assert (env.staging / transfers.DOWNLOAD_SUBDIR / name).is_file()
    listed = env.tools["proxmox_list_transfers"]()
    assert listed["files"] == []  # the sub folder is not a transit file
    gone = env.tools["proxmox_delete_transfer"](name=name)
    assert gone["status"] == "error"
    # A caller cannot overwrite the sub folder or a staged name through dest.
    clobber = await env.tools["proxmox_download_file"](
        node=NODE, vmid=CT, source=SRC, dest=transfers.DOWNLOAD_SUBDIR, overwrite=True,
    )
    assert clobber["status"] == "error"
    assert (await env.http.get(res["url"])).content == PAYLOAD


async def test_download_file_still_works_after_the_refactor(env) -> None:
    res = await env.tools["proxmox_download_file"](node=NODE, vmid=CT, source=SRC, dest="shot-copy.png")
    assert res["status"] == "success"
    assert (env.staging / "shot-copy.png").read_bytes() == PAYLOAD
    assert res["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert res["checksum_verified"] is True
