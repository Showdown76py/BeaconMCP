"""Passkey (WebAuthn) support for the BeaconMCP login pages.

A passkey stands in for the TOTP factor, never for the client secret. The
flow on both login pages stays two-factor:

1. ``client_id`` + ``client_secret`` -- something you know / have stored.
2. Either a 6-digit TOTP code **or** a WebAuthn assertion -- something you
   have on your device.

That ordering is deliberate. The dashboard session record encrypts the
client secret so it can re-mint MCP bearers later, so a purely
"usernameless" passkey login could not build a usable session anyway.
Keeping the secret as the first factor also means a stolen passkey alone
is worthless.

Credentials live in the dashboard SQLite database (``passkeys`` table);
challenges live in memory with a short TTL, since a challenge that
survives a restart is a replay window, not a feature.

The ``webauthn`` package (py_webauthn) is an optional dependency: when it
is missing, :class:`PasskeyService` reports ``available is False`` and
every login page simply hides its passkey affordances instead of erroring.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.requests import Request

    from .db import Database

try:
    import webauthn as _webauthn
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        AuthenticatorTransport,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
except ImportError:  # pragma: no cover - exercised only on slim installs
    _webauthn = None  # type: ignore[assignment]
    AuthenticatorSelectionCriteria = None  # type: ignore[assignment,misc]
    AuthenticatorTransport = None  # type: ignore[assignment,misc]
    PublicKeyCredentialDescriptor = None  # type: ignore[assignment,misc]
    ResidentKeyRequirement = None  # type: ignore[assignment,misc]
    UserVerificationRequirement = None  # type: ignore[assignment,misc]


#: How long a registration/authentication challenge stays usable. The spec
#: suggests a couple of minutes; the browser prompt itself times out at 60 s.
CHALLENGE_TTL_SECONDS = 180

#: Relying-party display name shown in the OS passkey prompt.
RP_NAME = "BeaconMCP"

#: Cap on credentials per client. High enough for phone + laptop + a
#: hardware key or two, low enough that the allowCredentials list stays sane.
MAX_PASSKEYS_PER_CLIENT = 10


class PasskeyError(Exception):
    """User-facing passkey failure. The message is safe to render."""


def webauthn_installed() -> bool:
    """True when the optional ``webauthn`` dependency is importable."""
    return _webauthn is not None


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# ---------------------------------------------------------------------------
# Relying-party identity, derived from the live request
# ---------------------------------------------------------------------------

def _forwarded_host(request: "Request") -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = request.url.netloc or "localhost"
    # A proxy chain may append entries; the first one is the client-facing host.
    return host.split(",")[0].strip()


def rp_id_for(request: "Request") -> str:
    """Return the WebAuthn RP ID (effective domain, no scheme, no port).

    Derived from the request rather than configured, so a deployment that
    moves behind a new hostname keeps working without a config edit. The
    trade-off is that credentials are scoped to the hostname they were
    registered under -- which is exactly the WebAuthn security model.
    """
    host = _forwarded_host(request)
    if host.startswith("["):  # IPv6 literal: [::1]:8420
        closing = host.find("]")
        if closing != -1:
            return host[1:closing]
    return host.rsplit(":", 1)[0] if ":" in host else host


def origin_for(request: "Request") -> str:
    """Return the origin the browser will put in ``clientDataJSON``."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    scheme = scheme.split(",")[0].strip()
    return f"{scheme}://{_forwarded_host(request)}"


def is_secure_context(request: "Request") -> bool:
    """True when the browser will expose ``navigator.credentials``.

    WebAuthn is gated on a secure context: HTTPS, or a loopback host. A
    plain-HTTP LAN deployment silently has no passkey API at all, so the
    UI needs to know before it offers the button.
    """
    scheme = (
        request.headers.get("x-forwarded-proto") or request.url.scheme
    ).split(",")[0].strip()
    if scheme == "https":
        return True
    return rp_id_for(request) in ("localhost", "127.0.0.1", "::1")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@dataclass
