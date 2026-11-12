"""sso via an external OIDC IdP (server.oidc): config, id_token checks, full round trip."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from beaconmcp import oidc  # noqa: E402
from beaconmcp.config import (  # noqa: E402
    ConfigError,
    OidcConfig,
    OidcRule,
    _parse_oidc,
)

ISSUER = "https://hydra.example.com/"
CLIENT_ID = "beaconmcp"
SECRET = "s3cret/with+chars"


# --- fake IdP ---------------------------------------------------------------


class FakeIdP:
    """minimal Hydra-ish IdP behind an httpx.MockTransport."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.claims: dict[str, Any] = {
            "sub": "user-123", "email": "alice@example.com", "email_verified": True,
        }
        self.codes: dict[str, dict[str, str]] = {}
        self.token_requests: list[httpx.Request] = []
        self.jwks_fetches = 0
        self.discovery_issuer = ISSUER

    def jwk(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return jwk

    def id_token(self, nonce: str, /, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": [CLIENT_ID], "iat": now, "exp": now + 300,
            "nonce": nonce, **self.claims,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def authorize(self, url: str) -> str:
        """what the browser does at the IdP: returns the code for this request."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = q
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer,
                "authorization_endpoint": "https://hydra.example.com/oauth2/auth",
                "token_endpoint": "https://hydra.example.com/oauth2/token",
                "jwks_uri": "https://hydra.example.com/.well-known/jwks.json",
            })
        if path == "/.well-known/jwks.json":
            self.jwks_fetches += 1
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if path == "/oauth2/token":
            self.token_requests.append(request)
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            req = self.codes.pop(form.get("code", ""), None)
            if req is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            ).rstrip(b"=").decode()
            if challenge != req["code_challenge"] or form["redirect_uri"] != req["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "at", "token_type": "bearer",
                "id_token": self.id_token(req["nonce"]),
            })
        return httpx.Response(404)


def _cfg(**kw: Any) -> OidcConfig:
    base = dict(
        enabled=True, issuer=ISSUER, client_id=CLIENT_ID, client_secret=SECRET,
        allow=[OidcRule(client="c1", emails=["alice@example.com"])],
    )
    base.update(kw)
    return OidcConfig(**base)


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdP:
    fake = FakeIdP()
    monkeypatch.setattr(oidc.OidcProvider, "transport", httpx.MockTransport(fake.handler))
    return fake


# --- config -----------------------------------------------------------------


def test_disabled_by_default() -> None:
    assert _parse_oidc(None).enabled is False
    # a parked block is not validated
    assert _parse_oidc({"enabled": False, "issuer": "nope"}).enabled is False


def test_enabled_parses_and_normalizes() -> None:
    cfg = _parse_oidc({
        "enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID,
        "client_secret": SECRET, "scopes": ["email"],
        "allow": [{"client": "c1", "emails": ["Alice@Example.com"], "groups": "ops"}],
    })
    assert cfg.scopes == ["openid", "email"]
    assert cfg.allow[0].emails == ["alice@example.com"]
    assert cfg.allow[0].groups == ["ops"]
    assert cfg.token_auth_method == "client_secret_basic"


@pytest.mark.parametrize(("raw", "msg"), [
    ({"issuer": "http://hydra.example.com"}, "https"),
    ({"allow": []}, "at least one rule"),
    ({"allow": [{"client": "c1"}]}, "emails, groups or subjects"),
    ({"token_auth_method": "private_key_jwt"}, "token_auth_method"),
    ({"client_secret": ""}, "client_secret"),
])
def test_enabled_validation(raw: dict[str, Any], msg: str) -> None:
    full = {
        "enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID,
        "client_secret": SECRET, "allow": [{"client": "c1", "subjects": ["x"]}],
    }
    full.update(raw)
    with pytest.raises(ConfigError, match=msg):
        _parse_oidc(full)


def test_example_config_has_oidc_off() -> None:
    import yaml

    raw = yaml.safe_load((Path(__file__).parent.parent / "beaconmcp.yaml.example").read_text())
    assert _parse_oidc((raw.get("server") or {}).get("oidc")).enabled is False


# --- allowlist --------------------------------------------------------------


def test_allowlist_matching() -> None:
    p = oidc.OidcProvider(_cfg(allow=[
        OidcRule(client="c1", emails=["alice@example.com"]),
        OidcRule(client="c2", groups=["ops"]),
        OidcRule(client="c3", subjects=["user-9"]),
    ]))
    assert p.allowed_clients({"sub": "u", "email": "ALICE@example.com", "email_verified": True}) == {"c1"}
    # unverified email doesn't count
    assert p.allowed_clients({"sub": "u", "email": "alice@example.com"}) == set()
    assert p.allowed_clients({"sub": "u", "groups": ["dev", "ops"]}) == {"c2"}
    assert p.allowed_clients({"sub": "u", "groups": "ops"}) == {"c2"}
    assert p.allowed_clients({"sub": "user-9"}) == {"c3"}
    assert p.allowed_clients({"sub": "nobody"}) == set()


def test_unverified_email_ok_when_opted_out() -> None:
    p = oidc.OidcProvider(_cfg(require_email_verified=False))
    assert p.allowed_clients({"sub": "u", "email": "alice@example.com"}) == {"c1"}


# --- id_token ---------------------------------------------------------------


async def test_id_token_ok(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    claims = await p.validate_id_token(idp.id_token("n1"), nonce="n1")
    assert claims["sub"] == "user-123"


@pytest.mark.parametrize("overrides", [
    {"aud": ["someone-else"]},
    {"iss": "https://evil.example.com/"},
    {"exp": int(time.time()) - 3600},
    {"nonce": "other"},
    {"aud": [CLIENT_ID, "x"]},
    {"aud": [CLIENT_ID, "x"], "azp": "x"},
    {"azp": "x"},
])
async def test_id_token_rejected(idp: FakeIdP, overrides: dict[str, Any]) -> None:
    p = oidc.OidcProvider(_cfg())
    with pytest.raises(oidc.OidcError):
        await p.validate_id_token(idp.id_token("n1", **overrides), nonce="n1")


async def test_id_token_azp_ok_with_several_aud(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    token = idp.id_token("n1", aud=[CLIENT_ID, "x"], azp=CLIENT_ID)
    assert (await p.validate_id_token(token, nonce="n1"))["azp"] == CLIENT_ID


async def test_id_token_hs256_and_none_rejected(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u", "iat": now, "exp": now + 60, "nonce": "n"}
    hs = jwt.encode(claims, SECRET * 3, algorithm="HS256")
    with pytest.raises(oidc.OidcError, match="alg"):
        await p.validate_id_token(hs, nonce="n")
    none = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(oidc.OidcError, match="alg"):
        await p.validate_id_token(none, nonce="n")


async def test_id_token_signed_by_other_key(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    await p.validate_id_token(idp.id_token("n1"), nonce="n1")  # caches the real key
    real = idp.key
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = idp.id_token("n2")  # same kid, attacker key
    idp.key = real
    with pytest.raises(oidc.OidcError, match="rejected"):
        await p.validate_id_token(forged, nonce="n2")


async def test_key_rotation_refetches_jwks_once(idp: FakeIdP) -> None:
    now = [time.time()]
    p = oidc.OidcProvider(_cfg(), clock=lambda: now[0])
    await p.validate_id_token(idp.id_token("n1"), nonce="n1")
    assert idp.jwks_fetches == 1
    now[0] += oidc.JWKS_REFRESH_GAP + 1
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.kid = "k2"
    await p.validate_id_token(idp.id_token("n2"), nonce="n2")
    assert idp.jwks_fetches == 2
    # unknown kid again right away: no refetch storm
    idp.kid = "k3"
    with pytest.raises(oidc.OidcError, match="kid"):
        await p.validate_id_token(idp.id_token("n3"), nonce="n3")
    assert idp.jwks_fetches == 2


async def test_discovery_issuer_mismatch(idp: FakeIdP) -> None:
    idp.discovery_issuer = "https://evil.example.com/"
    with pytest.raises(oidc.OidcError, match="issuer"):
        await oidc.OidcProvider(_cfg()).metadata()


# --- flow (provider level) --------------------------------------------------


async def test_start_and_finish_basic_auth(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    url, binding = await p.start(redirect_uri="https://b.example/cb", payload={"client_id": "c1"})
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["scope"] == "openid email profile"
    code = idp.authorize(url)
    assert p.take(q["state"], "wrong") is None  # other browser
    pending = p.take(q["state"], binding)
    assert pending is not None and pending.payload == {"client_id": "c1"}
    assert p.take(q["state"], binding) is None  # single use
    claims = await p.finish(pending, code)
    assert claims["email"] == "alice@example.com"
    auth = idp.token_requests[-1].headers["authorization"]
    user, _, pw = base64.b64decode(auth.split()[1]).decode().partition(":")
    assert user == CLIENT_ID and pw == "s3cret%2Fwith%2Bchars"


async def test_finish_client_secret_post(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg(token_auth_method="client_secret_post"))
    url, binding = await p.start(redirect_uri="https://b.example/cb", payload={})
    state = parse_qs(urlparse(url).query)["state"][0]
    code = idp.authorize(url)
    await p.finish(p.take(state, binding), code)
    req = idp.token_requests[-1]
    assert "authorization" not in req.headers
    assert "client_secret=" in req.content.decode()


async def test_finish_bad_code(idp: FakeIdP) -> None:
    p = oidc.OidcProvider(_cfg())
    url, binding = await p.start(redirect_uri="https://b.example/cb", payload={})
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(oidc.OidcError, match="invalid_grant"):
        await p.finish(p.take(state, binding), "nope")


# --- full round trip through the real stack ---------------------------------


HOST = "mcp.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rule_for_client: bool = True):
    from test_mcp_http_stack import _CI_ENV, ROOT

    for key, value in _CI_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BEACONMCP_CONFIG", str(ROOT / "beaconmcp.yaml.example"))
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("BEACONMCP_TOKENS_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setenv("BEACONMCP_CLIENTS_FILE", str(tmp_path / "clients.json"))

    import uvicorn

    from beaconmcp import __main__ as entry
    from beaconmcp import auth
    from beaconmcp.server import config, mcp

    client_id, _secret, _totp = auth.ClientStore(tmp_path / "clients.json").create("t")
    monkeypatch.setattr(config.server, "oidc", _cfg(allow=[
        OidcRule(client=client_id if rule_for_client else "other", emails=["alice@example.com"]),
    ]))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))
    entry._run_http(mcp, "127.0.0.1", 8420)
    return captured["app"], client_id


def _authorize_params(client_id: str) -> dict[str, str]:
    return {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "state": "client-state", "code_challenge": "x" * 43,
        "code_challenge_method": "S256",
    }


def _browser(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}",
    )


async def test_round_trip_issues_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, client_id = _build(tmp_path, monkeypatch)
    params = _authorize_params(client_id)
    async with _browser(app) as b:
        page = await b.get("/oauth/authorize", params=params)
        assert page.status_code == 200 and 'id="oidc-btn"' in page.text

        start = await b.post("/oauth/oidc/start", data=params)
        assert start.status_code == 303
        location = start.headers["location"]
        assert location.startswith("https://hydra.example.com/oauth2/auth?")
        q = parse_qs(urlparse(location).query)
        assert q["redirect_uri"] == [f"http://{HOST}/oauth/oidc/callback"]
        assert "beaconmcp_oidc" in start.headers["set-cookie"]

        code = idp.authorize(location)
        cb = await b.get("/oauth/oidc/callback", params={"code": code, "state": q["state"][0]})
        assert cb.status_code == 200
        assert "Two-factor confirmed" in cb.text
        ticket = cb.text.split('id="ticket" value="', 1)[1].split('"', 1)[0]

        done = await b.post("/oauth/authorize/finalize", data={"ticket": ticket})
        assert done.status_code == 302
        assert done.headers["location"].startswith(REDIRECT + "?code=")
        assert "state=client-state" in done.headers["location"]

        # state is single use
        again = await b.get("/oauth/oidc/callback", params={"code": code, "state": q["state"][0]})
        assert again.status_code == 400


async def test_round_trip_needs_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, client_id = _build(tmp_path, monkeypatch)
    async with _browser(app) as attacker:
        start = await attacker.post("/oauth/oidc/start", data=_authorize_params(client_id))
    location = start.headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    code = idp.authorize(location)
    async with _browser(app) as victim:
        cb = await victim.get("/oauth/oidc/callback", params={"code": code, "state": state})
    assert cb.status_code == 400


async def test_account_not_allowed_for_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, client_id = _build(tmp_path, monkeypatch)
    idp.claims["email"] = "someone@example.com"
    async with _browser(app) as b:
        start = await b.post("/oauth/oidc/start", data=_authorize_params(client_id))
        location = start.headers["location"]
        code = idp.authorize(location)
        state = parse_qs(urlparse(location).query)["state"][0]
        cb = await b.get("/oauth/oidc/callback", params={"code": code, "state": state})
    assert "allowed to authorize this client" in cb.text and 'id="ticket" value=""' in cb.text


async def test_idp_error_is_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, client_id = _build(tmp_path, monkeypatch)
    async with _browser(app) as b:
        start = await b.post("/oauth/oidc/start", data=_authorize_params(client_id))
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        cb = await b.get(
            "/oauth/oidc/callback",
            params={"error": "<script>x</script>", "state": state},
        )
    assert "cancelled or refused" in cb.text and "<script>x" not in cb.text


async def test_no_button_and_refused_for_unlisted_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdP,
) -> None:
    app, client_id = _build(tmp_path, monkeypatch, rule_for_client=False)
    params = _authorize_params(client_id)
    async with _browser(app) as b:
        page = await b.get("/oauth/authorize", params=params)
        assert 'id="oidc-btn"' not in page.text
        start = await b.post("/oauth/oidc/start", data=params)
    # html error pages keep 200, like the TOTP ones
    assert start.status_code == 200
    assert "SSO is not enabled for this client" in start.text


async def test_routes_404_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_mcp_http_stack import _build_stack

    app = _build_stack(tmp_path, monkeypatch)["app"]
    async with _browser(app) as b:
        r1 = await b.post("/oauth/oidc/start", data={})
        r2 = await b.get("/oauth/oidc/callback")
    assert r1.status_code == 404 and r2.status_code == 404
