"""Unit tests for the generic IPMI backend (mocked subprocess)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from beaconmcp.bmc.ipmi import GenericIPMIBackend
from beaconmcp.config import (
    BMCDevice,
    Config,
    FeaturesConfig,
    PVENode,
    ServerConfig,
)


def _cfg() -> Config:
    return Config(
        server=ServerConfig(),
        pve_nodes=[
            PVENode(
                name="pve1",
                host="pve1.example.com",
                token_id="root@pam!beaconmcp",
                token_secret="x",
            )
        ],
        bmc_devices=[],
        ssh=None,
        features=FeaturesConfig(),
        verify_ssl=False,
        infrastructure={},
    )


def _backend() -> GenericIPMIBackend:
    device = BMCDevice(
        id="rack1-ipmi",
        type="ipmi",
        host="10.0.0.21",
        user="admin",
        password="pw",
    )
    return GenericIPMIBackend(device, _cfg())


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", rc: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = rc

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_power_on_calls_ipmitool_with_correct_argv() -> None:
    backend = _backend()
    fake_proc = _FakeProc(b"Chassis Power Control: Up/On\n")
    mock = AsyncMock(return_value=fake_proc)

    with patch("asyncio.create_subprocess_exec", mock):
        result = await backend.power_on()

    mock.assert_awaited_once()
    assert mock.await_args is not None
    argv = mock.await_args.args
    assert argv[0] == "ipmitool"
    assert "-H" in argv and "10.0.0.21" in argv
    assert "-U" in argv and "admin" in argv
    assert "-P" in argv and "pw" in argv
    assert argv[-3:] == ("chassis", "power", "on")
    assert result["action"] == "power_on"
    assert result["result"] == "success"


@pytest.mark.asyncio
async def test_power_status_parses_on_off() -> None:
    backend = _backend()
    fake_proc = _FakeProc(b"Chassis Power is on\n")
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    ):
        result = await backend.power_status()
    assert result["power_status"] == "on"

    fake_proc = _FakeProc(b"Chassis Power is off\n")
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    ):
        result = await backend.power_status()
    assert result["power_status"] == "off"


@pytest.mark.asyncio
async def test_missing_ipmitool_binary_returns_error() -> None:
    backend = _backend()
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError()),
    ):
        result = await backend.power_on()
    assert "error" in result
    assert "ipmitool" in result["error"]


@pytest.mark.asyncio
async def test_event_log_limits_output() -> None:
    backend = _backend()
    lines = "\n".join(f"event {i}" for i in range(60)).encode()
    fake_proc = _FakeProc(lines)
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    ):
        result = await backend.event_log(limit=10)
    assert result["total"] == 10
    assert result["events"][0] == "event 50"
    assert result["events"][-1] == "event 59"
