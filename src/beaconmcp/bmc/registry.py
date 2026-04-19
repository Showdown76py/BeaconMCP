"""Instantiate one BMC backend per configured device."""

from __future__ import annotations

from ..config import Config
from .base import BMCClient
from .hp_ilo import HPILOBackend
from .ipmi import GenericIPMIBackend
from .redfish import RedfishBackend


_BACKENDS: dict[str, type] = {
    "hp_ilo": HPILOBackend,
    "ipmi": GenericIPMIBackend,
    "idrac": RedfishBackend,
    "supermicro": RedfishBackend,
    "redfish": RedfishBackend,
}


def build_registry(config: Config) -> dict[str, BMCClient]:
    """Return ``{device_id: BMCClient}`` for every configured BMC device.

    Raises :class:`ValueError` at startup for unknown backend types — this
    surfaces misconfiguration immediately rather than when a tool is first
    invoked.
    """
    registry: dict[str, BMCClient] = {}
    for device in config.bmc_devices:
        backend_cls = _BACKENDS.get(device.type)
        if backend_cls is None:
            supported = ", ".join(sorted(_BACKENDS))
            raise ValueError(
                f"Unknown BMC type {device.type!r} for device {device.id!r}. "
                f"Supported types: {supported}."
            )
        registry[device.id] = backend_cls(device, config)
    return registry
