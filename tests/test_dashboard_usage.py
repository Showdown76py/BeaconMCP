"""Unit + integration tests for per-client usage tracking and budget caps."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.dashboard.app import DashboardDeps, build_dashboard_routes
from beaconmcp.dashboard.chat import (
    FakeChatEngine,
    FakeScript,
    TextDelta,
    UsageAccumulated,
)
from beaconmcp.dashboard.confirmations import ConfirmationStore
from beaconmcp.dashboard.conversations import ConversationStore
from beaconmcp.dashboard.csrf import CSRF_COOKIE
from beaconmcp.dashboard.db import Database
from beaconmcp.dashboard.session import SessionStore
from beaconmcp.dashboard.usage import Budget, UsageMeter, UsageStore


# ---------------------------------------------------------------------------
# Helpers copied from test_dashboard_chat (kept local so this file is
# self-contained and can run even if that one is skipped).
# ---------------------------------------------------------------------------


class FakeClientStore:
    def verify(self, cid, sec): return cid == "c" and sec == "s"
    def verify_totp(self, cid, code): return code == "123456"
    def get_name(self, cid): return "Test"


class FakeTokenStore:
    NAMED_TOKEN_CAP = 3

    def __init__(self):
        self._live: dict[str, str] = {}

    def issue(self, cid, *, name=None):
        token = f"b_{len(self._live) + 1}" + ("x" * 60)
        self._live[token] = cid
        return token, 24 * 3600

    def validate(self, token):
        return self._live.get(token)

    def revoke(self, token):
        self._live.pop(token, None)
        return True


def _login(client) -> str:
    r = client.get("/app/login")
    csrf = r.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    r = client.post("/app/login", data={
        "csrf_token": csrf, "client_id": "c",
        "client_secret": "s", "totp": "123456", "remember": "on",
    })
    assert r.status_code == 303
    out = client.cookies.get(CSRF_COOKIE)
    assert out is not None
    return out


# ---------------------------------------------------------------------------
# Unit: UsageMeter
# ---------------------------------------------------------------------------


def test_cost_flash_no_cache():
    # 1M prompt + 1M output on 2.5-flash = $0.30 + $2.50 = $2.80.
    cost = UsageMeter.cost_usd(
        "gemini-2.5-flash",
        prompt_tokens=1_000_000, cached_tokens=0, output_tokens=1_000_000,
    )
    assert cost == pytest.approx(2.80)


def test_cost_flash_with_cache_hit():
    # Half the input comes from cache: 500k * $0.30 + 500k * $0.03 + 0 out.
    cost = UsageMeter.cost_usd(
        "gemini-2.5-flash",
        prompt_tokens=1_000_000, cached_tokens=500_000, output_tokens=0,
    )
    assert cost == pytest.approx(0.30 * 0.5 + 0.03 * 0.5)


def test_cost_pro_high_tier():
    # Pro model crosses the 200k threshold -> high-tier pricing applies.
    cost = UsageMeter.cost_usd(
        "gemini-2.5-pro",
        prompt_tokens=300_000, cached_tokens=0, output_tokens=0,
    )
    # 300k * $2.50 / 1M = $0.75
    assert cost == pytest.approx(0.75)


def test_cost_unknown_model_falls_back_to_flash():
    cost = UsageMeter.cost_usd(
        "gemini-unknown-9",
        prompt_tokens=1_000_000, cached_tokens=0, output_tokens=0,
    )
    # Same rate as gemini-2.5-flash input.
    assert cost == pytest.approx(0.30)


def test_cost_cached_over_prompt_is_clamped():
    # Defensive: if Gemini ever reports more cached tokens than prompt
    # tokens, the billable_input floor is 0 (not negative).
    cost = UsageMeter.cost_usd(
        "gemini-2.5-flash",
        prompt_tokens=100, cached_tokens=200, output_tokens=0,
    )
    # Billable input is 0, cached is 200 at cached rate.
    assert cost == pytest.approx(200 * 0.03 / 1_000_000)


# ---------------------------------------------------------------------------
# Unit: UsageStore (5h session + rolling week)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    db = Database(tmp_path / "usage.db")
    return UsageStore(db, Budget(limit_5h_usd=1.0, limit_week_usd=5.0))


def test_record_turn_opens_session(store):
    now = 1000.0
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1000, cached_tokens=0, output_tokens=500,
        cost_usd=0.10, now=now,
    )
    snap = store.snapshot("c", now=now)
    assert snap.spent_5h_usd == pytest.approx(0.10)
    assert snap.session_5h_started_at == now
    assert snap.session_5h_reset_at == now + 5 * 3600
    assert snap.spent_week_usd == pytest.approx(0.10)


def test_five_hour_window_resets_after_expiry(store):
    t0 = 1000.0
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1000, cached_tokens=0, output_tokens=500,
        cost_usd=0.60, now=t0,
    )
    # Just inside 5h: still part of the same session.
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m2",
        model="gemini-2.5-flash",
        prompt_tokens=500, cached_tokens=0, output_tokens=500,
        cost_usd=0.10, now=t0 + 1000,
    )
    snap_mid = store.snapshot("c", now=t0 + 1000)
    assert snap_mid.spent_5h_usd == pytest.approx(0.70)

    # Past 5h: next turn opens a fresh session at its own ts.
    t1 = t0 + 6 * 3600
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m3",
        model="gemini-2.5-flash",
        prompt_tokens=1000, cached_tokens=0, output_tokens=500,
        cost_usd=0.05, now=t1,
    )
    snap_new = store.snapshot("c", now=t1)
    assert snap_new.spent_5h_usd == pytest.approx(0.05)
    assert snap_new.session_5h_started_at == t1


def test_snapshot_with_expired_session_shows_zero(store):
    t0 = 1000.0
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1000, cached_tokens=0, output_tokens=500,
        cost_usd=0.40, now=t0,
    )
    # Look up the snapshot after the 5h window has already lapsed but
    # WITHOUT a new turn in between -- should report zero and no active
    # session, so the UI footer stops showing stale data.
    snap = store.snapshot("c", now=t0 + 6 * 3600)
    assert snap.spent_5h_usd == 0.0
    assert snap.session_5h_started_at is None


def test_check_budget_blocks_when_five_hour_exceeded(store):
    t0 = 1000.0
    # Spend exactly the cap in one go.
    store.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1000, cached_tokens=0, output_tokens=500,
        cost_usd=1.0, now=t0,
    )
    block = store.check_budget("c", now=t0 + 60)
    assert block is not None
    assert block.window == "5h"
    assert block.limit_usd == 1.0
    assert block.reset_at == t0 + 5 * 3600


def test_check_budget_blocks_when_weekly_exceeded(tmp_path):
    db = Database(tmp_path / "usage.db")
    # 5h cap disabled so we test the weekly path in isolation.
    s = UsageStore(db, Budget(limit_5h_usd=0.0, limit_week_usd=1.0))
    t0 = 1_000_000.0
    # Accumulate enough cost across two turns spread out in time.
    s.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1, cached_tokens=0, output_tokens=1,
        cost_usd=0.60, now=t0,
    )
    s.record_turn(
        client_id="c", conversation_id="x", message_id="m2",
        model="gemini-2.5-flash",
        prompt_tokens=1, cached_tokens=0, output_tokens=1,
        cost_usd=0.50, now=t0 + 3 * 24 * 3600,
    )
    block = s.check_budget("c", now=t0 + 4 * 24 * 3600)
    assert block is not None
    assert block.window == "week"


def test_check_budget_allows_when_disabled(tmp_path):
    db = Database(tmp_path / "usage.db")
    s = UsageStore(db, Budget(limit_5h_usd=0.0, limit_week_usd=0.0))
    # Huge spend but no limit configured -> always allow.
    s.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1, cached_tokens=0, output_tokens=1,
        cost_usd=999.0, now=time.time(),
    )
    assert s.check_budget("c") is None


def test_weekly_window_is_rolling(tmp_path):
    """Events older than 7 days must fall out of the weekly sum."""
    db = Database(tmp_path / "usage.db")
    s = UsageStore(db, Budget(limit_5h_usd=0.0, limit_week_usd=10.0))
    t_old = 1000.0
    s.record_turn(
        client_id="c", conversation_id="x", message_id="m1",
        model="gemini-2.5-flash",
        prompt_tokens=1, cached_tokens=0, output_tokens=1,
        cost_usd=5.0, now=t_old,
    )
    # >7 days later: old event drops out, new one is the only contributor.
    t_now = t_old + 8 * 24 * 3600
    s.record_turn(
        client_id="c", conversation_id="x", message_id="m2",
        model="gemini-2.5-flash",
        prompt_tokens=1, cached_tokens=0, output_tokens=1,
        cost_usd=1.0, now=t_now,
    )
    snap = s.snapshot("c", now=t_now)
    assert snap.spent_week_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Integration: chat stream enforces caps + persists cost
# ---------------------------------------------------------------------------


def _build_deps(tmp_path, *, engine, budget):
    db = Database(tmp_path / "dashboard.db")
    return DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=engine,
        confirmations=ConfirmationStore(),
        usage=UsageStore(db, budget),
    )


def test_stream_records_cost_after_turn(tmp_path):
    # Engine script: yields a text delta + a usage event reporting
    # 100k prompt (0 cached) + 50k output on flash => 100000*0.30/1M +
    # 50000*2.50/1M = 0.03 + 0.125 = 0.155.
    engine = FakeChatEngine(FakeScript(events=[
        TextDelta(text="hello"),
        UsageAccumulated(
            model="gemini-2.5-flash",
            prompt_tokens=100_000,
            cached_tokens=0,
            output_tokens=50_000,
        ),
    ], title_text="t"))

    deps = _build_deps(
        tmp_path, engine=engine,
        budget=Budget(limit_5h_usd=1.0, limit_week_usd=5.0),
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)
    csrf = _login(client)

    # Create conversation.
    r = client.post(
        "/app/api/conversations",
        json={"model": "gemini-2.5-flash", "effort": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = r.json()["conversation"]["id"]

    # Stream a turn.
    r = client.post(
        "/app/api/chat/stream",
        json={"conversation_id": conv_id, "content": "hi"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.text
    assert "event: usage_update" in body

    # Snapshot API should reflect the recorded cost.
    r = client.get("/app/api/usage")
    assert r.status_code == 200
    usage = r.json()["usage"]
    expected_cost = 100_000 * 0.30 / 1_000_000 + 50_000 * 2.50 / 1_000_000
    assert usage["spent_5h_usd"] == pytest.approx(expected_cost, rel=1e-6)
    assert usage["spent_week_usd"] == pytest.approx(expected_cost, rel=1e-6)
    assert usage["limit_5h_usd"] == 1.0
    assert usage["limit_week_usd"] == 5.0


def test_stream_rejects_when_over_5h_cap(tmp_path):
    engine = FakeChatEngine(FakeScript(events=[TextDelta(text="unused")]))
    deps = _build_deps(
        tmp_path, engine=engine,
        budget=Budget(limit_5h_usd=0.01, limit_week_usd=5.0),
    )
    # Pre-load a spend that already exceeds the 5h cap.
    assert deps.usage is not None
    deps.usage.record_turn(
        client_id="c", conversation_id=None, message_id=None,
        model="gemini-2.5-flash",
        prompt_tokens=0, cached_tokens=0, output_tokens=0,
        cost_usd=0.02,
    )

    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)
    csrf = _login(client)

    r = client.post(
        "/app/api/conversations",
        json={"model": "gemini-2.5-flash", "effort": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    conv_id = r.json()["conversation"]["id"]

    r = client.post(
        "/app/api/chat/stream",
        json={"conversation_id": conv_id, "content": "hi"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.text
    assert '"code": "quota_exceeded"' in body
    # Engine must not have been called -- the cap is enforced before
    # we reach google-genai.
    assert engine.calls == []


def test_usage_endpoint_returns_disabled_snapshot_when_store_missing(tmp_path):
    db = Database(tmp_path / "dashboard.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        conversations=ConversationStore(db),
        engine=FakeChatEngine(FakeScript()),
        confirmations=ConfirmationStore(),
        usage=None,   # explicitly disabled
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(app, follow_redirects=False)
    _login(client)

    r = client.get("/app/api/usage")
    assert r.status_code == 200
    u = r.json()["usage"]
    assert u["limit_5h_usd"] == 0.0
    assert u["limit_week_usd"] == 0.0
