"""clients.json is shared with the ``beaconmcp auth`` CLI.

The CLI runs in its own process while the server is up. The server used to
read the file once at startup, so ``beaconmcp auth revoke`` left the client
authenticating until the next restart -- and, with refresh tokens, able to
mint new bearers indefinitely. These tests pin the on-disk change detection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import ClientStore


def test_cli_revoke_is_seen_by_running_server(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    cid, secret, _ = server.create("laptop")
    assert server.verify(cid, secret)

    cli = ClientStore(path)  # another process
    assert cli.revoke(cid)

    assert server.verify(cid, secret) is False
    assert server.exists(cid) is False
    assert server.get(cid) is None
    assert server.get_name(cid) is None
    assert server.list_clients() == []


def test_cli_create_is_seen_by_running_server(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    assert server.list_clients() == []

    cli = ClientStore(path)
    cid, secret, _ = cli.create("phone")

    assert server.exists(cid)
    assert server.verify(cid, secret)


def test_reload_reports_surviving_clients(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    keep, _, _ = server.create("keep")
    gone, _, _ = server.create("gone")
    seen: list[set[str]] = []
    server.on_clients_removed = seen.append

    ClientStore(path).revoke(gone)
    assert server.exists(keep)
    assert seen == [{keep}]

    # Repeated lookups don't re-fire: the file didn't change again.
    server.exists(keep)
    server.verify(keep, "wrong")
    assert seen == [{keep}]


def test_own_writes_do_not_fire_the_callback(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    seen: list[set[str]] = []
    server.on_clients_removed = seen.append
    a, _, _ = server.create("a")
    b, _, _ = server.create("b")
    assert server.revoke(a)
    assert server.exists(b)
    assert seen == []


def test_cli_create_only_fires_when_clients_disappear(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    server.create("a")
    seen: list[set[str]] = []
    server.on_clients_removed = seen.append
    ClientStore(path).create("b")
    assert len(server.list_clients()) == 2
    assert seen == []


def test_server_save_does_not_clobber_a_cli_change(tmp_path: Path) -> None:
    # Before the reload, a server-side save (DCR provisioning) rewrote the
    # file from the startup snapshot and silently dropped CLI-created rows.
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    owner, _, _ = server.create("owner")
    second, _, _ = ClientStore(path).create("second")

    derived, _ = server.create_dynamic(
        owner_client_id=owner, name="chatgpt", registration_source="chatgpt:x",
    )

    fresh = ClientStore(path)
    assert fresh.exists(owner)
    assert fresh.exists(second)
    assert fresh.exists(derived)


def test_broken_file_keeps_the_current_clients(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    cid, secret, _ = server.create("laptop")

    path.write_text("{not json")
    assert server.verify(cid, secret)
    assert server.exists(cid)

    # Once the file is readable again its content wins.
    path.write_text('{"clients": []}')
    assert server.exists(cid) is False


def test_missing_file_keeps_the_current_clients(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    cid, secret, _ = server.create("laptop")

    path.unlink()
    assert server.verify(cid, secret)

    # The file coming back (restored deploy, CLI create) is picked up.
    ClientStore(path).create("other")
    assert server.exists(cid) is False
    assert len(server.list_clients()) == 1


def test_file_created_after_startup_is_picked_up(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    server = ClientStore(path)
    assert server.list_clients() == []
    cid, secret, _ = ClientStore(path).create("first")
    assert server.verify(cid, secret)
