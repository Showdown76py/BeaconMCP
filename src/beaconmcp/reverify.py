"""``/oauth/reverify``: renew a lapsed human session with a passkey or TOTP.

Reached from the link a tool call elicits (see :mod:`beaconmcp.reauth`).
The opaque id in the URL is the only capability; the second factor must
belong to the client that owns the waiting bearer.
"""

from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import audit, oauth_ui
from .auth import TOTP_REPLAY_MESSAGE, TotpResult
from .client_identity import ClientIdentity, identify
from .ratelimit import client_ip
from .reauth import ClientInfoRegistry, ClientSessionInfo, PendingReverify, PendingReverifyStore


@dataclass
class ReverifyDeps:
    client_store: Any
    token_store: Any
    pending: PendingReverifyStore
    registry: ClientInfoRegistry
    session_ttl: int
    totp_locked: Callable[[str], bool]
    totp_record_failure: Callable[[str], None]
    totp_record_success: Callable[[str], None]
    passkey_service: Any
    passkey_owner: Callable[[str], str]
    passkeys_enabled: Callable[[Request], bool]
    login_limiter: Any
    logo_url_for: Callable[[ClientIdentity, ClientSessionInfo | None, str], str]
    trusted_proxies: tuple[str, ...] = ()


def _ago(ts: float, now: float | None = None) -> str:
    secs = max(0, (now or time.time()) - ts)
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{round(secs / 60)} min ago"
    if secs < 48 * 3600:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} days ago"


def _args_line(args: dict) -> str:
    parts = []
    for k, v in args.items():
        shown = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={shown}")
    line = " · ".join(parts)
    return line[:300] + ("…" if len(line) > 300 else "")


def _device(entry: PendingReverify, info: ClientSessionInfo | None) -> str:
    if info and (info.title or info.name):
        label = info.title or info.name
        return f"{label} {info.version}".strip()
    return entry.user_agent[:80] or "Unknown"


def _ts(value: float, fallback_fmt: str = "%d %b %H:%M") -> str:
    fallback = time.strftime(fallback_fmt, time.localtime(value))
    return f'<time data-ts="{value:.0f}">{html.escape(fallback)}</time>'


def render_reverify(
    entry: PendingReverify, *, host: str, identity: ClientIdentity, logo_url: str,
    registered_name: str, info: ClientSessionInfo | None, passkeys_on: bool,
    session_ttl: int, error: str | None = None, done: bool = False,
    session_expires_at: float | None = None, via: str = "",
) -> str:
    e = html.escape
    client = e(identity.name)
    error_block = (
        f'<div class="banner" id="verify-error">{e(error)}</div>' if error
        else '<div class="banner" id="verify-error" hidden></div>'
    )
    expires_at = session_expires_at or (time.time() + session_ttl)
    verify = f"""
  <section id="step-verify"{' hidden' if done else ''}>
    <p class="last-seen">Last verified {_ts(entry.last_verified_at)}</p>
    <h1>Verify it's still you</h1>
    <p class="sub"><strong>{client}</strong> paused a request until you confirm.</p>
    <div class="pending">
      <div class="k">Waiting request</div>
      <code>{e(entry.tool)}</code>
      <div class="args">{e(_args_line(entry.args))}</div>
      <div class="meta"><span>From {client} · {e(_ago(entry.created_at))}</span><span>Link expires in <span id="link-countdown" data-expires="{entry.expires_at:.0f}">{max(0, round((entry.expires_at - time.time()) / 60))} min</span></span></div>
    </div>
    <div class="origin">
      <div class="row"><span>From</span><b>{e(entry.ip)}</b></div>
      <div class="row"><span>Device</span><b>{e(_device(entry, info))}</b></div>
      <div class="row"><span>Connected</span><b>{e(_ago(entry.bearer_issued_at))}</b></div>
    </div>
    {error_block}
    <button type="button" class="btn-primary" id="passkey-btn"{'' if passkeys_on else ' hidden'}>
      {oauth_ui.PASSKEY_ICON}<span class="btn-label">Verify with passkey</span>
    </button>
    <p class="center"{'' if passkeys_on else ' hidden'}><button type="button" class="link-btn" id="totp-toggle">Use a 6-digit code instead</button></p>
    <form method="POST" action="/oauth/reverify" id="reverify-form">
      <div id="totp-block"{' hidden' if passkeys_on else ''}>
        <input type="hidden" name="id" value="{e(entry.id)}">
        {oauth_ui.totp_boxes()}
        <noscript><input name="totp" inputmode="numeric" pattern="[0-9]{{6}}" placeholder="6-digit code" required></noscript>
        <button type="submit" class="btn-primary" id="verify-btn" disabled>
          <span class="btn-label">Verify</span>
        </button>
      </div>
    </form>
    <div class="foot">
      <div class="row"><span>Signed in as</span><b>{e(registered_name)}</b></div>
      <div class="row"><span>Not you?</span><button type="button" class="link-btn danger" id="revoke-btn">Revoke this access</button></div>
    </div>
  </section>"""
    success = f"""
  <section id="step-done"{'' if done else ' hidden'}>
    <div class="success-mark">{oauth_ui.CHECK_ICON}</div>
    <h1>You're verified</h1>
    <p class="sub">Return to <strong>{client}</strong>, your request is running now.</p>
    <div class="resume"><span class="spin" aria-hidden="true"></span><span><code>{e(entry.tool)}</code> resuming in {client}…</span></div>
    <div class="foot" style="margin-top:0;border-top:0;padding-top:0">
      <div class="row"><span>Session valid until</span><b><time id="session-until" data-ts="{expires_at:.0f}">{e(time.strftime("%d %b %H:%M", time.localtime(expires_at)))}</time></b></div>
      <div class="row"><span>Verified with</span><b id="verified-via">{e(via or "—")}</b></div>
    </div>
    <p class="hint">You can close this tab.</p>
  </section>"""
    body = f"""<div class="auth-card" data-pid="{e(entry.id)}" data-passkeys="{'true' if passkeys_on else 'false'}">
  {oauth_ui.server_bar(host)}
  <div class="auth-body">
  {oauth_ui.client_pair(logo_url, identity.name)}
  {verify}
  {success}
  </div>
</div>"""
    return oauth_ui.page("BeaconMCP · Verify", body, script=oauth_ui.REVERIFY_JS)


