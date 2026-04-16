from __future__ import annotations

import asyncio
from typing import Any

import asyncssh
import hpilo

from ..config import Config


_tunnel: asyncssh.SSHClientConnection | None = None
_tunnel_listener: Any = None
_tunnel_local_port: int | None = None


class ILOClient:
    """HP iLO 4 client that connects through an SSH tunnel via a Proxmox node."""

    def __init__(self, config: Config) -> None:
        self._config = config

    async def _ensure_tunnel(self) -> int:
        """Ensure the SSH tunnel to iLO is up and return the local port."""
        global _tunnel, _tunnel_listener, _tunnel_local_port

        if _tunnel is not None and not _tunnel.is_closed() and _tunnel_local_port is not None:
            return _tunnel_local_port

        # Clean up old tunnel
        if _tunnel_listener is not None:
            _tunnel_listener.close()
        if _tunnel is not None:
            _tunnel.close()

        ilo_cfg = self._config.ilo
        if not ilo_cfg:
            raise ILONotConfiguredError()

        ssh_cfg = self._config.ssh
        if not ssh_cfg:
            raise ILOTunnelError(
                "SSH credentials are required to tunnel to iLO. "
                "Set SSH_USER and SSH_PASSWORD in your .env file."
            )

        # Get jump host details
        jump_host = self._config.get_node_host(ilo_cfg.jump_host)
        if not jump_host:
            raise ILOTunnelError(
                f"Jump host '{ilo_cfg.jump_host}' is not configured as a Proxmox node. "
                f"Check ILO_JUMP_HOST in your .env file."
            )

        try:
            _tunnel = await asyncssh.connect(
                jump_host,
                username=ssh_cfg.user,
                password=ssh_cfg.password,
                known_hosts=None,
            )

            # Forward local port to iLO's HTTPS port (443)
            _tunnel_listener = await _tunnel.forward_local_port(
                "", 0,  # Bind to random available port
                ilo_cfg.host, 443,
            )
            _tunnel_local_port = _tunnel_listener.get_port()
            return _tunnel_local_port

        except Exception as e:
            _tunnel = None
            _tunnel_listener = None
            _tunnel_local_port = None
            raise ILOTunnelError(
                f"Failed to create SSH tunnel to iLO through '{ilo_cfg.jump_host}' ({jump_host}): {e}. "
                f"Check that {ilo_cfg.jump_host} is reachable with proxmox_list_nodes first."
            ) from e

    async def _call_ilo(self, method: str, **kwargs: Any) -> Any:
        """Call an hpilo method through the SSH tunnel.

        python-hpilo is synchronous, so we run it in a thread executor.
        """
        local_port = await self._ensure_tunnel()
        ilo_cfg = self._config.ilo
        if not ilo_cfg:
            raise ILONotConfiguredError()

        def _sync_call() -> Any:
            ilo = hpilo.Ilo(
                "localhost",
                port=local_port,
                login=ilo_cfg.user,
                password=ilo_cfg.password,
                ssl_context=None,  # Disable SSL verification for tunneled connection
            )
            return getattr(ilo, method)(**kwargs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_call)

    async def get_server_info(self) -> dict[str, Any]:
        try:
            product = await self._call_ilo("get_product_name")
            serial = await self._call_ilo("get_server_name")
            fw = await self._call_ilo("get_fw_version")
            return {
                "product_name": product,
                "server_name": serial,
                "firmware": fw,
            }
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to get server info from iLO: {e}"}

    async def get_health(self) -> dict[str, Any]:
        try:
            health = await self._call_ilo("get_embedded_health")
            return health
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to get health data from iLO: {e}"}

    async def get_power_status(self) -> dict[str, Any]:
        try:
            status = await self._call_ilo("get_host_power_status")
            return {"power_status": status}
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to get power status from iLO: {e}"}

    async def power_on(self) -> dict[str, Any]:
        try:
            await self._call_ilo("set_host_power", host_power=True)
            return {"action": "power_on", "result": "success"}
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to power on via iLO: {e}"}

    async def power_off(self, force: bool = False) -> dict[str, Any]:
        try:
            if force:
                await self._call_ilo("set_host_power", host_power=False)
            else:
                await self._call_ilo("press_pwr_btn")
            return {"action": "power_off", "force": force, "result": "success"}
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to power off via iLO: {e}"}

    async def power_reset(self) -> dict[str, Any]:
        try:
            await self._call_ilo("reset_server")
            return {"action": "power_reset", "result": "success"}
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to reset server via iLO: {e}"}

    async def get_event_log(self, limit: int = 50) -> dict[str, Any]:
        try:
            log = await self._call_ilo("get_ilo_event_log")
            if isinstance(log, list):
                log = log[:limit]
            return {"events": log, "total": len(log) if isinstance(log, list) else 0}
        except (ILONotConfiguredError, ILOTunnelError):
            raise
        except Exception as e:
            return {"error": f"Failed to get event log from iLO: {e}"}


class ILONotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "iLO credentials are not configured. "
            "Set ILO_HOST, ILO_USER, and ILO_PASSWORD in your .env file."
        )


class ILOTunnelError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
