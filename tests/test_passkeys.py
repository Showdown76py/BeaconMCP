"""Passkey (WebAuthn) tests.

Drives the real ceremonies end to end against a software authenticator
built here from ``cryptography`` primitives: a fake that only returned
canned dicts would prove nothing, since every interesting failure mode
(wrong origin, replayed challenge, foreign credential) lives inside the
signature verification.

Run with::

    pytest tests/test_passkeys.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

pytest.importorskip("webauthn", reason="passkey support is an optional extra")
cbor2 = pytest.importorskip("cbor2")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    decode_dss_signature,
    encode_dss_signature,
)

from beaconmcp.auth import TotpResult  # noqa: E402
from beaconmcp.dashboard.app import (  # noqa: E402
    DashboardDeps,
    SESSION_COOKIE,
    build_dashboard_routes,
)
from beaconmcp.dashboard.csrf import CSRF_COOKIE  # noqa: E402
from beaconmcp.dashboard.db import Database  # noqa: E402
from beaconmcp.dashboard.passkeys import (  # noqa: E402
    ChallengeStore,
    PasskeyError,
    PasskeyService,
    PasskeyStore,
    b64url_decode,
    b64url_encode,
    default_label,
)
from beaconmcp.dashboard.session import SessionStore  # noqa: E402


ORIGIN = "https://beacon.example"
RP_ID = "beacon.example"


# ---------------------------------------------------------------------------
# Software authenticator
# ---------------------------------------------------------------------------

class SoftwareAuthenticator:
    """A minimal ES256 authenticator: enough to produce valid ceremonies."""

    AAGUID = b"\x00" * 16

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    # --- helpers ---------------------------------------------------------

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2,      # kty: EC2
            3: -7,     # alg: ES256
            -1: 1,     # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def _auth_data(self, rp_id: str, flags: int, attested: bool) -> bytes:
        data = hashlib.sha256(rp_id.encode("utf-8")).digest()
        data += bytes([flags])
        data += struct.pack(">I", self.sign_count)
        if attested:
            key = self._cose_key()
            data += self.AAGUID
            data += struct.pack(">H", len(self.credential_id))
            data += self.credential_id
            data += key
        return data

    # --- ceremonies ------------------------------------------------------

    def create(self, options: dict, *, origin: str = ORIGIN, rp_id: str = RP_ID) -> dict:
        client_data = self._client_data(
            "webauthn.create", options["challenge"], origin,
        )
        # UP | UV | AT
        auth_data = self._auth_data(rp_id, 0x01 | 0x04 | 0x40, attested=True)
        attestation = cbor2.dumps({
            "fmt": "none", "attStmt": {}, "authData": auth_data,
        })
        return {
            "id": b64url_encode(self.credential_id),
            "rawId": b64url_encode(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url_encode(client_data),
                "attestationObject": b64url_encode(attestation),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict, *, origin: str = ORIGIN, rp_id: str = RP_ID) -> dict:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = self._auth_data(rp_id, 0x01 | 0x04, attested=False)
        payload = auth_data + hashlib.sha256(client_data).digest()
        raw = self.key.sign(payload, ec.ECDSA(hashes.SHA256()))
        # Re-encode so the DER is canonical whatever the backend produced.
        r, s = decode_dss_signature(raw)
        signature = encode_dss_signature(r, s)
        return {
            "id": b64url_encode(self.credential_id),
            "rawId": b64url_encode(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64url_encode(client_data),
                "authenticatorData": b64url_encode(auth_data),
                "signature": b64url_encode(signature),
                "userHandle": None,
            },
        }


class FakeRequest:
    """Just the surface :mod:`beaconmcp.dashboard.passkeys` reads."""

    def __init__(self, host: str = RP_ID, scheme: str = "https") -> None:
        self.headers = {"host": host, "x-forwarded-proto": scheme}

        class _URL:
            netloc = host

        self.url = _URL()
        self.url.scheme = scheme  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path) -> PasskeyStore:
    return PasskeyStore(Database(tmp_path / "dashboard.db"))


@pytest.fixture()
def service(store) -> PasskeyService:
    return PasskeyService(store)


@pytest.fixture()
def enrolled(service):
    """A client with one registered passkey, plus its authenticator."""
    request = FakeRequest()
    auth = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_test", client_name="Test Client",
    )
    record = service.verify_registration(
        request, state=state, credential=auth.create(options),
    )
    return auth, record


# ---------------------------------------------------------------------------
# RP identity
# ---------------------------------------------------------------------------

def test_rp_id_strips_port_and_scheme():
    from beaconmcp.dashboard.passkeys import origin_for, rp_id_for

    req = FakeRequest(host="beacon.example:8420")
    assert rp_id_for(req) == "beacon.example"
    assert origin_for(req) == "https://beacon.example:8420"


def test_rp_id_handles_ipv6_literal():
    from beaconmcp.dashboard.passkeys import rp_id_for

    assert rp_id_for(FakeRequest(host="[::1]:8420")) == "::1"


def test_secure_context_requires_https_or_loopback():
    from beaconmcp.dashboard.passkeys import is_secure_context

    assert is_secure_context(FakeRequest(host="beacon.example", scheme="https"))
    assert is_secure_context(FakeRequest(host="localhost:8420", scheme="http"))
    assert not is_secure_context(FakeRequest(host="192.168.1.5:8420", scheme="http"))


def test_forwarded_host_wins_over_host():
    from beaconmcp.dashboard.passkeys import rp_id_for

    req = FakeRequest(host="internal:8420")
    req.headers["x-forwarded-host"] = "public.example"
    assert rp_id_for(req) == "public.example"


def test_default_label_from_user_agent():
    assert default_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)") == "iPhone / iPad"
    assert default_label("Mozilla/5.0 (Windows NT 10.0)") == "Windows"
    assert default_label("") == "Passkey"


# ---------------------------------------------------------------------------
# Challenge store
# ---------------------------------------------------------------------------

def test_challenge_is_single_use():
    challenges = ChallengeStore()
    state = challenges.issue(
        purpose="authenticate", challenge=b"abc", client_id="c1",
    )
    assert challenges.consume(state, "authenticate") is not None
    assert challenges.consume(state, "authenticate") is None


def test_challenge_purpose_must_match():
    challenges = ChallengeStore()
    state = challenges.issue(purpose="register", challenge=b"abc", client_id="c1")
    assert challenges.consume(state, "authenticate") is None


def test_challenge_expires():
    challenges = ChallengeStore(ttl_seconds=-1)
    state = challenges.issue(purpose="register", challenge=b"abc", client_id="c1")
    assert challenges.consume(state, "register") is None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registration_round_trip(service, store):
    request = FakeRequest()
    auth = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_test", client_name="Test Client",
    )
    assert options["rp"]["id"] == RP_ID
    assert b64url_decode(options["user"]["id"]) == b"beaconmcp_test"

    record = service.verify_registration(
        request, state=state, credential=auth.create(options), label="My laptop",
    )
    assert record.client_id == "beaconmcp_test"
    assert record.label == "My laptop"
    assert record.transports == ["internal"]
    assert store.count_for_client("beaconmcp_test") == 1


def test_registration_rejects_wrong_origin(service):
    request = FakeRequest()
    auth = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_test", client_name="Test Client",
    )
    with pytest.raises(PasskeyError):
        service.verify_registration(
            request,
            state=state,
            credential=auth.create(options, origin="https://evil.example"),
        )


def test_registration_state_bound_to_session(service):
    request = FakeRequest()
    auth = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_test", client_name="Test",
        session_id="session-a",
    )
    with pytest.raises(PasskeyError, match="another session"):
        service.verify_registration(
            request, state=state, credential=auth.create(options),
            session_id="session-b",
        )


def test_registration_excludes_known_credentials(service, enrolled):
    _, record = enrolled
    options, _ = service.registration_options(
        FakeRequest(), client_id="beaconmcp_test", client_name="Test",
    )
    assert [c["id"] for c in options["excludeCredentials"]] == [record.credential_id]


def test_registration_label_defaults_from_user_agent(service):
    request = FakeRequest()
    request.headers["user-agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)"
    auth = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_test", client_name="Test",
    )
    record = service.verify_registration(
        request, state=state, credential=auth.create(options),
    )
    assert record.label == "Mac"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_authentication_round_trip(service, enrolled, store):
    auth, record = enrolled
    request = FakeRequest()
    options, state = service.authentication_options(
        request, client_id="beaconmcp_test",
    )
    assert [c["id"] for c in options["allowCredentials"]] == [record.credential_id]

    verified = service.verify_authentication(
        request, state=state, credential=auth.get(options),
    )
    assert verified.client_id == "beaconmcp_test"
    assert store.get(record.credential_id).last_used_at is not None


def test_authentication_without_credentials_is_refused(service):
    with pytest.raises(PasskeyError, match="No passkey"):
        service.authentication_options(FakeRequest(), client_id="nobody")


def test_authentication_rejects_replayed_challenge(service, enrolled):
    auth, _ = enrolled
    request = FakeRequest()
    options, state = service.authentication_options(
        request, client_id="beaconmcp_test",
    )
    assertion = auth.get(options)
    service.verify_authentication(request, state=state, credential=assertion)
    with pytest.raises(PasskeyError, match="expired"):
        service.verify_authentication(request, state=state, credential=assertion)


def test_authentication_rejects_foreign_credential(service, enrolled):
    """A challenge minted for client A must not accept client B's passkey."""
    auth_a, _ = enrolled
    request = FakeRequest()

    other = SoftwareAuthenticator()
    options, state = service.registration_options(
        request, client_id="beaconmcp_other", client_name="Other",
    )
    service.verify_registration(
        request, state=state, credential=other.create(options),
    )

    options, state = service.authentication_options(
        request, client_id="beaconmcp_other",
    )
    # Sign the *other* client's challenge with the first client's key.
    with pytest.raises(PasskeyError, match="different client"):
        service.verify_authentication(
            request, state=state, credential=auth_a.get(options),
        )


