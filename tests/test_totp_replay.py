"""TOTP replay-protection tests for :meth:`ClientStore.check_totp`.

A 6-digit TOTP code is valid for its whole 30s step (plus drift), so without
bookkeeping the same code could be redeemed twice. ``check_totp`` records
the last accepted timestep per SEED OWNER and rejects any non-newer code.
For delegated (DCR) clients the key is the owner, so two derived clients
cannot each spend the same code.

A replay reports ``REPLAY`` rather than ``INVALID`` so callers can avoid
charging it to the 5-strike lockout -- see ``test_dashboard_totp_replay.py``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pyotp
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import ClientStore, TotpResult


@pytest.fixture()
def clients(tmp_path: Path) -> ClientStore:
    return ClientStore(tmp_path / "clients.json")


def test_first_use_accepts_then_replay_rejected(clients: ClientStore) -> None:
    client_id, _, seed = clients.create("human")
    code = pyotp.TOTP(seed).now()
    assert clients.verify_totp(client_id, code) is True
    # Same code, same step -> replay, must be rejected.
    assert clients.verify_totp(client_id, code) is False
    # And again.
    assert clients.verify_totp(client_id, code) is False


def test_fresh_code_in_later_step_accepted(clients: ClientStore) -> None:
    client_id, _, seed = clients.create("human")
    totp = pyotp.TOTP(seed)
    now = time.time()
    code_now = totp.at(now)
    code_next = totp.at(now + totp.interval)

    assert clients.verify_totp(client_id, code_now) is True
    # A code from the following 30s step is strictly newer -> accepted,
    # even though the current one was just spent.
    assert clients.verify_totp(client_id, code_next) is True
    # Replaying either of the now-consumed codes still fails.
    assert clients.verify_totp(client_id, code_now) is False
    assert clients.verify_totp(client_id, code_next) is False


def test_wrong_code_does_not_advance_step(clients: ClientStore) -> None:
    """A rejected (wrong) code must not poison the replay counter."""
    client_id, _, seed = clients.create("human")
    assert clients.verify_totp(client_id, "000000") is False
    # A subsequent valid current code is still accepted.
    code = pyotp.TOTP(seed).now()
    assert clients.verify_totp(client_id, code) is True


def test_replay_is_per_owner_not_per_client(clients: ClientStore) -> None:
    """Two derived clients delegating to one owner share the replay window:
    a code spent through one cannot be replayed through the other."""
    owner_id, _, owner_seed = clients.create("human")
    d1, _ = clients.create_dynamic(
        owner_client_id=owner_id, name="d1", registration_source="chatgpt:s1",
    )
    d2, _ = clients.create_dynamic(
        owner_client_id=owner_id, name="d2", registration_source="chatgpt:s2",
    )
    code = pyotp.TOTP(owner_seed).now()
    # First derived client spends the code.
    assert clients.verify_totp(d1, code) is True
    # Second derived client cannot replay the SAME owner code.
    assert clients.verify_totp(d2, code) is False
    # The owner itself cannot replay it either.
    assert clients.verify_totp(owner_id, code) is False


def test_distinct_owners_have_independent_windows(
    clients: ClientStore,
) -> None:
    """Replay state is keyed by owner: burning A's window leaves B's intact."""
    a_id, _, a_seed = clients.create("owner_a")
    b_id, _, b_seed = clients.create("owner_b")
    now = time.time()

    # A spends the current step; B has not been touched yet.
    assert clients.verify_totp(a_id, pyotp.TOTP(a_seed).at(now)) is True
    assert clients.verify_totp(a_id, pyotp.TOTP(a_seed).at(now)) is False
    # B's *current* code is still spendable despite A's window being burnt.
    assert clients.verify_totp(b_id, pyotp.TOTP(b_seed).at(now)) is True
    # And B's next step is unaffected by anything A did.
    step = pyotp.TOTP(b_seed).interval
    assert clients.verify_totp(b_id, pyotp.TOTP(b_seed).at(now + step)) is True


def test_replay_is_reported_distinctly_from_a_wrong_code(
    clients: ClientStore,
) -> None:
    """The dashboard needs to tell "already used" apart from "wrong", because
    only the latter may count towards the lockout."""
    client_id, _, seed = clients.create("human")
    code = pyotp.TOTP(seed).now()

    assert clients.check_totp(client_id, code) is TotpResult.OK
    assert clients.check_totp(client_id, code) is TotpResult.REPLAY
    assert clients.check_totp(client_id, "000000") is TotpResult.INVALID
    assert clients.check_totp(client_id, "abc") is TotpResult.INVALID
    assert clients.check_totp("no-such-client", code) is TotpResult.INVALID
