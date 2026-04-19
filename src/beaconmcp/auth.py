"""OAuth 2.1 client & token management for BeaconMCP HTTP mode.

Supports two grants on top of a pre-provisioned client store:
- ``client_credentials`` for non-interactive clients (scripts, server-to-server)
- ``authorization_code`` with mandatory PKCE (S256) for browser-based clients
  such as Assistant Web / mobile connectors

Dynamic client registration (RFC 7591) is available through a narrow,
opt-in path: the dashboard mints a single-use bootstrap URL that lets a
client (typically ChatGPT) self-register a derived OAuth client bound to
its human owner. The derived client has no independent TOTP seed — 2FA at
``/oauth/authorize`` is verified against the owner's seed so the second
factor never leaves the owner's phone.
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


CLIENTS_FILE = Path("/opt/beaconmcp/clients.json")


# Allowlist of redirect_uri prefixes accepted from DCR clients. Attack:
# a rogue script registers itself against your /oauth/register/c/<slug>
# with redirect_uri="https://evil.example/cb". If you later authorize it
# (fooled into thinking it's ChatGPT), the authorization code lands on
# the attacker's server. Validating here stops that before a client row
# is ever persisted.
#
# Each prefix matches an origin + (optional) path prefix. Wildcards are
# only implicit via prefix matching — a listed origin covers every path
# beneath it. Custom OS URI schemes (vscode://, cursor://) are matched
# scheme-only because their host semantics don't carry meaning.
#
# Add a new client's origin here BEFORE flipping
# ``allow_dynamic_registration`` on for it, not after.
TRUSTED_REDIRECT_PREFIXES: tuple[str, ...] = (
    # Anthropic / Assistant
    "https://assistant.ai/",
    "https://assistant.com/",
    # OpenAI / ChatGPT + Codex + platform
    "https://chatgpt.com/",
    "https://chat.openai.com/",
    "https://platform.openai.com/",
    # Google / Gemini CLI + AI Studio
    "https://gemini.google.com/",
    "https://aistudio.google.com/",
    "https://console.cloud.google.com/",
    # Mistral
    "https://chat.mistral.ai/",
    "https://console.mistral.ai/",
    # VS Code web surfaces
    "https://vscode.dev/",
    "https://github.dev/",
    # Cursor dashboard
    "https://cursor.com/",
    # OS-level custom URI schemes used by desktop clients
    "vscode://",
    "vscode-insiders://",
    "cursor://",
    # Local loopback for every CLI / terminal client (Codex, Gemini CLI,
    # Mistral Vibe, OpenCode, mcp-remote, …). Ports are dynamic so we
    # match the scheme + loopback host and let the client pick the port.
    "http://localhost:",
    "http://localhost/",
    "http://127.0.0.1:",
    "http://127.0.0.1/",
    "http://[::1]:",
    "http://[::1]/",
)


def is_trusted_redirect_uri(redirect_uri: str) -> bool:
    """Return True iff ``redirect_uri`` starts with a known-trusted prefix.

    Callers SHOULD reject any DCR ``redirect_uris`` entry for which this
    returns False. See :data:`TRUSTED_REDIRECT_PREFIXES` for the rationale
    and the list of accepted prefixes.
    """
    if not isinstance(redirect_uri, str) or not redirect_uri:
        return False
    return any(redirect_uri.startswith(p) for p in TRUSTED_REDIRECT_PREFIXES)


@dataclass
class Client:
    client_id: str
    client_secret_hash: str
    name: str
    created_at: float
    # base32-encoded TOTP seed; plaintext on purpose. Empty string for
    # dynamically-registered clients, which delegate TOTP verification to
    # their owner.
    totp_secret: str
    # For clients born of an OAuth DCR bootstrap (e.g. ChatGPT), points at
    # the human client whose TOTP seed guards /oauth/authorize for this
    # client. ``None`` for CLI-provisioned clients (the common case).
    owner_client_id: str | None = None
    # Free-form tag describing how this client was created. ``None`` for
    # CLI-provisioned clients; ``"chatgpt:<slug>"`` for DCR-created ones.
    # Used by the dashboard to group and revoke derived clients.
    registration_source: str | None = None


@dataclass
class AccessToken:
    token: str
    client_id: str
    expires_at: float
    # Optional human label for tokens minted from the dashboard "API
    # tokens" page. ``None`` means it's an internal dashboard-session
    # bearer, which is not counted against the per-client cap and is
    # not listed in the external-tokens UI.
    name: str | None = None
    created_at: float = 0.0


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
        # implicitly revoked UNLESS they have an owner_client_id (a derived
        # DCR client delegates TOTP to its owner and legitimately has an
        # empty seed). We log legacy-pre-2FA rows and drop them; we keep
        # dynamic rows.
        revoked: list[str] = []
        for c in data.get("clients", []):
            has_secret = bool(c.get("totp_secret"))
            has_owner = bool(c.get("owner_client_id"))
            if not has_secret and not has_owner:
                revoked.append(f"{c.get('client_id', '?')} ({c.get('name', '?')})")
                continue
            try:
                self._clients[c["client_id"]] = Client(
                    client_id=c["client_id"],
                    client_secret_hash=c["client_secret_hash"],
                    name=c["name"],
                    created_at=c["created_at"],
                    totp_secret=c.get("totp_secret", ""),
                    owner_client_id=c.get("owner_client_id"),
                    registration_source=c.get("registration_source"),
                )
            except (KeyError, TypeError):
                revoked.append(f"{c.get('client_id', '?')} ({c.get('name', '?')})")

        if revoked:
            print(
                "WARNING: the following clients were revoked because they "
                "predate the 2FA migration (no TOTP secret). Recreate them "
                "with `beaconmcp auth create`: " + ", ".join(revoked),
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
                    "owner_client_id": c.owner_client_id,
                    "registration_source": c.registration_source,
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
        client_id = "beaconmcp_" + secrets.token_hex(8)
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

        Dynamic clients (those with ``owner_client_id`` set) delegate
        verification to the owner's seed: the human typing the code is
        always the account owner, regardless of which client they are
        minting a token for. The delegation chain is single-hop — an
        owner whose own ``owner_client_id`` is set would be a bug.
        """
        client = self._clients.get(client_id)
        if not client:
            return False
        if not code or not code.isdigit() or len(code) != 6:
            return False
        if client.owner_client_id is not None:
            owner = self._clients.get(client.owner_client_id)
            if owner is None or not owner.totp_secret:
                return False
            return pyotp.TOTP(owner.totp_secret).verify(code, valid_window=1)
        if not client.totp_secret:
            return False
        return pyotp.TOTP(client.totp_secret).verify(code, valid_window=1)

    def list_clients(self) -> list[dict[str, Any]]:
        """List all registered clients (without secrets)."""
        return [
            {
                "client_id": c.client_id,
                "name": c.name,
                "created_at": c.created_at,
                "owner_client_id": c.owner_client_id,
                "registration_source": c.registration_source,
            }
            for c in self._clients.values()
        ]

    def list_derived(self, owner_client_id: str) -> list[Client]:
        """Return every dynamic client delegating TOTP to this owner."""
        return [
            c for c in self._clients.values()
            if c.owner_client_id == owner_client_id
        ]

    def revoke(self, client_id: str) -> bool:
        """Revoke a client. Returns True if found and removed.

        Revoking an owner cascades: every derived client is dropped with
        it (a derived client can't authenticate without the owner's TOTP
        seed anyway — leaving orphaned rows around is pure clutter).
        """
        client = self._clients.get(client_id)
        if client is None:
            return False
        del self._clients[client_id]
        # Cascade to derived clients when the deleted row was an owner.
        if client.owner_client_id is None:
            derived = [
                c.client_id for c in self._clients.values()
                if c.owner_client_id == client_id
            ]
            for cid in derived:
                del self._clients[cid]
        self._save()
        return True

    def create_dynamic(
        self,
        *,
        owner_client_id: str,
        name: str,
        registration_source: str,
    ) -> tuple[str, str]:
        """Provision a derived OAuth client for DCR.

        Returns ``(client_id, client_secret)``. The client has no TOTP
        seed of its own; ``verify_totp`` for this client delegates to
        ``owner_client_id``'s seed. The owner MUST already exist.
        """
        if owner_client_id not in self._clients:
            raise ValueError(f"unknown owner_client_id: {owner_client_id!r}")
        client_id = "beaconmcp_" + secrets.token_hex(8)
        client_secret = "sk_" + secrets.token_hex(32)
        self._clients[client_id] = Client(
            client_id=client_id,
            client_secret_hash=_hash_secret(client_secret),
            name=name,
            created_at=time.time(),
            totp_secret="",
            owner_client_id=owner_client_id,
            registration_source=registration_source,
        )
        self._save()
        return client_id, client_secret

    def get(self, client_id: str) -> Client | None:
        return self._clients.get(client_id)