def test_authentication_rejects_wrong_rp_id(service, enrolled):
    auth, _ = enrolled
    request = FakeRequest()
    options, state = service.authentication_options(
        request, client_id="beaconmcp_test",
    )
    with pytest.raises(PasskeyError):
        service.verify_authentication(
            request, state=state,
            credential=auth.get(options, rp_id="evil.example"),
        )


def test_sign_counter_regression_is_rejected(service, enrolled, store):
    auth, record = enrolled
    request = FakeRequest()
    options, state = service.authentication_options(
        request, client_id="beaconmcp_test",
    )
    service.verify_authentication(request, state=state, credential=auth.get(options))

    # Roll the authenticator's counter back, as a cloned key would.
    auth.sign_count = 0
    options, state = service.authentication_options(
        request, client_id="beaconmcp_test",
    )
    with pytest.raises(PasskeyError, match="sign count"):
        service.verify_authentication(
            request, state=state, credential=auth.get(options),
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_delete_is_scoped_to_owner(store, service, enrolled):
    _, record = enrolled
    assert store.delete(record.credential_id, "someone_else") is False
    assert store.delete(record.credential_id, "beaconmcp_test") is True
    assert store.count_for_client("beaconmcp_test") == 0


def test_service_without_store_reports_unavailable():
    service = PasskeyService(None)
    assert service.available is False
    with pytest.raises(PasskeyError):
        service.authentication_options(FakeRequest(), client_id="x")


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

class FakeClientStore:
    def __init__(self):
        self.clients = {
            "beaconmcp_test": {
                "secret": "sk_test", "name": "Test Client", "totp": "123456",
            }
        }

    def verify(self, client_id, secret):
        c = self.clients.get(client_id)
        return bool(c and c["secret"] == secret)

    def check_totp(self, client_id, code):
        c = self.clients.get(client_id)
        return TotpResult.OK if (c and c["totp"] == code) else TotpResult.INVALID

    def get_name(self, client_id):
        c = self.clients.get(client_id)
        return c["name"] if c else None


class FakeTokenStore:
    def __init__(self):
        self._tokens: dict[str, str] = {}
        self._n = 0

    def issue(self, client_id, name=None):
        self._n += 1
        token = f"bearer_{self._n}"
        self._tokens[token] = client_id
        return token, 86400

    def validate(self, token):
        return self._tokens.get(token)

    def revoke(self, token):
        self._tokens.pop(token, None)
        return True

    def list_named(self, client_id):
        return []


@pytest.fixture()
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    db = Database(tmp_path / "dashboard.db")
    passkey_store = PasskeyStore(db)
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        passkeys=PasskeyService(passkey_store),
    )
    app = Starlette(routes=build_dashboard_routes(deps))
    client = TestClient(
        app, follow_redirects=False, base_url=ORIGIN,
        headers={"x-forwarded-proto": "https"},
    )
    return client, deps, passkey_store


