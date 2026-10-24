"""Human-session re-verification for interactive (authorization_code) bearers.

An interactive bearer lives 30 days but is only usable while its human
session is fresh: the last passkey/TOTP check happened less than
``session_ttl`` seconds ago. When it lapses, elicitation-capable clients
get a link to ``/oauth/reverify`` and the tool call waits for it; other
clients get the plain 401 they always got.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import audit
from .auth import AccessToken, token_hash
from .ratelimit import client_ip


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
    # display name for the waiting-request block, e.g. "Claude Code"
    label_for: Callable[[str, ClientSessionInfo | None], str] = (
        lambda client_id, info: (info.title or info.name) if info and (info.title or info.name) else client_id
    )


_runtime: ReauthRuntime | None = None


def configure(rt: ReauthRuntime | None) -> None:
    global _runtime
    _runtime = rt


def runtime() -> ReauthRuntime | None:
    return _runtime


def reverify_url(issuer: str, pid: str) -> str:
    return f"{issuer}/oauth/reverify?id={pid}"


def request_reverify(
    rt: ReauthRuntime, *, thash: str, at: AccessToken, tool: str, args: dict,
    ip: str, user_agent: str,
) -> tuple[PendingReverify, bool]:
    info = rt.registry.get(thash)
    entry, created = rt.pending.get_or_create(
        token_hash=thash, client_id=at.client_id, tool=tool,
        args=audit.compact_args(args), ip=ip, user_agent=user_agent,
        client_label=rt.label_for(at.client_id, info),
        bearer_issued_at=at.created_at, last_verified_at=at.verified_at,
    )
    if created:
        audit.emit(
            "auth.reverify.requested", client_id=at.client_id, tool=tool, ip=ip,
        )
    return entry, created


# methods that act on data; list/initialize/ping stay open so the client
# keeps its connection and tool list while the session is lapsed
GATED_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})
_MAX_INSPECT_BODY = 1024 * 1024


class McpGateApp:
    """ASGI wrapper around the MCP app.

    Records client capabilities at ``initialize`` and, for a lapsed human
    session, answers before the MCP app does: 401 for clients that can't
    elicit, a JSON-RPC error carrying the re-verify link for reads.
    ``tools/call`` from capable clients goes through to :func:`gate_tool_call`.
    """

    def __init__(self, inner, *, resource_metadata_for: Callable[[Request], str]):
        self._inner = inner
        self._resource_metadata_for = resource_metadata_for

    async def __call__(self, scope, receive, send):
        rt = runtime()
        if rt is None or scope["type"] != "http" or scope.get("method") != "POST":
            await self._inner(scope, receive, send)
            return
        request = Request(scope)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            await self._inner(scope, receive, send)
            return
        thash = token_hash(auth_header[7:])
        at = rt.token_store.get_interactive_by_hash(thash)
        if at is None:
            await self._inner(scope, receive, send)
            return

        buffered: list[dict] = []
        body = b""
        complete = False
        while len(body) <= _MAX_INSPECT_BODY:
            msg = await receive()
            buffered.append(msg)
            if msg["type"] != "http.request":
                break
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                complete = True
                break

        async def replay():
            if buffered:
                return buffered.pop(0)
            return await receive()

        messages: list[dict] = []
        if complete:
            try:
                parsed = json.loads(body or b"null")
            except ValueError:
                parsed = None
            items = parsed if isinstance(parsed, list) else [parsed]
            messages = [m for m in items if isinstance(m, dict)]

        ip = client_ip(request, rt.trusted_proxies)
        ua = request.headers.get("user-agent", "")
        for m in messages:
            if m.get("method") == "initialize":
                rt.registry.record(
                    thash, ClientSessionInfo.from_initialize(
                        m.get("params"), user_agent=ua, ip=ip,
                    ),
                )

        gated = [m for m in messages if m.get("method") in GATED_METHODS]
        if not gated or not session_lapsed(at, rt.session_ttl):
            await self._inner(scope, replay, send)
            return

        info = rt.registry.get(thash)
        if info is None or not info.can_elicit:
            meta = self._resource_metadata_for(request)
            response = JSONResponse(
                {"error": "invalid_token",
                 "error_description": "BeaconMCP session expired, sign in again"},
                status_code=401,
                headers={"WWW-Authenticate": (
                    f'Bearer realm="beaconmcp", error="invalid_token", '
                    f'resource_metadata="{meta}"'
                )},
            )
            await response(scope, replay, send)
            return

        reads = [m for m in gated if m.get("method") != "tools/call"]
        if not reads:
            await self._inner(scope, replay, send)
            return

        m = reads[0]
        params = m.get("params")
        params = params if isinstance(params, dict) else {}
        entry, _ = request_reverify(
            rt, thash=thash, at=at, tool=m["method"], args=params, ip=ip, user_agent=ua,
        )
        url = reverify_url(rt.issuer_for(request), entry.id)
        text = "Your BeaconMCP session expired. Verify it's you, then retry."
        if info.elicit_url:
            error = {"code": -32042, "message": text, "data": {"elicitations": [
                {"mode": "url", "elicitationId": entry.id, "url": url, "message": text},
            ]}}
        else:
            error = {"code": -32001, "message": f"{text} {url}"}
        response = JSONResponse({"jsonrpc": "2.0", "id": m.get("id"), "error": error})
        await response(scope, replay, send)
