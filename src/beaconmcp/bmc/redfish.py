from __future__ import annotations

import asyncio
from typing import Any

from ..config import BMCDevice, Config
from .base import BMCClient


class RedfishBackend(BMCClient):
    """Universal DMTF Redfish REST API backend.

    Supports Dell iDRAC (14G+), Supermicro (X11+), and modern HPE iLO.
    Communicates via HTTPS to /redfish/v1/ and uses basic auth.
    """

    type = "redfish"

    def __init__(self, device: BMCDevice, config: Config) -> None:
        self.device = device
        self.id = device.id
        self._url = f"https://{device.host}"
        self._auth = (device.user, device.password)
        self._verify = device.verify_tls  # BMC certificates are usually self-signed

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an async HTTP request to the Redfish API."""
        import httpx
        
        url = f"{self._url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(verify=self._verify) as client:
                response = await client.request(
                    method,
                    url,
                    auth=self._auth,
                    json=json,
                    timeout=15.0
                )
                response.raise_for_status()
                return response.json() if response.content else {}
        except Exception as e:
            return {"error": f"Redfish API request failed: {e}"}

    async def _get_system_path(self) -> str | None:
        """Discover the URI of the primary computer system."""
        # Standard Redfish path
        res = await self._request("GET", "/redfish/v1/Systems")
        if "error" in res:
            return None
        members = res.get("Members", [])
        if not members:
            return None
        return members[0].get("@odata.id")

    async def server_info(self) -> dict[str, Any]:
        system_path = await self._get_system_path()
        if not system_path:
            return {"error": "Failed to discover Redfish System path."}
            
        res = await self._request("GET", system_path)
        if "error" in res:
            return res
            
        return {
            "model": res.get("Model", "Unknown"),
            "manufacturer": res.get("Manufacturer", "Unknown"),
            "serial": res.get("SerialNumber", "Unknown"),
            "bios_version": res.get("BiosVersion", "Unknown"),
            "state": res.get("Status", {}).get("State", "Unknown"),
            "health": res.get("Status", {}).get("Health", "Unknown")
        }

    async def health(self) -> dict[str, Any]:
        # A simple health aggregation across standard endpoints
        chassis_res = await self._request("GET", "/redfish/v1/Chassis")
        if "error" in chassis_res:
            return chassis_res
            
        chassis_members = chassis_res.get("Members", [])
        if not chassis_members:
            return {"error": "No chassis found."}
            
        chassis_path = chassis_members[0].get("@odata.id")
        
        # We query the thermal and power endpoints
        thermal = await self._request("GET", f"{chassis_path}/Thermal")
        power = await self._request("GET", f"{chassis_path}/Power")
        
        fans = []
        temps = []
        power_supplies = []
        
        if "error" not in thermal:
            for f in thermal.get("Fans", []):
                fans.append({
                    "name": f.get("Name"),
                    "reading": f.get("Reading"),
                    "health": f.get("Status", {}).get("Health")
                })
            for t in thermal.get("Temperatures", []):
                temps.append({
                    "name": t.get("Name"),
                    "reading_celsius": t.get("ReadingCelsius"),
                    "health": t.get("Status", {}).get("Health")
                })
                
        if "error" not in power:
            for p in power.get("PowerSupplies", []):
                power_supplies.append({
                    "name": p.get("Name"),
                    "power_capacity_watts": p.get("PowerCapacityWatts"),
                    "health": p.get("Status", {}).get("Health")
                })
                
        return {
            "fans": fans,
            "temperatures": temps,
            "power_supplies": power_supplies,
            "overall_health": (await self.server_info()).get("health")
        }

    async def power_status(self) -> dict[str, Any]:
        system_path = await self._get_system_path()
        if not system_path:
            return {"error": "Failed to discover Redfish System path."}
            
        res = await self._request("GET", system_path)
        if "error" in res:
            return res
            
        return {"power_state": res.get("PowerState", "Unknown").lower()}

    async def _send_power_action(self, reset_type: str) -> dict[str, Any]:
        system_path = await self._get_system_path()
        if not system_path:
            return {"error": "Failed to discover Redfish System path."}
            
        action_path = f"{system_path}/Actions/ComputerSystem.Reset"
        payload = {"ResetType": reset_type}
        
        res = await self._request("POST", action_path, json=payload)
        if "error" in res:
            return res
            
        return {"status": "success", "action": reset_type}

    async def power_on(self) -> dict[str, Any]:
        return await self._send_power_action("On")

    async def power_off(self, force: bool = False) -> dict[str, Any]:
        return await self._send_power_action("ForceOff" if force else "GracefulShutdown")

    async def power_reset(self) -> dict[str, Any]:
        return await self._send_power_action("ForceRestart")

    async def event_log(self, limit: int = 50) -> dict[str, Any]:
        res = await self._request("GET", "/redfish/v1/Managers")
        if "error" in res:
            return res
            
        managers = res.get("Members", [])
        if not managers:
            return {"error": "No managers found."}
            
        manager_path = managers[0].get("@odata.id")
        
        # Discover LogServices dynamically instead of hardcoding Log1
        services_res = await self._request("GET", f"{manager_path}/LogServices")
        if "error" in services_res:
            return {"error": f"Failed to fetch LogServices: {services_res['error']}"}
            
        log_services = services_res.get("Members", [])
        if not log_services:
            return {"error": "No log services found for manager."}
            
        # Take the first available log service (e.g. SEL, IML, Log1)
        log_service_path = log_services[0].get("@odata.id")
        logs_path = f"{log_service_path}/Entries"
        
        logs_res = await self._request("GET", logs_path)
        if "error" in logs_res:
            return logs_res
            
        entries = logs_res.get("Members", [])
        
        parsed = []
        for e in entries[:limit]:
            parsed.append({
                "id": e.get("Id"),
                "severity": e.get("Severity"),
                "created": e.get("Created"),
                "message": e.get("Message")
            })
            
        return {"logs": parsed}
