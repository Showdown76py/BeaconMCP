"""Dell iDRAC backend (stub).

Placeholder so configured iDRAC devices fail with a clear runtime message
instead of an ``ImportError``. Replace with a real Redfish-based backend.
"""

from __future__ import annotations

from ..config import BMCDevice, Config
from .base import _StubBackend


class IDRACStubBackend(_StubBackend):
    type: str = "idrac"
    _message: str = (
        "Dell iDRAC backend is not implemented yet. Contributions welcome — see "
        "src/beaconmcp/bmc/idrac.py."
    )

    def __init__(self, device: BMCDevice, config: Config) -> None:
        super().__init__(device.id)
