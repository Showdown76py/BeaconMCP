from __future__ import annotations

import base64
import asyncio
import hashlib
import ipaddress
import socket
import time
import uuid
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from mcp.server.mcpserver import MCPServer

from .. import transfers
from .client import ProxmoxClient


@dataclass
class ExecSession:
    exec_id: str
    node: str
    vmid: int
    vm_type: str
    command: str
    status: str = "running"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    pid: int | None = None
    started_at: float = field(default_factory=time.time)


_exec_sessions: dict[str, ExecSession] = {}
_EXEC_SESSION_TTL = 3600


def _prune_exec_sessions() -> None:
    # Runs on executor threads while other threads add sessions: snapshot the
    # items first, or the dict can change size under the iteration.
    now = time.time()
    stale = [
        eid
        for eid, s in list(_exec_sessions.items())
        if s.status != "running" and now - s.started_at > _EXEC_SESSION_TTL
    ]
    for eid in stale:
        _exec_sessions.pop(eid, None)


def _detect_guest(client: ProxmoxClient, node: str, vmid: int) -> tuple[str, str] | None:
    """``(vm_type, status)`` of a guest, e.g. ``("lxc", "running")``."""
    for vm_type in ("qemu", "lxc"):
        data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
        if isinstance(data, dict) and "error" in data:
            continue
        if isinstance(data, dict) and data.get("status"):
            return vm_type, str(data["status"])
    return None


def _detect_vm_type(client: ProxmoxClient, node: str, vmid: int) -> str | None:
    guest = _detect_guest(client, node, vmid)
    return guest[0] if guest else None


async def _running_guest(
    client: ProxmoxClient, node: str, vmid: int,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a push target: ``(vm_type, None)`` or ``(None, error result)``.

    Files can only be pushed into a running guest (``pct push`` refuses a
    stopped CT, a stopped VM has no SSH), so check it up front rather than
    after the bytes were transferred.
    """
    loop = asyncio.get_running_loop()
    guest = await loop.run_in_executor(None, _detect_guest, client, node, vmid)
    if guest is None:
        return None, {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
    vm_type, status = guest
    if status != "running":
        return None, {
            "status": "error",
            "error": f"VM/CT {vmid} is {status}: start it first, files can only be pushed into a running guest.",
            "guest_status": status,
        }
    return vm_type, None


#: Added to an agent/exec error when the guest OS could not be detected, so a
#: Windows guest whose agent blocks guest-get-osinfo is not left guessing.
_OS_UNKNOWN_HINT = (
    "The guest agent did not report its OS (guest-get-osinfo failed or is "
    "blocked), so the command was run through /bin/sh -c. On a Windows guest, "
    "allow guest-get-osinfo in the agent so cmd.exe /c is used instead."
)


def _guest_argv(
    client: ProxmoxClient, node: str, vmid: int, command: str,
) -> tuple[list[str], str | None]:
    """Wrap ``command`` in the guest's shell for the QEMU guest agent.

    ``agent/exec`` takes an argv and runs it without a shell, so pipes,
    redirections, ``&&`` and quoting would otherwise reach the program as
    literal arguments. LXC commands already go through ``sh -c``; this gives
    QEMU guests the same semantics. Windows guests have no ``/bin/sh`` and get
    ``cmd.exe /c`` instead. When the agent can't report its OS (old agent,
    command blocked), assume a POSIX guest, which is by far the common case,
    and return a hint to attach to an exec error (``None`` when the OS is known).

    On Windows the agent joins the argv into one command line with C runtime
    quoting (``"`` becomes ``\\"``), which cmd.exe does not understand: a
    command that itself contains double quotes can reach cmd.exe mangled.
    Commands without inner double quotes are unaffected.
    """
    info = client.get(node, f"nodes/{node}/qemu/{vmid}/agent/get-osinfo")
    result = info.get("result") if isinstance(info, dict) else None
    if not isinstance(result, dict) or not result.get("id"):
        return ["/bin/sh", "-c", command], _OS_UNKNOWN_HINT
    if result.get("id") == "mswindows":
        return ["cmd.exe", "/c", command], None
    return ["/bin/sh", "-c", command], None


def _staging_dir(client: ProxmoxClient) -> Path:
    """Return the resolved staging directory, creating it on first use."""
    base = Path(client._config.server.transfers_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _staging_path(client: ProxmoxClient, name: str) -> Path:
    """Resolve ``name`` against the staging dir, refusing path traversal.

    Only plain basenames are accepted: no slashes, no ``..``, no absolute
    paths. The resolved target must remain inside the staging directory.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("staging filename must be a non-empty basename")
    if "/" in name or "\\" in name or name.startswith(".."):
        raise ValueError(
            "staging filename must be a plain basename (no slashes, no '..')"
        )
    base = _staging_dir(client)
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(
            f"staging filename {name!r} resolves outside the transfers directory"
        ) from exc
    return target


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Hard cap for the guest-agent file-read path (matches proxmox_write_file).
_FILE_READ_MAX_BYTES = 1024 * 1024
#: PVE caps agent/file-write ``content`` at 60 KiB, and the content goes
#: base64-encoded (4/3 larger): 45 KiB of raw bytes is what fits.
_FILE_WRITE_MAX_BYTES = 45 * 1024


