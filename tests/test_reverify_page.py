"""``/oauth/reverify`` page and endpoints."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from beaconmcp.auth import TokenStore, TotpResult, token_hash
from beaconmcp.reauth import ClientInfoRegistry, ClientSessionInfo, PendingReverifyStore
from beaconmcp.reverify import ReverifyDeps, build_reverify_routes

JSON = {"X-BeaconMCP-Mode": "json"}


class FakeClients:
    def check_totp(self, client_id, code):
        if code == "123456":
            return TotpResult.OK
        if code == "111111":
            return TotpResult.REPLAY
        return TotpResult.INVALID

    def get_name(self, client_id):
        return {"c1": "Claude Code · showdown"}.get(client_id)


class FakePasskeys:
    available = True

    def __init__(self):
        self.owner = "c1"

    def authentication_options(self, request, *, client_id):
        return {"challenge": "abc", "allowCredentials": [], "for": client_id}, "state-1"

    def verify_authentication(self, request, *, state, credential):
        return SimpleNamespace(client_id=self.owner, label="MacBook Pro")


@pytest.fixture()
def env():
    store = TokenStore()
    token, _ = store.issue_interactive("c1")
    thash = token_hash(token)
    store.get_interactive(token).verified_at = time.time() - 90000
    pending = PendingReverifyStore()
    registry = ClientInfoRegistry()
    registry.record(thash, ClientSessionInfo(elicit_url=True, name="claude-code", version="2.1.3"))
    entry, _ = pending.get_or_create(
        token_hash=thash, client_id="c1", tool="proxmox_vm_start",
        args={"node": "pve-02", "vmid": 104}, ip="82.64.12.7", user_agent="ua",
        client_label="Claude Code", bearer_issued_at=time.time() - 3 * 86400,
        last_verified_at=time.time() - 90000,
    )
    failures: list[str] = []
    passkeys = FakePasskeys()
    deps = ReverifyDeps(
        client_store=FakeClients(), token_store=store, pending=pending, registry=registry,
        session_ttl=86400, totp_locked=lambda cid: cid == "locked",
        totp_record_failure=failures.append, totp_record_success=lambda cid: None,
        passkey_service=passkeys, passkey_owner=lambda cid: cid,
        passkeys_enabled=lambda req: True, login_limiter=None,
        logo_url_for=lambda ident, info: f"/oauth/logo/known/{ident.key}",
    )
    client = TestClient(Starlette(routes=build_reverify_routes(deps)))
    return SimpleNamespace(client=client, store=store, token=token, thash=thash,
                           entry=entry, pending=pending, failures=failures,
                           passkeys=passkeys, deps=deps)


def test_get_shows_tool_args_origin_and_last_verified(env) -> None:
    res = env.client.get(f"/oauth/reverify?id={env.entry.id}")
    assert res.status_code == 200
    page = res.text
    assert "Verify it's still you" in page
    assert "proxmox_vm_start" in page and "node=pve-02 · vmid=104" in page
    assert "82.64.12.7" in page and "claude-code 2.1.3" in page
    assert "3 days ago" in page and "Last verified" in page
    assert "/oauth/logo/known/claude-code" in page and "Claude Code" in page
    assert "Revoke this access" in page
    assert "Nothing ran yet" not in page


def test_get_escapes_args(env) -> None:
    entry, _ = env.pending.get_or_create(
        token_hash="other", client_id="c1", tool="x", args={"cmd": "<script>alert(1)</script>"},
        ip="1.1.1.1", user_agent="<b>", client_label="x", bearer_issued_at=0, last_verified_at=0,
    )
    page = env.client.get(f"/oauth/reverify?id={entry.id}").text
    assert "<script>alert(1)</script>" not in page


def test_get_dead_id_410(env) -> None:
    res = env.client.get("/oauth/reverify?id=nope")
    assert res.status_code == 410 and "expired" in res.text


def test_totp_success_marks_verified_and_completes(env) -> None:
    res = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "123456"}, headers=JSON)
    body = res.json()
    assert res.status_code == 200 and body["ok"]
    assert body["session_expires_at"] > time.time() + 86000
    assert time.time() - env.store.get_interactive(env.token).verified_at < 5
    assert env.entry.done


def test_totp_form_post_without_js(env) -> None:
    res = env.client.post("/oauth/reverify", data={"id": env.entry.id, "totp": "123456"})
    assert res.status_code == 200
    assert 'id="step-done">' in res.text and 'id="step-verify" hidden' in res.text


def test_totp_wrong_counts_failure(env) -> None:
    res = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "000000"}, headers=JSON)
    assert res.status_code == 401 and env.failures == ["c1"]
    assert not env.entry.done


def test_totp_replay_not_counted(env) -> None:
    res = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "111111"}, headers=JSON)
    assert res.status_code == 401 and env.failures == []


def test_locked_client_refused(env) -> None:
    env.entry.client_id = "locked"
    res = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "123456"}, headers=JSON)
    assert res.status_code == 429


def test_single_use(env) -> None:
    ok = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "123456"}, headers=JSON)
    assert ok.status_code == 200
    again = env.client.post("/oauth/reverify", json={"id": env.entry.id, "totp": "123456"}, headers=JSON)
    assert again.status_code == 410


def test_passkey_success(env) -> None:
    opts = env.client.post("/oauth/reverify/passkey/options", json={"id": env.entry.id}, headers=JSON)
    assert opts.json()["options"]["for"] == "c1"
    res = env.client.post("/oauth/reverify/passkey/verify",
                          json={"id": env.entry.id, "state": "state-1", "credential": {"id": "x"}},
                          headers=JSON)
    assert res.status_code == 200 and res.json()["via"] == "Passkey · MacBook Pro"
    assert env.entry.done


def test_other_clients_passkey_rejected(env) -> None:
    env.passkeys.owner = "someone-else"
    res = env.client.post("/oauth/reverify/passkey/verify",
                          json={"id": env.entry.id, "state": "s", "credential": {"id": "x"}},
                          headers=JSON)
    assert res.status_code == 401 and "another client" in res.json()["error"]
    assert not env.entry.done


def test_revoke_from_page_kills_bearer_and_pending(env, monkeypatch) -> None:
    monkeypatch.setattr(TokenStore, "REVOKE_GRACE_SECONDS", 0.0)
    res = env.client.post("/oauth/reverify/revoke", json={"id": env.entry.id}, headers=JSON)
    assert res.status_code == 200
    time.sleep(0.01)
    assert env.store.validate(env.token) is None
    assert env.entry.revoked and env.pending.get(env.entry.id) is None


def test_passkeys_disabled_hides_button(env) -> None:
    env.deps.passkeys_enabled = lambda req: False
    page = env.client.get(f"/oauth/reverify?id={env.entry.id}").text
    assert 'id="passkey-btn" hidden' in page
    assert 'id="totp-block">' in page
