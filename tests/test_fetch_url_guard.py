"""proxmox_fetch_url guard: addresses owned by the BeaconMCP host are refused."""
from __future__ import annotations

import socket

import pytest

from beaconmcp.proxmox import system


def host_lan_ip() -> str | None:
    """A non-loopback IPv4 address this machine really owns, if any."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # UDP connect: nothing is sent
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def _owned(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_host_owned_non_loopback_ip_is_local() -> None:
    ip = host_lan_ip()
    if ip is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    assert system._is_local_address(ip)


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1"])
def test_loopback_stays_local(addr: str) -> None:
    assert system._is_local_address(addr)


def test_unowned_documentation_address_is_not_local() -> None:
    assert not system._is_local_address("192.0.2.10")


def test_unowned_rfc1918_stays_allowed() -> None:
    if _owned("10.255.255.254"):
        pytest.skip("this machine owns 10.255.255.254")
    assert not system._is_local_address("10.255.255.254")