def _agent_read_bytes(
    client: ProxmoxClient, node: str, vmid: int, path: str,
) -> bytes | dict[str, Any]:
    """Raw bytes of a guest file via agent/file-read, or an error result.

    Reads one byte past the limit so an oversized file is told apart from
    one exactly at it. ``decode=0`` keeps the guest agent's base64 as is:
    PVE's default decodes it server-side and the bytes then come back as
    JSON text, which isn't base64 any more.
    """
    api = f"nodes/{node}/qemu/{vmid}/agent/file-read"
    count = _FILE_READ_MAX_BYTES + 1
    result = client.get(node, api, file=path, count=count, decode=0)
    legacy = False
    if isinstance(result, dict) and "error" in result and any(
        name in str(result["error"]) for name in ("decode", "count")
    ):
        # PVE before the `decode` parameter: content arrives decoded, one
        # JSON code point per byte. `count` may be just as new, so it is left
        # out too; the caller's length check still enforces the limit.
        result = client.get(node, api, file=path)
        legacy = True
    if isinstance(result, dict) and "error" in result:
        return {"status": "error", "error": result["error"]}
    if not isinstance(result, dict) or not isinstance(result.get("content"), str):
        return {"status": "error", "error": f"Unexpected file-read response: {result!r}"[:300]}
    try:
        if legacy:
            return result["content"].encode("latin-1")
        return base64.b64decode(result["content"], validate=True)
    except (ValueError, UnicodeEncodeError) as e:
        return {"status": "error", "error": f"Failed to decode file content: {e}"}


def _qemu_file_size(client: ProxmoxClient, node: str, vmid: int, path: str) -> int | None:
    """Best-effort size (bytes) of ``path`` inside a QEMU VM via the guest agent.

    Returns ``None`` when the size can't be determined (no ``stat`` in the
    guest, agent error, slow exec). Callers use this to reject oversized
    reads *before* pulling the whole file into memory; ``None`` means
    "couldn't check" so the caller falls back to its post-read guard rather
    than blocking a legitimate read.
    """
    start = client.post(
        node, f"nodes/{node}/qemu/{vmid}/agent/exec",
        command=["stat", "-c", "%s", path],
    )
    if not isinstance(start, dict) or start.get("pid") is None:
        return None
    pid = start["pid"]
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get(
            node, f"nodes/{node}/qemu/{vmid}/agent/exec-status", pid=pid,
        )
        if not isinstance(status, dict):
            return None
        if status.get("exited"):
            if status.get("exitcode") != 0:
                return None
            out = status.get("out-data", "")
            if status.get("out-data-encoding") == "base64" and out:
                out = base64.b64decode(out).decode("utf-8", errors="replace")
            try:
                return int(str(out).strip())
            except (ValueError, AttributeError):
                return None
        time.sleep(0.5)
    return None


class _LocalTargetRefused(Exception):
    """A proxmox_fetch_url hop pointed at the BeaconMCP host or link-local."""


def _is_local_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


