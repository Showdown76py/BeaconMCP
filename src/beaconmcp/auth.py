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
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

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


# Redirect URI prefixes that are not CORS origins and therefore cannot be
# modeled via ``server.allowed_origins``.
TRUSTED_NON_ORIGIN_REDIRECT_PREFIXES: tuple[str, ...] = (
    # OS-level custom URI schemes used by desktop clients.
    "vscode://",
    "vscode-insiders://",
    "cursor://",
    # Local loopback for CLI / terminal clients (RFC 8252 style).
    "http://localhost:",
    "http://localhost/",
    "http://127.0.0.1:",
    "http://127.0.0.1/",
    "http://[::1]:",
    "http://[::1]/",
)


def is_trusted_redirect_uri(
    redirect_uri: str,
    allowed_origins: Iterable[str] | None = None,
) -> bool:
    """Return True iff ``redirect_uri`` is trusted for OAuth redirects.

    Web origins are sourced from ``allowed_origins`` (typically
    ``config.server.allowed_origins``), so redirect trust follows the same
    operator-controlled allowlist as CORS. Non-origin redirect forms (custom
    URI schemes and loopback callbacks) are covered by
    :data:`TRUSTED_NON_ORIGIN_REDIRECT_PREFIXES`.
    """
    if not isinstance(redirect_uri, str):
        return False
    redirect_uri = redirect_uri.strip()
    if not redirect_uri:
        return False
    if any(redirect_uri.startswith(p) for p in TRUSTED_NON_ORIGIN_REDIRECT_PREFIXES):
        return True
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
        # Last accepted TOTP timestep (30s counter) per seed owner, used to
        # reject replays of an already-consumed code. Keyed by the SEED owner
        # so two clients sharing one owner's seed can't each spend the same
        # code. Guarded by its own lock -- verify_totp runs on worker threads.
        self._last_totp_step: dict[str, int] = {}
        self._totp_lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
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
            _logger.warning(
                "Revoked %d pre-2FA client(s) with no TOTP secret. "
                "Recreate them with `beaconmcp auth create`: %s",
                len(revoked), ", ".join(revoked),
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
    """Access token store with expiration and optional named-token persistence.

    Internal (unnamed) bearers live only in memory -- a re-login or restart
    is expected to drop them. *Named* tokens (the ones a human mints on the
    dashboard "API tokens" page and pastes into an external client) are
    persisted to a small SQLite file when ``db_path`` is given, so a
    ``systemctl restart`` / redeploy no longer silently invalidates them.
    """

    TOKEN_TTL = 3600 * 24  # 24 hours -- internal/session bearers
    # Default lifetime for *named* tokens. They survive restarts (see the
    # persistence block), so a 24 h cap defeats the point of a token pasted
    # into an external client -- 30 days is the sensible default. Overridable
    # per-deployment via ``named_token_ttl`` (server.named_token_ttl / env).
    NAMED_TOKEN_TTL = 3600 * 24 * 30  # 30 days
    # Cap on named tokens (the ones listed in the dashboard's API
    # tokens page). Internal dashboard-session bearers are unlimited
    # because a re-login always revokes the prior one.
    NAMED_TOKEN_CAP = 3

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        named_token_ttl: int | None = None,
    ) -> None:
        self._tokens: dict[str, AccessToken] = {}
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
            conn.commit()
            self._db = conn
            now = time.time()
            for row in conn.execute(
                "SELECT token, client_id, name, expires_at, created_at "
                "FROM named_tokens WHERE expires_at > ?",
                (now,),
            ).fetchall():
                self._tokens[row[0]] = AccessToken(
                    token=row[0], client_id=row[1], expires_at=row[3],
                    name=row[2], created_at=row[4],
                )
            # Drop rows that expired while the process was down.
            conn.execute("DELETE FROM named_tokens WHERE expires_at <= ?", (now,))
            conn.commit()
        except Exception:  # noqa: BLE001 -- persistence must never block startup
            _logger.exception("named-token persistence disabled (db init failed)")
            self._db = None

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
                return None
            if time.time() > access_token.expires_at:
                if access_token.name is not None:
                    self._unpersist(token)
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
        with self._lock:
            access_token = self._tokens.get(token)
            if access_token is None:
                return False
            deadline = time.time() + self.REVOKE_GRACE_SECONDS
            if access_token.expires_at > deadline:
                access_token.expires_at = deadline
            # Drop from durable storage immediately so a restart inside the grace
            # window doesn't resurrect a token the user just revoked.
            if access_token.name is not None:
                self._unpersist(token)
            return True

    def _cleanup(self) -> None:
        with self._lock:
            now = time.time()
            expired = [t for t, at in self._tokens.items() if now > at.expires_at]
            for t in expired:
                if self._tokens[t].name is not None:
                    self._unpersist(t)
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
