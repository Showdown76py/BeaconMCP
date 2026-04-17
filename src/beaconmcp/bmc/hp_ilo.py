"""HP iLO 4/5 backend.

Connects to the iLO web interface with RIBCL (via the ``python-hpilo``
library). If ``jump_host`` is set on the device, the call goes through
an SSH port-forward opened on the referenced ``ssh.hosts[]`` entry;
otherwise the iLO is contacted directly on port 443.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncssh
import hpilo

from ..config import BMCDevice, Config
from ..ssh.client import _connect_to_host
from .base import BMCTunnelError


class HPILOBackend:
    """HP iLO 4/5 backend with an optional SSH jump tunnel."""

    type: str = "hp_ilo"

    def __init__(self, device: BMCDevice, config: Config) -> None:
        self.id = device.id
        self._device = device
        self._config = config
        # Per-instance so multiple HP iLO devices can coexist.
        self._tunnel: asyncssh.SSHClientConnection | None = None
        self._tunnel_listener: Any = None
        self._tunnel_local_port: int | None = None

    async def _resolve_endpoint(self) -> tuple[str, int]:
        """Return the (host, port) python-hpilo should talk to.

        If the device has no ``jump_host``, returns the BMC's own address.
        If it does, opens (and caches) an SSH port-forward through the
        named ``ssh.hosts[]`` entry and returns the local forward endpoint.
        """
        jump_host = self._device.jump_host
        if not jump_host:
            return self._device.host, 443

        if (
            self._tunnel is not None
            and not self._tunnel.is_closed()
            and self._tunnel_local_port is not None
        ):
            return "127.0.0.1", self._tunnel_local_port

        if self._tunnel_listener is not None:
            self._tunnel_listener.close()
        if self._tunnel is not None:
            self._tunnel.close()

        jump_spec = self._config.get_ssh_host(jump_host)
        if jump_spec is None:
            raise BMCTunnelError(
                f"BMC device {self.id!r} references jump_host={jump_host!r}, "
                "but that name is not declared under ssh.hosts[] in "
                "beaconmcp.yaml. Add an ssh.hosts entry (name, host, user, "
                "password or key_file) so the tunnel can be opened."
            )

        try:
            self._tunnel = await _connect_to_host(jump_spec)
            self._tunnel_listener = await self._tunnel.forward_local_port(
                "", 0, self._device.host, 443
            )
            local_port = int(self._tunnel_listener.get_port())
            self._tunnel_local_port = local_port
            return "127.0.0.1", local_port
        except Exception as exc:
            self._tunnel = None
            self._tunnel_listener = None
            self._tunnel_local_port = None
            raise BMCTunnelError(
                f"Failed to open SSH tunnel to BMC {self.id!r} via "
                f"jump_host {jump_host!r} ({jump_spec.host}): {exc}. "
                "Verify the jump host is reachable."
            ) from exc

    async def _call(self, method: str, **kwargs: Any) -> Any:
        host, port = await self._resolve_endpoint()

        def _sync_call() -> Any:
            # IMPORTANT: python-hpilo treats the literal string "localhost" as
            # a signal to shell out to the local `hponcfg` utility (ILO_LOCAL
            # mode) and ignores host/port/credentials. Use the loopback IP
            # string so RIBCL is sent over our tunnel instead.
            ilo = hpilo.Ilo(
                host,
                port=port,
                login=self._device.user,
                password=self._device.password,
                ssl_context=None,
            )
            return getattr(ilo, method)(**kwargs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_call)

    async def server_info(self) -> dict[str, Any]:
        try:
            product = await self._call("get_product_name")
            serial = await self._call("get_server_name")
            fw = await self._call("get_fw_version")
            return {"product_name": product, "server_name": serial, "firmware": fw}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"server_info via iLO failed: {exc}"}

    async def health(self) -> dict[str, Any]:
        try:
            return await self._call("get_embedded_health")
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"health via iLO failed: {exc}"}

    async def power_status(self) -> dict[str, Any]:
        try:
            status = await self._call("get_host_power_status")
            return {"power_status": status}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"power_status via iLO failed: {exc}"}

    async def power_on(self) -> dict[str, Any]:
        try:
            await self._call("set_host_power", host_power=True)
            return {"action": "power_on", "result": "success"}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"power_on via iLO failed: {exc}"}

    async def power_off(self, force: bool = False) -> dict[str, Any]:
        try:
            if force:
                await self._call("set_host_power", host_power=False)
            else:
                await self._call("press_pwr_btn")
            return {"action": "power_off", "force": force, "result": "success"}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"power_off via iLO failed: {exc}"}

    async def power_reset(self) -> dict[str, Any]:
        try:
            await self._call("reset_server")
            return {"action": "power_reset", "result": "success"}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"power_reset via iLO failed: {exc}"}

    async def event_log(self, limit: int = 50) -> dict[str, Any]:
        try:
            log = await self._call("get_ilo_event_log")
            if isinstance(log, list):
                log = log[:limit]
            return {"events": log, "total": len(log) if isinstance(log, list) else 0}
        except BMCTunnelError:
            raise
        except Exception as exc:
            return {"error": f"event_log via iLO failed: {exc}"}