async def _host_addresses(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _refuse_local_target(request: httpx.Request) -> None:
    """httpx request hook: refuse loopback / link-local targets on every hop.

    Keeps proxmox_fetch_url from reading services that only listen on the
    BeaconMCP host (its own /metrics, the Proxmox API on 127.0.0.1) or a
    cloud metadata endpoint into a guest. This early check gives a clear
    error before any connection; the one that holds is
    :class:`_GuardedNetworkBackend`, which connects to the very address it
    checked (a DNS answer that changes between two lookups can't slip a
    loopback address past it).
    """
    host = request.url.host
    try:
        ipaddress.ip_address(host)
        addrs = [host]  # IP literal, nothing to resolve
    except ValueError:
        try:
            addrs = await _host_addresses(host)
        except OSError:
            return  # unresolvable: the request fails on its own
    local = next((a for a in addrs if _is_local_address(a)), None)
    if local is not None:
        raise _LocalTargetRefused(
            f"Refusing to fetch from {host} ({local}): loopback and link-local "
            "addresses are not allowed."
        )


class _GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, refuse local addresses, connect to the checked IP.

    TLS still verifies against the URL's hostname: httpcore takes the SNI
    and certificate name from the request origin, not from this address.
    Resolution failures fail closed.
    """

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            ipaddress.ip_address(host)
            addrs = [host]
        except ValueError:
            try:
                addrs = await _host_addresses(host)
            except OSError as e:
                raise httpcore.ConnectError(f"cannot resolve {host}: {e}") from e
        if not addrs:
            raise httpcore.ConnectError(f"cannot resolve {host}")
        local = next((a for a in addrs if _is_local_address(a)), None)
        if local is not None:
            raise _LocalTargetRefused(
                f"Refusing to fetch from {host} ({local}): loopback and link-local "
                "addresses are not allowed."
            )
        # Every address was checked above, so trying them in turn keeps the
        # guarantee: an unreachable AAAA on an IPv4-only host must not end it.
        last: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    addr, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        raise httpcore.ConnectError(f"cannot connect to {host}: {last}") from last

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _fetch_transport() -> httpx.AsyncBaseTransport:
    """Transport for proxmox_fetch_url: every connection goes through the guard.

    Passing a transport also turns off HTTP(S)_PROXY from the environment:
    a proxy would resolve the name itself, out of the guard's reach.
    """
    transport = httpx.AsyncHTTPTransport()
    # httpx has no public hook for the network backend; `_pool` is the pool
    # every request goes through (checked against httpx 0.28.1 / httpcore
    # 1.0.9, and pinned by test_upload_links' rebinding test).
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        network_backend=_GuardedNetworkBackend(),
    )
    return transport


async def _remove_quietly(ssh_client: Any, host: str, path: str) -> None:
    """Best-effort cleanup that also runs when the caller is being cancelled
    (a restart cancels in-flight requests after a few seconds)."""
    try:
        await asyncio.shield(ssh_client.sftp_remove(host, path))
    except (Exception, asyncio.CancelledError):
        pass


def _checksum_from(sum_res: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(sha, error)`` from a ``sha256sum <dest>`` run in the guest.

    Only a missing ``sha256sum`` (exit 127) or no output counts as "can't
    verify". Any other failure (a directory, no permission, a wrong path)
    means the file is not where the caller asked, so it is an error.
    """
    code = sum_res.get("exit_code")
    if code == 0 and sum_res.get("stdout"):
        return sum_res["stdout"].split()[0], None
    if code in (0, 127):
        return None, None
    detail = (sum_res.get("stderr") or sum_res.get("error") or f"exit {code}").strip()
    return None, f"Checksum check of the pushed file failed: {detail}"


async def _push_staged(
    client: ProxmoxClient,
    ssh_client: Any,
    node: str,
    vmid: int,
    local_path: Path,
    dest: str,
    verify_checksum: bool = True,
) -> dict[str, Any]:
    """Push a file from the staging dir into a VM/CT (see proxmox_upload_file)."""
    if not ssh_client:
        return {
            "status": "error",
            "error": "Large-file transfer requires SSH. Add an ssh.hosts[] entry for the Proxmox node (or the VM).",
        }
    if not isinstance(dest, str) or not dest.startswith("/"):
        return {"status": "error", "error": "`dest` must be an absolute path inside the guest."}
    size_bytes = local_path.stat().st_size

    loop = asyncio.get_running_loop()
    vm_type, err = await _running_guest(client, node, vmid)
    if err is not None:
        return err

    started = time.monotonic()
    local_sha = await loop.run_in_executor(None, _sha256_file, local_path) if verify_checksum else None

    if vm_type == "lxc":
        node_tmp = f"/tmp/beaconmcp-upload-{uuid.uuid4().hex[:12]}"
        try:
            try:
                await ssh_client.sftp_put(node, str(local_path), node_tmp)
            except Exception as e:
                return {"status": "error", "error": f"SFTP to node {node!r} failed: {e}"}
            push_res = await ssh_client.exec_command(
                node,
                f"pct push {vmid} {shlex.quote(node_tmp)} {shlex.quote(dest)}",
                timeout=600,
            )
        finally:
            # Also after a partial SFTP (full /tmp) or a cancelled request:
            # the node's /tmp must not keep the copy.
            await _remove_quietly(ssh_client, node, node_tmp)
        if push_res.get("exit_code") != 0:
            return {
                "status": "error",
                "error": f"pct push failed: {push_res.get('stderr') or push_res.get('error') or 'unknown'}",
            }
        remote_sha = None
        if verify_checksum:
            sum_res = await ssh_client.exec_command(
                node,
                f"pct exec {vmid} -- sha256sum {shlex.quote(dest)}",
                timeout=300,
            )
            remote_sha, sum_err = _checksum_from(sum_res)
            if sum_err:
                return {"status": "error", "error": sum_err, "bytes": size_bytes}
            if remote_sha and remote_sha != local_sha:
                return {
                    "status": "error",
                    "error": f"Checksum mismatch after upload: local={local_sha} remote={remote_sha}",
                    "bytes": size_bytes,
                }
        result: dict[str, Any] = {
            "status": "success", "vmid": vmid, "node": node, "dest": dest,
            "bytes": size_bytes, "sha256": local_sha,
            "duration_s": round(time.monotonic() - started, 2),
            "transport": "sftp+pct_push",
            "checksum_verified": remote_sha is not None,
        }
        if verify_checksum and remote_sha is None:
            result["warning"] = (
                "sha256sum unavailable in guest; transfer was not checksum-verified."
            )
        return result

    # QEMU: try direct SSH into the VM.
    try:
        ssh_client.resolve(str(vmid))
    except Exception as e:
        return {
            "status": "error",
            "error": (
                f"VM {vmid} is not reachable via SSH ({e}). "
                "Either declare it under ssh.hosts[] (or set ssh.vmid_to_ip), "
                "or use proxmox_write_file for files up to 45 KiB."
            ),
        }
    # SFTP put into an existing directory would drop the file inside it
    # under the staging name (upload-<id>), not at `dest`.
    dir_check = await ssh_client.exec_command(
        str(vmid), f"test -d {shlex.quote(dest)}", timeout=30,
    )
    if dir_check.get("exit_code") == 0:
        return {
            "status": "error",
            "error": f"`dest` {dest!r} is a directory in VM {vmid}: give the full file path.",
        }
    try:
        await ssh_client.sftp_put(str(vmid), str(local_path), dest)
    except Exception as e:
        return {"status": "error", "error": f"SFTP to VM {vmid} failed: {e}"}
    remote_sha = None
    if verify_checksum:
        sum_res = await ssh_client.exec_command(
            str(vmid), f"sha256sum {shlex.quote(dest)}", timeout=300,
        )
        remote_sha, sum_err = _checksum_from(sum_res)
        if sum_err:
            return {"status": "error", "error": sum_err, "bytes": size_bytes}
        if remote_sha and remote_sha != local_sha:
            return {
                "status": "error",
                "error": f"Checksum mismatch after upload: local={local_sha} remote={remote_sha}",
                "bytes": size_bytes,
            }
    result: dict[str, Any] = {
        "status": "success", "vmid": vmid, "node": node, "dest": dest,
        "bytes": size_bytes, "sha256": local_sha,
        "duration_s": round(time.monotonic() - started, 2),
        "transport": "sftp",
        "checksum_verified": remote_sha is not None,
    }
    if verify_checksum and remote_sha is None:
        result["warning"] = (
            "sha256sum unavailable in guest; transfer was not checksum-verified."
        )
    return result


def register_system_tools(mcp: MCPServer, client: ProxmoxClient, ssh_client: Any = None) -> None:
    """Register Proxmox system administration and command execution tools."""

    async def _push_for_link(
        node: str, vmid: int, local_path: Path, dest: str, verify_checksum: bool,
    ) -> dict[str, Any]:
        return await _push_staged(
            client, ssh_client, node, vmid, local_path, dest, verify_checksum,
        )

    # The upload-link HTTP route lives outside MCP; hand it the push path.
    transfers.register_pusher(_push_for_link if ssh_client else None)

    @mcp.tool()
    async def proxmox_read_file(node: str, vmid: int, path: str, binary: bool = False) -> dict[str, Any]:
        """Read a file from a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely (file must be < 1MB).
        For containers, this requires SSH to be configured.
        """
        # Every Proxmox call below is a blocking proxmoxer request (5 s timeout,
        # retried). This tool is async, so server._metric_tool runs it on the
        # event loop as-is: each call must go through an executor or one slow
        # node freezes every other request, /health included.
        loop = asyncio.get_running_loop()
        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        if vm_type == "qemu":
            # Pre-flight: refuse oversized files before the guest agent pulls
            # the whole thing into memory. Best-effort -- if the guest has no
            # `stat` we fall through to the post-read guard below.
            size = await loop.run_in_executor(
                None, _qemu_file_size, client, node, vmid, path,
            )
            if size is not None and size > _FILE_READ_MAX_BYTES:
                return {
                    "status": "error",
                    "error": (
                        f"File is {size} bytes, over the 1MB guest-agent read "
                        "limit. Use proxmox_download_file for large files."
                    ),
                }
            data = await loop.run_in_executor(
                None, _agent_read_bytes, client, node, vmid, path,
            )
            if isinstance(data, dict):
                return data
            if len(data) > _FILE_READ_MAX_BYTES:
                return {
                    "status": "error",
                    "error": "File exceeds the 1MB guest-agent read limit. Use proxmox_download_file for large files.",
                }
            if binary:
                return {
                    "status": "success", "vmid": vmid, "node": node, "path": path,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                }
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"status": "error", "error": "Binary data detected. Pass binary=True to retrieve as base64."}
            return {"status": "success", "vmid": vmid, "node": node, "path": path, "content": content}
        
        return {"status": "error", "error": "LXC file reading is currently unsupported via API. Please use ssh_run to cat the file."}

    @mcp.tool()
    async def proxmox_write_file(node: str, vmid: int, path: str, content: str) -> dict[str, Any]:
        """Write a file to a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely to avoid shell escaping issues.
        For containers, this requires SSH to be configured.
        """
        # Blocking proxmoxer calls go through the executor (see proxmox_read_file).
        loop = asyncio.get_running_loop()
        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        raw = content.encode("utf-8")
        if len(raw) > _FILE_WRITE_MAX_BYTES:
            return {
                "status": "error",
                "error": (
                    f"Content is {len(raw)} bytes, over the {_FILE_WRITE_MAX_BYTES} byte "
                    "guest-agent write limit. Use proxmox_upload_link or proxmox_upload_file."
                ),
            }

        if vm_type == "qemu":
            # encode=0: the content is already base64. With PVE's default
            # (encode=1) it would be encoded a second time and the guest
            # would get the base64 text instead of the file.
            encoded = base64.b64encode(raw).decode("ascii")
            result = await loop.run_in_executor(
                None,
                lambda: client.post(
                    node, f"nodes/{node}/qemu/{vmid}/agent/file-write",
                    file=path, content=encoded, encode=0,
                ),
            )
            if isinstance(result, dict) and "error" in result:
                return {"status": "error", "error": result["error"]}
            return {"status": "success", "vmid": vmid, "node": node, "path": path, "action": "file_write"}
            
        return {"status": "error", "error": "LXC file writing is currently unsupported via API. Please use ssh_exec_command to write the file."}

    @mcp.tool()
    async def proxmox_upload_file(
        node: str,
        vmid: int,
        source: str,
        dest: str,
        verify_checksum: bool = True,
    ) -> dict[str, Any]:
        """Upload a large file from the BeaconMCP staging dir into a VM/CT.

        ``source`` must be a plain basename of a file present in
        ``server.transfers_dir`` (default ``~/.cache/beaconmcp/transfers``).
        ``dest`` is an absolute path inside the guest. Capped by
        ``server.transfers_max_mb`` (default 500 MB).

        - **LXC**: SFTP-streams to ``/tmp/`` on the Proxmox node, then
          ``pct push <vmid>`` into the container, and cleans up.
        - **VM**: SFTP-streams directly into the VM. The VM must be
          SSH-reachable — declared under ``ssh.hosts[]`` or matched by
          ``ssh.vmid_to_ip``. For VMs without SSH, fall back to
          ``proxmox_write_file`` (45 KiB cap).

        When ``verify_checksum=True``, computes a SHA-256 locally and
        re-checks it inside the guest after transfer. If the guest lacks
        ``sha256sum`` (e.g. minimal Alpine CTs), the success response
        carries ``checksum_verified=False`` and a ``warning`` field
        rather than silently treating the transfer as verified.
        """
        if not ssh_client:
            return {
                "status": "error",
                "error": "Large-file transfer requires SSH. Add an ssh.hosts[] entry for the Proxmox node (or the VM).",
            }
        try:
            local_path = _staging_path(client, source)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if not local_path.is_file():
            return {
                "status": "error",
                "error": (
                    f"Staging file {source!r} not found. Place it in "
                    f"{_staging_dir(client)} first (SCP, dashboard, etc.)."
                ),
            }
        size_bytes = local_path.stat().st_size
        max_bytes = client._config.server.transfers_max_mb * 1024 * 1024
        if size_bytes > max_bytes:
            return {
                "status": "error",
                "error": (
                    f"File size {size_bytes} bytes exceeds the configured "
                    f"transfers_max_mb ({client._config.server.transfers_max_mb} MB)."
                ),
            }
        return await _push_staged(
            client, ssh_client, node, vmid, local_path, dest, verify_checksum,
        )

    @mcp.tool()
    async def proxmox_download_file(
        node: str,
        vmid: int,
        source: str,
        dest: str,
        verify_checksum: bool = True,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Download a large file from a VM/CT into the BeaconMCP staging dir.

        ``source`` is an absolute path inside the guest. ``dest`` is a plain
        basename written under ``server.transfers_dir`` (default
        ``~/.cache/beaconmcp/transfers``). Capped by
        ``server.transfers_max_mb`` (default 500 MB) — oversized sources
        are rejected before any data is moved.

        Set ``overwrite=True`` to replace an existing file in the staging
        directory. Without it, the tool refuses to clobber existing files.
        Data is streamed to a sibling ``<dest>.part`` file and atomically
        renamed on success, so an interrupted transfer never leaves a
        half-written file at ``dest``.

        When ``verify_checksum=True`` but the guest lacks ``sha256sum``,
        the success response carries ``checksum_verified=False`` and a
        ``warning`` field rather than silently treating the transfer as
        verified.

        - **LXC**: ``pct pull`` to ``/tmp/`` on the Proxmox node, then
          SFTP-streams it back to the staging dir, and cleans up.
        - **VM**: SFTP-streams directly from the VM. Requires the VM to be
          SSH-reachable (see ``proxmox_upload_file`` for details).
        """
        if not ssh_client:
            return {
                "status": "error",
                "error": "Large-file transfer requires SSH. Add an ssh.hosts[] entry for the Proxmox node (or the VM).",
            }
        try:
            local_path = _staging_path(client, dest)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if local_path.exists() and not overwrite:
            return {
                "status": "error",
                "error": (
                    f"Staging file {dest!r} already exists. "
                    "Pass overwrite=True to replace it."
                ),
            }
        if not isinstance(source, str) or not source.startswith("/"):
            return {"status": "error", "error": "`source` must be an absolute path inside the guest."}

        loop = asyncio.get_running_loop()
        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
        max_bytes = client._config.server.transfers_max_mb * 1024 * 1024
        started = time.monotonic()

        if vm_type == "lxc":
            # Pre-flight: refuse oversized files before any data movement.
            size_res = await ssh_client.exec_command(
                node,
                f"pct exec {vmid} -- stat -c %s {shlex.quote(source)}",
                timeout=60,
            )
            if size_res.get("exit_code") != 0:
                return {
                    "status": "error",
                    "error": f"Could not stat {source!r} in CT {vmid}: {size_res.get('stderr') or size_res.get('error')}",
                }
            try:
                src_size = int((size_res.get("stdout") or "0").strip())
            except ValueError:
                src_size = 0
            if src_size > max_bytes:
                return {
                    "status": "error",
                    "error": (
                        f"Source size {src_size} bytes exceeds the configured "
                        f"transfers_max_mb ({client._config.server.transfers_max_mb} MB)."
                    ),
                }
            remote_sha = None
            if verify_checksum:
                sum_res = await ssh_client.exec_command(
                    node,
                    f"pct exec {vmid} -- sha256sum {shlex.quote(source)}",
                    timeout=300,
                )
                if sum_res.get("exit_code") == 0 and sum_res.get("stdout"):
                    remote_sha = sum_res["stdout"].split()[0]
            node_tmp = f"/tmp/beaconmcp-download-{uuid.uuid4().hex[:12]}"
            # Stream into a sibling .part file and rename on success so an
            # interrupted transfer never leaves a half-written file at dest.
            tmp_path = local_path.with_name(local_path.name + ".part")
            pull_res = await ssh_client.exec_command(
                node,
                f"pct pull {vmid} {shlex.quote(source)} {shlex.quote(node_tmp)}",
                timeout=600,
            )
            if pull_res.get("exit_code") != 0:
                await ssh_client.sftp_remove(node, node_tmp)
                return {
                    "status": "error",
                    "error": f"pct pull failed: {pull_res.get('stderr') or pull_res.get('error') or 'unknown'}",
                }
            try:
                await ssh_client.sftp_get(node, node_tmp, str(tmp_path))
            except Exception as e:
                await ssh_client.sftp_remove(node, node_tmp)
                tmp_path.unlink(missing_ok=True)
                return {"status": "error", "error": f"SFTP from node {node!r} failed: {e}"}
            await ssh_client.sftp_remove(node, node_tmp)
            local_sha = await loop.run_in_executor(None, _sha256_file, tmp_path) if verify_checksum else None
            if verify_checksum and remote_sha and local_sha != remote_sha:
                size = tmp_path.stat().st_size
                tmp_path.unlink(missing_ok=True)
                return {
                    "status": "error",
                    "error": f"Checksum mismatch after download: remote={remote_sha} local={local_sha}",
                    "bytes": size,
                }
            tmp_path.replace(local_path)
            result: dict[str, Any] = {
                "status": "success", "vmid": vmid, "node": node,
                "source": source, "staged_path": str(local_path),
                "bytes": local_path.stat().st_size, "sha256": local_sha or remote_sha,
                "duration_s": round(time.monotonic() - started, 2),
                "transport": "pct_pull+sftp",
                "checksum_verified": remote_sha is not None,
            }
            if verify_checksum and remote_sha is None:
                result["warning"] = (
                    "sha256sum unavailable in guest; transfer was not checksum-verified."
                )
            return result

        # QEMU: direct SSH.
        try:
            ssh_client.resolve(str(vmid))
        except Exception as e:
            return {
                "status": "error",
                "error": (
                    f"VM {vmid} is not reachable via SSH ({e}). "
                    "Either declare it under ssh.hosts[] (or set ssh.vmid_to_ip), "
                    "or use proxmox_read_file for files ≤1MB."
                ),
            }
        size_res = await ssh_client.exec_command(
            str(vmid), f"stat -c %s {shlex.quote(source)}", timeout=60,
        )
        if size_res.get("exit_code") != 0:
            return {
                "status": "error",
                "error": f"Could not stat {source!r} in VM {vmid}: {size_res.get('stderr') or size_res.get('error')}",
            }
        try:
            src_size = int((size_res.get("stdout") or "0").strip())
        except ValueError:
            src_size = 0
        if src_size > max_bytes:
            return {
                "status": "error",
                "error": (
                    f"Source size {src_size} bytes exceeds the configured "
                    f"transfers_max_mb ({client._config.server.transfers_max_mb} MB)."
                ),
            }
        remote_sha = None
        if verify_checksum:
            sum_res = await ssh_client.exec_command(
                str(vmid), f"sha256sum {shlex.quote(source)}", timeout=300,
            )
            if sum_res.get("exit_code") == 0 and sum_res.get("stdout"):
                remote_sha = sum_res["stdout"].split()[0]
        # Stream into a sibling .part file and rename on success so an
        # interrupted transfer never leaves a half-written file at dest.
        tmp_path = local_path.with_name(local_path.name + ".part")
        try:
            await ssh_client.sftp_get(str(vmid), source, str(tmp_path))
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            return {"status": "error", "error": f"SFTP from VM {vmid} failed: {e}"}
        local_sha = await loop.run_in_executor(None, _sha256_file, tmp_path) if verify_checksum else None
        if verify_checksum and remote_sha and local_sha != remote_sha:
            size = tmp_path.stat().st_size
            tmp_path.unlink(missing_ok=True)
            return {
                "status": "error",
                "error": f"Checksum mismatch after download: remote={remote_sha} local={local_sha}",
                "bytes": size,
            }
        tmp_path.replace(local_path)
        result: dict[str, Any] = {
            "status": "success", "vmid": vmid, "node": node,
            "source": source, "staged_path": str(local_path),
            "bytes": local_path.stat().st_size, "sha256": local_sha or remote_sha,
            "duration_s": round(time.monotonic() - started, 2),
            "transport": "sftp",
            "checksum_verified": remote_sha is not None,
        }
        if verify_checksum and remote_sha is None:
            result["warning"] = (
                "sha256sum unavailable in guest; transfer was not checksum-verified."
            )
        return result

    @mcp.tool()
    def proxmox_list_transfers() -> dict[str, Any]:
        """List files currently in the BeaconMCP staging directory.

        Returns each file's basename, size in bytes, and last-modified epoch.
        Use this to discover which ``source`` names are available for
        ``proxmox_upload_file``, or to confirm a ``proxmox_download_file``
        landed.
        """
        base = _staging_dir(client)
        entries: list[dict[str, Any]] = []
        for entry in sorted(base.iterdir()):
            if not entry.is_file():
                continue
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "bytes": stat.st_size,
                "modified": int(stat.st_mtime),
            })
        return {
            "transfers_dir": str(base),
            "max_mb": client._config.server.transfers_max_mb,
            "files": entries,
            "total": len(entries),
        }

    @mcp.tool()
    def proxmox_delete_transfer(name: str) -> dict[str, Any]:
        """Delete a file from the BeaconMCP staging directory.

        ``name`` must be a plain basename (no slashes). Use
        ``proxmox_list_transfers`` to see available files.
        """
        try:
            target = _staging_path(client, name)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if not target.is_file():
            return {"status": "error", "error": f"File {name!r} not found in staging directory."}
        size = target.stat().st_size
        target.unlink()
        return {"status": "success", "deleted": name, "freed_bytes": size}

    @mcp.tool()
    async def proxmox_upload_link(
        node: str = "",
        vmid: int = 0,
        dest: str = "",
        expires_in: int = transfers.DEFAULT_TTL,
        verify_checksum: bool = True,
        link_id: str = "",
    ) -> dict[str, Any]:
        """Mint a one-shot HTTP link that writes the uploaded bytes into a VM/CT.

        Use this to send a file you hold locally (any size up to
        ``server.transfers_max_mb``, binary included) without base64-ing it
        into a tool call. Two call patterns:

        - **Mint**: pass ``node``, ``vmid`` and ``dest`` (absolute path inside
          the guest). Returns ``url``; send the raw file to it with
          ``curl -T <file> <url>`` (PUT; POST with ``--data-binary @<file>``
          works too). The server stages the bytes, pushes them into the guest
          like ``proxmox_upload_file`` (SHA-256 checked by default) and
          answers with the transfer result.
        - **Status**: pass ``link_id`` only. Returns the link's state
          (pending, uploading, success, error, expired) and, once finished,
          the transfer result. Useful when the HTTP reply got lost.

        The link works once, expires after ``expires_in`` seconds (60-3600,
        default 600), and dies on server restart. Anyone holding the URL can
        use it until then, so treat it like a password. Behind Cloudflare, a
        request body is capped at 100 MB on the free plan.
        """
        store = transfers.get_store()
        if link_id:
            link = store.get(link_id)
            if link is None:
                return {"status": "error", "error": f"No upload link with link_id {link_id!r}."}
            return link.public()

        if not ssh_client:
            return {
                "status": "error",
                "error": "Large-file transfer requires SSH. Add an ssh.hosts[] entry for the Proxmox node (or the VM).",
            }
        if not node or not vmid:
            return {"status": "error", "error": "`node` and `vmid` are required to mint a link."}
        if not isinstance(dest, str) or not dest.startswith("/"):
            return {"status": "error", "error": "`dest` must be an absolute path inside the guest."}
        vm_type, err = await _running_guest(client, node, vmid)
        if err is not None:
            return err

        max_bytes = client._config.server.transfers_max_mb * 1024 * 1024
        try:
            link = store.issue(
                node=node, vmid=vmid, dest=dest, max_bytes=max_bytes,
                staging_dir=_staging_dir(client),
                verify_checksum=verify_checksum, ttl=expires_in,
            )
        except RuntimeError as e:
            return {"status": "error", "error": str(e)}

        path = f"{transfers.UPLOAD_PATH_PREFIX}{link.token}"
        base = transfers.public_base_url(client._config.features.dashboard.public_url)
        url = f"{base}{path}" if base else path
        result: dict[str, Any] = {
            "status": "success",
            "link_id": link.link_id,
            "url": url,
            "method": "PUT",
            "curl": f"curl -sS -T <file> '{url}'",
            "node": node,
            "vmid": vmid,
            "vm_type": vm_type,
            "dest": dest,
            "max_bytes": max_bytes,
            "expires_at": int(link.expires_at),
        }
        if not base:
            result["warning"] = (
                "Public URL unknown: prefix `url` with the address this server "
                "is reached at (set features.dashboard.public_url to fix)."
            )
        return result

    @mcp.tool()
    async def proxmox_fetch_url(
        node: str,
        vmid: int,
        url: str,
        dest: str,
        verify_checksum: bool = True,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """Download ``url`` on the BeaconMCP server and write it into a VM/CT.

        The server fetches the file (http/https, redirects followed, capped by
        ``server.transfers_max_mb``), stages it, then pushes it to ``dest``
        (absolute path inside the guest) like ``proxmox_upload_file``. Use it
        when the file is reachable by URL, e.g. a release asset or a raw file
        on GitHub. The staged copy is deleted afterwards.

        URLs that resolve to a loopback or link-local address (the BeaconMCP
        host itself, cloud metadata endpoints) are refused, redirects
        included. Private LAN addresses are allowed.
        """
        if not ssh_client:
            return {
                "status": "error",
                "error": "Large-file transfer requires SSH. Add an ssh.hosts[] entry for the Proxmox node (or the VM).",
            }
        if urlparse(url).scheme not in ("http", "https"):
            return {"status": "error", "error": "`url` must be an http:// or https:// URL."}
        if not isinstance(dest, str) or not dest.startswith("/"):
            return {"status": "error", "error": "`dest` must be an absolute path inside the guest."}
        # Check the target before pulling a possibly large file for nothing.
        _vm_type, err = await _running_guest(client, node, vmid)
        if err is not None:
            return err

        max_bytes = client._config.server.transfers_max_mb * 1024 * 1024
        staged = _staging_dir(client) / f"fetch-{uuid.uuid4().hex[:12]}"
        part = staged.with_name(staged.name + ".part")
        received = 0
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=min(max(timeout, 10), 1800),
                event_hooks={"request": [_refuse_local_target]},
                transport=_fetch_transport(),
            ) as http:
                async with http.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return {"status": "error", "error": f"GET {url} returned HTTP {resp.status_code}."}
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        return {
                            "status": "error",
                            "error": f"Remote file is {declared} bytes, over the {max_bytes} byte transfer cap.",
                        }
                    with open(part, "wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            received += len(chunk)
                            if received > max_bytes:
                                return {
                                    "status": "error",
                                    "error": f"Download exceeded the {max_bytes} byte transfer cap.",
                                }
                            fh.write(chunk)
            part.replace(staged)
            fetch_s = round(time.monotonic() - started, 2)
            result = await _push_staged(
                client, ssh_client, node, vmid, staged, dest, verify_checksum,
            )
        except httpx.HTTPError as e:
            return {"status": "error", "error": f"Download of {url} failed: {e}"}
        except _LocalTargetRefused as e:
            return {"status": "error", "error": str(e)}
        finally:
            part.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
        result["source_url"] = url
        result["fetch_s"] = fetch_s
        return result

    @mcp.tool()
    def proxmox_storage_status(node: str = "") -> dict[str, Any]:
        """Get storage status across the cluster: usage, type, content types.

        Use to check disk space, storage health, or find available storage.
        Omit 'node' to list storage from all configured nodes.
        """
        target_nodes = [node] if node else client.configured_nodes
        by_node: dict[str, list[dict[str, Any]]] = {}

        for n in target_nodes:
            entries: list[dict[str, Any]] = []
            data = client.get(n, f"nodes/{n}/storage")
            if isinstance(data, dict) and "error" in data:
                entries.append({"error": data["error"]})
                by_node[n] = entries
                continue
            if not isinstance(data, list):
                by_node[n] = entries
                continue
            for s in data:
                storage_name = s.get("storage")
                if not storage_name:
                    continue
                status = client.get(n, f"nodes/{n}/storage/{storage_name}/status")
                used = status.get("used", 0) if isinstance(status, dict) and "error" not in status else 0
                total = status.get("total", 0) if isinstance(status, dict) and "error" not in status else 0

                entries.append({
                    "name": storage_name,
                    "type": s.get("type"),
                    "content": s.get("content"),
                    "enabled": s.get("enabled", 1) == 1,
                    "used_gb": round(used / 1073741824, 1),
                    "total_gb": round(total / 1073741824, 1),
                    "usage_pct": round(used / total * 100, 1) if total > 0 else 0,
                })
            by_node[n] = entries

        return {"storage": by_node}

    @mcp.tool()
    def proxmox_network_config(node: str) -> dict[str, Any]:
        """Get network interface configuration of a Proxmox node."""
        data = client.get(node, f"nodes/{node}/network")
        if isinstance(data, dict) and "error" in data:
            return data
        if not isinstance(data, list):
            return {"node": node, "interfaces": [], "raw": str(data)}

        interfaces = []
        for iface in data:
            interfaces.append({
                "name": iface.get("iface"),
                "type": iface.get("type"),
                "address": iface.get("address"),
                "netmask": iface.get("netmask"),
                "gateway": iface.get("gateway"),
                "bridge_ports": iface.get("bridge_ports"),
                "active": iface.get("active", False),
                "method": iface.get("method"),
                "cidr": iface.get("cidr"),
            })
        return {"node": node, "interfaces": interfaces}

    def _start_async_qemu(node: str, vmid: int, command: str) -> dict[str, Any]:
        _prune_exec_sessions()
        exec_id = str(uuid.uuid4())[:8]
        session = ExecSession(
            exec_id=exec_id,
            node=node,
            vmid=vmid,
            vm_type="qemu",
            command=command,
        )
        _exec_sessions[exec_id] = session

        parts, os_hint = _guest_argv(client, node, vmid, command)
        result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/exec", command=parts)
        if isinstance(result, dict) and "error" in result:
            error = str(result["error"])
            if os_hint:
                error = f"{error} ({os_hint})"
            session.status = "failed"
            session.stderr = error
            return {"exec_id": exec_id, "status": "failed", "error": error}
        session.pid = result.get("pid") if isinstance(result, dict) else None
        return {"exec_id": exec_id, "status": "running"}

    def _poll_session(exec_id: str) -> dict[str, Any]:
        session = _exec_sessions.get(exec_id)
        if not session:
            return {"status": "error", "error": f"No command found with exec_id '{exec_id}'."}

        if session.status != "running":
            return {
                "exec_id": exec_id,
                "status": "ok" if session.status == "completed" and session.exit_code == 0 else session.status,
                "stdout": session.stdout,
                "stderr": session.stderr,
                "exit_code": session.exit_code,
                "command": session.command,
                "elapsed_s": round(time.time() - session.started_at, 1),
            }

        if session.vm_type == "qemu" and session.pid is not None:
            status_data = client.get(
                session.node,
                f"nodes/{session.node}/qemu/{session.vmid}/agent/exec-status",
                pid=session.pid,
            )
            if isinstance(status_data, dict) and status_data.get("exited"):
                stdout = status_data.get("out-data", "")
                stderr = status_data.get("err-data", "")
                if status_data.get("out-data-encoding") == "base64" and stdout:
                    stdout = base64.b64decode(stdout).decode("utf-8", errors="replace")
                if status_data.get("err-data-encoding") == "base64" and stderr:
                    stderr = base64.b64decode(stderr).decode("utf-8", errors="replace")
                session.status = "completed"
                session.stdout = stdout
                session.stderr = stderr
                session.exit_code = status_data.get("exitcode", -1)

        if time.time() - session.started_at > 600:
            session.status = "timeout"

        elapsed = round(time.time() - session.started_at, 1)
        if session.status == "running":
            return {"status": "running", "exec_id": exec_id, "command": session.command, "elapsed_s": elapsed}
        
        return {
            "status": "ok" if session.status == "completed" and session.exit_code == 0 else session.status,
            "exec_id": exec_id,
            "command": session.command,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "duration_s": elapsed,
        }

    @mcp.tool()
    async def proxmox_run(
        node: str = "",
        vmid: int = 0,
        command: str = "",
        timeout: int = 60,
        wait: bool = True,
        exec_id: str = "",
    ) -> dict[str, Any]:
        """Run a command inside a VM (QEMU Guest Agent) or container (LXC pct exec).

        ``command`` is a shell command line in both cases: pipes, redirections,
        ``&&`` and quoting work. Linux/Unix VMs and containers run it with
        ``sh -c``; Windows VMs run it with ``cmd.exe /c`` (cmd syntax, not
        PowerShell; double quotes inside the command may not survive).

        Three call patterns:
        - **Sync** (default): pass ``node``, ``vmid``, ``command``. Blocks up to ``timeout`` seconds (max 600).
        - **Async start**: pass ``node``, ``vmid``, ``command``, ``wait=False``. Returns immediately.
        - **Poll existing**: pass ``exec_id`` only. Returns the current status/output for that session.
        """
        loop = asyncio.get_running_loop()

        def _format_ssh_result(xid: str, s) -> dict[str, Any]:
            elapsed = round(time.time() - s.started_at, 1)
            if s.status == "running":
                return {"status": "running", "exec_id": xid, "command": s.command, "elapsed_s": elapsed}
            return {
                "status": "ok" if s.status == "completed" and s.exit_code == 0 else s.status,
                "exec_id": xid,
                "command": s.command,
                "stdout": s.stdout,
                "stderr": s.stderr,
                "exit_code": s.exit_code,
                "duration_s": elapsed,
            }

        if exec_id:
            if exec_id in _exec_sessions:
                return await loop.run_in_executor(None, _poll_session, exec_id)
            if ssh_client:
                ssh_sess = ssh_client.get_session(exec_id)
                if ssh_sess:
                    return _format_ssh_result(exec_id, ssh_sess)
            return {"status": "error", "error": f"No command found with exec_id {exec_id!r}."}

        if not command:
            return {"status": "error", "error": "`command` is required when `exec_id` is not provided."}
        if not node or not vmid:
            return {"status": "error", "error": "`node` and `vmid` are required to start a command."}

        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
        
        max_timeout = min(max(timeout, 1), 600)
        
        if vm_type == "lxc":
            if not ssh_client:
                return {"status": "error", "error": "LXC execution requires SSH access to the Proxmox node, but SSH is not configured."}
            try:
                # Safe escaping to prevent shell injection (fixing the owner's feedback)
                escaped_cmd = shlex.quote(command)
                lxc_cmd = f"pct exec {vmid} -- sh -c {escaped_cmd}"
                new_id = await ssh_client.exec_command_async(node, lxc_cmd)
            except Exception as e:
                return {"status": "error", "error": str(e)}

            if not wait:
                return {"status": "running", "exec_id": new_id, "elapsed_s": 0}

            deadline = time.time() + max_timeout
            while time.time() < deadline:
                session = ssh_client.get_session(new_id)
                if session and session.status != "running":
                    return _format_ssh_result(new_id, session)
                await asyncio.sleep(1)
                
            session = ssh_client.get_session(new_id)
            if session and session.status != "running":
                return _format_ssh_result(new_id, session)
            return {
                "status": "running",
                "exec_id": new_id,
                "elapsed_s": int(time.time() - (session.started_at if session else time.time())),
                "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
            }
        else:
            started = await loop.run_in_executor(None, _start_async_qemu, node, vmid, command)
            if started.get("status") == "failed":
                return started
            new_id = started["exec_id"]

            if not wait:
                return {"status": "running", "exec_id": new_id, "elapsed_s": 0}

            deadline = time.time() + max_timeout
            while time.time() < deadline:
                result = await loop.run_in_executor(None, _poll_session, new_id)
                if result["status"] != "running":
                    return result
                await asyncio.sleep(1)
            
            return {
                "status": "running",
                "exec_id": new_id,
                "elapsed_s": int(time.time() - _exec_sessions[new_id].started_at),
                "hint": "Command still running. Call proxmox_run(exec_id=...) to poll.",
            }