def _csrf(client) -> str:
    client.get("/app/login")
    return client.cookies.get(CSRF_COOKIE)


def _json_headers(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token, "X-BeaconMCP-Mode": "json"}


def _login(client) -> str:
    """Sign in with TOTP and return the *rotated* CSRF token."""
    token = _csrf(client)
    res = client.post(
        "/app/login",
        data={
            "csrf_token": token, "client_id": "beaconmcp_test",
            "client_secret": "sk_test", "totp": "123456",
        },
        headers=_json_headers(token),
    )
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def test_login_json_mode_returns_expiry_and_passkey_state(web):
    client, _, _ = web
    token = _csrf(client)
    res = client.post(
        "/app/login",
        data={
            "csrf_token": token, "client_id": "beaconmcp_test",
            "client_secret": "sk_test", "totp": "123456",
        },
        headers=_json_headers(token),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["client_name"] == "Test Client"
    assert body["bearer_expires_at"] > time.time()
    assert body["session_expires_at"] > body["bearer_expires_at"]
    assert body["passkeys_enabled"] is True
    assert body["passkey_count"] == 0
    assert client.cookies.get(SESSION_COOKIE)


def test_login_form_mode_still_redirects(web):
    client, _, _ = web
    token = _csrf(client)
    res = client.post(
        "/app/login",
        data={
            "csrf_token": token, "client_id": "beaconmcp_test",
            "client_secret": "sk_test", "totp": "123456",
        },
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/app/tokens"


def test_login_json_mode_reports_errors_as_json(web):
    client, _, _ = web
    token = _csrf(client)
    res = client.post(
        "/app/login",
        data={
            "csrf_token": token, "client_id": "beaconmcp_test",
            "client_secret": "sk_test", "totp": "000000",
        },
        headers=_json_headers(token),
    )
    assert res.status_code == 401
    assert res.json()["ok"] is False
    assert "2FA" in res.json()["error"]


def _register_passkey(client, token) -> SoftwareAuthenticator:
    auth = SoftwareAuthenticator()
    res = client.post(
        "/app/api/passkeys/register/options", json={}, headers=_json_headers(token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    res = client.post(
        "/app/api/passkeys/register/verify",
        json={"state": body["state"], "credential": auth.create(body["options"])},
        headers=_json_headers(token),
    )
    assert res.status_code == 201, res.text
    return auth


def test_register_then_sign_in_with_passkey(web):
    client, _, passkey_store = web
    token = _login(client)
    auth = _register_passkey(client, token)
    assert passkey_store.count_for_client("beaconmcp_test") == 1

    listed = client.get("/app/api/passkeys", headers=_json_headers(token)).json()
    assert len(listed["passkeys"]) == 1

    # Fresh browser: credentials + passkey, no TOTP anywhere.
    fresh = TestClient(
        client.app, follow_redirects=False, base_url=ORIGIN,
        headers={"x-forwarded-proto": "https"},
    )
    token2 = _csrf(fresh)
    res = fresh.post(
        "/app/api/passkeys/auth/options",
        json={"client_id": "beaconmcp_test", "client_secret": "sk_test"},
        headers=_json_headers(token2),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    res = fresh.post(
        "/app/api/passkeys/auth/verify",
        json={
            "client_id": "beaconmcp_test",
            "client_secret": "sk_test",
            "state": body["state"],
            "credential": auth.get(body["options"]),
        },
        headers=_json_headers(token2),
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["passkey_count"] == 1
    assert fresh.cookies.get(SESSION_COOKIE)


def test_passkey_auth_requires_valid_client_secret(web):
    client, _, _ = web
    token = _csrf(client)
    res = client.post(
        "/app/api/passkeys/auth/options",
        json={"client_id": "beaconmcp_test", "client_secret": "wrong"},
        headers=_json_headers(token),
    )
    assert res.status_code == 401
    assert res.json()["error"] == "Invalid credentials."


def test_passkey_auth_verify_rechecks_the_secret(web):
    """The state token alone must never be enough to mint a session."""
    client, _, _ = web
    token = _login(client)
    auth = _register_passkey(client, token)
    body = client.post(
        "/app/api/passkeys/auth/options",
        json={"client_id": "beaconmcp_test", "client_secret": "sk_test"},
        headers=_json_headers(token),
    ).json()
    res = client.post(
        "/app/api/passkeys/auth/verify",
        json={
            "client_id": "beaconmcp_test",
            "client_secret": "wrong",
            "state": body["state"],
            "credential": auth.get(body["options"]),
        },
        headers=_json_headers(token),
    )
    assert res.status_code == 401


def test_passkey_endpoints_require_csrf(web):
    client, _, _ = web
    _csrf(client)
    res = client.post(
        "/app/api/passkeys/auth/options",
        json={"client_id": "beaconmcp_test", "client_secret": "sk_test"},
    )
    assert res.status_code == 403


def test_passkey_registration_requires_a_session(web):
    client, _, _ = web
    token = _csrf(client)
    res = client.post(
        "/app/api/passkeys/register/options", json={}, headers=_json_headers(token),
    )
    assert res.status_code == 401


def test_passkey_delete_scoped_to_session_client(web):
    client, _, passkey_store = web
    token = _login(client)
    _register_passkey(client, token)
    credential_id = passkey_store.list_for_client("beaconmcp_test")[0].credential_id
    res = client.post(
        "/app/api/passkeys/delete",
        json={"credential_id": credential_id},
        headers=_json_headers(token),
    )
    assert res.status_code == 200
    assert passkey_store.count_for_client("beaconmcp_test") == 0


def test_tokens_page_lists_and_removes_passkeys(web):
    client, _, passkey_store = web
    token = _login(client)
    _register_passkey(client, token)

    page = client.get("/app/tokens").text
    assert "Passkeys" in page
    credential_id = passkey_store.list_for_client("beaconmcp_test")[0].credential_id
    assert credential_id in page
    assert "never used" in page

    res = client.post(
        "/app/passkeys/remove",
        data={"csrf_token": token, "credential_id": credential_id},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/app/tokens"
    assert passkey_store.count_for_client("beaconmcp_test") == 0


def test_passkeys_remove_requires_csrf(web):
    client, _, passkey_store = web
    token = _login(client)
    _register_passkey(client, token)
    credential_id = passkey_store.list_for_client("beaconmcp_test")[0].credential_id
    res = client.post(
        "/app/passkeys/remove",
        data={"csrf_token": "wrong", "credential_id": credential_id},
    )
    assert res.status_code == 403
    assert passkey_store.count_for_client("beaconmcp_test") == 1


def test_routes_report_unavailable_without_a_service(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "d.db"))
    db = Database(tmp_path / "d.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        passkeys=None,
    )
    client = TestClient(
        Starlette(routes=build_dashboard_routes(deps)),
        follow_redirects=False, base_url=ORIGIN,
    )
    client.get("/app/login")
    token = client.cookies.get(CSRF_COOKIE)
    res = client.post(
        "/app/api/passkeys/auth/options",
        json={"client_id": "beaconmcp_test", "client_secret": "sk_test"},
        headers=_json_headers(token),
    )
    assert res.status_code == 503


def test_login_page_hides_passkeys_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "d2.db"))
    db = Database(tmp_path / "d2.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
        passkeys=None,
    )
    client = TestClient(
        Starlette(routes=build_dashboard_routes(deps)), base_url=ORIGIN,
    )
    body = client.get("/app/login").text
    assert 'data-passkeys-enabled="false"' in body
    assert "passkey-login-btn" not in body