class TokenCapExceeded(Exception):
    """Raised when a client already has the maximum number of named tokens."""


class TokenStore:
    """In-memory access token store with expiration."""

    TOKEN_TTL = 3600 * 24  # 24 hours
    # Cap on named tokens (the ones listed in the dashboard's API
    # tokens page). Internal dashboard-session bearers are unlimited
    # because a re-login always revokes the prior one.
    NAMED_TOKEN_CAP = 3

    def __init__(self) -> None:
        self._tokens: dict[str, AccessToken] = {}

    def issue(
        self, client_id: str, *, name: str | None = None,
    ) -> tuple[str, int]:
        """Issue an access token. Returns ``(token, expires_in)``.

        If ``name`` is provided the token counts against the per-client
        named-token cap. Raises :class:`TokenCapExceeded` if the cap is
        already met.
        """
        if name is not None:
            if self.count_named(client_id) >= self.NAMED_TOKEN_CAP:
                raise TokenCapExceeded(
                    f"client {client_id} already has "
                    f"{self.NAMED_TOKEN_CAP} named tokens"
                )
        token = secrets.token_hex(32)
        now = time.time()
        self._tokens[token] = AccessToken(
            token=token,
            client_id=client_id,
            expires_at=now + self.TOKEN_TTL,
            name=name,
            created_at=now,
        )
        self._cleanup()
        return token, self.TOKEN_TTL

    def list_named(self, client_id: str) -> list[AccessToken]:
        """Return named tokens for ``client_id`` (newest first)."""
        self._cleanup()
        out = [
            t for t in self._tokens.values()
            if t.client_id == client_id and t.name is not None
        ]
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out

    def count_named(self, client_id: str) -> int:
        self._cleanup()
        return sum(
            1 for t in self._tokens.values()
            if t.client_id == client_id and t.name is not None
        )

    def revoke_named(self, token_prefix: str, client_id: str) -> bool:
        """Revoke a named token owned by ``client_id``, identified by prefix.

        The prefix must match exactly one of the client's named tokens.
        Returns ``True`` on successful revocation, ``False`` otherwise.
        """
        if len(token_prefix) < 6:
            return False
        matches = [
            t for t in self._tokens.values()
            if t.token.startswith(token_prefix)
            and t.client_id == client_id
            and t.name is not None
        ]
        if len(matches) != 1:
            return False
        return self.revoke(matches[0].token)

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
