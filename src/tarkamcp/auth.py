"""OAuth 2.1 client & token management for TarkaMCP HTTP mode.

Supports two grants on top of a pre-provisioned client store:
- ``client_credentials`` for non-interactive clients (scripts, server-to-server)
- ``authorization_code`` with mandatory PKCE (S256) for browser-based clients
  such as Claude Web / mobile connectors

Dynamic client registration (RFC 7591) is intentionally NOT supported: clients
must be created out-of-band via ``tarkamcp auth create``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyotp


# Populated by the HTTP auth middleware at the start of each request and
# cleared at the end. Lets MCP tools discover which bearer token they are
# executing under, so a tool like ``security_end_session`` can revoke it.
current_bearer_token: ContextVar[str | None] = ContextVar(
    "current_bearer_token", default=None
)

# Registered by the HTTP layer so MCP tools (instantiated via FastMCP, which
# runs before ``_run_http``) can reach the running TokenStore without an
# import cycle.
_active_token_store: "TokenStore | None" = None


def register_token_store(store: "TokenStore") -> None:
    global _active_token_store
    _active_token_store = store


def revoke_current_token() -> bool:
    """Revoke the bearer token associated with the in-flight request.

    Returns True if a token was found and revoked. Safe no-op if called
    outside an HTTP request context.
    """
    token = current_bearer_token.get()
    if not token or _active_token_store is None:
        return False
    return _active_token_store.revoke(token)


CLIENTS_FILE = Path("/opt/tarkamcp/clients.json")


@dataclass
class Client:
    client_id: str
    client_secret_hash: str
    name: str
    created_at: float
    totp_secret: str  # base32-encoded TOTP seed; plaintext on purpose


@dataclass
class AccessToken:
    token: str
    client_id: str
    expires_at: float


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    expires_at: float


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify a PKCE code_verifier against the stored code_challenge.

    OAuth 2.1 forbids the ``plain`` method; only S256 is accepted.
    """
    if method != "S256":
        return False
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)


