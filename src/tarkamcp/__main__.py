import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv MUST run before importing server, because server.py
# triggers Config.from_env() at import time
load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="TarkaMCP - Proxmox MCP Server")
    sub = parser.add_subparsers(dest="command")

    # --- serve (default) ---
    serve_parser = sub.add_parser("serve", help="Start the MCP HTTP server")
    serve_parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TARKAMCP_PORT", "8420")),
        help="HTTP port (default: 8420)",
    )
    serve_parser.add_argument(
        "--host", default=os.environ.get("TARKAMCP_HOST", "0.0.0.0"),
        help="HTTP bind address (default: 0.0.0.0)",
    )

    # --- auth ---
    auth_parser = sub.add_parser("auth", help="Manage OAuth client credentials")
    auth_sub = auth_parser.add_subparsers(dest="auth_command")

    create_parser = auth_sub.add_parser("create", help="Create a new client")
    create_parser.add_argument("--name", required=True, help="Client name (e.g., 'Claude Web', 'Mon iPhone')")
    create_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    list_parser = auth_sub.add_parser("list", help="List all clients")
    list_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    revoke_parser = auth_sub.add_parser("revoke", help="Revoke a client")
    revoke_parser.add_argument("client_id", help="Client ID to revoke")
    revoke_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        _cmd_serve(args)
    elif args.command == "auth":
        _cmd_auth(args)


def _cmd_serve(args):
    from .server import mcp

    host = getattr(args, "host", os.environ.get("TARKAMCP_HOST", "0.0.0.0"))
    port = getattr(args, "port", int(os.environ.get("TARKAMCP_PORT", "8420")))
    _run_http(mcp, host, port)


