from __future__ import annotations

import base64
import asyncio
import hashlib
import time
import uuid
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..utils import filter_fields
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
    now = time.time()
    stale = [
        eid
        for eid, s in _exec_sessions.items()
        if s.status != "running" and now - s.started_at > _EXEC_SESSION_TTL
    ]
    for eid in stale:
        del _exec_sessions[eid]


def _detect_vm_type(client: ProxmoxClient, node: str, vmid: int) -> str | None:
    for vm_type in ("qemu", "lxc"):
        data = client.get(node, f"nodes/{node}/{vm_type}/{vmid}/status/current")
        if isinstance(data, dict) and "error" in data:
            continue
        if isinstance(data, dict) and data.get("status"):
            return vm_type
    return None


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


def register_system_tools(mcp: FastMCP, client: ProxmoxClient, ssh_client: Any = None) -> None:
    """Register Proxmox system administration and command execution tools."""


    @mcp.tool()
    async def proxmox_read_file(node: str, vmid: int, path: str, binary: bool = False) -> dict[str, Any]:
        """Read a file from a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely (file must be < 1MB).
        For containers, this requires SSH to be configured.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        if vm_type == "qemu":
            result = client.get(node, f"nodes/{node}/qemu/{vmid}/agent/file-read", file=path)
            if isinstance(result, dict) and "error" in result:
                return {"status": "error", "error": result["error"]}
            # QEMU Guest Agent returns file content base64-encoded
            try:
                if isinstance(result, dict) and "content" in result:
                    import base64
                    raw_b64 = result["content"]
                    if len(raw_b64) > 1398101: # ~1MB limit in base64
                        return {"status": "error", "error": "File exceeds 1MB limit. Use SSH to download large files."}
                    
                    if binary:
                        return {"status": "success", "vmid": vmid, "node": node, "path": path, "content_base64": raw_b64}
                        
                    try:
                        content = base64.b64decode(raw_b64).decode("utf-8")
                        return {"status": "success", "vmid": vmid, "node": node, "path": path, "content": content}
                    except UnicodeDecodeError:
                        return {"status": "error", "error": "Binary data detected. Pass binary=True to retrieve as base64."}
                return {"status": "success", "vmid": vmid, "node": node, "path": path, "content": str(result)}
            except Exception as e:
                return {"status": "error", "error": f"Failed to decode file content: {e}"}
        
        return {"status": "error", "error": "LXC file reading is currently unsupported via API. Please use ssh_run to cat the file."}

    @mcp.tool()
    async def proxmox_write_file(node: str, vmid: int, path: str, content: str) -> dict[str, Any]:
        """Write a file to a VM or container.
        
        For VMs, this uses the QEMU Guest Agent safely to avoid shell escaping issues.
        For containers, this requires SSH to be configured.
        """
        vm_type = _detect_vm_type(client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}
            
        if len(content.encode("utf-8")) > 1024 * 1024:
            return {"status": "error", "error": "Content exceeds 1MB limit. Use SSH for large files."}
            
        if vm_type == "qemu":
            import base64
            encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
            result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/file-write", file=path, content=encoded, encode=1)
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
          ``proxmox_write_file`` (1 MB cap).

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
        if not isinstance(dest, str) or not dest.startswith("/"):
            return {"status": "error", "error": "`dest` must be an absolute path inside the guest."}

        loop = asyncio.get_running_loop()
        vm_type = await loop.run_in_executor(None, _detect_vm_type, client, node, vmid)
        if not vm_type:
            return {"status": "error", "error": f"VM/CT {vmid} not found on node '{node}'."}

        started = time.monotonic()
        local_sha = await loop.run_in_executor(None, _sha256_file, local_path) if verify_checksum else None

        if vm_type == "lxc":
            node_tmp = f"/tmp/beaconmcp-upload-{uuid.uuid4().hex[:12]}"
            try:
                await ssh_client.sftp_put(node, str(local_path), node_tmp)
            except Exception as e:
                return {"status": "error", "error": f"SFTP to node {node!r} failed: {e}"}
            push_res = await ssh_client.exec_command(
                node,
                f"pct push {vmid} {shlex.quote(node_tmp)} {shlex.quote(dest)}",
                timeout=600,
            )
            await ssh_client.sftp_remove(node, node_tmp)
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
                if sum_res.get("exit_code") == 0 and sum_res.get("stdout"):
                    remote_sha = sum_res["stdout"].split()[0]
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
                    "or use proxmox_write_file for files ≤1MB."
                ),
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
            if sum_res.get("exit_code") == 0 and sum_res.get("stdout"):
                remote_sha = sum_res["stdout"].split()[0]
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

        parts = shlex.split(command)
        result = client.post(node, f"nodes/{node}/qemu/{vmid}/agent/exec", command=parts)
        if isinstance(result, dict) and "error" in result:
            session.status = "failed"
            session.stderr = str(result["error"])
            return {"exec_id": exec_id, "status": "failed", "error": result["error"]}
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
