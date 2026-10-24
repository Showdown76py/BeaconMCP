from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from ..config import Config, SSHHost


class SSHNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "SSH is not configured. Add an 'ssh:' section with at least "
            "one entry under 'ssh.hosts[]' in beaconmcp.yaml."
        )


class SSHHostResolutionError(Exception):
    """Raised when a host identifier cannot be resolved to a declared SSH host."""


@dataclass
class SSHExecSession:
    exec_id: str
    host: str
    command: str
    status: str = "running"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)


_ssh_sessions: dict[str, SSHExecSession] = {}
_ssh_tasks: set[asyncio.Task[None]] = set()
_connection_cache: dict[str, tuple[asyncssh.SSHClientConnection, float]] = {}
_CONNECTION_TTL = 300  # 5 minutes
_SSH_SESSION_TTL = 3600  # drop completed sessions older than this


def _prune_ssh_sessions() -> None:
    now = time.time()
    stale = [
        eid
        for eid, s in _ssh_sessions.items()
        if s.status != "running" and now - s.started_at > _SSH_SESSION_TTL
    ]
    for eid in stale:
        del _ssh_sessions[eid]


class HostKeyChangedError(Exception):
    """A host presented a different key than the one recorded on first contact."""


# Name of the managed known_hosts file used for trust-on-first-use, kept
# beside clients.json so it lives on the same persistent volume.
TOFU_KNOWN_HOSTS_NAME = "ssh_known_hosts"


@dataclass(frozen=True)
class HostKeyPolicy:
    """How a connection verifies the server's host key.

    * ``file``: only keys listed in ``path`` (an OpenSSH known_hosts file).
    * ``system``: only keys in the user's ``~/.ssh/known_hosts``.
    * ``tofu``: like OpenSSH ``accept-new``. A host never seen before is
      trusted and its key recorded in ``path``; afterwards only that key is
      accepted.
    * ``any``: accept every key (explicit ``strict_host_key_checking: false``).
    """

    mode: str
    path: str | None = None


def tofu_known_hosts_path(config: Config) -> Path:
    clients = os.environ.get("BEACONMCP_CLIENTS_FILE") or str(config.server.clients_file)
    return Path(clients).expanduser().parent / TOFU_KNOWN_HOSTS_NAME


def host_key_policy(config: Config, spec: SSHHost) -> HostKeyPolicy:
    """Resolve the host-key policy for ``spec`` from the YAML settings.

    Per-host ``known_hosts`` / ``strict_host_key_checking`` win over the
    ``ssh:`` level ones; ``None`` means "inherit". A known_hosts file, once
    set at either level, always applies (overrides only tighten). With
    nothing set, new hosts are trusted on first use and pinned.
    """
    ssh = config.ssh
    known_hosts = spec.known_hosts or (ssh.known_hosts if ssh else None)
    strict = spec.strict_host_key_checking
    if strict is None and ssh is not None:
        strict = ssh.strict_host_key_checking
    if known_hosts:
        return HostKeyPolicy("file", os.path.expanduser(known_hosts))
    if strict is True:
        return HostKeyPolicy("system")
    if strict is False:
        return HostKeyPolicy("any")
    return HostKeyPolicy("tofu", str(tofu_known_hosts_path(config)))


_tofu_lock = threading.Lock()