def _cmd_auth(args):
    from .auth import ClientStore

    store = ClientStore(getattr(args, "clients_file", None))

    if args.auth_command == "create":
        import pyotp
        import qrcode

        client_id, client_secret, totp_secret = store.create(args.name)
        provisioning_uri = pyotp.TOTP(totp_secret).provisioning_uri(
            name=client_id, issuer_name="TarkaMCP"
        )
        qr = qrcode.QRCode(border=1)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        print()
        print("  Client créé avec succès !")
        print()
        print(f"  Name:          {args.name}")
        print(f"  Client ID:     {client_id}")
        print(f"  Client Secret: {client_secret}")
        print()
        print("  --- 2FA / Google Authenticator ---")
        print("  Scanne ce QR code dans ton app (Google Authenticator, Authy, 1Password) :")
        print()
        qr.print_ascii(invert=True)
        print()
        print(f"  Secret manuel (si le scan ne marche pas) : {totp_secret}")
        print(f"  URI otpauth                              : {provisioning_uri}")
        print()
        print("  Le Client Secret et le secret TOTP ne seront PLUS affichés.")
        print("  Conserve-les maintenant, sinon tu devras recréer le client.")
        print()

    elif args.auth_command == "list":
        clients = store.list_clients()
        if not clients:
            print("Aucun client enregistré.")
            return
        print(f"\n{'Client ID':<30} {'Name':<25} {'Créé le'}")
        print("-" * 75)
        from datetime import datetime
        for c in clients:
            created = datetime.fromtimestamp(c["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{c['client_id']:<30} {c['name']:<25} {created}")
        print()

    elif args.auth_command == "revoke":
        if store.revoke(args.client_id):
            print(f"Client {args.client_id} révoqué.")
        else:
            print(f"Client {args.client_id} introuvable.")
            sys.exit(1)

    else:
        print("Usage: tarkamcp auth {create|list|revoke}")
        sys.exit(1)


def _run_http(mcp, host: str, port: int):
    """Run the MCP server over Streamable HTTP with OAuth client credentials."""
    import html
    import time
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Mount, Route

    from urllib.parse import urlencode, urlparse

    from .auth import ClientStore, CodeStore, TokenStore

    clients_file = os.environ.get("TARKAMCP_CLIENTS_FILE")
    client_store = ClientStore(Path(clients_file) if clients_file else None)
    token_store = TokenStore()
    code_store = CodeStore()

    # In-memory TOTP bruteforce guard: per client_id, (failures, cooldown_until).
    # 5 failed attempts → 5-minute lockout. Reset on first success.
    totp_fail_max = 5
    totp_lockout_seconds = 300
    totp_failures: dict[str, tuple[int, float]] = {}

    def totp_locked(client_id: str) -> bool:
        entry = totp_failures.get(client_id)
        if not entry:
            return False
        count, until = entry
        if count < totp_fail_max:
            return False
        if time.time() >= until:
            totp_failures.pop(client_id, None)
            return False
        return True

    def totp_record_failure(client_id: str) -> None:
        count, _ = totp_failures.get(client_id, (0, 0.0))
        count += 1
        totp_failures[client_id] = (count, time.time() + totp_lockout_seconds)

    def totp_record_success(client_id: str) -> None:
        totp_failures.pop(client_id, None)

    def _issuer(request: Request) -> str:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host_header = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "localhost")
        )
        return f"{scheme}://{host_header}"

    async def oauth_metadata(request: Request) -> Response:
        issuer = _issuer(request)
        # registration_endpoint is intentionally omitted: dynamic client
        # registration is disabled, clients must be provisioned via CLI.
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        })

    async def protected_resource_metadata(request: Request) -> Response:
        # RFC 9728 - required by the MCP 2025-06-18 spec so that clients
        # (Claude Web in particular) can discover which authorization server
        # protects the /mcp resource. We act as our own authorization server.
        issuer = _issuer(request)
        return JSONResponse({
            "resource": issuer,
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        })

    def _validate_authorize_params(
        params: dict[str, str],
    ) -> tuple[dict[str, str], Response | None]:
        """Validate the standard OAuth2 authorize parameters.

        Returns ``(normalized, error_response)``. If ``error_response`` is not
        None it must be returned directly without redirecting, because until
        client_id and redirect_uri are trusted we cannot send the user back
        to an attacker-controlled URL (OAuth 2.1 §4.1.2.1).
        """
        response_type = params.get("response_type", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        if response_type != "code":
            return {}, JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if not client_id or not client_store.exists(client_id):
            return {}, JSONResponse({"error": "unauthorized_client"}, status_code=400)
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must be an absolute URL"},
                status_code=400,
            )
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must use https"},
                status_code=400,
            )
        if not code_challenge or code_challenge_method != "S256":
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE with S256 is required"},
                status_code=400,
            )

        return (
            {
                "response_type": response_type,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            },
            None,
        )

    def _render_authorize_form(
        normalized: dict[str, str], error: str | None = None, locked: bool = False
    ) -> HTMLResponse:
        client_name = client_store.get_name(normalized["client_id"]) or normalized["client_id"]
        hidden = "\n".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in normalized.items()
        )
        banner = ""
        if locked:
            banner = (
                '<p class="error">Trop de tentatives. R\u00e9essaie dans 5 minutes.</p>'
            )
        elif error:
            banner = f'<p class="error">{html.escape(error)}</p>'

        disabled = "disabled" if locked else ""
        page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>TarkaMCP - Authentification 2FA</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#111; color:#eee; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#1c1c1c; padding:2rem; border-radius:12px; max-width:380px; width:100%; box-shadow:0 4px 16px rgba(0,0,0,0.4); }}
  h1 {{ font-size:1.25rem; margin:0 0 0.5rem; }}
  p {{ color:#aaa; font-size:0.9rem; margin:0.25rem 0 1rem; }}
  .error {{ color:#ff6b6b; }}
  input[type=text] {{ width:100%; box-sizing:border-box; padding:0.75rem; font-size:1.5rem; text-align:center; letter-spacing:0.5rem; border:1px solid #333; background:#0d0d0d; color:#fff; border-radius:8px; }}
  button {{ margin-top:1rem; width:100%; padding:0.75rem; border:none; border-radius:8px; background:#e57000; color:#fff; font-size:1rem; cursor:pointer; }}
  button:disabled {{ background:#555; cursor:not-allowed; }}
</style></head>
<body><div class="card">
<h1>TarkaMCP</h1>
<p>Authentification \u00e0 deux facteurs pour <strong>{html.escape(client_name)}</strong>.</p>
{banner}
<form method="POST" action="/oauth/authorize">
{hidden}
<input type="text" name="totp" inputmode="numeric" pattern="\\d{{6}}" maxlength="6" autocomplete="one-time-code" autofocus required placeholder="000000" {disabled}>
<button type="submit" {disabled}>Valider</button>
</form>
</div></body></html>
"""
        return HTMLResponse(page)

    async def oauth_authorize_get(request: Request) -> Response:
        normalized, err = _validate_authorize_params(dict(request.query_params))
        if err is not None:
            return err
        return _render_authorize_form(
            normalized, locked=totp_locked(normalized["client_id"])
        )

    async def oauth_authorize_post(request: Request) -> Response:
        form = await request.form()
        body = {k: v for k, v in form.items() if isinstance(v, str)}
        normalized, err = _validate_authorize_params(body)
        if err is not None:
            return err

        client_id = normalized["client_id"]
        if totp_locked(client_id):
            return _render_authorize_form(normalized, locked=True)

        code_totp = body.get("totp", "")
        if not client_store.verify_totp(client_id, code_totp):
            totp_record_failure(client_id)
            return _render_authorize_form(
                normalized,
                error="Code incorrect. V\u00e9rifie l'horloge de ton t\u00e9l\u00e9phone.",
                locked=totp_locked(client_id),
            )
        totp_record_success(client_id)

        redirect_uri = normalized["redirect_uri"]
        code = code_store.issue(
            client_id,
            redirect_uri,
            normalized["code_challenge"],
            normalized["code_challenge_method"],
        )
        query = {"code": code}
        if normalized["state"]:
            query["state"] = normalized["state"]
        parsed = urlparse(redirect_uri)
        sep = "&" if parsed.query else "?"
        location = f"{redirect_uri}{sep}{urlencode(query)}"
        return Response(status_code=302, headers={"Location": location})

    async def oauth_token(request: Request) -> Response:
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                raw = await request.json()
                body: dict[str, str] = {k: v for k, v in raw.items() if isinstance(v, str)}
            else:
                form = await request.form()
                body = {k: v for k, v in form.items() if isinstance(v, str)}
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        grant_type = body.get("grant_type", "")
        client_id = body.get("client_id", "")
        client_secret = body.get("client_secret", "")

        if not client_store.verify(client_id, client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if grant_type == "client_credentials":
            # 2FA mandatory on every client_credentials exchange (design choice:
            # no non-interactive escape hatch, the operator must re-type a TOTP
            # code at every 24 h token refresh).
            if totp_locked(client_id):
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "too many failed TOTP attempts, retry later",
                    },
                    status_code=400,
                )
            code_totp = body.get("totp", "")
            if not client_store.verify_totp(client_id, code_totp):
                totp_record_failure(client_id)
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "missing or invalid totp",
                    },
                    status_code=400,
                )
            totp_record_success(client_id)
            token, expires_in = token_store.issue(client_id)
            return JSONResponse({
                "access_token": token,
                "token_type": "bearer",
                "expires_in": expires_in,
            })

        if grant_type == "authorization_code":
            code = body.get("code", "")
            redirect_uri = body.get("redirect_uri", "")
            code_verifier = body.get("code_verifier", "")
            if not code_store.consume(code, client_id, redirect_uri, code_verifier):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            token, expires_in = token_store.issue(client_id)
            return JSONResponse({
                "access_token": token,
                "token_type": "bearer",
                "expires_in": expires_in,
            })

        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def oauth_register(_request: Request) -> Response:
        # Dynamic client registration is disabled by design. Respond explicitly
        # instead of letting the request fall through to a generic 404.
        return JSONResponse(
            {
                "error": "registration_not_supported",
                "error_description": "Dynamic client registration is disabled. Ask the administrator to provision a client via `tarkamcp auth create`.",
            },
            status_code=403,
        )

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "server": "tarkamcp"})

    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in (
            "/health",
            "/oauth/token",
            "/oauth/authorize",
            "/oauth/register",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            return await call_next(request)

        # MCP 2025-06-18 + RFC 9728: point unauth'd clients at the resource
        # metadata so they can discover the authorization server.
        issuer = _issuer(request)
        resource_meta = f"{issuer}/.well-known/oauth-protected-resource"

        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="tarkamcp", resource_metadata="{resource_meta}"',
                },
            )

        client_id = token_store.validate(authorization[7:])
        if not client_id:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="tarkamcp", error="invalid_token", resource_metadata="{resource_meta}"',
                },
            )
        return await call_next(request)

    mcp_app = mcp.streamable_http_app()

    # The MCP streamable-HTTP app starts its session manager task group in its
    # own lifespan. When we Mount it under a parent Starlette, only the parent
    # app's lifespan runs — so we forward the child's lifespan explicitly,
    # otherwise requests fail with "Task group is not initialized".
    @asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.router.lifespan_context(_app):
            yield

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/.well-known/oauth-authorization-server", oauth_metadata),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata),
            Route("/oauth/authorize", oauth_authorize_get, methods=["GET"]),
            Route("/oauth/authorize", oauth_authorize_post, methods=["POST"]),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/oauth/register", oauth_register, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_middleware)],
        lifespan=lifespan,
    )

    n_clients = len(client_store.list_clients())
    print(f"TarkaMCP starting on {host}:{port}")
    print(f"Clients: {n_clients}")
    print(f"MCP:       http://{host}:{port}/mcp")
    print(f"Authorize: http://{host}:{port}/oauth/authorize")
    print(f"Token:     http://{host}:{port}/oauth/token")
    print(f"Health:    http://{host}:{port}/health")
    if n_clients == 0:
        print(f"\nAucun client ! Créer avec : tarkamcp auth create --name 'Mon Client'")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
