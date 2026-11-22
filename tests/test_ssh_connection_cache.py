"""SSH connection cache: idle expiry, in-use protection, one connect per host."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from beaconmcp.ssh import client as ssh_client
from beaconmcp.ssh.client import SSHClient


class FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


@pytest.fixture
def harness(monkeypatch):
    ssh_client._connection_cache.clear()
    ssh_client._connection_locks.clear()
    conns: list[FakeConn] = []

    async def fake_connect(spec, policy):
        await asyncio.sleep(0.01)  # yield so concurrent callers interleave
        conn = FakeConn()
        conns.append(conn)
        return conn

    monkeypatch.setattr(ssh_client, "_connect_to_host", fake_connect)
    monkeypatch.setattr(ssh_client, "host_key_policy", lambda cfg, spec: None)
    client = SSHClient.__new__(SSHClient)
    client._config = None
    monkeypatch.setattr(client, "resolve", lambda ident: SimpleNamespace(name="h"), raising=False)
    yield client, conns
    ssh_client._connection_cache.clear()
    ssh_client._connection_locks.clear()


def _entry(name: str = "h"):
    return ssh_client._connection_cache[name]


async def test_recently_used_old_connection_is_reused(harness) -> None:
    client, conns = harness
    first = await client._get_connection("h")
    _entry().created_at -= 10 * ssh_client._CONNECTION_TTL  # created long ago
    assert await client._get_connection("h") is first
    assert len(conns) == 1 and not first.closed


async def test_idle_connection_is_replaced_and_closed(harness) -> None:
    client, conns = harness
    first = await client._get_connection("h")
    _entry().last_used -= ssh_client._CONNECTION_TTL + 1
    second = await client._get_connection("h")
    assert second is not first
    assert first.closed and not second.closed
    assert len(conns) == 2


async def test_idle_connection_with_running_command_is_kept(harness) -> None:
    client, conns = harness
    async with client._use_connection("h") as first:
        _entry().last_used -= ssh_client._CONNECTION_TTL + 1
        again = await client._get_connection("h")
        assert again is first and not first.closed
    assert len(conns) == 1
    # Releasing refreshes last_used, so it is still reusable right after.
    assert await client._get_connection("h") is first
    assert _entry().active == 0


async def test_dead_connection_is_replaced(harness) -> None:
    client, conns = harness
    first = await client._get_connection("h")
    first.closed = True  # dropped by the server
    second = await client._get_connection("h")
    assert second is not first and len(conns) == 2


async def test_concurrent_calls_open_a_single_connection(harness) -> None:
    client, conns = harness
    results = await asyncio.gather(*(client._get_connection("h") for _ in range(5)))
    assert len(conns) == 1
    assert all(r is conns[0] for r in results)
