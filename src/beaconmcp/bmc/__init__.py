"""Baseboard Management Controller (BMC) abstraction.

One common interface, several hardware backends (HP iLO, generic IPMI,
Dell iDRAC, Supermicro). Each entry in ``bmc.devices`` in the config
instantiates one backend; the :func:`bmc.registry.build_registry`
function materializes all of them, and :func:`bmc.tools.register_bmc_tools`
exposes a fixed set of MCP tools that dispatch to the right backend by
``device_id``.
"""

from .base import BMCClient, BMCNotConfiguredError, BMCTunnelError
from .registry import build_registry
from .tools import register_bmc_tools

__all__ = [
    "BMCClient",
    "BMCNotConfiguredError",
    "BMCTunnelError",
    "build_registry",
    "register_bmc_tools",
]
