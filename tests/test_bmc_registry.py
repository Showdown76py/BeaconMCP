"""Unit tests for the BMC registry."""

from __future__ import annotations

import pytest

from beaconmcp.bmc import build_registry
from beaconmcp.bmc.hp_ilo import HPILOBackend
from beaconmcp.bmc.idrac import IDRACStubBackend
from beaconmcp.bmc.ipmi import GenericIPMIBackend
from beaconmcp.bmc.supermicro import SupermicroStubBackend
from beaconmcp.config import (
    BMCDevice,
    Config,
    FeaturesConfig,
    PVENode,
    ServerConfig,
)


def _make_config(devices: list[BMCDevice]) -> Config:
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
        bmc_devices=devices,
        ssh=None,
        features=FeaturesConfig(),
        verify_ssl=False,
        infrastructure={},
    )


def test_empty_registry() -> None:
    registry = build_registry(_make_config([]))
    assert registry == {}


def test_single_hp_ilo_device() -> None:
    dev = BMCDevice(id="rack1-ilo", type="hp_ilo", host="10.0.0.10", user="admin", password="pw")
    registry = build_registry(_make_config([dev]))
    assert list(registry.keys()) == ["rack1-ilo"]
    assert isinstance(registry["rack1-ilo"], HPILOBackend)
    assert registry["rack1-ilo"].type == "hp_ilo"


def test_multiple_mixed_devices() -> None:
    cfg = _make_config(
        [
            BMCDevice(id="ilo", type="hp_ilo", host="10.0.0.10", user="a", password="x"),
            BMCDevice(id="ipmi", type="ipmi", host="10.0.0.11", user="a", password="y"),
            BMCDevice(id="dell", type="idrac", host="10.0.0.12", user="a", password="z"),
            BMCDevice(id="smci", type="supermicro", host="10.0.0.13", user="a", password="w"),
        ]
    )
    registry = build_registry(cfg)

    assert set(registry.keys()) == {"ilo", "ipmi", "dell", "smci"}
    assert isinstance(registry["ilo"], HPILOBackend)
    assert isinstance(registry["ipmi"], GenericIPMIBackend)
    assert isinstance(registry["dell"], IDRACStubBackend)
    assert isinstance(registry["smci"], SupermicroStubBackend)


def test_unknown_type_raises_at_startup() -> None:
    cfg = _make_config(
        [BMCDevice(id="x", type="nope", host="10.0.0.10", user="a", password="b")]
    )
    with pytest.raises(ValueError, match="Unknown BMC type 'nope'"):
        build_registry(cfg)


@pytest.mark.asyncio
async def test_stub_backend_returns_error() -> None:
    cfg = _make_config(
        [BMCDevice(id="dell", type="idrac", host="10.0.0.12", user="a", password="z")]
    )
    registry = build_registry(cfg)
    result = await registry["dell"].power_status()
    assert "error" in result
    assert "iDRAC" in result["error"]
