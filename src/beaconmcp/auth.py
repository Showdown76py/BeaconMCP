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
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyotp

_logger = logging.getLogger("beaconmcp.auth")


class TotpResult(Enum):
    """Outcome of :meth:`ClientStore.check_totp`."""

    OK = "ok"
    #: Wrong, malformed, or expired code -- a real failed auth attempt.
    INVALID = "invalid"
    #: Correct code, but already spent in this 30 s step. Not an attack:
    #: the operator hit a second 2FA prompt before the code rolled over.
    REPLAY = "replay"


#: Shown to the operator when a correct-but-already-used code is submitted.
#: The distinction matters -- "invalid code" sends people hunting for a clock
#: skew problem they don't have.
TOTP_REPLAY_MESSAGE = (
    "That 2FA code was already used. Wait for your authenticator to show the "
    "next one, then try again."
)


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


def current_client_id() -> str | None:
    """Resolve the client_id behind the in-flight request's bearer token.

    Returns ``None`` outside an HTTP request context (e.g. CLI use) or when
    no token store is registered. Used by the audit log to attribute MCP
    tool calls to the client that made them.
    """
    token = current_bearer_token.get()
    if not token or _active_token_store is None:
        return None
    return _active_token_store.validate(token)


CLIENTS_FILE = Path("/opt/beaconmcp/clients.json")


# OS-level custom URI schemes used by desktop clients. Not CORS origins, so
# they cannot be modeled via ``server.allowed_origins``. Prefix-matched: the
# scheme itself is the authority (only the registering app can claim it).
TRUSTED_CUSTOM_SCHEME_PREFIXES: tuple[str, ...] = (
    "vscode://",
    "vscode-insiders://",
    "cursor://",
)

# Loopback callback hosts for CLI / terminal clients (RFC 8252 style).
# Matched against the *parsed* hostname, never by string prefix: a prefix
# test on "http://localhost:" also accepts
# ``http://localhost:1@attacker.example/cb``, whose real host is
# attacker.example -- a straight authorization-code exfiltration path.
TRUSTED_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1"}
)

# Kept for backwards compatibility with anything importing the old name.
TRUSTED_NON_ORIGIN_REDIRECT_PREFIXES: tuple[str, ...] = (
    TRUSTED_CUSTOM_SCHEME_PREFIXES
    + (
        "http://localhost:",
        "http://localhost/",
        "http://127.0.0.1:",
        "http://127.0.0.1/",
        "http://[::1]:",
        "http://[::1]/",
    )
)


def is_trusted_redirect_uri(
    redirect_uri: str,
    allowed_origins: Iterable[str] | None = None,
) -> bool:
    """Return True iff ``redirect_uri`` is trusted for OAuth redirects.

    Web origins are sourced from ``allowed_origins`` (typically
    ``config.server.allowed_origins``), so redirect trust follows the same
    operator-controlled allowlist as CORS. Non-origin redirect forms are
    covered by :data:`TRUSTED_CUSTOM_SCHEME_PREFIXES` (desktop schemes) and
    :data:`TRUSTED_LOOPBACK_HOSTS` (RFC 8252 loopback callbacks).
    """
    if not isinstance(redirect_uri, str):
        return False
    redirect_uri = redirect_uri.strip()
    if not redirect_uri:
        return False
    if any(redirect_uri.startswith(p) for p in TRUSTED_CUSTOM_SCHEME_PREFIXES):
        return True
    # Loopback: resolve the real host instead of trusting the string shape,
    # so userinfo tricks (``http://localhost:1@evil.example/cb``) don't slip
    # through as "starts with http://localhost:".
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    if parsed.scheme == "http":
        hostname = (parsed.hostname or "").strip().lower()
        if hostname in TRUSTED_LOOPBACK_HOSTS:
            return True
        # Any other plaintext-HTTP callback is untrusted, full stop --
        # never fall through to the origin allowlist for it.
        return False
    if allowed_origins:
        for origin in allowed_origins:
            if not isinstance(origin, str) or not origin:
                continue
            origin = origin.strip()
            if not origin:
                continue
            prefix = origin if origin.endswith("/") else origin + "/"
            if redirect_uri.startswith(prefix):
                return True
    return False


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
    # Refresh-token family the token was minted in (OAuth
    # ``authorization_code`` grant only). Lets reuse detection and
    # end-of-session revoke every bearer of the family at once.
    family_id: str | None = None


