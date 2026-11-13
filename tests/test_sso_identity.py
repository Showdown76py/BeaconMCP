"""sso as an identity: server.auth_mode, hidden per-user clients, open DCR, dashboard sign-in."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from test_oidc import (  # noqa: E402
    CLIENT_ID,
    ISSUER,
    REDIRECT,
    SECRET,
    FakeIdP,
    _authorize_params,
    _browser,
    _cfg,
)

from beaconmcp import auth, oidc  # noqa: E402
from beaconmcp.auth import ClientStore, TotpResult  # noqa: E402
from beaconmcp.config import (  # noqa: E402
    ConfigError,
    OidcRule,
    _parse_auth_mode,
    _parse_oidc,
)

IDENTITY = [OidcRule(client=None, groups=["tsa-beaconmcp"])]


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdP:
    fake = FakeIdP()
    monkeypatch.setattr(oidc.OidcProvider, "transport", httpx.MockTransport(fake.handler))
    return fake


# --- config -----------------------------------------------------------------


def _oidc(allow: list[dict[str, Any]]):
    return _parse_oidc({
        "enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID,
        "client_secret": SECRET, "allow": allow,
    })


def test_identity_rule_has_no_client() -> None:
    cfg = _oidc([{"groups": ["tsa-beaconmcp"]}, {"client": "c1", "subjects": ["x"]}])
    assert [r.client for r in cfg.allow] == [None, "c1"]
    assert len(cfg.identity_rules) == 1


def test_auth_mode_defaults() -> None:
    assert _parse_auth_mode(None, _parse_oidc(None)) == "builtin"
    assert _parse_auth_mode(None, _oidc([{"groups": ["g"]}])) == "both"
    assert _parse_auth_mode("SSO", _oidc([{"groups": ["g"]}])) == "sso"


@pytest.mark.parametrize(("mode", "allow", "msg"), [
    ("builtin", [{"groups": ["g"]}], "ambiguous"),
    ("sso", [{"client": "c1", "groups": ["g"]}], "without `client`"),
    ("nope", [{"groups": ["g"]}], "expected one of"),
])
def test_auth_mode_refused(mode: str, allow: list[dict[str, Any]], msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        _parse_auth_mode(mode, _oidc(allow))


def test_sso_mode_needs_oidc() -> None:
    with pytest.raises(ConfigError, match="needs server.oidc"):
        _parse_auth_mode("sso", _parse_oidc(None))


# --- client store -----------------------------------------------------------


def test_sso_user_is_stable_and_survives_reload(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "clients.json")
    a = store.ensure_sso_user("https://idp#u1", "alice@example.com")
    assert store.ensure_sso_user("https://idp#u1", "alice@example.com") == a
    assert store.ensure_sso_user("https://idp#u2", "bob") != a
    # no seed by design: not dropped as a pre-2FA client, and no TOTP way in
    reloaded = ClientStore(tmp_path / "clients.json")
    client = reloaded.get(a)
    assert client is not None and client.is_sso_user
    assert reloaded.check_totp(a, "123456") is TotpResult.INVALID


def test_open_dcr_bind_first_wins_and_prune(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "clients.json")
    alice = store.ensure_sso_user("https://idp#u1", "alice")
    bob = store.ensure_sso_user("https://idp#u2", "bob")
    cid, _ = store.create_open_dcr(name="Claude")
    assert ClientStore(tmp_path / "clients.json").get(cid) is not None
    assert store.bind_owner(cid, alice) and store.bind_owner(cid, alice)
    assert not store.bind_owner(cid, bob)
    # a builtin client can't be grabbed
    builtin, _, _ = store.create("ops")
    assert not store.bind_owner(builtin, alice)

    stale, _ = store.create_open_dcr(name="old")
    store.get(stale).created_at = time.time() - auth.OPEN_DCR_UNBOUND_TTL - 1
    store.create_open_dcr(name="new")
    assert store.get(stale) is None
    assert store.get(cid) is not None  # bound ones are kept


# --- through the real stack -------------------------------------------------


def _build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str = "both",
    rules: list[OidcRule] | None = None, dcr: bool = True,
):
    from test_mcp_http_stack import _CI_ENV, ROOT

    for key, value in _CI_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BEACONMCP_CONFIG", str(ROOT / "beaconmcp.yaml.example"))
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("BEACONMCP_TOKENS_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setenv("BEACONMCP_CLIENTS_FILE", str(tmp_path / "clients.json"))

    import uvicorn

    from beaconmcp import __main__ as entry
    from beaconmcp.server import config, mcp

    builtin_id, _secret, _totp = ClientStore(tmp_path / "clients.json").create("ops")
    monkeypatch.setattr(config.server, "oidc", _cfg(allow=IDENTITY if rules is None else rules))
    monkeypatch.setattr(config.server, "auth_mode", mode)
    monkeypatch.setattr(config.server, "allow_dynamic_registration", dcr)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))
    entry._run_http(mcp, "127.0.0.1", 8420)
    return captured["app"], builtin_id


async def _register(b: httpx.AsyncClient) -> httpx.Response:
    return await b.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [REDIRECT],
    })


async def _idp_round_trip(
    b: httpx.AsyncClient, idp: FakeIdP, path: str, data: dict[str, str],
) -> httpx.Response:
    start = await b.post(path, data=data)
    assert start.status_code == 303, start.text
    location = start.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    return await b.get(
        "/oauth/oidc/callback", params={"code": idp.authorize(location), "state": state},
    )


def _audit(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage()) for r in caplog.records
        if r.name == "beaconmcp.audit" and f'"{event}"' in r.getMessage()
    ]


async def test_open_dcr_connector_authorized_by_sso_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _ = _build(tmp_path, monkeypatch)
    idp.claims["groups"] = ["tsa-beaconmcp"]
    async with _browser(app) as b:
        meta = (await b.get("/.well-known/oauth-authorization-server")).json()
        assert meta["registration_endpoint"].endswith("/oauth/register")
        reg = await _register(b)
        assert reg.status_code == 201
        client_id = reg.json()["client_id"]
        params = _authorize_params(client_id)

        page = await b.get("/oauth/authorize", params=params)
        assert 'id="oidc-btn"' in page.text
        # no TOTP / passkey path for a client only sso can authorize
        assert 'id="authorize-form" hidden' in page.text
        refused = await b.post("/oauth/authorize", data={**params, "totp": "123456"})
        assert "Sign in with SSO" in refused.text

        with caplog.at_level("INFO", logger="beaconmcp.audit"):
            cb = await _idp_round_trip(b, idp, "/oauth/oidc/start", params)
        assert "Two-factor confirmed" in cb.text
        ticket = cb.text.split('id="ticket" value="', 1)[1].split('"', 1)[0]
        done = await b.post("/oauth/authorize/finalize", data={"ticket": ticket})
        assert done.status_code == 302 and done.headers["location"].startswith(REDIRECT)

    assert [e["rule"] for e in _audit(caplog, "auth.authorize.2fa")] == ["user"]
    store = ClientStore(tmp_path / "clients.json")
    owner = store.get(store.get(client_id).owner_client_id)
    assert owner is not None and owner.sso_subject == f"{ISSUER.rstrip('/')}#user-123"


async def test_open_dcr_connector_refused_for_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, _ = _build(tmp_path, monkeypatch)
    idp.claims["groups"] = ["tsa-beaconmcp"]
    async with _browser(app) as b:
        client_id = (await _register(b)).json()["client_id"]
        params = _authorize_params(client_id)
        assert "Two-factor confirmed" in (
            await _idp_round_trip(b, idp, "/oauth/oidc/start", params)
        ).text
        idp.claims["sub"] = "someone-else"
        cb = await _idp_round_trip(b, idp, "/oauth/oidc/start", params)
    assert "allowed to authorize this client" in cb.text


async def test_open_dcr_needs_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, _ = _build(tmp_path, monkeypatch)
    idp.claims["groups"] = ["dev"]
    async with _browser(app) as b:
        client_id = (await _register(b)).json()["client_id"]
        cb = await _idp_round_trip(
            b, idp, "/oauth/oidc/start", _authorize_params(client_id),
        )
    assert "allowed to authorize this client" in cb.text


async def test_open_dcr_off_without_identity_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _build(
        tmp_path, monkeypatch, rules=[OidcRule(client="*", groups=["tsa-beaconmcp"])],
    )
    async with _browser(app) as b:
        meta = (await b.get("/.well-known/oauth-authorization-server")).json()
        reg = await _register(b)
    assert "registration_endpoint" not in meta
    assert reg.status_code == 403


async def test_open_dcr_rejects_foreign_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _build(tmp_path, monkeypatch)
    async with _browser(app) as b:
        reg = await b.post("/oauth/register", json={
            "client_name": "x", "redirect_uris": ["https://evil.example.net/cb"],
        })
    assert reg.status_code == 400


async def test_sso_mode_refuses_totp_for_builtin_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, builtin_id = _build(
        tmp_path, monkeypatch, mode="sso",
        rules=IDENTITY + [OidcRule(client=None, emails=["x@example.com"])],
    )
    params = _authorize_params(builtin_id)
    async with _browser(app) as b:
        page = await b.get("/oauth/authorize", params=params)
        post = await b.post("/oauth/authorize", data={**params, "totp": "123456"})
        pk = await b.post("/oauth/passkey/options", json={"client_id": builtin_id})
    # no client rule covers it either: nothing to offer
    assert 'id="authorize-form" hidden' in page.text and 'id="oidc-btn"' not in page.text
    assert "Sign in with SSO" in post.text
    assert pk.status_code in (403, 503)


async def test_dashboard_sso_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _ = _build(tmp_path, monkeypatch, mode="sso")
    idp.claims["groups"] = ["tsa-beaconmcp"]
    async with _browser(app) as b:
        login = await b.get("/app/login")
        assert 'id="sso-btn"' in login.text and "Continue with SSO" in login.text
        assert 'id="builtin-login" hidden' in login.text
        assert (await b.post("/app/login", data={})).status_code == 403

        with caplog.at_level("INFO", logger="beaconmcp.audit"):
            cb = await _idp_round_trip(
                b, idp, "/oauth/oidc/login", {"next": "/app/tokens"},
            )
        assert cb.status_code == 303 and cb.headers["location"] == "/app/tokens"
        assert "beaconmcp_session" in cb.headers.get("set-cookie", "") or b.cookies

        tokens = await b.get("/app/tokens")
        assert tokens.status_code == 200
        assert "Confirmed by your SSO sign-in" in tokens.text
        assert 'name="totp"' not in tokens.text.split('action="/app/tokens"', 1)[1].split("</form>", 1)[0]

    events = _audit(caplog, "dashboard.login.ok")
    assert len(events) == 1 and events[0]["via"] == "oidc"


async def test_dashboard_sso_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, _ = _build(tmp_path, monkeypatch, mode="sso")
    idp.claims["groups"] = ["dev"]
    async with _browser(app) as b:
        cb = await _idp_round_trip(b, idp, "/oauth/oidc/login", {})
        assert cb.headers["location"] == "/app/login?sso_error=not_allowed"
        page = await b.get(cb.headers["location"])
    assert "isn&#39;t allowed to use this server" in page.text or "isn't allowed" in page.text


async def test_dashboard_both_mode_keeps_builtin_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _build(tmp_path, monkeypatch, mode="both")
    async with _browser(app) as b:
        login = await b.get("/app/login")
    assert 'id="sso-btn"' in login.text
    assert 'id="builtin-login">' in login.text


async def test_sso_login_route_404_in_builtin_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_mcp_http_stack import _build_stack

    app = _build_stack(tmp_path, monkeypatch)["app"]
    async with _browser(app) as b:
        r = await b.post("/oauth/oidc/login", data={})
        login = await b.get("/app/login")
    assert r.status_code == 404 and 'id="sso-btn"' not in login.text


async def test_sso_mode_drops_builtin_dashboard_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, builtin_id = _build(tmp_path, monkeypatch, mode="sso")
    from beaconmcp.dashboard.db import Database
    from beaconmcp.dashboard.session import SessionStore

    # a session left over from before the switch to sso
    store = SessionStore(Database(tmp_path / "dashboard.db"))
    old = store.create(
        client_id=builtin_id, client_secret="sk_x", mcp_bearer="b",
        bearer_ttl_seconds=3600, user_agent="t",
    )
    async with _browser(app) as b:
        b.cookies.set("beaconmcp_session", old.session_id)
        r = await b.get("/app/tokens")
    assert r.status_code == 302 and r.headers["location"].startswith("/app/login")
    assert store.load(old.session_id) is None
