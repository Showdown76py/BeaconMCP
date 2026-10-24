"""Human-session re-verification for interactive (authorization_code) bearers.

An interactive bearer lives 30 days but is only usable while its human
session is fresh: the last passkey/TOTP check happened less than
``session_ttl`` seconds ago. When it lapses, elicitation-capable clients
get a link to ``/oauth/reverify`` and the tool call waits for it; other
clients get the plain 401 they always got.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import audit
from .auth import HumanSession, token_hash
from .ratelimit import client_ip

_logger = logging.getLogger(__name__)


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
    """Latest ``initialize`` info per sign-in (keyed by session key).

    Keyed by sign-in rather than bearer: clients refresh their access token
    without re-initializing, and the capabilities must survive that.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, ClientSessionInfo] = {}

    def record(self, key: str, info: ClientSessionInfo) -> None:
        self._by_key[key] = info

    def get(self, key: str) -> ClientSessionInfo | None:
        return self._by_key.get(key)


def session_lapsed(session: HumanSession, ttl: int, now: float | None = None) -> bool:
    if not session.interactive:
        return False
    now = time.time() if now is None else now
    return now - session.verified_at > ttl


@dataclass
class PendingReverify:
    id: str
    session_key: str
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
        self, *, session_key: str, client_id: str, tool: str, args: dict,
        ip: str, user_agent: str, client_label: str,
        bearer_issued_at: float, last_verified_at: float,
    ) -> tuple[PendingReverify, bool]:
        self._prune()
        for entry in self._entries.values():
            if entry.session_key == session_key and entry.live:
                return entry, False
        now = time.time()
        entry = PendingReverify(
            id=secrets.token_urlsafe(24), session_key=session_key,
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

    def revoke_token(self, session_key: str) -> int:
        n = 0
        for entry in self._entries.values():
            if entry.session_key == session_key and entry.live:
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
    rt: ReauthRuntime, *, session: HumanSession, tool: str, args: dict,
    ip: str, user_agent: str,
) -> tuple[PendingReverify, bool]:
    info = rt.registry.get(session.key)
    entry, created = rt.pending.get_or_create(
        session_key=session.key, client_id=session.client_id, tool=tool,
        args=audit.compact_args(args), ip=ip, user_agent=user_agent,
        client_label=rt.label_for(session.client_id, info),
        bearer_issued_at=session.created_at, last_verified_at=session.verified_at,
    )
    if created:
        audit.emit(
            "auth.reverify.requested", client_id=session.client_id, tool=tool, ip=ip,
        )
    return entry, created


class _Verified(BaseModel):
    verified: bool = Field(default=False, description="I've verified in the browser")


def elicit_modes(ctx: Any, info: ClientSessionInfo | None) -> tuple[bool, bool]:
    """(form, url) support for this session, from its initialize params."""
    elicitation = None
    try:
        session = ctx.request_context.session
        # mcp 2.x: client_capabilities also covers stateless 2026-07-28 clients
        caps = getattr(session, "client_capabilities", None)
        if caps is None:
            caps = session.client_params.capabilities
        elicitation = caps.elicitation
    except (AttributeError, ValueError, LookupError):
        pass
    if elicitation is None:
        if info is not None:
            return info.elicit_form, info.elicit_url
        return False, False
    url = elicitation.url is not None
    form = elicitation.form is not None or not url
    return form, url


async def gate_tool_call(ctx: Any, tool: str, args: dict) -> None:
    """Block a tool call while the caller's human session is lapsed.

    Elicits a re-verify link (URL mode, else a form carrying the link) and
    waits up to ``hold_timeout`` for ``/oauth/reverify`` to complete it.
    Raises :class:`ToolError` with the link when that doesn't happen.
    """
    rt = runtime()
    if rt is None:
        return
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError, LookupError):
        request = None
    # with re-auth on, every tool call arrives over HTTP: no request means a
    # path we don't know, so refuse rather than run on an unchecked session
    if request is None:
        raise ToolError("BeaconMCP could not check this session. Retry the request.")
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return
    session = rt.token_store.get_interactive_by_hash(token_hash(auth_header[7:]))
    if session is None or not session_lapsed(session, rt.session_ttl):
        return

    form, url_mode = elicit_modes(ctx, rt.registry.get(session.key))
    entry, _ = request_reverify(
        rt, session=session, tool=tool, args=args,
        ip=client_ip(request, rt.trusted_proxies),
        user_agent=request.headers.get("user-agent", ""),
    )
    link = reverify_url(rt.issuer_for(request), entry.id)
    message = f"Your BeaconMCP session expired. Verify it's you to run {tool}."

    verified = False
    if form or url_mode:
        async with anyio.create_task_group() as tg:

            async def ask() -> None:
                try:
                    if url_mode:
                        result = await ctx.elicit_url(
                            message=message, url=link, elicitation_id=entry.id,
                        )
                    else:
                        result = await ctx.elicit(
                            message=f"{message}\nOpen {link} then confirm here.",
                            schema=_Verified,
                        )
                except Exception:  # noqa: BLE001 -- the wait still decides
                    _logger.debug("re-verify elicitation failed", exc_info=True)
                    return
                # a dismissed prompt ends the hold now, the link stays valid
                if getattr(result, "action", "accept") != "accept":
                    tg.cancel_scope.cancel()

            # every call prompts (same link): a retry after a timeout or a
            # second window sharing the bearer must not hang silently
            tg.start_soon(ask)
            verified = await rt.pending.wait(entry.id, rt.hold_timeout)
            tg.cancel_scope.cancel()
    if verified:
        if url_mode:
            try:
                await ctx.request_context.session.send_elicit_complete(
                    entry.id, related_request_id=ctx.request_context.request_id,
                )
            except Exception:  # noqa: BLE001
                _logger.debug("elicitation complete notification failed", exc_info=True)
        return
    raise ToolError(f"BeaconMCP session expired. Verify at {link} then retry.")


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
        session = rt.token_store.get_interactive_by_hash(token_hash(auth_header[7:]))
        if session is None:
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
                    session.key, ClientSessionInfo.from_initialize(
                        m.get("params"), user_agent=ua, ip=ip,
                    ),
                )

        lapsed = session_lapsed(session, rt.session_ttl)
        gated = [m for m in messages if m.get("method") in GATED_METHODS]
        # a body we couldn't inspect (too big, not JSON) can't be cleared
        uninspected = not complete or (body.strip() and not messages)
        if not lapsed or (not gated and not uninspected):
            await self._inner(scope, replay, send)
            return

        info = rt.registry.get(session.key)
        if uninspected or info is None or not info.can_elicit:
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
            rt, session=session, tool=m["method"], args=params, ip=ip, user_agent=ua,
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
