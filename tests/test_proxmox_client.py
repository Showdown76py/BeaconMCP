"""Tests for :class:`ProxmoxClient`'s connection cache.

A ProxmoxAPI wraps a requests.Session, which is not thread-safe. Sync Proxmox
tools now run on a worker-thread pool (see ``server._metric_tool``), so the
cache is per-thread: every thread gets its own connection object, and no two
threads are ever handed the same one.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import beaconmcp.proxmox.client as client_mod
from beaconmcp.proxmox.client import NodeNotFoundError, ProxmoxClient


class _FakeNode:
    def __init__(self, name: str) -> None:
        self.name = name
        self.host = f"{name}.example.com"
        self.token_id = "root@pam!mytoken"
        self.token_secret = "secret"


class _FakeConfig:
    """Minimal stand-in exposing only what ProxmoxClient touches."""

    def __init__(self, node_names: list[str]) -> None:
        self.pve_nodes = [_FakeNode(n) for n in node_names]
        self.verify_ssl = False

    def get_node(self, name: str) -> _FakeNode | None:
        for n in self.pve_nodes:
            if n.name == name:
                return n
        return None


@pytest.fixture()
def fake_proxmox_api(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace ``ProxmoxAPI`` with a counting dummy.

    Returns the list of constructed dummies so a test can assert how many
    connections were built.
    """
    built: list[object] = []
    build_lock = threading.Lock()

    class _DummyAPI:
        def __init__(self, host: str, **kwargs: object) -> None:
            self.host = host
            self.kwargs = kwargs
            with build_lock:
                built.append(self)

    monkeypatch.setattr(client_mod, "ProxmoxAPI", _DummyAPI)
    return built


def test_get_connection_caches_within_a_thread(fake_proxmox_api: list[object]) -> None:
    c = ProxmoxClient(_FakeConfig(["pve1"]))
    first = c._get_connection("pve1")
    second = c._get_connection("pve1")
    assert first is second
    assert len(fake_proxmox_api) == 1


def test_get_connection_unknown_node_raises(fake_proxmox_api: list[object]) -> None:
    c = ProxmoxClient(_FakeConfig(["pve1"]))
    with pytest.raises(NodeNotFoundError):
        c._get_connection("nope")


def test_each_thread_gets_its_own_connection(fake_proxmox_api: list[object]) -> None:
    """The point of the thread-local cache: no two threads share a ProxmoxAPI,
    and therefore never share a requests.Session."""
    c = ProxmoxClient(_FakeConfig(["pve1"]))

    n_threads = 20
    barrier = threading.Barrier(n_threads)
    results: list[object] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            conn = c._get_connection("pve1")
            # Within one thread the cache must still hold.
            assert c._get_connection("pve1") is conn
            with lock:
                results.append(conn)
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors}"
    assert len({id(r) for r in results}) == n_threads
    assert len(fake_proxmox_api) == n_threads


def test_concurrent_get_connection_many_nodes(fake_proxmox_api: list[object]) -> None:
    """Threads hammering several distinct nodes always get the connection for
    the node they asked for."""
    node_names = [f"pve{i}" for i in range(8)]
    c = ProxmoxClient(_FakeConfig(node_names))

    barrier = threading.Barrier(len(node_names) * 4)
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait()
        try:
            for _ in range(5):
                assert c._get_connection(name).host == f"{name}.example.com"
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(name,))
        for name in node_names
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors}"


class _DeadEndpoint:
    """Stands in for ``conn.nodes``; fails the way a dropped socket does."""

    def get(self, **_kwargs: object) -> None:
        raise RequestsConnectionError("socket died")


def test_transient_error_evicts_only_the_calling_thread(
    fake_proxmox_api: list[object],
) -> None:
    """A retry drops the failing thread's socket without yanking the
    connection out from under another thread that may be mid-request on it."""
    c = ProxmoxClient(_FakeConfig(["pve1"]))
    primed = threading.Event()
    main_done = threading.Event()
    seen: list[object] = []

    def worker() -> None:
        seen.append(c._get_connection("pve1"))
        primed.set()
        main_done.wait(timeout=10)
        seen.append(c._get_connection("pve1"))

    t = threading.Thread(target=worker)
    t.start()
    assert primed.wait(timeout=10)

    c._get_connection("pve1").nodes = _DeadEndpoint()  # type: ignore[attr-defined]
    assert "error" in c.api_call("pve1", "get", "nodes")

    main_done.set()
    t.join(timeout=10)

    # The worker's connection survived the main thread's eviction.
    assert len(seen) == 2
    assert seen[1] is seen[0]
