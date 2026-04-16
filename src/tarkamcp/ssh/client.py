from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncssh

from ..config import Config


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


class SSHClient:
    """Async SSH client with host resolution and connection caching."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def resolve_host(self, host: str) -> str:
        """Resolve a host identifier to an actual hostname/IP.

        Accepts:
        - Node name (pve1, pve2) -> resolved from config
        - Numeric VMID (101) -> resolved to 192.168.1.{VMID} via convention
        - Direct IP or hostname -> passed through
        """
        # Check if it's a configured node name
        node_host = self._config.get_node_host(host)
        if node_host:
            return node_host

        # Check if it's a numeric VMID
        if host.isdigit():
            return f"192.168.1.{host}"

        # Direct IP/hostname
        return host

    async def _get_connection(self, host: str) -> asyncssh.SSHClientConnection:
        """Get or create an SSH connection with caching."""
        resolved = self.resolve_host(host)

        if resolved in _connection_cache:
            conn, created_at = _connection_cache[resolved]
            if time.time() - created_at < _CONNECTION_TTL:
                try:
                    # Verify connection is still alive
                    if not conn.is_closed():
                        return conn
                except Exception:
                    pass
            # Expired or dead connection
            try:
                conn.close()
            except Exception:
                pass
            del _connection_cache[resolved]

        if not self._config.ssh:
            raise SSHNotConfiguredError()

        conn = await asyncssh.connect(
            resolved,
            username=self._config.ssh.user,
            password=self._config.ssh.password,
            known_hosts=None,  # Accept all host keys (infra is trusted)
        )
        _connection_cache[resolved] = (conn, time.time())
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
                "error": f"Command timed out after {timeout}s. Use ssh_exec_command_async for long-running commands.",
            }
        except SSHNotConfiguredError:
            raise
        except Exception as e:
            return {"error": f"SSH connection to '{host}' failed: {e}. Check SSH credentials and host accessibility."}

    async def exec_command_async(self, host: str, command: str) -> str:
        """Start a long-running command and return an exec_id."""
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
                "elapsed_seconds": round(time.time() - s.started_at),
            }
            for s in _ssh_sessions.values()
        ]


class SSHNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "SSH credentials are not configured. "
            "Set SSH_USER and SSH_PASSWORD in your .env file."
        )