class ClientStore:
    """Persistent client credential storage backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CLIENTS_FILE
        self._clients: dict[str, Client] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError as e:
            print(
                f"ERROR: {self._path} is not valid JSON ({e}). "
                "Fix or delete it before restarting.",
                file=sys.stderr,
            )
            raise

        # Clients missing totp_secret predate the 2FA migration and are
        # implicitly revoked. We log them, skip them, and rewrite the file
        # below so they can't be re-loaded next boot.
        revoked: list[str] = []
        for c in data.get("clients", []):
            if not c.get("totp_secret"):
                revoked.append(f"{c.get('client_id', '?')} ({c.get('name', '?')})")
                continue
            try:
                self._clients[c["client_id"]] = Client(**c)
            except TypeError:
                # Unknown fields or missing required fields: treat as revoked.
                revoked.append(f"{c.get('client_id', '?')} ({c.get('name', '?')})")

        if revoked:
            print(
                "WARNING: the following clients were revoked because they "
                "predate the 2FA migration (no TOTP secret). Recreate them "
                "with `tarkamcp auth create`: " + ", ".join(revoked),
                file=sys.stderr,
            )
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "clients": [
                {
                    "client_id": c.client_id,
                    "client_secret_hash": c.client_secret_hash,
                    "name": c.name,
                    "created_at": c.created_at,
                    "totp_secret": c.totp_secret,
                }
                for c in self._clients.values()
            ]
        }
        # Write atomically with restrictive permissions: the file holds secret
        # hashes and must not be world-readable.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)

    def create(self, name: str) -> tuple[str, str, str]:
        """Create a new client.

        Returns ``(client_id, client_secret, totp_secret)``. The TOTP secret is
        base32-encoded and meant to be displayed once so the operator can
        register it in Google Authenticator / Authy / 1Password.
        """
        client_id = "tarkamcp_" + secrets.token_hex(8)
        client_secret = "sk_" + secrets.token_hex(32)
        totp_secret = pyotp.random_base32()

        self._clients[client_id] = Client(
            client_id=client_id,
            client_secret_hash=_hash_secret(client_secret),
            name=name,
            created_at=time.time(),
            totp_secret=totp_secret,
        )
        self._save()
        return client_id, client_secret, totp_secret

    def verify(self, client_id: str, client_secret: str) -> bool:
        """Verify client credentials."""
        client = self._clients.get(client_id)
        if not client:
            return False
        # Constant-time comparison to avoid leaking the hash via timing.
        return hmac.compare_digest(client.client_secret_hash, _hash_secret(client_secret))

    def exists(self, client_id: str) -> bool:
        return client_id in self._clients

    def get_name(self, client_id: str) -> str | None:
        client = self._clients.get(client_id)
        return client.name if client else None

    def verify_totp(self, client_id: str, code: str) -> bool:
        """Validate a TOTP code for a given client.

        Uses ``valid_window=1`` so a ±30 s clock drift between the server and
        the authenticator app is tolerated.
        """
        client = self._clients.get(client_id)
        if not client:
            return False
        if not code or not code.isdigit() or len(code) != 6:
            return False
        return pyotp.TOTP(client.totp_secret).verify(code, valid_window=1)

    def list_clients(self) -> list[dict[str, Any]]:
        """List all registered clients (without secrets)."""
        return [
            {
                "client_id": c.client_id,
                "name": c.name,
                "created_at": c.created_at,
            }
            for c in self._clients.values()
        ]

    def revoke(self, client_id: str) -> bool:
        """Revoke a client. Returns True if found and removed."""
        if client_id in self._clients:
            del self._clients[client_id]
            self._save()
            return True
        return False


class TokenStore:
    """In-memory access token store with expiration."""

    TOKEN_TTL = 3600 * 24  # 24 hours

    def __init__(self) -> None:
        self._tokens: dict[str, AccessToken] = {}

    def issue(self, client_id: str) -> tuple[str, int]:
        """Issue an access token. Returns (token, expires_in)."""
        token = secrets.token_hex(32)
        self._tokens[token] = AccessToken(
            token=token,
            client_id=client_id,
            expires_at=time.time() + self.TOKEN_TTL,
        )
        self._cleanup()
        return token, self.TOKEN_TTL

    def validate(self, token: str) -> str | None:
        """Validate a token. Returns client_id if valid, None otherwise."""
        access_token = self._tokens.get(token)
        if not access_token:
            return None
        if time.time() > access_token.expires_at:
            del self._tokens[token]
            return None
        return access_token.client_id

    # Seconds to keep a revoked token alive so the current MCP response /
    # SSE stream has time to reach the client before the middleware starts
    # rejecting follow-up requests.
    REVOKE_GRACE_SECONDS = 8.0

    def revoke(self, token: str) -> bool:
        """Schedule a token for revocation after a short grace period.

        Returns True if the token existed. The token stays technically valid
        for :attr:`REVOKE_GRACE_SECONDS` seconds so the in-flight HTTP
        response (and any immediate SSE follow-up that MCP streamable-HTTP
        needs) can finish; after that the standard expiration check in
        :meth:`validate` rejects it.
        """
        access_token = self._tokens.get(token)
        if access_token is None:
            return False
        deadline = time.time() + self.REVOKE_GRACE_SECONDS
        if access_token.expires_at > deadline:
            access_token.expires_at = deadline
        return True

    def _cleanup(self) -> None:
        now = time.time()
        expired = [t for t, at in self._tokens.items() if now > at.expires_at]
        for t in expired:
            del self._tokens[t]


class CodeStore:
    """In-memory single-use authorization-code store with PKCE binding."""

    CODE_TTL = 60  # OAuth 2.1 recommends very short codes (<= 60s).

    def __init__(self) -> None:
        self._codes: dict[str, AuthCode] = {}

    def issue(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=time.time() + self.CODE_TTL,
        )
        self._cleanup()
        return code

    def consume(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> bool:
        """Validate and one-time consume a code.

        The code is popped unconditionally so a replay attempt cannot retry.
        """
        auth_code = self._codes.pop(code, None)
        if not auth_code:
            return False
        if time.time() > auth_code.expires_at:
            return False
        if auth_code.client_id != client_id:
            return False
        if auth_code.redirect_uri != redirect_uri:
            return False
        return verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method)

    def _cleanup(self) -> None:
        now = time.time()
        expired = [c for c, ac in self._codes.items() if now > ac.expires_at]
        for c in expired:
            del self._codes[c]