def _known_host_pattern(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _pinned_keys(path: str, host: str, port: int) -> list[asyncssh.SSHKey]:
    """Keys recorded in the managed file for exactly ``host`` on ``port``.

    asyncssh's own matching retries a non-22 port without the port, so a
    ``[host]:2222`` target would be checked against the key of ``host`` on
    port 22, which is often a different machine behind the same NAT address.
    """
    if not os.path.isfile(path):
        return []
    pattern = _known_host_pattern(host, port)
    keys: list[asyncssh.SSHKey] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            parts = raw.split()
            if len(parts) < 3 or parts[0].startswith(("#", "@")):
                continue
            if pattern not in parts[0].split(","):
                continue
            try:
                keys.append(asyncssh.import_public_key(f"{parts[1]} {parts[2]}"))
            except (asyncssh.KeyImportError, ValueError):
                continue
    return keys


class _TofuClient(asyncssh.SSHClient):
    """Accepts and records the key of a host the managed file doesn't know.

    asyncssh only calls ``validate_host_public_key`` when the presented key
    is not among the trusted keys it loaded from the managed file when the
    connection started. The file is re-read here, under the lock: a parallel
    first connection may have recorded this very key in the meantime, which
    is a match. Only a different key on file means the key has changed.
    """

    def __init__(self, path: str, host: str, port: int) -> None:
        self._path = path
        self._host = host
        self._port = port
        self.changed = False

    def validate_host_public_key(self, host: str, addr: str, port: int, key) -> bool:
        with _tofu_lock:
            pinned = _pinned_keys(self._path, self._host, self._port)
            if key in pinned:
                return True
            if pinned:
                self.changed = True
                return False
            line = key.export_public_key("openssh").decode().split()
            entry = f"{_known_host_pattern(self._host, self._port)} {line[0]} {line[1]}\n"
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(entry)
        return True


async def _connect_to_host(
    spec: SSHHost,
    policy: HostKeyPolicy,
) -> asyncssh.SSHClientConnection:
    """Open an asyncssh connection to a declared host using its auth method.

    Exposed at module level so BMC jump-host tunneling in ``bmc/hp_ilo.py``
    can reuse the same auth plumbing (password vs. key_file, port override,
    host-key policy from :func:`host_key_policy`) instead of duplicating it.
    """
    connect_kwargs: dict[str, Any] = {
        "host": spec.host,
        "port": spec.port,
        "username": spec.user,
    }
    tofu_client: _TofuClient | None = None
    if policy.mode == "file":
        connect_kwargs["known_hosts"] = policy.path
    elif policy.mode == "any":
        connect_kwargs["known_hosts"] = None
    elif policy.mode == "tofu":
        assert policy.path is not None
        connect_kwargs["known_hosts"] = (
            _pinned_keys(policy.path, spec.host, spec.port), [], [],
        )
        tofu_client = _TofuClient(policy.path, spec.host, spec.port)
        connect_kwargs["client_factory"] = lambda: tofu_client
    # "system": omit the kwarg -> asyncssh uses ~/.ssh/known_hosts.
    if spec.password:
        connect_kwargs["password"] = spec.password
    elif spec.key_file:
        connect_kwargs["client_keys"] = [os.path.expanduser(spec.key_file)]
    try:
        return await asyncssh.connect(**connect_kwargs)
    except asyncssh.HostKeyNotVerifiable as exc:
        if tofu_client is not None and tofu_client.changed:
            pattern = _known_host_pattern(spec.host, spec.port)
            raise HostKeyChangedError(
                f"SSH host key for {spec.name!r} ({pattern}) does not match the "
                f"key recorded on first contact in {policy.path}. Someone may "
                "be intercepting the connection. If the host was reinstalled "
                f"or its keys regenerated, remove its entry (ssh-keygen -R "
                f"'{pattern}' -f {policy.path}) and retry."
            ) from exc
        raise


class SSHClient:
    """Async SSH client with declarative host resolution and connection caching.

    Every SSH target must be declared under ``ssh.hosts[]`` in beaconmcp.yaml.
    The client looks up an identifier (a host name, a numeric VMID, or a raw
    IP/hostname) against that declaration to recover full connect params
    (host, port, user, password or key_file). There is no implicit
    passthrough to an arbitrary host with shared credentials — this keeps
    credentials a declarative concern, not a runtime guess.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    def resolve(self, identifier: str) -> SSHHost:
        """Resolve an identifier to a declared :class:`SSHHost`.

        Resolution order:
        1. Match ``identifier`` against ``ssh.hosts[].name``.
        2. If numeric and ``ssh.vmid_to_ip`` is set, apply the template and
           match the resulting IP against ``ssh.hosts[].host``.
        3. Otherwise, match ``identifier`` directly against
           ``ssh.hosts[].host``.

        Raises :class:`SSHNotConfiguredError` if SSH has no hosts declared,
        or :class:`SSHHostResolutionError` with an actionable message when
        no host matches.
        """
        if not self._config.ssh or not self._config.ssh.hosts:
            raise SSHNotConfiguredError()

        # 1. By declared name
        by_name = self._config.get_ssh_host(identifier)
        if by_name is not None:
            return by_name

        # 2. Numeric VMID → template → address match
        if identifier.isdigit():
            template = self._config.ssh.vmid_to_ip
            if not template:
                raise SSHHostResolutionError(
                    f"Identifier {identifier!r} looks like a numeric VMID "
                    "but no 'ssh.vmid_to_ip' template is configured. Either "
                    "set the template (e.g. '192.168.1.{id}') or reference "
                    "one of the declared host names under ssh.hosts[]."
                )
            try:
                resolved_ip = template.format(id=identifier)
            except (KeyError, IndexError) as exc:
                raise SSHHostResolutionError(
                    f"ssh.vmid_to_ip template {template!r} is invalid: {exc}. "
                    "Use '{id}' as the only placeholder."
                ) from exc
            by_addr = self._config.get_ssh_host_by_address(resolved_ip)
            if by_addr is not None:
                return by_addr
            raise SSHHostResolutionError(
                f"VMID {identifier!r} resolves to {resolved_ip!r} via "
                "ssh.vmid_to_ip, but no ssh.hosts[] entry has that address. "
                f"Declare one (e.g. name: vm-{identifier}, host: "
                f"{resolved_ip}, user: ..., password or key_file: ...)."
            )

        # 3. Direct IP/hostname
        by_addr = self._config.get_ssh_host_by_address(identifier)
        if by_addr is not None:
            return by_addr

        declared = ", ".join(h.name for h in self._config.ssh.hosts) or "<none>"
        hint = ""
        # When the identifier matches a Proxmox node that wasn't declared as
        # an SSH host, point the caller at the two common fixes instead of
        # just reporting "not declared". This is the single most common
        # foot-gun — pre-2.0 code let you SSH into a Proxmox node by name
        # implicitly.
        if any(n.name == identifier for n in self._config.pve_nodes):
            hint = (
                f" Note: {identifier!r} is a Proxmox node. To reach it via "
                "SSH, either add it under ssh.hosts[] explicitly, or set "
                "'ssh.inherit_proxmox_nodes: true' with 'ssh.defaults:' so "
                "every node is auto-declared. If you meant to run something "
                "*inside* a VM/LXC on that node, use proxmox_run(node=..., "
                "vmid=..., command=...) instead — it goes through QEMU Guest "
                "Agent / pct exec and doesn't need SSH."
            )
        elif identifier.isdigit():
            hint = (
                f" Note: {identifier!r} looks like a VMID. To run a command "
                "inside that guest, prefer proxmox_run(node=..., "
                f"vmid={identifier}, command=...)."
            )
        raise SSHHostResolutionError(
            f"Host {identifier!r} is not declared in ssh.hosts[]. Add an "
            "entry (name, host, user, password or key_file) to enable SSH "
            f"to this target. Declared hosts: {declared}.{hint}"
        )

    def resolve_host(self, identifier: str) -> str:
        """Return the connect-target address for an identifier.

        Helper used by ``ssh_run`` (and its ``wait=False`` / ``exec_id=…``
        polling paths) to surface the resolved IP/hostname in the tool
        response. Prefer :meth:`resolve` when the full host spec (port,
        user, auth) is needed.
        """
        return self.resolve(identifier).host

    async def _get_connection(self, identifier: str) -> asyncssh.SSHClientConnection:
        """Get or create an SSH connection with caching.

        Cache key is the declared host *name*, so distinct declarations
        sharing the same address still get distinct cached connections
        (useful when one address has multiple user accounts).
        """
        host_spec = self.resolve(identifier)
        cache_key = host_spec.name

        if cache_key in _connection_cache:
            conn, created_at = _connection_cache[cache_key]
            if time.time() - created_at < _CONNECTION_TTL:
                try:
                    if not conn.is_closed():
                        return conn
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
            del _connection_cache[cache_key]

        conn = await _connect_to_host(
            host_spec, host_key_policy(self._config, host_spec),
        )
        _connection_cache[cache_key] = (conn, time.time())
        return conn

    async def exec_command(self, host: str, command: str, timeout: int = 60) -> dict[str, Any]:
        """Execute a command via SSH and wait for the result."""
        try:
            conn = await self._get_connection(host)
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.exit_status,
            }
        except asyncio.TimeoutError:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "status": "timeout",
                "error": f"Command timed out after {timeout}s. Use ssh_run(..., wait=False) to start async and poll with ssh_run(exec_id=...).",
            }
        except SSHNotConfiguredError:
            raise
        except Exception as e:
            return {"error": f"SSH connection to '{host}' failed: {e}. Check SSH credentials and host accessibility."}

    async def exec_command_async(self, host: str, command: str) -> str:
        """Start a long-running command and return an exec_id."""
        _prune_ssh_sessions()
        exec_id = str(uuid.uuid4())[:8]
        session = SSHExecSession(exec_id=exec_id, host=host, command=command)
        _ssh_sessions[exec_id] = session

        async def _run() -> None:
            try:
                conn = await self._get_connection(host)
                result = await asyncio.wait_for(conn.run(command, check=False), timeout=600)
                session.stdout = result.stdout or ""
                session.stderr = result.stderr or ""
                session.exit_code = result.exit_status
                session.status = "completed"
            except asyncio.TimeoutError:
                session.status = "timeout"
            except Exception as e:
                session.status = "failed"
                session.stderr = str(e)

        # Keep a reference to the task so it isn't garbage collected mid-run.
        task = asyncio.create_task(_run())
        _ssh_tasks.add(task)
        task.add_done_callback(_ssh_tasks.discard)
        return exec_id

    async def sftp_put(self, host: str, local_path: str, remote_path: str) -> None:
        """Stream a local file to ``remote_path`` on ``host`` via SFTP.

        Used by ``proxmox_upload_file`` for large transfers (asyncssh streams
        in chunks, so RAM usage stays bounded regardless of file size).
        """
        conn = await self._get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)

    async def sftp_get(self, host: str, remote_path: str, local_path: str) -> None:
        """Stream ``remote_path`` from ``host`` into ``local_path`` via SFTP."""
        conn = await self._get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await sftp.get(remote_path, local_path)

    async def sftp_remove(self, host: str, remote_path: str) -> None:
        """Best-effort SFTP unlink, used to clean up staging files on a node."""
        try:
            conn = await self._get_connection(host)
            async with conn.start_sftp_client() as sftp:
                await sftp.remove(remote_path)
        except Exception:
            pass

    @staticmethod
    def get_session(exec_id: str) -> SSHExecSession | None:
        return _ssh_sessions.get(exec_id)

    @staticmethod
    def list_sessions() -> list[dict[str, Any]]:
        return [
            {
                "exec_id": s.exec_id,
                "host": s.host,
                "command": s.command,
                "status": s.status,
                "elapsed_s": round(time.time() - s.started_at),
            }
            for s in _ssh_sessions.values()
        ]
