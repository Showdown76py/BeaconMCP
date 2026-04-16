"""Starlette routes for the TarkaMCP dashboard.

Builds the route list and dependencies. Mounted under ``/app/*`` from
``__main__._run_http``. The MCP routes (``/mcp``, ``/oauth/*``,
``/.well-known/*``) are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import csrf as csrf
from .db import Database
from .session import SESSION_TTL_SECONDS, Session, SessionStore


SESSION_COOKIE = "tarkamcp_session"

# 24h, matches the MCP TokenStore.TOKEN_TTL.
BEARER_TTL_SECONDS = 24 * 3600


_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=_DASHBOARD_DIR / "templates")


@dataclass
class DashboardDeps:
    """Dependencies injected from ``__main__._run_http``."""

    database: Database
    session_store: SessionStore
    client_store: object  # tarkamcp.auth.ClientStore -- avoid import cycle
    token_store: object  # tarkamcp.auth.TokenStore
    totp_locked: Callable[[str], bool]
    totp_record_failure: Callable[[str], None]
    totp_record_success: Callable[[str], None]


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


def _set_csrf_cookie(response: Response, secure: bool) -> str:
    token = csrf.issue_token()
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
    # CSP for /app/* pages. No external resources, no inline scripts.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
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


# ---------------------------------------------------------------------------
# Route factories
# ---------------------------------------------------------------------------

def build_dashboard_routes(deps: DashboardDeps) -> list[Route | Mount]:
    """Return Starlette routes for the dashboard, ready to mount."""

    async def index(request: Request) -> Response:
        session = _load_session(request, deps)
        if session and session.bearer_valid():
            return RedirectResponse("/app/chat", status_code=302)
        if session:
            return RedirectResponse("/app/refresh", status_code=302)
        return RedirectResponse("/app/login", status_code=302)

    async def login_get(request: Request) -> Response:
        # If a valid session already exists, send straight to chat.
        session = _load_session(request, deps)
        if session and session.bearer_valid():
            return RedirectResponse("/app/chat", status_code=302)
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
        )

    async def login_post(request: Request) -> Response:
        if not await csrf.verify(request):
            return JSONResponse({"error": "csrf"}, status_code=403)

        form = await request.form()

        def _v(name: str) -> str:
            v = form.get(name, "")
            return v if isinstance(v, str) else ""

        client_id = _v("client_id").strip()
        client_secret = _v("client_secret")
        totp = _v("totp").strip()
        next_url = _v("next").strip() or "/app/chat"
        if not (next_url.startswith("/app/") or next_url == "/app/chat"):
            next_url = "/app/chat"

        def _fail(message: str, *, status: int = 400, locked: bool = False) -> Response:
            return _render(
                "login.html",
                request,
                client_id=client_id,
                next=next_url if next_url != "/app/chat" else "",
                banner=message,
                locked=locked,
                status_code=status,
            )

        if not client_id or not client_secret or not totp:
            return _fail("Tous les champs sont requis.")

        if not deps.client_store.verify(client_id, client_secret):  # type: ignore[attr-defined]
            return _fail("Identifiants invalides.", status=401)

        if deps.totp_locked(client_id):
            return _fail(
                "Trop de tentatives. Réessaie dans 5 minutes.",
                status=429,
                locked=True,
            )

        if not deps.client_store.verify_totp(client_id, totp):  # type: ignore[attr-defined]
            deps.totp_record_failure(client_id)
            return _fail(
                "Code 2FA invalide. Vérifie l'horloge de ton téléphone.",
                status=401,
                locked=deps.totp_locked(client_id),
            )

        deps.totp_record_success(client_id)
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
        response = RedirectResponse(next_url, status_code=303)
        _set_session_cookie(response, session.session_id, secure)
        _set_csrf_cookie(response, secure)
        _apply_security_headers(response)
        return response

    async def refresh_get(request: Request) -> Response:
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if session.bearer_valid():
            return RedirectResponse("/app/chat", status_code=302)

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
            next_url = "/app/chat"

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
                next=next_url if next_url != "/app/chat" else "",
                banner=message,
                locked=locked,
                status_code=status,
            )

        if not totp:
            return _fail("Code 2FA requis.")

        if deps.totp_locked(session.client_id):
            return _fail(
                "Trop de tentatives. Réessaie dans 5 minutes.",
                status=429, locked=True,
            )

        if not deps.client_store.verify_totp(session.client_id, totp):  # type: ignore[attr-defined]
            deps.totp_record_failure(session.client_id)
            return _fail(
                "Code 2FA invalide.",
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

    async def chat_get(request: Request) -> Response:
        # Placeholder until Stage 2/3 wire the chat shell.
        session = _load_session(request, deps)
        if not session:
            return RedirectResponse("/app/login", status_code=302)
        if not session.bearer_valid():
            return RedirectResponse("/app/refresh", status_code=302)
        body = (
            "<!doctype html><meta charset=utf-8><title>TarkaMCP</title>"
            "<style>body{font-family:-apple-system,system-ui,sans-serif;"
            "padding:2rem;color:#444}</style>"
            "<h1>Dashboard chat</h1><p>Connecté en tant que "
            f"<code>{session.client_id}</code>. UI à venir.</p>"
            "<form method=POST action=/app/logout>"
            f'<input type=hidden name=csrf_token value="{csrf.cookie_token(request)}">'
            "<button>Déconnexion</button></form>"
        )
        response = HTMLResponse(body)
        _apply_security_headers(response)
        return response

    return [
        Route("/app/login", login_get, methods=["GET"]),
        Route("/app/login", login_post, methods=["POST"]),
        Route("/app/refresh", refresh_get, methods=["GET"]),
        Route("/app/refresh", refresh_post, methods=["POST"]),
        Route("/app/logout", logout, methods=["POST"]),
        Route("/app/chat", chat_get, methods=["GET"]),
        Route("/", index, methods=["GET"]),
        Mount(
            "/app/static",
            app=StaticFiles(directory=_DASHBOARD_DIR / "static"),
            name="dashboard-static",
        ),
    ]