class PasskeyRecord:
    credential_id: str  # base64url
    client_id: str
    public_key: bytes
    sign_count: int
    transports: list[str]
    label: str
    created_at: float
    last_used_at: float | None
    backed_up: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "label": self.label,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "backed_up": self.backed_up,
            "transports": self.transports,
        }


class PasskeyStore:
    """Thin wrapper around the ``passkeys`` table."""

    def __init__(self, database: "Database") -> None:
        self._db = database

    @staticmethod
    def _row_to_record(row: Any) -> PasskeyRecord:
        try:
            transports = json.loads(row["transports"] or "[]")
        except (TypeError, ValueError):
            transports = []
        return PasskeyRecord(
            credential_id=row["credential_id"],
            client_id=row["client_id"],
            public_key=row["public_key"],
            sign_count=row["sign_count"],
            transports=[t for t in transports if isinstance(t, str)],
            label=row["label"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            backed_up=bool(row["backed_up"]),
        )

    def add(
        self,
        *,
        credential_id: str,
        client_id: str,
        public_key: bytes,
        sign_count: int,
        transports: list[str],
        label: str,
        backed_up: bool,
    ) -> PasskeyRecord:
        now = time.time()
        self._db.conn().execute(
            """
            INSERT INTO passkeys (
              credential_id, client_id, public_key, sign_count,
              transports, label, created_at, last_used_at, backed_up
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                credential_id,
                client_id,
                public_key,
                sign_count,
                json.dumps(transports),
                label,
                now,
                1 if backed_up else 0,
            ),
        )
        return PasskeyRecord(
            credential_id=credential_id,
            client_id=client_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            label=label,
            created_at=now,
            last_used_at=None,
            backed_up=backed_up,
        )

    def list_for_client(self, client_id: str) -> list[PasskeyRecord]:
        rows = self._db.conn().execute(
            """
            SELECT credential_id, client_id, public_key, sign_count, transports,
                   label, created_at, last_used_at, backed_up
              FROM passkeys WHERE client_id = ?
              ORDER BY created_at DESC
            """,
            (client_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get(self, credential_id: str) -> PasskeyRecord | None:
        row = self._db.conn().execute(
            """
            SELECT credential_id, client_id, public_key, sign_count, transports,
                   label, created_at, last_used_at, backed_up
              FROM passkeys WHERE credential_id = ?
            """,
            (credential_id,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def count_for_client(self, client_id: str) -> int:
        row = self._db.conn().execute(
            "SELECT COUNT(*) AS n FROM passkeys WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def touch(self, credential_id: str, sign_count: int) -> None:
        self._db.conn().execute(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? "
            "WHERE credential_id = ?",
            (sign_count, time.time(), credential_id),
        )

    def delete(self, credential_id: str, client_id: str) -> bool:
        cur = self._db.conn().execute(
            "DELETE FROM passkeys WHERE credential_id = ? AND client_id = ?",
            (credential_id, client_id),
        )
        return bool(cur.rowcount)

    def delete_all_for_client(self, client_id: str) -> int:
        cur = self._db.conn().execute(
            "DELETE FROM passkeys WHERE client_id = ?", (client_id,)
        )
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------

@dataclass
class _Challenge:
    purpose: str          # "register" | "authenticate"
    challenge: bytes
    client_id: str
    session_id: str | None
    expires_at: float


class ChallengeStore:
    """In-memory, single-use challenge store.

    Deliberately not persisted: a challenge that outlives the process is a
    replay window. A restart mid-ceremony just makes the user click again.
    """

    def __init__(self, ttl_seconds: int = CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, _Challenge] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        purpose: str,
        challenge: bytes,
        client_id: str,
        session_id: str | None = None,
    ) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_locked()
            self._items[state] = _Challenge(
                purpose=purpose,
                challenge=challenge,
                client_id=client_id,
                session_id=session_id,
                expires_at=time.time() + self._ttl,
            )
        return state

    def consume(self, state: str, purpose: str) -> _Challenge | None:
        with self._lock:
            self._prune_locked()
            item = self._items.pop(state, None)
        if item is None or item.purpose != purpose:
            return None
        if time.time() > item.expires_at:
            return None
        return item

    def _prune_locked(self) -> None:
        now = time.time()
        for key in [k for k, v in self._items.items() if now > v.expires_at]:
            del self._items[key]


# ---------------------------------------------------------------------------
# Ceremony orchestration
# ---------------------------------------------------------------------------

def default_label(user_agent: str) -> str:
    """Best-effort human label for a freshly registered credential.

    Users never get asked to name their passkey during login -- one more
    field in the middle of a sign-in is friction for no security gain -- so
    we guess from the User-Agent and let them recognise it later.
    """
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua or "ios" in ua:
        return "iPhone / iPad"
    if "android" in ua:
        return "Android"
    if "mac os" in ua or "macintosh" in ua:
        return "Mac"
    if "windows" in ua:
        return "Windows"
    if "linux" in ua:
        return "Linux"
    return "Passkey"


def _transport_descriptors(transports: list[str]) -> list[Any] | None:
    """Map stored transport strings onto the library enum, dropping unknowns."""
    if not transports or AuthenticatorTransport is None:
        return None
    out = []
    for t in transports:
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            continue
    return out or None


class PasskeyService:
    """Generates and verifies WebAuthn ceremonies against a :class:`PasskeyStore`."""

    def __init__(self, store: PasskeyStore | None) -> None:
        self._store = store
        self._challenges = ChallengeStore()

    @property
    def available(self) -> bool:
        """True when passkeys can actually be used (library + storage present)."""
        return _webauthn is not None and self._store is not None

    @property
    def unavailable_reason(self) -> str | None:
        """Why passkeys are off, or ``None`` when they work.

        The login pages hide their passkey affordances silently -- there is
        nothing an anonymous visitor could do about it anyway -- so the
        operator needs to be able to ask the question somewhere. This backs
        the startup banner and ``beaconmcp doctor``.
        """
        if _webauthn is None:
            return (
                "the 'webauthn' Python package is missing "
                "(pip install 'webauthn>=2,<4', or reinstall BeaconMCP)"
            )
        if self._store is None:
            return "no writable dashboard database to store credentials in"
        return None

    @property
    def store(self) -> PasskeyStore:
        if self._store is None:
            raise PasskeyError("Passkeys are not available on this deployment.")
        return self._store

    def _require(self) -> None:
        if _webauthn is None:
            raise PasskeyError(
                "Passkey support requires the 'webauthn' package. "
                "Reinstall BeaconMCP to pull it in."
            )
        if self._store is None:
            raise PasskeyError("Passkeys are not available on this deployment.")

    # --- registration ----------------------------------------------------

    def registration_options(
        self,
        request: "Request",
        *,
        client_id: str,
        client_name: str,
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Return ``(options_dict, state)`` for ``navigator.credentials.create``."""
        self._require()
        existing = self.store.list_for_client(client_id)
        if len(existing) >= MAX_PASSKEYS_PER_CLIENT:
            raise PasskeyError(
                f"This client already has {MAX_PASSKEYS_PER_CLIENT} passkeys. "
                "Remove one before adding another."
            )
        options = _webauthn.generate_registration_options(
            rp_id=rp_id_for(request),
            rp_name=RP_NAME,
            user_id=client_id.encode("utf-8"),
            user_name=client_id,
            user_display_name=client_name or client_id,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            # Stops the authenticator offering to overwrite a passkey the
            # client already registered on this device.
            exclude_credentials=[
                PublicKeyCredentialDescriptor(
                    id=b64url_decode(p.credential_id),
                    transports=_transport_descriptors(p.transports),
                )
                for p in existing
            ],
        )
        state = self._challenges.issue(
            purpose="register",
            challenge=options.challenge,
            client_id=client_id,
            session_id=session_id,
        )
        return json.loads(_webauthn.options_to_json(options)), state

    def verify_registration(
        self,
        request: "Request",
        *,
        state: str,
        credential: dict[str, Any],
        label: str | None = None,
        session_id: str | None = None,
    ) -> PasskeyRecord:
        """Verify a ``navigator.credentials.create`` result and persist it."""
        self._require()
        pending = self._challenges.consume(state, "register")
        if pending is None:
            raise PasskeyError(
                "This passkey request expired. Start the registration again."
            )
        if pending.session_id is not None and pending.session_id != session_id:
            raise PasskeyError("This passkey request belongs to another session.")

        try:
            verified = _webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=pending.challenge,
                expected_rp_id=rp_id_for(request),
                expected_origin=origin_for(request),
            )
        except Exception as exc:  # noqa: BLE001 - library raises many subtypes
            raise PasskeyError(f"Passkey registration rejected: {exc}") from exc

        credential_id = b64url_encode(verified.credential_id)
        if self.store.get(credential_id) is not None:
            raise PasskeyError("This passkey is already registered.")

        transports = []
        raw_transports = (credential.get("response") or {}).get("transports")
        if isinstance(raw_transports, list):
            transports = [t for t in raw_transports if isinstance(t, str)]

        return self.store.add(
            credential_id=credential_id,
            client_id=pending.client_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=transports,
            label=(label or "").strip()[:60]
            or default_label(request.headers.get("user-agent", "")),
            backed_up=bool(getattr(verified, "credential_backed_up", False)),
        )

    # --- authentication --------------------------------------------------

    def authentication_options(
        self, request: "Request", *, client_id: str,
    ) -> tuple[dict[str, Any], str]:
        """Return ``(options_dict, state)`` for ``navigator.credentials.get``."""
        self._require()
        credentials = self.store.list_for_client(client_id)
        if not credentials:
            raise PasskeyError(
                "No passkey is registered for this client. Sign in with your "
                "2FA code once, then add one."
            )
        options = _webauthn.generate_authentication_options(
            rp_id=rp_id_for(request),
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=b64url_decode(p.credential_id),
                    transports=_transport_descriptors(p.transports),
                )
                for p in credentials
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        state = self._challenges.issue(
            purpose="authenticate",
            challenge=options.challenge,
            client_id=client_id,
        )
        return json.loads(_webauthn.options_to_json(options)), state

    def verify_authentication(
        self, request: "Request", *, state: str, credential: dict[str, Any],
    ) -> PasskeyRecord:
        """Verify a ``navigator.credentials.get`` result.

        Returns the credential that signed the challenge. The caller is
        responsible for turning that into a session -- this module never
        touches bearers or cookies.
        """
        self._require()
        pending = self._challenges.consume(state, "authenticate")
        if pending is None:
            raise PasskeyError(
                "This passkey request expired. Try signing in again."
            )

        raw_id = credential.get("id") or credential.get("rawId")
        if not isinstance(raw_id, str) or not raw_id:
            raise PasskeyError("Malformed passkey response.")
        record = self.store.get(raw_id)
        if record is None:
            raise PasskeyError("Unknown passkey.")
        # The challenge was issued for one client; a credential belonging to
        # another must never satisfy it, even if both are valid on their own.
        if record.client_id != pending.client_id:
            raise PasskeyError("This passkey belongs to a different client.")

        try:
            verified = _webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=pending.challenge,
                expected_rp_id=rp_id_for(request),
                expected_origin=origin_for(request),
                credential_public_key=record.public_key,
                credential_current_sign_count=record.sign_count,
            )
        except Exception as exc:  # noqa: BLE001 - library raises many subtypes
            # This also covers the clone signal: py_webauthn refuses any
            # assertion whose signature counter did not move forward, as
            # long as either side maintains one (platform authenticators
            # commonly pin it at 0, and that stays legal).
            raise PasskeyError(f"Passkey rejected: {exc}") from exc

        self.store.touch(record.credential_id, verified.new_sign_count)
        record.sign_count = verified.new_sign_count
        record.last_used_at = time.time()
        return record
