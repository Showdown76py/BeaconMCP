"""Generic IPMI 2.0 backend, shelling out to the ``ipmitool`` binary.

Requires ``ipmitool`` on the host running BeaconMCP. The backend is
direct-only: if the BMC is not reachable from the server, deploy
BeaconMCP on a host that is. SSH-jump tunneling is not supported here
(PRs welcome).
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import BMCDevice, Config


class GenericIPMIBackend:
    """IPMI 2.0 backend via local ``ipmitool``."""

    type: str = "ipmi"

    def __init__(self, device: BMCDevice, config: Config) -> None:
        self.id = device.id
        self._device = device
        self._config = config

    def _argv(self, *extra: str) -> list[str]:
        return [
            "ipmitool",
            "-H", self._device.host,
            "-U", self._device.user,
            "-P", self._device.password,
            "-I", "lanplus",
            *extra,
        ]

    async def _run(self, *extra: str) -> dict[str, Any]:
        argv = self._argv(*extra)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await proc.communicate()
        except FileNotFoundError:
            return {"error": "ipmitool is not installed on this host."}
        except Exception as exc:
            return {"error": f"ipmitool call failed: {exc}"}

        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return {
                "error": stderr or f"ipmitool exited {proc.returncode}.",
                "stdout": stdout,
            }
        return {"stdout": stdout, "stderr": stderr}

    async def server_info(self) -> dict[str, Any]:
        result = await self._run("fru", "list")
        if "error" in result:
            return result
        return {"fru": result["stdout"]}

    async def health(self) -> dict[str, Any]:
        result = await self._run("sdr", "elist")
        if "error" in result:
            return result
        return {"sensors": result["stdout"]}

    async def power_status(self) -> dict[str, Any]:
        result = await self._run("chassis", "power", "status")
        if "error" in result:
            return result
        text = result["stdout"].lower()
        if "is on" in text:
            state = "on"
        elif "is off" in text:
            state = "off"
        else:
            state = "unknown"
        return {"power_status": state, "raw": result["stdout"]}

    async def power_on(self) -> dict[str, Any]:
        result = await self._run("chassis", "power", "on")
        if "error" in result:
            return result
        return {"action": "power_on", "result": "success", "raw": result["stdout"]}

    async def power_off(self, force: bool = False) -> dict[str, Any]:
        op = "off" if force else "soft"
        result = await self._run("chassis", "power", op)
        if "error" in result:
            return result
        return {
            "action": "power_off",
            "force": force,
            "result": "success",
            "raw": result["stdout"],
        }

    async def power_reset(self) -> dict[str, Any]:
        result = await self._run("chassis", "power", "reset")
        if "error" in result:
            return result
        return {"action": "power_reset", "result": "success", "raw": result["stdout"]}

    async def event_log(self, limit: int = 50) -> dict[str, Any]:
        result = await self._run("sel", "list")
        if "error" in result:
            return result
        lines = [ln for ln in result["stdout"].splitlines() if ln.strip()]
        events = lines[-limit:] if len(lines) > limit else lines
        return {"events": events, "total": len(events)}
