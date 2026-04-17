"""Supermicro BMC backend (stub).

Placeholder so configured Supermicro devices fail with a clear runtime
message instead of an ``ImportError``. Replace with a real Redfish- or
IPMI-based backend.
"""

from __future__ import annotations

from ..config import BMCDevice, Config
from .base import _StubBackend


class SupermicroStubBackend(_StubBackend):
    type: str = "supermicro"
    _message: str = (
        "Supermicro BMC backend is not implemented yet. Contributions welcome — "
        "see src/beaconmcp/bmc/supermicro.py."
    )

    def __init__(self, device: BMCDevice, config: Config) -> None:
        super().__init__(device.id)
