"""TOTP lockout keyed on (client_id, source IP) with a per-client cap.

Keyed on client_id alone, anyone who knew a client_id (not a secret: it is in
authorize URLs and proxy logs) could lock the owner out of the dashboard,
token issuance and /oauth/authorize with five wrong codes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp import ratelimit  # noqa: E402
from beaconmcp.ratelimit import TotpLockout  # noqa: E402

ATTACKER = "203.0.113.7"
OWNER = "198.51.100.1"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ratelimit.time, "time", c.time)
    return c


def test_failures_from_one_ip_do_not_lock_another(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20, lockout_seconds=300)
    for _ in range(5):
        guard.record_failure("c1", ATTACKER)
    assert guard.locked("c1", ATTACKER)
    assert not guard.locked("c1", OWNER)


def test_source_lock_is_per_client(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    for _ in range(5):
        guard.record_failure("c1", ATTACKER)
    assert not guard.locked("c2", ATTACKER)


def test_per_client_cap_stops_distributed_guessing(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    # 4 wrong codes from each of 5 addresses: no single source is locked...
    for i in range(5):
        for _ in range(4):
            guard.record_failure("c1", f"203.0.113.{i}")
    # ...but the client as a whole is, from everywhere.
    assert guard.locked("c1", "203.0.113.0")
    assert guard.locked("c1", OWNER)


def test_below_thresholds_nothing_is_locked(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    for _ in range(4):
        guard.record_failure("c1", ATTACKER)
    assert not guard.locked("c1", ATTACKER)


def test_lock_expires_after_cooldown(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20, lockout_seconds=300)
    for _ in range(5):
        guard.record_failure("c1", ATTACKER)
    clock.now += 299
    assert guard.locked("c1", ATTACKER)
    clock.now += 2
    assert not guard.locked("c1", ATTACKER)


def test_old_strikes_do_not_accumulate_across_cooldowns(clock) -> None:
    # Four wrong codes today and one tomorrow must not lock.
    guard = TotpLockout(per_source=5, per_client=20, lockout_seconds=300)
    for _ in range(4):
        guard.record_failure("c1", ATTACKER)
    clock.now += 86_400
    guard.record_failure("c1", ATTACKER)
    assert not guard.locked("c1", ATTACKER)


def test_success_clears_only_the_callers_source(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    for _ in range(4):
        guard.record_failure("c1", ATTACKER)
    for _ in range(4):
        guard.record_failure("c1", OWNER)
    guard.record_success("c1", OWNER)
    guard.record_failure("c1", OWNER)
    assert not guard.locked("c1", OWNER)
    guard.record_failure("c1", ATTACKER)
    assert guard.locked("c1", ATTACKER)


def test_success_does_not_reset_the_per_client_cap(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    for i in range(5):
        for _ in range(4):
            guard.record_failure("c1", f"203.0.113.{i}")
    guard.record_success("c1", OWNER)
    # The owner's own source is exempt now, every other one stays locked.
    assert not guard.locked("c1", OWNER)
    assert guard.locked("c1", "203.0.113.200")


def test_known_good_source_survives_a_distributed_lockout(clock) -> None:
    """Two attacker IPs at the per-source limit must not lock the owner out
    of every TOTP path once the owner has logged in from their address."""
    guard = TotpLockout()
    guard.record_success("c1", OWNER)
    for ip in ("203.0.113.1", "203.0.113.2"):
        for _ in range(5):
            guard.record_failure("c1", ip)
    assert not guard.locked("c1", OWNER)
    assert guard.locked("c1", "203.0.113.3")


def test_known_good_source_still_has_its_own_cap(clock) -> None:
    guard = TotpLockout()
    guard.record_success("c1", OWNER)
    for _ in range(5):
        guard.record_failure("c1", OWNER)
    assert guard.locked("c1", OWNER)


def test_known_good_expires(clock) -> None:
    guard = TotpLockout(known_good_seconds=60)
    guard.record_success("c1", OWNER)
    for ip in ("203.0.113.1", "203.0.113.2"):
        for _ in range(5):
            guard.record_failure("c1", ip)
    clock.now += 61
    for ip in ("203.0.113.4", "203.0.113.5"):
        for _ in range(5):
            guard.record_failure("c1", ip)
    assert guard.locked("c1", OWNER)


def test_unknown_ip_is_its_own_source(clock) -> None:
    guard = TotpLockout(per_source=5, per_client=20)
    for _ in range(5):
        guard.record_failure("c1")
    assert guard.locked("c1")
    assert not guard.locked("c1", OWNER)


def test_default_per_client_cap_is_ten(clock) -> None:
    """Two addresses at the per-source limit are enough to hit the client cap."""
    guard = TotpLockout()
    for ip in ("203.0.113.1", "203.0.113.2"):
        for _ in range(5):
            guard.record_failure("c1", ip)
    assert guard.locked("c1", OWNER)


def test_forwarded_header_without_trusted_proxies_warns_once(caplog, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(ratelimit, "_forwarding_warned", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.2"),
        headers={"x-forwarded-for": ATTACKER},
    )
    with caplog.at_level("WARNING", logger="beaconmcp"):
        assert ratelimit.client_ip(request) == "10.0.0.2"
        ratelimit.client_ip(request)
    warnings = [r for r in caplog.records if "not in server.trusted_proxies" in r.getMessage()]
    assert len(warnings) == 1
    assert "10.0.0.2" in warnings[0].getMessage()


def test_forwarded_header_from_an_unlisted_proxy_warns(caplog, monkeypatch) -> None:
    """``trusted_proxies: [cloudflare]`` doesn't cover a cloudflared tunnel,
    which connects from its own LAN address: say so instead of staying quiet."""
    from types import SimpleNamespace

    monkeypatch.setattr(ratelimit, "_forwarding_warned", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="192.168.1.201"),
        headers={"x-forwarded-for": ATTACKER},
    )
    with caplog.at_level("WARNING", logger="beaconmcp"):
        assert ratelimit.client_ip(request, ("127.0.0.1", "173.245.48.0/20")) == "192.168.1.201"
    assert [r for r in caplog.records if "192.168.1.201" in r.getMessage()]


def test_no_warning_when_the_proxy_is_trusted(caplog, monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(ratelimit, "_forwarding_warned", False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.2"),
        headers={"x-forwarded-for": ATTACKER},
    )
    with caplog.at_level("WARNING", logger="beaconmcp"):
        assert ratelimit.client_ip(request, ("10.0.0.2",)) == ATTACKER
    assert not [r for r in caplog.records if "trusted_proxies" in r.getMessage()]
