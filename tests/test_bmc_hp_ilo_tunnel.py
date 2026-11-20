"""The iLO SSH tunnel must only listen on the loopback interface."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beaconmcp.bmc.hp_ilo import HPILOBackend
from beaconmcp.config import BMCDevice, SSHHost


def _backend() -> tuple[HPILOBackend, MagicMock]:
    device = BMCDevice(
        id="ilo1",
        type="hp_ilo",
        host="10.0.0.10",
        user="admin",
        password="pw",
        jump_host="pve1",
    )
    config = MagicMock()
    config.get_ssh_host.return_value = SSHHost(
        name="pve1", host="pve1.example.com", user="root", password="x"
    )
    listener = MagicMock()
    listener.get_port.return_value = 45678
    conn = MagicMock()
    conn.forward_local_port = AsyncMock(return_value=listener)
    conn.is_closed.return_value = False
    return HPILOBackend(device, config), conn


@pytest.mark.asyncio
async def test_tunnel_listens_on_loopback_only() -> None:
    backend, conn = _backend()
    with (
        patch("beaconmcp.bmc.hp_ilo._connect_to_host", AsyncMock(return_value=conn)),
        patch("beaconmcp.bmc.hp_ilo.host_key_policy", MagicMock()),
    ):
        host, port = await backend._resolve_endpoint()

    conn.forward_local_port.assert_awaited_once_with("127.0.0.1", 0, "10.0.0.10", 443)
    assert (host, port) == ("127.0.0.1", 45678)
