"""Human-session state: client capabilities and pending re-verifications."""

from __future__ import annotations

import asyncio
import time

from beaconmcp.auth import AccessToken
from beaconmcp.reauth import (
    ClientSessionInfo,
    PendingReverifyStore,
    session_lapsed,
)


def _pending_kwargs(thash: str = "h1") -> dict:
    return dict(
        token_hash=thash, client_id="c1", tool="proxmox_vm_start",
        args={"node": "pve-02", "vmid": 104}, ip="82.64.12.7",
        user_agent="claude-code/2.1", client_label="Claude Code",
        bearer_issued_at=time.time() - 3 * 86400,
        last_verified_at=time.time() - 90000,
    )


def test_from_initialize_empty_elicitation_is_form_only() -> None:
    info = ClientSessionInfo.from_initialize(
        {"capabilities": {"elicitation": {}}, "clientInfo": {"name": "x"}},
        user_agent="ua", ip="1.2.3.4",
    )
    assert info.elicit_form and not info.elicit_url
    assert info.name == "x" and info.user_agent == "ua" and info.ip == "1.2.3.4"


def test_from_initialize_url_and_form() -> None:
    info = ClientSessionInfo.from_initialize(
        {
            "capabilities": {"elicitation": {"form": {}, "url": {}}},
            "clientInfo": {
                "name": "claude-code", "title": "Claude Code", "version": "2.1.3",
                "websiteUrl": "https://claude.com",
                "icons": [{"src": "https://claude.com/i.png"}, {"nosrc": 1}],
            },
        },
        user_agent="", ip="",
    )
    assert info.elicit_form and info.elicit_url
    assert info.title == "Claude Code" and info.version == "2.1.3"
    assert info.website_url == "https://claude.com"
    assert info.icons == ["https://claude.com/i.png"]


def test_from_initialize_no_elicitation_and_garbage() -> None:
    info = ClientSessionInfo.from_initialize(
        {"capabilities": "nope", "clientInfo": None}, user_agent="", ip="",
    )
    assert not info.elicit_form and not info.elicit_url and info.name == ""


def test_url_only_client() -> None:
    info = ClientSessionInfo.from_initialize(
        {"capabilities": {"elicitation": {"url": {}}}}, user_agent="", ip="",
    )
    assert info.elicit_url and not info.elicit_form


def test_session_lapsed_only_for_interactive() -> None:
    now = time.time()
    fresh = AccessToken(token="", client_id="c", expires_at=now + 99,
                        interactive=True, verified_at=now - 10)
    old = AccessToken(token="", client_id="c", expires_at=now + 99,
                      interactive=True, verified_at=now - 100)
    plain = AccessToken(token="t", client_id="c", expires_at=now + 99)
    assert not session_lapsed(fresh, 60, now)
    assert session_lapsed(old, 60, now)
    assert not session_lapsed(plain, 60, now)


def test_get_or_create_shares_live_entry_per_token() -> None:
    store = PendingReverifyStore(link_ttl=600)
    a, created_a = store.get_or_create(**_pending_kwargs())
    b, created_b = store.get_or_create(**_pending_kwargs())
    c, created_c = store.get_or_create(**_pending_kwargs("h2"))
    assert created_a and not created_b and created_c
    assert a.id == b.id != c.id
    assert len(a.id) >= 24


def test_pending_expires_after_link_ttl() -> None:
    store = PendingReverifyStore(link_ttl=600)
    entry, _ = store.get_or_create(**_pending_kwargs())
    entry.expires_at = time.time() - 1
    assert store.get(entry.id) is None
    fresh, created = store.get_or_create(**_pending_kwargs())
    assert created and fresh.id != entry.id


async def test_complete_wakes_waiter() -> None:
    store = PendingReverifyStore(link_ttl=600)
    entry, _ = store.get_or_create(**_pending_kwargs())
    waiter = asyncio.create_task(store.wait(entry.id, timeout=5))
    await asyncio.sleep(0)
    assert store.complete(entry.id) is entry
    assert await waiter is True
    assert store.get(entry.id) is None
    assert store.complete(entry.id) is None


async def test_wait_times_out_false() -> None:
    store = PendingReverifyStore(link_ttl=600)
    entry, _ = store.get_or_create(**_pending_kwargs())
    assert await store.wait(entry.id, timeout=0.05) is False
    # still usable after a timed-out wait
    assert store.get(entry.id) is entry


async def test_revoke_token_wakes_waiters_with_false() -> None:
    store = PendingReverifyStore(link_ttl=600)
    entry, _ = store.get_or_create(**_pending_kwargs())
    waiter = asyncio.create_task(store.wait(entry.id, timeout=5))
    await asyncio.sleep(0)
    assert store.revoke_token("h1") == 1
    assert await waiter is False
    assert store.get(entry.id) is None


async def test_wait_unknown_id_false() -> None:
    assert await PendingReverifyStore().wait("nope", timeout=1) is False
