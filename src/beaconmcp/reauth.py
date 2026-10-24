"""Human-session re-verification for interactive (authorization_code) bearers.

An interactive bearer lives 30 days but is only usable while its human
session is fresh: the last passkey/TOTP check happened less than
``session_ttl`` seconds ago. When it lapses, elicitation-capable clients
get a link to ``/oauth/reverify`` and the tool call waits for it; other
clients get the plain 401 they always got.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio

from .auth import AccessToken


@dataclass
class ClientSessionInfo:
    """What a client said about itself at ``initialize`` (self-declared)."""

    elicit_form: bool = False
    elicit_url: bool = False
    name: str = ""
    title: str = ""
    version: str = ""
    website_url: str = ""
    icons: list[str] = field(default_factory=list)
    user_agent: str = ""
    ip: str = ""

    @classmethod
    def from_initialize(
        cls, params: Any, *, user_agent: str, ip: str,
    ) -> ClientSessionInfo:
        params = params if isinstance(params, dict) else {}
        caps = params.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}
        elicitation = caps.get("elicitation")
        form = url = False
        if isinstance(elicitation, dict):
            url = isinstance(elicitation.get("url"), dict)
            form = isinstance(elicitation.get("form"), dict)
            # 2025-06-18 clients declare a bare {} and only know form mode
            if not form and not url:
                form = True
        info = params.get("clientInfo")
        info = info if isinstance(info, dict) else {}

        def _s(key: str) -> str:
            v = info.get(key)
            return v[:200] if isinstance(v, str) else ""

        icons_raw = info.get("icons")
        icons = [
            i["src"] for i in (icons_raw if isinstance(icons_raw, list) else [])
            if isinstance(i, dict) and isinstance(i.get("src"), str)
        ][:8]
        return cls(
            elicit_form=form, elicit_url=url,
            name=_s("name"), title=_s("title"), version=_s("version"),
            website_url=_s("websiteUrl"), icons=icons,
            user_agent=user_agent[:300], ip=ip,
        )

    @property
    def can_elicit(self) -> bool:
        return self.elicit_form or self.elicit_url


class ClientInfoRegistry:
    """Latest ``initialize`` info per interactive bearer (keyed by token hash)."""

    def __init__(self) -> None:
        self._by_hash: dict[str, ClientSessionInfo] = {}

    def record(self, thash: str, info: ClientSessionInfo) -> None:
        self._by_hash[thash] = info

    def get(self, thash: str) -> ClientSessionInfo | None:
        return self._by_hash.get(thash)


def session_lapsed(at: AccessToken, ttl: int, now: float | None = None) -> bool:
    if not at.interactive:
        return False
    now = time.time() if now is None else now
    return now - at.verified_at > ttl


@dataclass
class PendingReverify:
    id: str
    token_hash: str
    client_id: str
    tool: str
    args: dict
    created_at: float
    expires_at: float
    ip: str
    user_agent: str
    client_label: str
    bearer_issued_at: float
    last_verified_at: float
    done: bool = False
    revoked: bool = False
    event: anyio.Event = field(default_factory=anyio.Event, repr=False)

    @property
    def live(self) -> bool:
        return not self.done and not self.revoked and time.time() < self.expires_at


class PendingReverifyStore:
    """Short-lived re-verification requests, one live entry per bearer."""

    def __init__(self, link_ttl: int = 600) -> None:
        self.link_ttl = link_ttl
        self._entries: dict[str, PendingReverify] = {}

    def _prune(self) -> None:
        now = time.time()
        for pid in [p for p, e in self._entries.items() if now >= e.expires_at]:
            del self._entries[pid]

    def get_or_create(
        self, *, token_hash: str, client_id: str, tool: str, args: dict,
        ip: str, user_agent: str, client_label: str,
        bearer_issued_at: float, last_verified_at: float,
    ) -> tuple[PendingReverify, bool]:
        self._prune()
        for entry in self._entries.values():
            if entry.token_hash == token_hash and entry.live:
                return entry, False
        now = time.time()
        entry = PendingReverify(
            id=secrets.token_urlsafe(24), token_hash=token_hash,
            client_id=client_id, tool=tool, args=dict(args),
            created_at=now, expires_at=now + self.link_ttl,
            ip=ip, user_agent=user_agent, client_label=client_label,
            bearer_issued_at=bearer_issued_at, last_verified_at=last_verified_at,
        )
        self._entries[entry.id] = entry
        return entry, True

    def get(self, pid: str) -> PendingReverify | None:
        entry = self._entries.get(pid)
        return entry if entry is not None and entry.live else None

    def complete(self, pid: str) -> PendingReverify | None:
        entry = self.get(pid)
        if entry is None:
            return None
        entry.done = True
        entry.event.set()
        return entry

    def revoke_token(self, thash: str) -> int:
        n = 0
        for entry in self._entries.values():
            if entry.token_hash == thash and entry.live:
                entry.revoked = True
                entry.event.set()
                n += 1
        return n

    async def wait(self, pid: str, timeout: float) -> bool:
        """True once the entry is completed, False on timeout/revoke/unknown."""
        entry = self._entries.get(pid)
        if entry is None:
            return False
        # kept after completion until expiry so a verify that lands while
        # the caller is still in the elicitation round-trip isn't missed
        with anyio.move_on_after(timeout):
            await entry.event.wait()
        return entry.done


@dataclass
class ReauthRuntime:
    token_store: Any
    registry: ClientInfoRegistry
    pending: PendingReverifyStore
    session_ttl: int
    hold_timeout: float
    issuer_for: Callable[[Any], str]
    trusted_proxies: tuple[str, ...] = ()


_runtime: ReauthRuntime | None = None


def configure(rt: ReauthRuntime | None) -> None:
    global _runtime
    _runtime = rt


def runtime() -> ReauthRuntime | None:
    return _runtime