def _gone_page(host: str) -> str:
    body = f"""<div class="auth-card">
  {oauth_ui.server_bar(host)}
  <div class="auth-body">
    <h1>This link has expired</h1>
    <p class="sub">It was already used, revoked, or is more than a few minutes old. Retry the request in your client to get a new one.</p>
  </div>
</div>"""
    return oauth_ui.page("BeaconMCP · Link expired", body)


def build_reverify_routes(deps: ReverifyDeps) -> list[Route]:
    def _host(request: Request) -> str:
        return request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))

    def _wants_json(request: Request) -> bool:
        return request.headers.get("x-beaconmcp-mode", "") == "json"

    def _identity(entry: PendingReverify) -> tuple[ClientIdentity, ClientSessionInfo | None, str]:
        info = deps.registry.get(entry.session_key)
        registered = deps.client_store.get_name(entry.client_id) or entry.client_id
        identity = identify(
            client_info_name=(info.name if info else ""), registered_name=registered,
        )
        return identity, info, registered

    def _render(request: Request, entry: PendingReverify, *, status: int = 200, **kw) -> HTMLResponse:
        identity, info, registered = _identity(entry)
        return HTMLResponse(render_reverify(
            entry, host=_host(request), identity=identity,
            logo_url=deps.logo_url_for(identity, info, entry.client_id), registered_name=registered,
            info=info, passkeys_on=deps.passkeys_enabled(request),
            session_ttl=deps.session_ttl, **kw,
        ), status_code=status, headers=oauth_ui.SECURITY_HEADERS)

    def _gone(request: Request) -> Response:
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "This link has expired. Retry the request in your client."},
                status_code=410,
            )
        return HTMLResponse(
            _gone_page(_host(request)), status_code=410, headers=oauth_ui.SECURITY_HEADERS,
        )

    def _limited(request: Request) -> Response | None:
        ip = client_ip(request, deps.trusted_proxies)
        if deps.login_limiter is not None and not deps.login_limiter.check(ip):
            retry = deps.login_limiter.retry_after(ip)
            return JSONResponse(
                {"ok": False, "error": f"Too many attempts. Retry in {retry}s."},
                status_code=429,
            )
        return None

    def _fail(request: Request, entry: PendingReverify, message: str, status: int) -> Response:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": message}, status_code=status)
        return _render(request, entry, status=status, error=message)

    def _succeed(request: Request, entry: PendingReverify, via: str) -> Response:
        now = time.time()
        deps.token_store.mark_verified(entry.session_key, at=now)
        deps.pending.complete(entry.id)
        audit.emit(
            "auth.reverify.ok", client_id=entry.client_id, tool=entry.tool, via=via,
            ip=client_ip(request, deps.trusted_proxies),
        )
        expires = now + deps.session_ttl
        if _wants_json(request):
            return JSONResponse({"ok": True, "session_expires_at": expires, "via": via})
        return _render(request, entry, done=True, session_expires_at=expires, via=via)

    async def _body(request: Request) -> dict:
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                data = await request.json()
            except Exception:  # noqa: BLE001
                return {}
            return data if isinstance(data, dict) else {}
        form = await request.form()
        return {k: v for k, v in form.items() if isinstance(v, str)}

    async def reverify_get(request: Request) -> Response:
        entry = deps.pending.get(request.query_params.get("id", ""))
        if entry is None:
            return _gone(request)
        return _render(request, entry)

    async def reverify_post(request: Request) -> Response:
        blocked = _limited(request)
        if blocked is not None:
            return blocked
        body = await _body(request)
        entry = deps.pending.get(str(body.get("id") or ""))
        if entry is None:
            return _gone(request)
        client_id = entry.client_id
        if deps.totp_locked(client_id):
            return _fail(request, entry, "Too many attempts. Try again in 5 minutes.", 429)
        result = deps.client_store.check_totp(client_id, str(body.get("totp") or "").strip())
        if result is not TotpResult.OK:
            if result is TotpResult.INVALID:
                deps.totp_record_failure(client_id)
            audit.emit(
                "auth.reverify.fail", client_id=client_id, reason=result.value,
                ip=client_ip(request, deps.trusted_proxies),
            )
            message = (
                TOTP_REPLAY_MESSAGE if result is TotpResult.REPLAY
                else "Incorrect code. Check that your device clock is in sync."
            )
            return _fail(request, entry, message, 401)
        deps.totp_record_success(client_id)
        return _succeed(request, entry, "6-digit code")

    def _passkeys_ready() -> Response | None:
        service = deps.passkey_service
        if service is None or not service.available:
            return JSONResponse(
                {"ok": False, "error": "Passkeys are not available here."}, status_code=503,
            )
        return None

    async def passkey_options(request: Request) -> Response:
        blocked = _limited(request) or _passkeys_ready()
        if blocked is not None:
            return blocked
        body = await _body(request)
        entry = deps.pending.get(str(body.get("id") or ""))
        if entry is None:
            return _gone(request)
        if deps.totp_locked(entry.client_id):
            return JSONResponse(
                {"ok": False, "error": "Too many attempts. Try again in 5 minutes."},
                status_code=429,
            )
        try:
            options, state = deps.passkey_service.authentication_options(
                request, client_id=deps.passkey_owner(entry.client_id),
            )
        except Exception as exc:  # noqa: BLE001 -- PasskeyError and friends
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "options": options, "state": state})

    async def passkey_verify(request: Request) -> Response:
        blocked = _limited(request) or _passkeys_ready()
        if blocked is not None:
            return blocked
        body = await _body(request)
        entry = deps.pending.get(str(body.get("id") or ""))
        if entry is None:
            return _gone(request)
        credential = body.get("credential")
        state = str(body.get("state") or "")
        if not isinstance(credential, dict) or not state:
            return JSONResponse(
                {"ok": False, "error": "Malformed passkey response."}, status_code=400,
            )
        try:
            record = deps.passkey_service.verify_authentication(
                request, state=state, credential=credential,
            )
        except Exception as exc:  # noqa: BLE001
            audit.emit(
                "auth.reverify.fail", client_id=entry.client_id, reason=f"passkey:{exc}",
                ip=client_ip(request, deps.trusted_proxies),
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        if record.client_id != deps.passkey_owner(entry.client_id):
            audit.emit(
                "auth.reverify.fail", client_id=entry.client_id, reason="passkey:other_client",
                ip=client_ip(request, deps.trusted_proxies),
            )
            return JSONResponse(
                {"ok": False, "error": "This passkey belongs to another client."},
                status_code=401,
            )
        deps.totp_record_success(entry.client_id)
        label = getattr(record, "label", "") or ""
        return _succeed(request, entry, f"Passkey · {label}" if label else "Passkey")

    async def revoke(request: Request) -> Response:
        blocked = _limited(request)
        if blocked is not None:
            return blocked
        body = await _body(request)
        entry = deps.pending.get(str(body.get("id") or ""))
        if entry is None:
            return _gone(request)
        deps.token_store.revoke_session(entry.session_key)
        deps.pending.revoke_token(entry.session_key)
        audit.emit(
            "auth.reverify.revoked", client_id=entry.client_id, tool=entry.tool,
            ip=client_ip(request, deps.trusted_proxies),
        )
        return JSONResponse({"ok": True})

    return [
        Route("/oauth/reverify", reverify_get, methods=["GET"]),
        Route("/oauth/reverify", reverify_post, methods=["POST"]),
        Route("/oauth/reverify/passkey/options", passkey_options, methods=["POST"]),
        Route("/oauth/reverify/passkey/verify", passkey_verify, methods=["POST"]),
        Route("/oauth/reverify/revoke", revoke, methods=["POST"]),
    ]