@dataclass
class RefreshToken:
    # SHA-256 of the raw token: only the client ever holds the raw value.
    token_hash: str
    client_id: str
    family_id: str
    expires_at: float
    created_at: float
    # Absolute end of the sign-in this token belongs to: the
    # ``authorization_code`` exchange plus ``TokenStore.SESSION_LIFETIME``.
    # Shared by every token of the family and never pushed back by a
    # refresh, so a connector goes back through TOTP once a day.
    family_expires_at: float
    # Rotated out. Kept until the family ends so a replay can be recognised
    # as reuse (and the family revoked) instead of a plain unknown token.
    used: bool = False
    # When it was rotated out, and the pair that rotation minted. Memory
    # only, never persisted: they let a client that timed out and resent
    # the very same refresh call get the pair it missed (see
    # ``TokenStore.REFRESH_REUSE_GRACE``) instead of tripping reuse
    # detection. Cleared once the grace window is over.
    used_at: float = 0.0
    replay: OAuthGrant | None = None


@dataclass
class OAuthGrant:
    """Result of an OAuth token issuance or refresh."""

    access_token: str
    expires_in: int
    refresh_token: str | None = None


class RefreshTokenError(Exception):
    """A refresh token was rejected.

    ``reason`` is one of ``invalid`` (unknown, rotated-out-and-purged or
    owned by another client), ``expired`` or ``reused`` (an already-rotated
    token was replayed: its whole family has been revoked).
    """

    def __init__(self, reason: str, *, family_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.family_id = family_id


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
    """Persistent client credential storage backed by a JSON file.

    The file is shared with the ``beaconmcp auth`` CLI, which runs in its
    own process while the server is up. Every public method first checks
    whether the file changed on disk and re-reads it if so, so an ``auth
    revoke`` cuts the client off at its next request instead of at the next
    restart, and a server-side save (DCR) can't overwrite a CLI change.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CLIENTS_FILE
        self._clients: dict[str, Client] = {}
        # Guards ``_clients`` and the on-disk signature below: reads come
        # from the event loop and worker threads alike, and a reload swaps
        # the whole dict. Re-entrant because ``_load`` may call ``_save``.
        self._lock = threading.RLock()
        # (mtime_ns, size, inode) of the file as last read or written by
        # this process. ``None`` when the file does not exist yet.
        self._disk_sig: tuple[int, int, int] | None = None
        # Called with the ids of the surviving clients after a reload that
        # dropped at least one client. The server hooks
        # ``TokenStore.prune_unknown_clients`` here so the revoked client's
        # OAuth bearers and refresh tokens die with it.
        self.on_clients_removed: Callable[[set[str]], object] | None = None
        # Last accepted TOTP timestep (30s counter) per seed owner, used to
        # reject replays of an already-consumed code. Keyed by the SEED owner
        # so two clients sharing one owner's seed can't each spend the same
        # code. Guarded by its own lock -- verify_totp runs on worker threads.
        self._last_totp_step: dict[str, int] = {}
        self._totp_lock = threading.Lock()
        self._load()

    def _stat_sig(self) -> tuple[int, int, int] | None:
        """Cheap change detector for the file: mtime alone is not enough,
        two CLI writes inside one kernel tick share it, so size and inode
        (``_save`` replaces the file, so the inode moves) are folded in."""
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _maybe_reload(self) -> None:
        """Re-read ``clients.json`` if another process rewrote it."""
        sig = self._stat_sig()
        if sig is None or sig == self._disk_sig:
            # Unchanged, or file gone: keep what we have. A missing file is
            # not "no clients" -- it is more likely a broken deploy, and
            # dropping every client (and their tokens) on it would be worse.
            return
        with self._lock:
            if sig == self._disk_sig:
                return
            before = set(self._clients)
            try:
                self._load()
            except (json.JSONDecodeError, OSError):
                # Half-written or broken file: keep serving the current set
                # rather than nothing, and don't retry until it changes again.
                self._disk_sig = sig
                return
            removed = before - set(self._clients)
            callback = self.on_clients_removed if removed else None
        if removed:
            _logger.info(
                "%s changed on disk: %d client(s) removed (%s)",
                self._path, len(removed), ", ".join(sorted(removed)),
            )
        if callback is not None:
            callback(set(self._clients))

    def _load(self) -> None:
        sig = self._stat_sig()
        if sig is None:
            self._disk_sig = None
            return
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError as e:
            _logger.error(
                "%s is not valid JSON (%s). Fix or delete it before restarting.",
                self._path, e,
            )
            raise

        # Clients missing totp_secret predate the 2FA migration and are
        # implicitly revoked UNLESS they have an owner_client_id (a derived
        # DCR client delegates TOTP to its owner and legitimately has an
        # empty seed). We log legacy-pre-2FA rows and drop them; we keep
        # dynamic rows.
        revoked: list[str] = []
        clients: dict[str, Client] = {}
        for c in data.get("clients", []):
            has_secret = bool(c.get("totp_secret"))
            has_owner = bool(c.get("owner_client_id"))
            if not has_secret and not has_owner:
                revoked.append(f"{c.get('client_id', '?')} ({c.get('name', '?')})")
                continue
            try:
                clients[c["client_id"]] = Client(
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

        self._clients = clients
        self._disk_sig = sig
        if revoked:
            _logger.warning(
                "Revoked %d pre-2FA client(s) with no TOTP secret. "
                "Recreate them with `beaconmcp auth create`: %s",
                len(revoked), ", ".join(revoked),
            )
            self._save()

    def _save(self) -> None:
        """Write the file atomically, owner-only. Caller holds the lock."""
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
        # Our own write is not a change worth reloading.
        self._disk_sig = self._stat_sig()

    def create(self, name: str) -> tuple[str, str, str]:
        """Create a new client.

        Returns ``(client_id, client_secret, totp_secret)``. The TOTP secret is
        base32-encoded and meant to be displayed once so the operator can
        register it in Google Authenticator / Authy / 1Password.
        """
        client_id = "beaconmcp_" + secrets.token_hex(8)
        client_secret = "sk_" + secrets.token_hex(32)
        totp_secret = pyotp.random_base32()

        self._maybe_reload()
        with self._lock:
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
        self._maybe_reload()
        client = self._clients.get(client_id)
        if not client:
            return False
        # Constant-time comparison to avoid leaking the hash via timing.
        return hmac.compare_digest(client.client_secret_hash, _hash_secret(client_secret))

    def exists(self, client_id: str) -> bool:
        self._maybe_reload()
        return client_id in self._clients

    def get_name(self, client_id: str) -> str | None:
        self._maybe_reload()
        client = self._clients.get(client_id)
        return client.name if client else None

    def check_totp(self, client_id: str, code: str) -> TotpResult:
        """Validate a TOTP code for a given client.

        Uses a ±1 step (±30 s) window so a clock drift between the server and
        the authenticator app is tolerated.

        Dynamic clients (those with ``owner_client_id`` set) delegate
        verification to the owner's seed: the human typing the code is
        always the account owner, regardless of which client they are
        minting a token for. The delegation chain is single-hop — an
        owner whose own ``owner_client_id`` is set would be a bug.

        Replay protection: each 6-digit code stays valid for its whole 30 s
        step (plus drift), so without bookkeeping the same code could be
        spent twice. We record the last accepted timestep per SEED OWNER and
        reject any code whose matched step is not strictly newer. Keying on
        the owner (not the client) ensures two clients sharing one owner's
        seed cannot each replay the same code.

        ``REPLAY`` is reported separately from ``INVALID`` because the two
        need different handling: a genuinely wrong code is a failed auth
        attempt and must count towards the lockout, whereas re-submitting a
        code the operator already spent (a second 2FA prompt inside the same
        30 s step, a browser POST replay) is an honest mistake. Counting it
        as a failure would lock a legitimate operator out for 5 minutes.
        """
        self._maybe_reload()
        client = self._clients.get(client_id)
        if not client:
            return TotpResult.INVALID
        if not code or not code.isdigit() or len(code) != 6:
            return TotpResult.INVALID

        if client.owner_client_id is not None:
            owner = self._clients.get(client.owner_client_id)
            if owner is None or not owner.totp_secret:
                return TotpResult.INVALID
            seed = owner.totp_secret
            replay_key = client.owner_client_id
        else:
            if not client.totp_secret:
                return TotpResult.INVALID
            seed = client.totp_secret
            replay_key = client_id

        # Find which 30s timestep the code matches (within the ±1 window).
        totp = pyotp.TOTP(seed)
        now = time.time()
        current_step = int(now // totp.interval)
        matched_step: int | None = None
        for offset in (-1, 0, 1):
            at_time = now + offset * totp.interval
            if totp.verify(code, for_time=at_time):
                matched_step = current_step + offset
                break
        if matched_step is None:
            return TotpResult.INVALID

        # Reject replays: the matched step must be strictly newer than the
        # last one accepted for this owner.
        with self._totp_lock:
            last = self._last_totp_step.get(replay_key)
            if last is not None and matched_step <= last:
                return TotpResult.REPLAY
            self._last_totp_step[replay_key] = matched_step
        return TotpResult.OK

    def verify_totp(self, client_id: str, code: str) -> bool:
        """Boolean form of :meth:`check_totp` for callers that don't need to
        tell a replay apart from a wrong code."""
        return self.check_totp(client_id, code) is TotpResult.OK

    def list_clients(self) -> list[dict[str, Any]]:
        """List all registered clients (without secrets)."""
        self._maybe_reload()
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
        self._maybe_reload()
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
        self._maybe_reload()
        with self._lock:
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
        self._maybe_reload()
        with self._lock:
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
        self._maybe_reload()
        return self._clients.get(client_id)


class TokenCapExceeded(Exception):
    """Raised when a client already has the maximum number of named tokens."""


class TokenStore:
    """Access token store with expiration and optional persistence.

    Dashboard-session bearers (``issue`` without a name) live only in
    memory -- a re-login or restart is expected to drop them. Everything a
    remote client holds is persisted to a small SQLite file when
    ``db_path`` is given, so a ``systemctl restart`` / redeploy no longer
    silently invalidates it:

    * *named* tokens (dashboard "API tokens" page), stored as-is;
    * OAuth access tokens and refresh tokens (``issue_oauth`` / ``refresh``),
      stored as SHA-256 hashes and kept in memory keyed by that hash.
    """

    TOKEN_TTL = 3600 * 24  # 24 hours -- internal/session bearers
    # Default lifetime for *named* tokens. They survive restarts (see the
    # persistence block), so a 24 h cap defeats the point of a token pasted
    # into an external client -- 30 days is the sensible default. Overridable
    # per-deployment via ``named_token_ttl`` (server.named_token_ttl / env).
    NAMED_TOKEN_TTL = 3600 * 24 * 30  # 30 days
    # Absolute lifetime of an OAuth sign-in (``authorization_code`` grant).
    # Every access and refresh token of a family expires at the exchange
    # time plus this, whatever ``refresh_token_ttl`` says: a refresh can
    # carry the client up to that point but never past it, so a connector
    # goes back through the TOTP page once a day. This server can run shell
    # on the hypervisors; a sign-in that never ends is not an option.
    SESSION_LIFETIME = 3600 * 24  # 24 hours
    # Idle lifetime of an OAuth refresh token: a refresh rotates it and
    # restarts this clock, within the session lifetime above. Overridable
    # via ``refresh_token_ttl`` (server.refresh_token_ttl / env), capped at
    # ``SESSION_LIFETIME``; ``0`` (never expires) is not accepted.
    REFRESH_TOKEN_TTL = SESSION_LIFETIME
    # A refresh token presented again this soon after it was rotated out
    # is treated as a retry of the same call (client timed out, resent)
    # and gets the pair the first call minted, instead of being counted as
    # reuse and killing the family -- which forced a TOTP re-login on a
    # plain network retry. Strict RFC 9700 behaviour resumes after it.
    REFRESH_REUSE_GRACE = 60.0
    # Cap on named tokens (the ones listed in the dashboard's API
    # tokens page). Internal dashboard-session bearers are unlimited
    # because a re-login always revokes the prior one.
    NAMED_TOKEN_CAP = 3

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        named_token_ttl: int | None = None,
        refresh_token_ttl: int | None = None,
    ) -> None:
        self._tokens: dict[str, AccessToken] = {}
        # OAuth bearers and refresh tokens, keyed by SHA-256 of the raw token.
        self._oauth_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._db: sqlite3.Connection | None = None
        # Re-entrant: tools now run on worker threads (see server._metric_tool),
        # so the in-memory dict is touched concurrently. Several methods hold
        # the lock and then call a helper that re-acquires it (issue ->
        # _cleanup, revoke_named -> revoke), so a plain Lock would deadlock.
        # Never held across a blocking call -- all work here is in-memory.
        self._lock = threading.RLock()
        # Effective named-token lifetime: explicit value wins, else default.
        # ``0`` is a deliberate setting (tokens never expire, revoke-only),
        # so only ``None`` falls back to the 30-day default.
        self.named_token_ttl = (
            named_token_ttl if named_token_ttl is not None else self.NAMED_TOKEN_TTL
        )
        if refresh_token_ttl is None:
            refresh_token_ttl = self.REFRESH_TOKEN_TTL
        if not 0 < refresh_token_ttl <= self.SESSION_LIFETIME:
            raise ValueError(
                "refresh_token_ttl must be between 1 and "
                f"{self.SESSION_LIFETIME} seconds (a sign-in never outlives 24 h)"
            )
        self.refresh_token_ttl = refresh_token_ttl
        if db_path is not None:
            self._init_db(Path(db_path))

    # --- persistence -----------------------------------------------------

    def _init_db(self, path: Path) -> None:
        """Open the SQLite store and load any still-valid named tokens."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            # Raw bearer tokens live in this file -- owner-only, like
            # clients.json (sqlite3 creates it with the default umask).
            os.chmod(path, 0o600)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS named_tokens ("
                " token TEXT PRIMARY KEY,"
                " client_id TEXT NOT NULL,"
                " name TEXT NOT NULL,"
                " expires_at REAL NOT NULL,"
                " created_at REAL NOT NULL)"
            )
            # OAuth material is stored hashed: a leaked tokens.db must not
            # hand out working bearers or refresh tokens.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS oauth_tokens ("
                " token_hash TEXT PRIMARY KEY,"
                " client_id TEXT NOT NULL,"
                " family_id TEXT,"
                " expires_at REAL NOT NULL,"
                " created_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS refresh_tokens ("
                " token_hash TEXT PRIMARY KEY,"
                " client_id TEXT NOT NULL,"
                " family_id TEXT NOT NULL,"
                " expires_at REAL NOT NULL,"
                " created_at REAL NOT NULL,"
                " used INTEGER NOT NULL DEFAULT 0,"
                " family_expires_at REAL)"
            )
            conn.commit()
            self._db = conn
            now = time.time()
            self._bound_legacy_sessions(conn)
            for row in conn.execute(
                "SELECT token, client_id, name, expires_at, created_at "
                "FROM named_tokens WHERE expires_at > ?",
                (now,),
            ).fetchall():
                self._tokens[row[0]] = AccessToken(
                    token=row[0], client_id=row[1], expires_at=row[3],
                    name=row[2], created_at=row[4],
                )
            for row in conn.execute(
                "SELECT token_hash, client_id, family_id, expires_at, created_at "
                "FROM oauth_tokens WHERE expires_at > ?",
                (now,),
            ).fetchall():
                self._oauth_tokens[row[0]] = AccessToken(
                    token=row[0], client_id=row[1], expires_at=row[3],
                    created_at=row[4], family_id=row[2],
                )
            for row in conn.execute(
                "SELECT token_hash, client_id, family_id, expires_at, created_at, used, "
                "family_expires_at FROM refresh_tokens WHERE expires_at > ?",
                (now,),
            ).fetchall():
                self._refresh_tokens[row[0]] = RefreshToken(
                    token_hash=row[0], client_id=row[1], family_id=row[2],
                    expires_at=row[3], created_at=row[4], family_expires_at=row[6],
                    used=bool(row[5]),
                )
            # Drop rows that expired while the process was down.
            conn.execute("DELETE FROM named_tokens WHERE expires_at <= ?", (now,))
            conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,))
            conn.execute("DELETE FROM refresh_tokens WHERE expires_at <= ?", (now,))
            conn.commit()
        except Exception:  # noqa: BLE001 -- persistence must never block startup
            _logger.exception("token persistence disabled (db init failed)")
            self._db = None

    def _bound_legacy_sessions(self, conn: sqlite3.Connection) -> None:
        """Give rows written before ``family_expires_at`` existed a session end.

        Those builds let a family live as long as it kept refreshing. Each
        family is bounded from its first token (the rotated-out rows are
        kept, so that row is still there), and every access or refresh
        token of the family is capped at that end.
        """
        try:
            conn.execute("ALTER TABLE refresh_tokens ADD COLUMN family_expires_at REAL")
        except sqlite3.OperationalError:
            pass  # column already there
        conn.execute(
            "UPDATE refresh_tokens SET family_expires_at = ? + ("
            " SELECT MIN(created_at) FROM refresh_tokens AS first"
            " WHERE first.family_id = refresh_tokens.family_id)"
            " WHERE family_expires_at IS NULL",
            (self.SESSION_LIFETIME,),
        )
        conn.execute(
            "UPDATE refresh_tokens SET expires_at = MIN(expires_at, family_expires_at)"
        )
        conn.execute(
            "UPDATE oauth_tokens SET expires_at = MIN(expires_at, COALESCE("
            " (SELECT MIN(family_expires_at) FROM refresh_tokens"
            "  WHERE refresh_tokens.family_id = oauth_tokens.family_id), expires_at))"
            " WHERE family_id IS NOT NULL"
        )
        conn.commit()

    def _persist(self, at: AccessToken) -> None:
        if self._db is None or at.name is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO named_tokens "
                    "(token, client_id, name, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (at.token, at.client_id, at.name, at.expires_at, at.created_at),
                )
                self._db.commit()
        except Exception:  # noqa: BLE001
            _logger.exception("failed to persist named token")

    def _unpersist(self, token: str) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM named_tokens WHERE token = ?", (token,))
                self._db.commit()
        except Exception:  # noqa: BLE001
            _logger.exception("failed to delete persisted named token")

    def _db_write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Run one write statement against the store; never raises."""
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(sql, params)
                self._db.commit()
        except Exception:  # noqa: BLE001
            _logger.exception("token store write failed")

    def _persist_oauth(self, at: AccessToken) -> None:
        self._db_write(
            "INSERT OR REPLACE INTO oauth_tokens "
            "(token_hash, client_id, family_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (at.token, at.client_id, at.family_id, at.expires_at, at.created_at),
        )

    def _persist_refresh(self, rt: RefreshToken) -> None:
        self._db_write(
            "INSERT OR REPLACE INTO refresh_tokens "
            "(token_hash, client_id, family_id, expires_at, created_at, used, "
            "family_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rt.token_hash, rt.client_id, rt.family_id, rt.expires_at,
             rt.created_at, int(rt.used), rt.family_expires_at),
        )

    def issue(
        self, client_id: str, *, name: str | None = None,
    ) -> tuple[str, int]:
        """Issue an access token. Returns ``(token, expires_in)``.

        If ``name`` is provided the token counts against the per-client
        named-token cap. Raises :class:`TokenCapExceeded` if the cap is
        already met.
        """
        with self._lock:
            if name is not None:
                if self.count_named(client_id) >= self.NAMED_TOKEN_CAP:
                    raise TokenCapExceeded(
                        f"client {client_id} already has "
                        f"{self.NAMED_TOKEN_CAP} named tokens"
                    )
            # Named tokens get the (longer, configurable) named lifetime; internal
            # session bearers keep the short 24 h TTL. A named TTL of 0 means
            # "never expires": the token lives until explicitly revoked.
            ttl = self.named_token_ttl if name is not None else self.TOKEN_TTL
            token = secrets.token_hex(32)
            now = time.time()
            expires_at = float("inf") if (name is not None and ttl == 0) else now + ttl
            at = AccessToken(
                token=token,
                client_id=client_id,
                expires_at=expires_at,
                name=name,
                created_at=now,
            )
            self._tokens[token] = at
            if name is not None:
                self._persist(at)
            self._cleanup()
            return token, ttl

    # --- OAuth grants --------------------------------------------------------

    def _mint_oauth(
        self, client_id: str, family_id: str | None, family_expires_at: float,
    ) -> OAuthGrant:
        """Mint a persisted OAuth bearer, plus a refresh token when
        ``family_id`` is given. Neither outlives ``family_expires_at``, the
        absolute end of the sign-in. Caller holds the lock."""
        now = time.time()
        access = secrets.token_hex(32)
        access_expires_at = min(now + self.TOKEN_TTL, family_expires_at)
        # ``round``, not ``int``: the family end was stamped a few µs before
        # ``now`` on a fresh sign-in, and 86399.9999 must read as 24 h.
        expires_in = min(self.TOKEN_TTL, max(0, round(family_expires_at - now)))
        at = AccessToken(
            token=_hash_secret(access),
            client_id=client_id,
            expires_at=access_expires_at,
            created_at=now,
            family_id=family_id,
        )
        self._oauth_tokens[at.token] = at
        self._persist_oauth(at)
        refresh: str | None = None
        if family_id is not None:
            refresh = secrets.token_hex(32)
            rt = RefreshToken(
                token_hash=_hash_secret(refresh),
                client_id=client_id,
                family_id=family_id,
                expires_at=min(now + self.refresh_token_ttl, family_expires_at),
                created_at=now,
                family_expires_at=family_expires_at,
            )
            self._refresh_tokens[rt.token_hash] = rt
            self._persist_refresh(rt)
        return OAuthGrant(
            access_token=access, expires_in=expires_in, refresh_token=refresh,
        )

    def issue_oauth(self, client_id: str, *, refresh: bool = False) -> OAuthGrant:
        """Issue a persisted OAuth access token for ``client_id``.

        With ``refresh=True`` (``authorization_code`` grant) a refresh token
        opening a new rotation family is issued alongside it. The family,
        and every token it will ever mint, ends ``SESSION_LIFETIME`` from now.
        """
        with self._lock:
            family_id = secrets.token_hex(16) if refresh else None
            grant = self._mint_oauth(
                client_id, family_id, time.time() + self.SESSION_LIFETIME,
            )
            self._cleanup()
            return grant

    def refresh(self, refresh_token: str, client_id: str) -> OAuthGrant:
        """Exchange a refresh token for a new access + refresh token pair.

        The presented token is rotated out. Replaying a rotated-out token
        revokes its whole family (OAuth 2.1 / RFC 9700 reuse detection):
        either the legitimate client or an attacker holds a stolen copy,
        and there is no telling which. The new pair stops at the family's
        ``family_expires_at``, however recent the rotation: once the sign-in
        is 24 h old the client goes back through ``/oauth/authorize``.
        Raises :class:`RefreshTokenError`.
        """
        with self._lock:
            now = time.time()
            rt = self._refresh_tokens.get(_hash_secret(refresh_token))
            if rt is None or rt.client_id != client_id:
                raise RefreshTokenError("invalid")
            if now > rt.expires_at or now >= rt.family_expires_at:
                # The whole sign-in is over with the family end; a token
                # that idled past its own TTL only takes itself out.
                if now >= rt.family_expires_at:
                    self.revoke_family(rt.family_id)
                else:
                    self._drop_refresh(rt.token_hash)
                raise RefreshTokenError("expired", family_id=rt.family_id)
            if rt.used:
                if (
                    rt.replay is not None
                    and now - rt.used_at <= self.REFRESH_REUSE_GRACE
                ):
                    return rt.replay
                self.revoke_family(rt.family_id)
                raise RefreshTokenError("reused", family_id=rt.family_id)
            rt.used = True
            rt.used_at = now
            self._persist_refresh(rt)
            grant = self._mint_oauth(client_id, rt.family_id, rt.family_expires_at)
            rt.replay = grant
            self._cleanup()
            return grant

    def family_of(self, refresh_token: str) -> str | None:
        """Family id of a refresh token, for audit attribution."""
        with self._lock:
            rt = self._refresh_tokens.get(_hash_secret(refresh_token))
            return rt.family_id if rt is not None else None

    def _drop_refresh(self, token_hash: str) -> None:
        self._refresh_tokens.pop(token_hash, None)
        self._db_write("DELETE FROM refresh_tokens WHERE token_hash = ?", (token_hash,))

    def _drop_oauth(self, token_hash: str) -> None:
        self._oauth_tokens.pop(token_hash, None)
        self._db_write("DELETE FROM oauth_tokens WHERE token_hash = ?", (token_hash,))

    def revoke_family(self, family_id: str) -> None:
        """Immediately drop every access and refresh token of a family."""
        with self._lock:
            for h in [h for h, rt in self._refresh_tokens.items()
                      if rt.family_id == family_id]:
                del self._refresh_tokens[h]
            for h in [h for h, at in self._oauth_tokens.items()
                      if at.family_id == family_id]:
                del self._oauth_tokens[h]
            self._db_write("DELETE FROM refresh_tokens WHERE family_id = ?", (family_id,))
            self._db_write("DELETE FROM oauth_tokens WHERE family_id = ?", (family_id,))

    def revoke_client(self, client_id: str) -> int:
        """Drop every OAuth access and refresh token held by ``client_id``.

        Called when the client itself is deleted. Returns the number of
        access tokens dropped.
        """
        with self._lock:
            hashes = [h for h, at in self._oauth_tokens.items()
                      if at.client_id == client_id]
            for h in hashes:
                del self._oauth_tokens[h]
            for h in [h for h, rt in self._refresh_tokens.items()
                      if rt.client_id == client_id]:
                del self._refresh_tokens[h]
            self._db_write("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
            self._db_write("DELETE FROM refresh_tokens WHERE client_id = ?", (client_id,))
            return len(hashes)

    def prune_unknown_clients(self, known_client_ids: set[str]) -> int:
        """Drop OAuth tokens of clients that no longer exist.

        Covers clients deleted while the server was down or from another
        process (``beaconmcp auth revoke``). Returns the number of clients
        pruned.
        """
        with self._lock:
            owners = {at.client_id for at in self._oauth_tokens.values()}
            owners |= {rt.client_id for rt in self._refresh_tokens.values()}
            stale = owners - known_client_ids
            for cid in stale:
                self.revoke_client(cid)
            return len(stale)

    def list_named(self, client_id: str) -> list[AccessToken]:
        """Return named tokens for ``client_id`` (newest first)."""
        with self._lock:
            self._cleanup()
            out = [
                t for t in self._tokens.values()
                if t.client_id == client_id and t.name is not None
            ]
        out.sort(key=lambda t: t.created_at, reverse=True)
        return out

    def count_named(self, client_id: str) -> int:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            access_token = self._tokens.get(token)
            if not access_token:
                return self._validate_oauth(token)
            if time.time() > access_token.expires_at:
                if access_token.name is not None:
                    self._unpersist(token)
                del self._tokens[token]
                return None
            return access_token.client_id

    def _validate_oauth(self, token: str) -> str | None:
        with self._lock:
            token_hash = _hash_secret(token)
            at = self._oauth_tokens.get(token_hash)
            if at is None:
                return None
            if time.time() > at.expires_at:
                self._drop_oauth(token_hash)
                return None
            return at.client_id

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
        with self._lock:
            access_token = self._tokens.get(token)
            if access_token is None:
                return self._revoke_oauth(token)
            deadline = time.time() + self.REVOKE_GRACE_SECONDS
            if access_token.expires_at > deadline:
                access_token.expires_at = deadline
            # Drop from durable storage immediately so a restart inside the grace
            # window doesn't resurrect a token the user just revoked.
            if access_token.name is not None:
                self._unpersist(token)
            return True

    def _revoke_oauth(self, token: str) -> bool:
        """OAuth flavour of :meth:`revoke`: same grace window for the bearer,
        and its refresh family dies right away so the client can't quietly
        mint a replacement (this is what ``security_end_session`` hits)."""
        with self._lock:
            token_hash = _hash_secret(token)
            at = self._oauth_tokens.get(token_hash)
            if at is None:
                return False
            deadline = time.time() + self.REVOKE_GRACE_SECONDS
            if at.expires_at > deadline:
                at.expires_at = deadline
            # Unpersisted now, like named tokens: a restart inside the grace
            # window must not resurrect it.
            self._db_write("DELETE FROM oauth_tokens WHERE token_hash = ?", (token_hash,))
            if at.family_id is not None:
                family_id = at.family_id
                for h in [h for h, rt in self._refresh_tokens.items()
                          if rt.family_id == family_id]:
                    del self._refresh_tokens[h]
                self._db_write(
                    "DELETE FROM refresh_tokens WHERE family_id = ?", (family_id,),
                )
            return True

    def _cleanup(self) -> None:
        with self._lock:
            now = time.time()
            expired = [t for t, at in self._tokens.items() if now > at.expires_at]
            for t in expired:
                if self._tokens[t].name is not None:
                    self._unpersist(t)
                del self._tokens[t]
            for h in [h for h, at in self._oauth_tokens.items()
                      if now > at.expires_at]:
                self._drop_oauth(h)
            for h in [h for h, rt in self._refresh_tokens.items()
                      if now > rt.expires_at]:
                self._drop_refresh(h)
            # Raw pairs held for the retry grace are dropped once it's over.
            for rt in self._refresh_tokens.values():
                if rt.replay is not None and now - rt.used_at > self.REFRESH_REUSE_GRACE:
                    rt.replay = None


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
