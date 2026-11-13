"""sso on /oauth/authorize: BeaconMCP as an OIDC RP of an external IdP (Ory Hydra, Keycloak...).

BeaconMCP stays the OAuth AS for MCP clients. the IdP only replaces the human
step (TOTP / passkey): auth code + PKCE toward the IdP, id_token checked here
(sig via JWKS, iss, aud, azp, exp, nonce), then ``allow`` rules map the
account to the BeaconMCP clients it may authorize. deny by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from .config import OIDC_WILDCARD as WILDCARD
from .config import OidcConfig, OidcRule

_logger = logging.getLogger("beaconmcp.oidc")

# asym only: no "none", no HS* (the secret is shared with the IdP, never a sig key here)
ALLOWED_ALGS = frozenset({
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512", "EdDSA",
})

PENDING_TTL = 600.0
METADATA_TTL = 3600.0
# min gap between two JWKS refetches triggered by an unknown kid
JWKS_REFRESH_GAP = 60.0
CLOCK_LEEWAY = 60
HTTP_TIMEOUT = 10.0
MAX_PENDING = 1000


class OidcError(Exception):
    """sign-in failed. the message is for logs / audit, never shown verbatim to users."""


@dataclass
class PendingLogin:
    nonce: str
    verifier: str
    binding_hash: str
    redirect_uri: str
    payload: dict[str, str]
    expires_at: float
    # OIDC core 3.1.2.1: asked the IdP for an authentication this recent
    max_age: int | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OidcProvider:
    """one configured IdP. pending logins live in memory: a restart drops them, on purpose."""

    # tests swap this for an httpx.MockTransport
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(
        self, cfg: OidcConfig, *, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, PendingLogin] = {}
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    # --- allowlist -------------------------------------------------------

    def covers(self, *client_ids: str) -> bool:
        """true when some rule could let an account authorize one of these clients."""
        return any(
            r.client is not None and (r.client == WILDCARD or r.client in client_ids)
            for r in self.cfg.allow
        )

    def configured_clients(self) -> set[str]:
        return {r.client for r in self.cfg.allow if r.client is not None}

    @property
    def has_identity_rules(self) -> bool:
        return bool(self.cfg.identity_rules)

    def identity_allowed(self, claims: dict[str, Any]) -> bool:
        """an identity rule lets this account sign in as itself."""
        return any(self._rule_matches(r, claims) for r in self.cfg.identity_rules)

    def subject_key(self, claims: dict[str, Any]) -> str:
        """stable id of the account across sign-ins: issuer + sub."""
        return f"{self.cfg.issuer.rstrip('/')}#{claims.get('sub')}"

    def _rule_matches(self, rule: OidcRule, claims: dict[str, Any]) -> bool:
        sub = str(claims.get("sub") or "")
        email = claims.get("email")
        email = email.strip().lower() if isinstance(email, str) else ""
        # an unverified email is whatever the user typed at the IdP
        if self.cfg.require_email_verified and claims.get("email_verified") is not True:
            email = ""
        raw_groups = claims.get(self.cfg.groups_claim)
        if isinstance(raw_groups, str):
            groups = {raw_groups}
        elif isinstance(raw_groups, list):
            groups = {g for g in raw_groups if isinstance(g, str)}
        else:
            groups = set()
        return bool(
            (sub and sub in rule.subjects)
            or (email and email in rule.emails)
            or (groups & set(rule.groups))
        )

    def allowed_clients(self, claims: dict[str, Any]) -> set[str]:
        """rule clients these claims match (may hold ``*``). empty = nobody."""
        return {
            r.client for r in self.cfg.allow
            if r.client is not None and self._rule_matches(r, claims)
        }

    def matching_rule(self, claims: dict[str, Any], *client_ids: str) -> str | None:
        """which rule lets these claims authorize one of ``client_ids``: the exact
        id, or ``*`` when only a wildcard rule does. None = refused."""
        allowed = self.allowed_clients(claims)
        for cid in client_ids:
            if cid and cid in allowed:
                return cid
        # exact rule wins over the wildcard so the audit names the precise one
        return WILDCARD if WILDCARD in allowed else None

    # --- discovery -------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=HTTP_TIMEOUT, follow_redirects=False,
        )

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self._client() as http:
                resp = await http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"fetch {url} failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise OidcError(f"fetch {url}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OidcError(f"fetch {url}: not JSON") from exc
        if not isinstance(data, dict):
            raise OidcError(f"fetch {url}: not a JSON object")
        return data

    async def metadata(self) -> dict[str, Any]:
        now = self._clock()
        if self._metadata is not None and now - self._metadata_at < METADATA_TTL:
            return self._metadata
        url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
        data = await self._get_json(url)
        # OIDC discovery 4.3: the doc must name the issuer we asked for
        if str(data.get("issuer") or "").rstrip("/") != self.cfg.issuer.rstrip("/"):
            raise OidcError(
                f"discovery issuer mismatch: {data.get('issuer')!r} != {self.cfg.issuer!r}"
            )
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise OidcError(f"discovery doc has no {key}")
        self._metadata, self._metadata_at = data, now
        return data

    async def _jwks_keys(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = self._clock()
        stale = self._jwks is None or now - self._jwks_at >= METADATA_TTL
        can_force = force and now - self._jwks_at >= JWKS_REFRESH_GAP
        if stale or can_force:
            meta = await self.metadata()
            self._jwks = await self._get_json(meta["jwks_uri"])
            self._jwks_at = now
        keys = (self._jwks or {}).get("keys")
        return [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []

    # --- flow ------------------------------------------------------------

    def _prune(self) -> None:
        now = self._clock()
        for state in [s for s, p in self._pending.items() if p.expires_at < now]:
            del self._pending[state]

    async def start(
        self, *, redirect_uri: str, payload: dict[str, str],
        max_age: int | None = None,
    ) -> tuple[str, str]:
        """returns ``(idp_url, binding)``. ``binding`` goes in a cookie and must come back on callback.

        ``max_age`` asks the IdP to re-authenticate the user if their last
        authentication is older (0 = always); ``finish`` then checks auth_time.
        """
        meta = await self.metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        binding = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            if len(self._pending) >= MAX_PENDING:
                raise OidcError("too many sign-ins in flight")
            self._pending[state] = PendingLogin(
                nonce=nonce,
                verifier=verifier,
                binding_hash=_sha256(binding),
                redirect_uri=redirect_uri,
                payload=dict(payload),
                expires_at=self._clock() + PENDING_TTL,
                max_age=max_age,
            )
        query = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.cfg.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
        }
        if max_age is not None:
            query["max_age"] = str(max_age)
        endpoint = meta["authorization_endpoint"]
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{urlencode(query)}", binding

    def take(self, state: str, binding: str) -> PendingLogin | None:
        """single-use: pops the pending login if state and cookie binding match."""
        if not state:
            return None
        with self._lock:
            self._prune()
            pending = self._pending.get(state)
            if pending is None:
                return None
            if not binding or not hmac.compare_digest(
                pending.binding_hash, _sha256(binding),
            ):
                # wrong browser: leave it for the right one, it expires anyway
                return None
            del self._pending[state]
        return pending

    async def finish(self, pending: PendingLogin, code: str) -> dict[str, Any]:
        """code -> tokens -> validated id_token claims. raises OidcError."""
        if not code:
            raise OidcError("callback without code")
        meta = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.verifier,
        }
        auth: httpx.BasicAuth | None = None
        if self.cfg.token_auth_method == "client_secret_post":
            data["client_id"] = self.cfg.client_id
            data["client_secret"] = self.cfg.client_secret
        else:
            # RFC 6749 2.3.1: form-urlencode both parts before basic auth
            auth = httpx.BasicAuth(
                quote(self.cfg.client_id, safe=""),
                quote(self.cfg.client_secret, safe=""),
            )
        try:
            async with self._client() as http:
                resp = await http.post(
                    meta["token_endpoint"], data=data, auth=auth,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise OidcError(f"token request failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            err = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    err = str(body.get("error") or "")
            except ValueError:
                pass
            raise OidcError(f"token endpoint HTTP {resp.status_code} {err}".strip())
        try:
            tokens = resp.json()
        except ValueError as exc:
            raise OidcError("token response not JSON") from exc
        id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("token response has no id_token (is the openid scope allowed?)")
        claims = await self.validate_id_token(id_token, nonce=pending.nonce)
        if pending.max_age is not None:
            # OIDC core 3.1.2.1 / 2: auth_time is required once max_age is
            # sent, and an IdP that ignored max_age must not pass for fresh
            auth_time = claims.get("auth_time")
            if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
                raise OidcError("id_token has no auth_time although max_age was sent")
            if self._clock() - float(auth_time) > pending.max_age + CLOCK_LEEWAY:
                raise OidcError("id_token auth_time is older than the max_age asked for")
        return claims

    async def validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError(f"id_token malformed: {exc}") from exc
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise OidcError(f"id_token alg {alg!r} not allowed")
        kid = header.get("kid")

        jwk = self._pick_key(await self._jwks_keys(), kid, alg)
        if jwk is None:
            # key rotation at the IdP: refetch once
            jwk = self._pick_key(await self._jwks_keys(force=True), kid, alg)
        if jwk is None:
            raise OidcError(f"no JWKS key for kid {kid!r}")
        try:
            key = jwt.PyJWK(jwk, algorithm=alg).key
        except jwt.PyJWTError as exc:
            raise OidcError(f"unusable JWKS key: {exc}") from exc

        meta = await self.metadata()
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[alg],
                audience=self.cfg.client_id,
                issuer=meta["issuer"],
                leeway=CLOCK_LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"id_token rejected: {exc}") from exc

        # OIDC core 3.1.3.7: several aud -> azp must be us; azp present -> must be us
        aud = claims.get("aud")
        multi_aud = isinstance(aud, list) and len(aud) > 1
        if (multi_aud or "azp" in claims) and claims.get("azp") != self.cfg.client_id:
            raise OidcError("id_token azp does not name this client")
        got_nonce = claims.get("nonce")
        if not isinstance(got_nonce, str) or not hmac.compare_digest(got_nonce, nonce):
            raise OidcError("id_token nonce mismatch")
        if not str(claims.get("sub") or ""):
            raise OidcError("id_token without sub")
        return claims

    @staticmethod
    def _pick_key(
        keys: list[dict[str, Any]], kid: Any, alg: str,
    ) -> dict[str, Any] | None:
        candidates = [
            k for k in keys
            if k.get("use", "sig") == "sig"
            and (not k.get("alg") or k.get("alg") == alg)
        ]
        if kid is not None:
            for k in candidates:
                if k.get("kid") == kid:
                    return k
            return None
        # no kid in the header: only ok when there's no ambiguity
        return candidates[0] if len(candidates) == 1 else None
