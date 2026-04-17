"""Protocol and shared exceptions for BMC backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BMCClient(Protocol):
    """Async interface implemented by every BMC backend.

    All action methods return a JSON-safe ``dict``. Backends signal
    per-operation failures with ``{"error": "..."}`` rather than raising,
    so downstream tool wrappers can forward the message verbatim. The two
    exceptions below are reserved for setup-time failures (unknown device
    id, broken SSH tunnel) and are caught by the tool wrappers.
    """

    id: str
    type: str

    async def server_info(self) -> dict[str, Any]: ...
    async def health(self) -> dict[str, Any]: ...
    async def power_status(self) -> dict[str, Any]: ...
    async def power_on(self) -> dict[str, Any]: ...
    async def power_off(self, force: bool = False) -> dict[str, Any]: ...
    async def power_reset(self) -> dict[str, Any]: ...
    async def event_log(self, limit: int = 50) -> dict[str, Any]: ...


class BMCNotConfiguredError(Exception):
    """Raised when a requested BMC device id is not in the registry."""


class BMCTunnelError(Exception):
    """Raised when the SSH jump tunnel to a BMC cannot be established."""


class _StubBackend:
    """Shared base for backends that exist only as placeholders for now.

    Every action returns ``{"error": ...}`` so users of unsupported backends
    get a clear runtime message instead of an ``ImportError`` at startup.
    """

    type: str = "stub"
    _message: str = "This BMC backend is not yet implemented."

    def __init__(self, device_id: str) -> None:
        self.id = device_id

    def _stub(self) -> dict[str, Any]:
        return {"error": self._message}

    async def server_info(self) -> dict[str, Any]:
        return self._stub()

    async def health(self) -> dict[str, Any]:
        return self._stub()

    async def power_status(self) -> dict[str, Any]:
        return self._stub()

    async def power_on(self) -> dict[str, Any]:
        return self._stub()

    async def power_off(self, force: bool = False) -> dict[str, Any]:
        return self._stub()

    async def power_reset(self) -> dict[str, Any]:
        return self._stub()

    async def event_log(self, limit: int = 50) -> dict[str, Any]:
        return self._stub()
