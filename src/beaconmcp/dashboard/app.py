"""Starlette routes for the BeaconMCP dashboard.

Builds the route list and dependencies. Mounted under ``/app/*`` from
``__main__._run_http``. The MCP routes (``/mcp``, ``/oauth/*``,
``/.well-known/*``) are untouched.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import audit
from ..auth import TOTP_REPLAY_MESSAGE, TotpResult
from . import csrf as csrf
from .chat import (
    ChatEngine,
    ErrorEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolConfirmRequired,
    TurnInput,
    UsageAccumulated,
)
from .confirmations import ConfirmationStore
from .conversations import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    VALID_EFFORTS,
    VALID_MODELS,
    ConversationStore,
)
from .db import Database
from .dyn_reg import DynamicSlugStore, SLUG_TTL_SECONDS
from .passkeys import PasskeyError, PasskeyService, is_secure_context
from .session import SESSION_TTL_SECONDS, Session, SessionStore
from .usage import UsageMeter, UsageStore


SESSION_COOKIE = "beaconmcp_session"

# 24h, matches the MCP TokenStore.TOKEN_TTL.
BEARER_TTL_SECONDS = 24 * 3600


_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=_DASHBOARD_DIR / "templates")


@dataclass
class DashboardDeps:
    """Dependencies injected from ``__main__._run_http``."""

    database: Database
    session_store: SessionStore
    client_store: object  # beaconmcp.auth.ClientStore -- avoid import cycle
    token_store: object  # beaconmcp.auth.TokenStore
    totp_locked: Callable[[str], bool]
    totp_record_failure: Callable[[str], None]
    totp_record_success: Callable[[str], None]
    conversations: ConversationStore | None = None
    engine: ChatEngine | None = None
    confirmations: ConfirmationStore | None = None
    # Optional usage accounting + per-client budget. When unset, the
    # dashboard runs without any cost tracking or quota enforcement.
    usage: UsageStore | None = None
    # Public URL used by Gemini's backend to call this MCP server in
    # "remote" mode. Falls back to the request's Host header at call time
    # when unset. Ignored in "local" mode.
    mcp_public_url: str | None = None
    # "local": dashboard holds an MCP ClientSession (default; works on
    # any API key). "remote": pass an McpServer to Gemini with a public
    # URL so Google's backend calls MCP directly (Gemini 3 native).
    mcp_mode: str = "local"
    # Slug store for OAuth DCR bootstrap URLs (ChatGPT). When unset, the
    # /app/connectors page is hidden and the slug-scoped OAuth endpoints
    # are not mounted.
    dyn_reg: DynamicSlugStore | None = None
    # Per-IP limiter guarding /app/login against brute-force. Optional so
    # tests/embedding paths can skip the limiter entirely.
    login_limiter: object | None = None
    trusted_proxies: tuple[str, ...] = ()
    # WebAuthn ceremonies for the passkey login/registration flow. When
    # unset (or reporting ``available is False``), the login page hides
    # every passkey affordance and the TOTP path is the only way in.
    passkeys: PasskeyService | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def _set_session_cookie(response: Response, session_id: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/app",
    )


def _set_csrf_cookie(
    response: Response, secure: bool, token: str | None = None,
) -> str:
    """Issue (or re-issue) the double-submit CSRF cookie.

    ``token`` lets a caller mint the value first so it can also hand it to
    the client out-of-band -- the JSON login does that, because rotating
    the cookie without telling the page would leave its in-DOM token stale
    and every follow-up fetch would 403.
    """
    token = token or csrf.issue_token()
    response.set_cookie(
        csrf.CSRF_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,  # JS reads it for the X-CSRF-Token header
        secure=secure,
        samesite="strict",
        path="/app",
    )
    return token


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/app")
    response.delete_cookie(csrf.CSRF_COOKIE, path="/app")


def _render(
    template: str,
    request: Request,
    *,
    status_code: int = 200,
    csrf_token: str | None = None,
    secure: bool | None = None,
    **context,
) -> HTMLResponse:
    """Render a Jinja2 template, ensuring a CSRF cookie is in place."""
    if secure is None:
        secure = _is_secure(request)
    token = csrf_token or csrf.cookie_token(request)
    if not token:
        token = csrf.issue_token()
    context["csrf_token"] = token
    response = _TEMPLATES.TemplateResponse(
        request, template, context, status_code=status_code
    )
    if csrf.cookie_token(request) != token:
        # Need to (re-)issue the cookie carrying the value we just rendered.
        response.set_cookie(
            csrf.CSRF_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=False,
            secure=secure,
            samesite="strict",
            path="/app",
        )
    _apply_security_headers(response)
    return response


def _apply_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault(
        "Referrer-Policy", "strict-origin-when-cross-origin"
    )
    # CSP for /app/* pages. Google Fonts is allowlisted so the UI can
    # pull Inter + JetBrains Mono; everything else stays same-origin.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'",
    )


def _load_session(request: Request, deps: DashboardDeps) -> Session | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    session = deps.session_store.load(session_id)
    if session:
        deps.session_store.touch(session.session_id)
    return session


def _bearer_live(deps: DashboardDeps, session: Session) -> bool:
    """Timestamp says the bearer is valid AND TokenStore still knows it.

    The TokenStore is in-memory; a systemctl restart wipes every token
    while the SQLite-backed session keeps them. Without the second
    check, redirects like /app/refresh -> /app/chat -> stream ->
    session_expired -> /app/refresh loop forever because each step
    trusts ``session.bearer_valid()`` in isolation.
    """
    if not session.bearer_valid():
        return False
    validator = getattr(deps.token_store, "validate", None)
    if callable(validator) and session.mcp_bearer:
        if validator(session.mcp_bearer) is None:
            return False
    return True


def _check_totp(deps: DashboardDeps, client_id: str, code: str) -> TotpResult:
    """Check a 2FA code, recording a lockout strike only for a wrong one.

    The dashboard prompts for 2FA on several actions (login, minting a token,
    adding a connector). Doing two of them inside the same 30 s step means the
    authenticator still shows the code the operator just spent, so a replay is
    an ordinary mistake -- counting it towards the 5-strike lockout would lock
    a legitimate operator out of their own panel.
    """
    result = deps.client_store.check_totp(client_id, code)  # type: ignore[attr-defined]
    if result is TotpResult.INVALID:
        deps.totp_record_failure(client_id)
    return result


def _totp_error(result: TotpResult, invalid_message: str) -> str:
    return TOTP_REPLAY_MESSAGE if result is TotpResult.REPLAY else invalid_message


def _wants_json(request: Request) -> bool:
    """True when the caller is the login page's fetch() rather than a form POST.

    The login form still works with JavaScript disabled -- it posts and gets
    a redirect, exactly as before. The enhanced flow (shimmer, then the
    post-2FA screen offering to enrol a passkey) needs to stay on the page,
    so it opts in explicitly with this header.
    """
    return request.headers.get("x-beaconmcp-mode", "") == "json"


def _passkeys_ready(deps: DashboardDeps) -> bool:
    return deps.passkeys is not None and deps.passkeys.available


# ---------------------------------------------------------------------------
# Route factories
# ---------------------------------------------------------------------------

def build_dashboard_routes(deps: DashboardDeps) -> list[Route | Mount]:
    """Return Starlette routes for the dashboard, ready to mount."""

    def _default_landing() -> str:
        """Post-login destination: chat if Gemini is configured, else tokens."""
        return "/app/chat" if deps.engine is not None else "/app/tokens"

    async def index(request: Request) -> Response:
        session = _load_session(request, deps)
        if session and _bearer_live(deps, session):
            return RedirectResponse(_default_landing(), status_code=302)
        if session:
            return RedirectResponse("/app/refresh", status_code=302)
        return RedirectResponse("/app/login", status_code=302)

    async def login_get(request: Request) -> Response:
        # If a valid session already exists, send to the default landing.
        session = _load_session(request, deps)
        if session and _bearer_live(deps, session):
            return RedirectResponse(_default_landing(), status_code=302)
        if session:
            # Session valid but bearer stale -> refresh page is the right one.
            return RedirectResponse("/app/refresh", status_code=302)

        return _render(
            "login.html",
            request,
            client_id="",
            next=request.query_params.get("next", ""),
            banner=None,
            locked=False,
            passkeys_enabled=_passkeys_ready(deps),
            secure_context=is_secure_context(request),
        )

    def _login_success_payload(
        request: Request, session: Session, *, next_url: str, bearer_ttl: int,
    ) -> dict[str, Any]:
        """Data the post-2FA screen renders: expiry + passkey affordances."""
        return {
            "ok": True,
            "next": next_url,
            "client_id": session.client_id,
            "client_name": (
                deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                or session.client_id
            ),
            # Two different clocks, and users care about both: the MCP bearer
            # is what actually stops working (a 2FA re-prompt at /app/refresh),
            # while the signed-in cookie is what saves them the full login.
            "bearer_expires_at": time.time() + bearer_ttl,
            "session_expires_at": session.expires_at,
            "passkeys_enabled": _passkeys_ready(deps),
            "secure_context": is_secure_context(request),
            "passkey_count": (
                deps.passkeys.store.count_for_client(session.client_id)
                if _passkeys_ready(deps) else 0
            ),
        }

    def _start_session(
        request: Request,
        *,
        client_id: str,
        client_secret: str,
        next_url: str,
        as_json: bool | None = None,
    ) -> tuple[Session, int, Response]:
        """Mint the MCP bearer + session row and attach the cookies.

        Shared by the TOTP login and the passkey login so the two paths
        cannot drift on cookie flags or bearer lifetime.
        """
        bearer, ttl = deps.token_store.issue(client_id)  # type: ignore[attr-defined]
        ua = request.headers.get("user-agent", "")[:200]
        session = deps.session_store.create(
            client_id=client_id,
            client_secret=client_secret,
            mcp_bearer=bearer,
            bearer_ttl_seconds=ttl,
            user_agent=ua,
        )
        secure = _is_secure(request)
        new_csrf = csrf.issue_token()
        if as_json if as_json is not None else _wants_json(request):
            payload = _login_success_payload(
                request, session, next_url=next_url, bearer_ttl=ttl,
            )
            payload["csrf_token"] = new_csrf
            response: Response = JSONResponse(payload)
        else:
            response = RedirectResponse(next_url, status_code=303)
        _set_session_cookie(response, session.session_id, secure)
        _set_csrf_cookie(response, secure, new_csrf)
        _apply_security_headers(response)
        return session, ttl, response

    async def login_post(request: Request) -> Response:
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)

        # Per-IP rate limit. Comes before CSRF was fine too, but putting it
        # after CSRF keeps the error ordering consistent with /oauth/token.
        limiter = deps.login_limiter
        if limiter is not None:
            from ..ratelimit import client_ip as _client_ip  # local import: avoid cycle at module load
            ip = _client_ip(request, deps.trusted_proxies)
            if not limiter.check(ip):  # type: ignore[attr-defined]
                retry = limiter.retry_after(ip)  # type: ignore[attr-defined]
                message = (
                    f"Too many attempts from this address. Retry in {retry}s."
                )
                if _wants_json(request):
                    return _json(
                        {"ok": False, "error": message, "locked": True}, status=429,
                    )
                return _render(
                    "login.html",
                    request,
                    client_id="",
                    next="",
                    banner=message,
                    locked=True,
                    status_code=429,
                    passkeys_enabled=_passkeys_ready(deps),
                    secure_context=is_secure_context(request),
                )

        form = await request.form()

        def _v(name: str) -> str:
            v = form.get(name, "")
            return v if isinstance(v, str) else ""

        client_id = _v("client_id").strip()
        client_secret = _v("client_secret")
        totp = _v("totp").strip()
        next_url = _v("next").strip() or _default_landing()
        if not next_url.startswith("/app/"):
            next_url = _default_landing()

        def _fail(message: str, *, status: int = 400, locked: bool = False) -> Response:
            if _wants_json(request):
                return _json(
                    {"ok": False, "error": message, "locked": locked}, status=status,
                )
            return _render(
                "login.html",
                request,
                client_id=client_id,
                next=next_url if next_url != _default_landing() else "",
                banner=message,
                locked=locked,
                status_code=status,
                passkeys_enabled=_passkeys_ready(deps),
                secure_context=is_secure_context(request),
            )

        if not client_id or not client_secret or not totp:
            return _fail("Tous les champs sont requis.")

        if not deps.client_store.verify(client_id, client_secret):  # type: ignore[attr-defined]
            return _fail("Invalid credentials.", status=401)

        if deps.totp_locked(client_id):
            return _fail(
                "Too many attempts. Try again in 5 minutes.",
                status=429,
                locked=True,
            )

        totp_result = _check_totp(deps, client_id, totp)
        if totp_result is not TotpResult.OK:
            audit.emit(
                "dashboard.login.fail", client_id=client_id,
                reason=f"totp:{totp_result.value}",
            )
            return _fail(
                _totp_error(
                    totp_result,
                    "Invalid 2FA code. Check that your device clock is in sync.",
                ),
                status=401,
                locked=deps.totp_locked(client_id),
            )

        deps.totp_record_success(client_id)
        audit.emit("dashboard.login.ok", client_id=client_id)
        _, _, response = _start_session(
            request,
            client_id=client_id,
            client_secret=client_secret,
            next_url=next_url,
        )
        return response

    async def refresh_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if _bearer_live(deps, session):
            return RedirectResponse(_default_landing(), status_code=302)

        client_name = (
            deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
            or session.client_id
        )
        return _render(
            "totp_refresh.html",
            request,
            client_id=session.client_id,
            client_name=client_name,
            next=request.query_params.get("next", ""),
            banner=None,
            locked=deps.totp_locked(session.client_id),
        )

    async def refresh_post(request: Request) -> Response:
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)

        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)

        form = await request.form()
        totp_value = form.get("totp", "")
        totp = totp_value.strip() if isinstance(totp_value, str) else ""
        next_value = form.get("next", "")
        next_url = next_value.strip() if isinstance(next_value, str) else ""
        if not (next_url.startswith("/app/")):
            next_url = _default_landing()

        client_name = (
            deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
            or session.client_id
        )

        def _fail(message: str, *, status: int = 400, locked: bool = False) -> Response:
            return _render(
                "totp_refresh.html",
                request,
                client_id=session.client_id,
                client_name=client_name,
                next=next_url if next_url != _default_landing() else "",
                banner=message,
                locked=locked,
                status_code=status,
            )

        if not totp:
            return _fail("2FA code is required.")

        if deps.totp_locked(session.client_id):
            return _fail(
                "Too many attempts. Try again in 5 minutes.",
                status=429, locked=True,
            )

        totp_result = _check_totp(deps, session.client_id, totp)
        if totp_result is not TotpResult.OK:
            return _fail(
                _totp_error(totp_result, "Invalid 2FA code."),
                status=401,
                locked=deps.totp_locked(session.client_id),
            )

        deps.totp_record_success(session.client_id)

        # Re-issue MCP bearer using the stored client_secret.
        secret = deps.session_store.get_client_secret(session.session_id)
        if not secret or not deps.client_store.verify(session.client_id, secret):  # type: ignore[attr-defined]
            # Stored credentials no longer valid (admin revoked client, key rotated).
            deps.session_store.delete(session.session_id)
            response = RedirectResponse("/app/login", status_code=302)
            _clear_session_cookies(response)
            _apply_security_headers(response)
            return response

        bearer, ttl = deps.token_store.issue(session.client_id)  # type: ignore[attr-defined]
        deps.session_store.update_bearer(
            session.session_id, mcp_bearer=bearer, bearer_ttl_seconds=ttl
        )

        response = RedirectResponse(next_url, status_code=303)
        _apply_security_headers(response)
        return response

    async def logout(request: Request) -> Response:
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            bearer = deps.session_store.delete(session_id)
            if bearer:
                deps.token_store.revoke(bearer)  # type: ignore[attr-defined]
        response = RedirectResponse("/app/login", status_code=303)
        _clear_session_cookies(response)
        _apply_security_headers(response)
        return response

    async def overview_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/overview", status_code=302)
        return _render(
            "overview.html",
            request,
            client_name=(
                deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                or "Unknown"
            ),
        )

    async def usage_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/usage", status_code=302)
        return _render(
            "usage_cost.html",
            request,
            client_name=(
                deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                or "Unknown"
            ),
        )

    async def tokens_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/tokens", status_code=302)
        return _render_tokens_page(request, session, deps)

    async def tokens_create(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/tokens", status_code=302)
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)

        form = await request.form()
        name_raw = form.get("name", "")
        name = (name_raw if isinstance(name_raw, str) else "").strip()
        totp_raw = form.get("totp", "")
        totp = (totp_raw if isinstance(totp_raw, str) else "").strip()

        if not name:
            return _render_tokens_page(
                request, session, deps,
                form_error="Name is required.",
                form_name=name,
            )
        if len(name) > 60:
            return _render_tokens_page(
                request, session, deps,
                form_error="Name cannot exceed 60 characters.",
                form_name=name,
            )
        if not totp:
            return _render_tokens_page(
                request, session, deps,
                form_error="2FA code is required.",
                form_name=name,
            )
        if deps.totp_locked(session.client_id):
            return _render_tokens_page(
                request, session, deps,
                form_error="Too many 2FA attempts; try again in 5 minutes.",
                form_name=name,
            )
        totp_result = _check_totp(deps, session.client_id, totp)
        if totp_result is not TotpResult.OK:
            return _render_tokens_page(
                request, session, deps,
                form_error=_totp_error(totp_result, "Invalid 2FA code."),
                form_name=name,
            )
        deps.totp_record_success(session.client_id)

        try:
            token, ttl = deps.token_store.issue(  # type: ignore[attr-defined]
                session.client_id, name=name,
            )
        except Exception as exc:  # noqa: BLE001
            # TokenCapExceeded lives in beaconmcp.auth; catch broadly so
            # we don't take on a circular import just for this check.
            if type(exc).__name__ == "TokenCapExceeded":
                return _render_tokens_page(
                    request, session, deps,
                    form_error=(
                        "Limit reached: 3 active tokens maximum. "
                        "Revoke one before creating a new one."
                    ),
                    form_name=name,
                )
            raise

        return _render_tokens_page(
            request, session, deps,
            just_created={"name": name, "token": token, "ttl": ttl},
        )

    # --- ChatGPT connectors (OAuth DCR) ----------------------------------

    def _connectors_enabled() -> bool:
        return deps.dyn_reg is not None

    def _render_connectors_page(
        request: Request,
        session: Session,
        *,
        form_error: str | None = None,
        form_label: str = "",
        just_created: dict[str, Any] | None = None,
    ) -> Response:
        store = deps.dyn_reg
        assert store is not None  # checked by caller
        now = time.time()
        slugs = store.list_for_owner(session.client_id)
        pending = [
            {
                "slug": s.slug,
                "label": s.label,
                "expires_in_minutes": max(0, round((s.expires_at - now) / 60)),
            }
            for s in slugs
            if s.used_at is None and s.expires_at > now
        ]
        derived = deps.client_store.list_derived(session.client_id)  # type: ignore[attr-defined]
        derived_rows = [
            {
                "client_id": c.client_id,
                "name": c.name,
                "created_at_human": _human_time(c.created_at),
            }
            for c in derived
        ]
        return _render(
            "connectors.html",
            request,
            client_id=session.client_id,
            client_name=(
                deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                or session.client_id
            ),
            pending_slugs=pending,
            derived_clients=derived_rows,
            slug_ttl_minutes=max(1, SLUG_TTL_SECONDS // 60),
            form_error=form_error,
            form_label=form_label,
            just_created=just_created,
            locked=deps.totp_locked(session.client_id),
        )

    async def connectors_get(request: Request) -> Response:
        if not _connectors_enabled():
            return RedirectResponse("/app/tokens", status_code=302)
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse(
                "/app/refresh?next=/app/connectors", status_code=302,
            )
        return _render_connectors_page(request, session)

    async def connectors_mint(request: Request) -> Response:
        if not _connectors_enabled():
            return RedirectResponse("/app/tokens", status_code=302)
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse(
                "/app/refresh?next=/app/connectors", status_code=302,
            )
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)

        form = await request.form()
        label_raw = form.get("label", "")
        label = (label_raw if isinstance(label_raw, str) else "").strip()
        totp_raw = form.get("totp", "")
        totp_code = (totp_raw if isinstance(totp_raw, str) else "").strip()

        if not label:
            return _render_connectors_page(
                request, session, form_error="Label is required.", form_label=label,
            )
        if len(label) > 60:
            return _render_connectors_page(
                request, session,
                form_error="Label cannot exceed 60 characters.",
                form_label=label,
            )
        if deps.totp_locked(session.client_id):
            return _render_connectors_page(
                request, session,
                form_error="Too many 2FA attempts; try again in 5 minutes.",
                form_label=label,
            )
        totp_result = _check_totp(deps, session.client_id, totp_code)
        if totp_result is not TotpResult.OK:
            return _render_connectors_page(
                request, session,
                form_error=_totp_error(totp_result, "Invalid 2FA code."),
                form_label=label,
            )
        deps.totp_record_success(session.client_id)

        store = deps.dyn_reg
        assert store is not None
        store.prune_expired()
        row = store.mint(owner_client_id=session.client_id, label=label)

        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host_hdr = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "localhost"),
        )
        url = f"{scheme}://{host_hdr}/mcp/c/{row.slug}"
        return _render_connectors_page(
            request, session,
            just_created={"url": url},
        )

    async def connectors_slug_delete(request: Request) -> Response:
        if not _connectors_enabled():
            return RedirectResponse("/app/tokens", status_code=302)
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse(
                "/app/refresh?next=/app/connectors", status_code=302,
            )
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        form = await request.form()
        slug_raw = form.get("slug", "")
        slug = (slug_raw if isinstance(slug_raw, str) else "").strip()
        if slug and deps.dyn_reg is not None:
            deps.dyn_reg.delete_unused(slug, session.client_id)
        return RedirectResponse("/app/connectors", status_code=303)

    async def connectors_revoke(request: Request) -> Response:
        if not _connectors_enabled():
            return RedirectResponse("/app/tokens", status_code=302)
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse(
                "/app/refresh?next=/app/connectors", status_code=302,
            )
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        form = await request.form()
        client_id_raw = form.get("client_id", "")
        client_id = (client_id_raw if isinstance(client_id_raw, str) else "").strip()
        if client_id:
            target = deps.client_store.get(client_id)  # type: ignore[attr-defined]
            # Only allow revoking clients WE own.
            if target is not None and target.owner_client_id == session.client_id:
                deps.client_store.revoke(client_id)  # type: ignore[attr-defined]
                audit.emit(
                    "auth.client.revoke", client_id=client_id,
                    owner_client_id=session.client_id, via="dashboard",
                )
        return RedirectResponse("/app/connectors", status_code=303)

    async def tokens_revoke(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/tokens", status_code=302)
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        form = await request.form()
        prefix_raw = form.get("token_prefix", "")
        prefix = (prefix_raw if isinstance(prefix_raw, str) else "").strip()
        if prefix:
            deps.token_store.revoke_named(  # type: ignore[attr-defined]
                prefix, session.client_id,
            )
        return RedirectResponse("/app/tokens", status_code=303)

    # --- Passkeys (WebAuthn) ---------------------------------------------

    def _passkey_service() -> PasskeyService | Response:
        if not _passkeys_ready(deps):
            return _json({"error": "passkeys_unavailable"}, status=503)
        assert deps.passkeys is not None
        return deps.passkeys

    async def api_passkeys_list(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        return _json({
            "passkeys": [
                p.to_json() for p in service.store.list_for_client(session.client_id)
            ],
        })

    async def api_passkeys_register_options(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        try:
            options, state = service.registration_options(
                request,
                client_id=session.client_id,
                client_name=(
                    deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                    or session.client_id
                ),
                session_id=session.session_id,
            )
        except PasskeyError as exc:
            return _json({"error": str(exc)}, status=400)
        return _json({"options": options, "state": state})

    async def api_passkeys_register_verify(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        body = await _read_json(request)
        credential = body.get("credential")
        state = str(body.get("state") or "")
        if not isinstance(credential, dict) or not state:
            return _json({"error": "invalid_request"}, status=400)
        label_raw = body.get("label")
        try:
            record = service.verify_registration(
                request,
                state=state,
                credential=credential,
                label=label_raw if isinstance(label_raw, str) else None,
                session_id=session.session_id,
            )
        except PasskeyError as exc:
            audit.emit(
                "dashboard.passkey.register.fail",
                client_id=session.client_id, reason=str(exc),
            )
            return _json({"error": str(exc)}, status=400)
        audit.emit(
            "dashboard.passkey.register.ok",
            client_id=session.client_id, label=record.label,
        )
        return _json({"ok": True, "passkey": record.to_json()}, status=201)

    async def api_passkeys_delete(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        body = await _read_json(request)
        credential_id = str(body.get("credential_id") or "").strip()
        if not credential_id:
            return _json({"error": "invalid_request"}, status=400)
        if not service.store.delete(credential_id, session.client_id):
            return _json({"error": "not_found"}, status=404)
        audit.emit("dashboard.passkey.revoke", client_id=session.client_id)
        return _json({"ok": True})

    async def passkeys_remove(request: Request) -> Response:
        """Form-POST twin of the JSON delete, for the tokens page.

        The tokens page is deliberately JS-free for destructive actions, so
        removing a passkey posts a plain form and lands back on the page.
        """
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh?next=/app/tokens", status_code=302)
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if _passkeys_ready(deps):
            form = await request.form()
            raw = form.get("credential_id", "")
            credential_id = (raw if isinstance(raw, str) else "").strip()
            assert deps.passkeys is not None
            if credential_id and deps.passkeys.store.delete(
                credential_id, session.client_id,
            ):
                audit.emit("dashboard.passkey.revoke", client_id=session.client_id)
        return RedirectResponse("/app/tokens", status_code=303)

    def _passkey_login_rate_limited(request: Request) -> Response | None:
        """Apply the login limiter to the unauthenticated passkey endpoints.

        These take a client_id/client_secret pair, so they are just as much
        a credential-stuffing surface as /app/login itself.
        """
        limiter = deps.login_limiter
        if limiter is None:
            return None
        from ..ratelimit import client_ip as _client_ip  # local: avoid import cycle
        ip = _client_ip(request, deps.trusted_proxies)
        if limiter.check(ip):  # type: ignore[attr-defined]
            return None
        retry = limiter.retry_after(ip)  # type: ignore[attr-defined]
        return _json(
            {
                "ok": False,
                "error": f"Too many attempts from this address. Retry in {retry}s.",
            },
            status=429,
        )

    async def api_passkeys_auth_options(request: Request) -> Response:
        """Start a passkey sign-in. Requires the client credentials first.

        The passkey replaces the TOTP factor only: without a valid
        client_id/client_secret we will not even disclose whether a client
        has credentials registered.
        """
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        limited = _passkey_login_rate_limited(request)
        if limited is not None:
            return limited
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        body = await _read_json(request)
        client_id = str(body.get("client_id") or "").strip()
        client_secret = str(body.get("client_secret") or "")
        if not client_id or not client_secret:
            return _json({"ok": False, "error": "Missing credentials."}, status=400)
        if not deps.client_store.verify(client_id, client_secret):  # type: ignore[attr-defined]
            return _json({"ok": False, "error": "Invalid credentials."}, status=401)
        try:
            options, state = service.authentication_options(
                request, client_id=client_id,
            )
        except PasskeyError as exc:
            return _json({"ok": False, "error": str(exc)}, status=400)
        return _json({"ok": True, "options": options, "state": state})

    async def api_passkeys_auth_verify(request: Request) -> Response:
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        limited = _passkey_login_rate_limited(request)
        if limited is not None:
            return limited
        service = _passkey_service()
        if isinstance(service, Response):
            return service
        body = await _read_json(request)
        client_id = str(body.get("client_id") or "").strip()
        client_secret = str(body.get("client_secret") or "")
        state = str(body.get("state") or "")
        credential = body.get("credential")
        if not client_id or not client_secret or not state:
            return _json({"ok": False, "error": "Missing credentials."}, status=400)
        if not isinstance(credential, dict):
            return _json({"ok": False, "error": "Malformed passkey response."}, status=400)
        # Re-verify the secret: the options call proved it once, but the
        # state token alone must never be enough to mint a session.
        if not deps.client_store.verify(client_id, client_secret):  # type: ignore[attr-defined]
            return _json({"ok": False, "error": "Invalid credentials."}, status=401)

        try:
            record = service.verify_authentication(
                request, state=state, credential=credential,
            )
        except PasskeyError as exc:
            audit.emit(
                "dashboard.login.fail", client_id=client_id,
                reason=f"passkey:{exc}",
            )
            return _json({"ok": False, "error": str(exc)}, status=401)
        if record.client_id != client_id:
            return _json({"ok": False, "error": "Invalid credentials."}, status=401)

        next_url = str(body.get("next") or "").strip()
        if not next_url.startswith("/app/"):
            next_url = _default_landing()
        # A passkey sign-in is a clean second factor: clear any TOTP
        # lockout the same way a correct code would.
        deps.totp_record_success(client_id)
        audit.emit("dashboard.login.ok", client_id=client_id, via="passkey")
        _, _, response = _start_session(
            request,
            client_id=client_id,
            client_secret=client_secret,
            next_url=next_url,
            as_json=True,
        )
        return response

    async def chat_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not _bearer_live(deps, session):
            return RedirectResponse("/app/refresh", status_code=302)
        # No Gemini key configured: the chat panel has nothing to drive,
        # but the Tokens page is still useful. Redirect there.
        if deps.engine is None:
            return RedirectResponse("/app/tokens", status_code=302)
        return _render(
            "chat.html",
            request,
            client_id=session.client_id,
            client_name=(
                deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
                or session.client_id
            ),
            default_model=DEFAULT_MODEL,
            default_effort=DEFAULT_EFFORT,
            valid_models=list(VALID_MODELS),
            valid_efforts=list(VALID_EFFORTS),
        )

    # --- Conversations API ------------------------------------------------

    def _require_conversations() -> ConversationStore:
        if deps.conversations is None:
            raise RuntimeError("ConversationStore not wired")
        return deps.conversations

    async def api_conv_list(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        convs = _require_conversations().list_for_client(session.client_id)
        return _json({"conversations": [c.to_json() for c in convs]})

    async def api_conv_create(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        body = await _read_json(request)
        conv = _require_conversations().create(
            client_id=session.client_id,
            model=str(body.get("model") or "gemini-3-flash"),
            effort=str(body.get("effort") or "low"),
        )
        return _json({"conversation": conv.to_json()}, status=201)

    async def api_conv_detail(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        conv_id = request.path_params["conv_id"]
        store = _require_conversations()
        conv = store.get(conv_id, client_id=session.client_id)
        if conv is None:
            return _json({"error": "not_found"}, status=404)
        messages = store.list_messages(conv.id)
        return _json({
            "conversation": conv.to_json(),
            "messages": [m.to_json() for m in messages],
        })

    async def api_conv_patch(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        body = await _read_json(request)
        conv_id = request.path_params["conv_id"]
        store = _require_conversations()
        title = body.get("title")
        model = body.get("model")
        effort = body.get("effort")
        conv = store.patch(
            conv_id, client_id=session.client_id,
            title=title if isinstance(title, str) else None,
            model=model if isinstance(model, str) else None,
            effort=effort if isinstance(effort, str) else None,
        )
        if conv is None:
            return _json({"error": "not_found"}, status=404)
        return _json({"conversation": conv.to_json()})

    async def api_conv_delete(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        conv_id = request.path_params["conv_id"]
        if not _require_conversations().delete(conv_id, client_id=session.client_id):
            return _json({"error": "not_found"}, status=404)
        return Response(status_code=204)

    async def api_usage(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if deps.usage is None:
            # Return a disabled snapshot so the UI can render "usage
            # tracking off" without a 404/error path.
            return _json({
                "usage": {
                    "spent_5h_usd": 0.0, "limit_5h_usd": 0.0,
                    "session_5h_started_at": None, "session_5h_reset_at": None,
                    "spent_week_usd": 0.0, "limit_week_usd": 0.0,
                },
            })
        return _json({
            "usage": deps.usage.snapshot(session.client_id).to_json(),
        })

    async def api_chat_confirm(request: Request) -> Response:
        session = _require_active_session(request, deps)
        if isinstance(session, Response):
            return session
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)
        if deps.confirmations is None:
            return _json({"error": "chat_disabled"}, status=503)
        body = await _read_json(request)
        call_id = str(body.get("call_id") or "").strip()
        if not call_id:
            return _json({"error": "invalid_request"}, status=400)
        approved = bool(body.get("approve"))
        ok = deps.confirmations.resolve(
            call_id=call_id,
            session_id=session.session_id,
            approved=approved,
        )
        if not ok:
            return _json({"error": "not_found"}, status=404)
        return _json({"ok": True, "approved": approved})

    async def api_chat_stream(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return _json({"error": "unauthorized"}, status=401)
        if not await csrf.verify(request):
            return _json({"error": "csrf"}, status=403)

        if deps.engine is None or deps.conversations is None:
            return _json({"error": "chat_disabled"}, status=503)

        body = await _read_json(request)
        conv_id = str(body.get("conversation_id") or "").strip()
        user_text = str(body.get("content") or "").strip()
        override_model = body.get("model")
        override_effort = body.get("effort")
        if not conv_id or not user_text:
            return _json({"error": "invalid_request"}, status=400)

        store = deps.conversations
        conv = store.get(conv_id, client_id=session.client_id)
        if conv is None:
            return _json({"error": "not_found"}, status=404)

        model = override_model if override_model in VALID_MODELS else conv.model
        effort = override_effort if override_effort in VALID_EFFORTS else conv.thinking_effort
        if model != conv.model or effort != conv.thinking_effort:
            store.patch(
                conv.id, client_id=session.client_id,
                model=model if isinstance(model, str) else None,
                effort=effort if isinstance(effort, str) else None,
            )

        async def stream():
            if not _bearer_live(deps, session):
                yield _sse("session_expired", {})
                return

            # Budget pre-check. Refuse the turn BEFORE we hit Google so
            # we never burn tokens on a user who is already over-cap.
            if deps.usage is not None:
                block = deps.usage.check_budget(session.client_id)
                if block is not None:
                    yield _sse("error", {
                        "code": "quota_exceeded",
                        "message": _format_quota_message(block),
                    })
                    yield _sse("usage_update", deps.usage.snapshot(
                        session.client_id,
                    ).to_json())
                    return

            # History = everything prior to this turn. The engine appends
            # the user message itself via TurnInput.user_text.
            history = store.list_messages(conv.id)
            is_first_turn = not any(m.role == "user" for m in history)
            store.add_user_message(conv.id, user_text)

            collected: list[Any] = []
            usage_event: UsageAccumulated | None = None

            confirmations = deps.confirmations
            issued_call_ids: list[str] = []

            async def _confirm(req: ToolConfirmRequired) -> bool:
                if confirmations is None:
                    return False
                fut = confirmations.create(
                    call_id=req.id, session_id=session.session_id,
                )
                issued_call_ids.append(req.id)
                try:
                    # 5 minute ceiling matches the upstream MCP SSE
                    # read timeout; anything longer and the browser
                    # connection tends to time out anyway.
                    return await asyncio.wait_for(fut, timeout=300)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    confirmations.cancel(req.id)
                    return False

            turn = TurnInput(
                history=history,
                user_text=user_text,
                model=model,
                effort=effort,
                bearer=session.mcp_bearer or "",
                mcp_url=_resolve_mcp_url(request, deps),
                mcp_mode=deps.mcp_mode,
                confirm_tool=_confirm,
            )

            try:
                async for event in deps.engine.run(turn):  # type: ignore[union-attr]
                    collected.append(event)
                    if isinstance(event, TextDelta):
                        yield _sse("text_delta", {"text": event.text})
                    elif isinstance(event, ThinkingDelta):
                        yield _sse("thinking_delta", {"summary": event.summary})
                    elif isinstance(event, ToolCallStart):
                        yield _sse("tool_call", {
                            "id": event.id, "name": event.name, "args": event.args,
                        })
                    elif isinstance(event, ToolConfirmRequired):
                        yield _sse("tool_confirm_required", {
                            "id": event.id, "name": event.name, "args": event.args,
                        })
                    elif isinstance(event, ToolCallEnd):
                        yield _sse("tool_result", {
                            "id": event.id, "status": event.status,
                            "preview": event.preview, "duration_ms": event.duration_ms,
                        })
                    elif isinstance(event, UsageAccumulated):
                        # Engine-internal event: keep the last one the
                        # engine emitted (it always sends exactly one
                        # at end-of-turn) and skip SSE forwarding.
                        usage_event = event
                        continue
                    elif isinstance(event, ErrorEvent):
                        yield _sse("error", {
                            "code": event.code, "message": event.message,
                        })
                        break

                    if await request.is_disconnected():
                        yield _sse("aborted", {})
                        break
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {
                    "code": "internal", "message": str(exc),
                })
            finally:
                # Free any confirmation futures left dangling (client
                # disconnected before approving, engine errored mid-loop).
                if confirmations is not None:
                    for cid in issued_call_ids:
                        confirmations.cancel(cid)

            from .chat import assemble_assistant_message  # local to avoid cycle
            content, tool_calls, thinking = assemble_assistant_message(collected)
            msg = store.add_assistant_message(
                conv.id,
                content=content,
                tool_calls=tool_calls,
                thinking_summary=thinking,
                model=model,
                effort=effort,
            )
            yield _sse("done", {"message_id": msg.id})

            # Bill the client now that the assistant message has an id.
            # Swallow DB failures here -- a billing blip shouldn't break
            # a turn the user has already seen complete.
            if deps.usage is not None and usage_event is not None:
                try:
                    cost = UsageMeter.cost_usd(
                        usage_event.model,
                        prompt_tokens=usage_event.prompt_tokens,
                        cached_tokens=usage_event.cached_tokens,
                        output_tokens=usage_event.output_tokens,
                    )
                    deps.usage.record_turn(
                        client_id=session.client_id,
                        conversation_id=conv.id,
                        message_id=msg.id,
                        model=usage_event.model,
                        prompt_tokens=usage_event.prompt_tokens,
                        cached_tokens=usage_event.cached_tokens,
                        output_tokens=usage_event.output_tokens,
                        cost_usd=cost,
                    )
                except Exception:  # noqa: BLE001
                    pass
                yield _sse(
                    "usage_update",
                    deps.usage.snapshot(session.client_id).to_json(),
                )

            if is_first_turn and deps.engine is not None:
                title = await deps.engine.title(model=model, user_text=user_text)
                if title:
                    store.patch(conv.id, client_id=session.client_id, title=title)
                    yield _sse("title_updated", {
                        "conversation_id": conv.id, "title": title,
                    })

        response = StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        _apply_security_headers(response)
        return response

    return [
        Route("/app/login", login_get, methods=["GET"]),
        Route("/app/login", login_post, methods=["POST"]),
        Route("/app/refresh", refresh_get, methods=["GET"]),
        Route("/app/refresh", refresh_post, methods=["POST"]),
        Route("/app/logout", logout, methods=["POST"]),
        Route("/app/chat", chat_get, methods=["GET"]),
        Route("/app/overview", overview_get, methods=["GET"]),
        Route("/app/usage", usage_get, methods=["GET"]),
        Route("/app/tokens", tokens_get, methods=["GET"]),
        Route("/app/tokens", tokens_create, methods=["POST"]),
        Route("/app/tokens/revoke", tokens_revoke, methods=["POST"]),
        Route("/app/connectors", connectors_get, methods=["GET"]),
        Route("/app/connectors/slug", connectors_mint, methods=["POST"]),
        Route("/app/connectors/slug/delete", connectors_slug_delete, methods=["POST"]),
        Route("/app/connectors/revoke", connectors_revoke, methods=["POST"]),
        Route("/app/api/conversations", api_conv_list, methods=["GET"]),
        Route("/app/api/conversations", api_conv_create, methods=["POST"]),
        Route("/app/api/conversations/{conv_id}", api_conv_detail, methods=["GET"]),
        Route("/app/api/conversations/{conv_id}", api_conv_patch, methods=["PATCH"]),
        Route("/app/api/conversations/{conv_id}", api_conv_delete, methods=["DELETE"]),
        Route("/app/api/chat/stream", api_chat_stream, methods=["POST"]),
        Route("/app/api/chat/confirm", api_chat_confirm, methods=["POST"]),
        Route("/app/api/usage", api_usage, methods=["GET"]),
        Route("/app/api/passkeys", api_passkeys_list, methods=["GET"]),
        Route(
            "/app/api/passkeys/register/options",
            api_passkeys_register_options, methods=["POST"],
        ),
        Route(
            "/app/api/passkeys/register/verify",
            api_passkeys_register_verify, methods=["POST"],
        ),
        Route("/app/api/passkeys/delete", api_passkeys_delete, methods=["POST"]),
        Route("/app/passkeys/remove", passkeys_remove, methods=["POST"]),
        Route(
            "/app/api/passkeys/auth/options",
            api_passkeys_auth_options, methods=["POST"],
        ),
        Route(
            "/app/api/passkeys/auth/verify",
            api_passkeys_auth_verify, methods=["POST"],
        ),
        Route("/", index, methods=["GET"]),
        Mount(
            "/app/static",
            app=StaticFiles(directory=_DASHBOARD_DIR / "static"),
            name="dashboard-static",
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers used by API routes
# ---------------------------------------------------------------------------

def _json(payload: Any, *, status: int = 200) -> Response:
    response = JSONResponse(payload, status_code=status)
    _apply_security_headers(response)
    return response


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _require_active_session(
    request: Request, deps: DashboardDeps,
) -> Session | Response:
    session = _load_session(request, deps)
    if not session:
        return _json({"error": "unauthorized"}, status=401)
    if not _bearer_live(deps, session):
        return _json({"error": "bearer_expired"}, status=401)
    return session


def _render_tokens_page(
    request: Request,
    session: Session,
    deps: DashboardDeps,
    *,
    form_error: str | None = None,
    form_name: str = "",
    just_created: dict[str, Any] | None = None,
) -> Response:
    """Render the /app/tokens page (list + create form)."""
    tokens_raw = deps.token_store.list_named(session.client_id)  # type: ignore[attr-defined]
    now = time.time()
    def _expires_label(expires_at: float) -> str:
        if math.isinf(expires_at):
            return "never expires (revoke-only)"
        secs = max(0, expires_at - now)
        if secs >= 48 * 3600:
            return f"expires in {round(secs / 86400)} days"
        return f"expires in {max(0, round(secs / 3600, 1))} h"

    tokens = [
        {
            "name": t.name,
            "prefix": t.token[:12],
            "created_at": t.created_at,
            "expires_at": t.expires_at,
            "expires_in_hours": (
                None if math.isinf(t.expires_at)
                else max(0, round((t.expires_at - now) / 3600, 1))
            ),
            "expires_label": _expires_label(t.expires_at),
        }
        for t in tokens_raw
    ]
    count = len(tokens)
    cap = getattr(deps.token_store, "NAMED_TOKEN_CAP", 3)
    # Named-token lifetime for the page copy. A TTL of 0 means
    # "never expires" -- the token lives until revoked.
    named_ttl_seconds = getattr(
        deps.token_store, "named_token_ttl", 24 * 3600,
    )
    if named_ttl_seconds == 0:
        named_ttl_label = "until revoked (no expiry)"
    else:
        days = max(1, round(named_ttl_seconds / 86400))
        named_ttl_label = f"{days} day{'s' if days != 1 else ''}"

    client_name = (
        deps.client_store.get_name(session.client_id)  # type: ignore[attr-defined]
        or session.client_id
    )

    # Build the MCP URL the user should paste into external clients.
    # We prefer the public URL if configured (that's what Gemini/ChatGPT
    # need to reach us) and otherwise reconstruct from the request.
    if deps.mcp_public_url:
        mcp_url = deps.mcp_public_url.rstrip("/") + "/mcp"
    else:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "localhost"),
        )
        mcp_url = f"{scheme}://{host}/mcp"

    return _render(
        "tokens.html",
        request,
        client_id=session.client_id,
        client_name=client_name,
        tokens=tokens,
        count=count,
        cap=cap,
        named_ttl_label=named_ttl_label,
        can_create=count < cap,
        form_error=form_error,
        form_name=form_name,
        just_created=just_created,
        mcp_url=mcp_url,
        locked=deps.totp_locked(session.client_id),
        chat_enabled=deps.engine is not None,
        dcr_enabled=deps.dyn_reg is not None,
        passkeys_enabled=_passkeys_ready(deps),
        passkeys=(
            [
                {
                    "credential_id": p.credential_id,
                    "label": p.label,
                    "created_human": _human_time(p.created_at),
                    "last_used_human": (
                        _human_time(p.last_used_at) if p.last_used_at else None
                    ),
                }
                for p in deps.passkeys.store.list_for_client(session.client_id)
            ]
            if _passkeys_ready(deps) else []
        ),
    )


def _resolve_mcp_url(request: Request, deps: DashboardDeps) -> str:
    """Return the URL the chat engine should use for MCP.

    - In **local** mode the dashboard opens the session itself, so we
      ALWAYS use loopback. Going through a public URL means a round-trip
      via Cloudflare (or whatever reverse proxy) which can strip the
      Authorization header, deterministically producing a 401 on /mcp
      that then cascades into a malformed tools list and a 500 from
      Gemini. ``mcp_public_url`` is ignored here by design.
    - In **remote** mode Google's backend calls the URL directly, so we
      must return a publicly reachable address. Users can override via
      ``BEACONMCP_DASHBOARD_PUBLIC_URL``; otherwise we fall back to the
      reverse-proxy Host header.
    """
    if deps.mcp_mode == "remote":
        if deps.mcp_public_url:
            return deps.mcp_public_url.rstrip("/") + "/mcp"
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "localhost"),
        )
        return f"{scheme}://{host}/mcp"
    import os as _os
    port = _os.environ.get("BEACONMCP_PORT", "8420")
    return f"http://127.0.0.1:{port}/mcp"


def _sse(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _human_time(ts: float) -> str:
    """Render a Unix timestamp as a short human-readable date."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _format_quota_message(block: Any) -> str:
    """Build a French user-facing sentence for a :class:`BudgetBlock`.

    Kept here (rather than on ``BudgetBlock``) so the usage module stays
    locale-agnostic and UI-agnostic.
    """
    spent = f"${block.spent_usd:.2f}"
    limit = f"${block.limit_usd:.2f}"
    if block.window == "5h":
        reset_at = block.reset_at
        if reset_at:
            # Format in local time, HH:MM
            import datetime as _dt
            tail = _dt.datetime.fromtimestamp(reset_at).strftime("%H:%M")
            reset_txt = f"The session resets at {tail}."
        else:
            reset_txt = (
                "The session will reset after 5 h of inactivity."
            )
        return (
            f"Per-5h-session limit of {limit} reached "
            f"(spent {spent}). {reset_txt}"
        )
    # weekly
    return (
        f"Weekly limit of {limit} reached "
        f"(spent {spent} over the rolling 7-day window). "
        "Try again later."
    )
