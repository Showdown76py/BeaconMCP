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
        client_id, client_secret = store.create(args.name)
        print()
        print("  Client créé avec succès !")
        print()
        print(f"  Name:          {args.name}")
        print(f"  Client ID:     {client_id}")
        print(f"  Client Secret: {client_secret}")
        print()
        print("  Utilise ces credentials dans :")
        print("  - Claude web/mobile : champs OAuth Client ID / Client Secret")
        print("  - ChatGPT / Gemini  : POST /oauth/token pour obtenir un bearer token")
        print()
        print("  Le Client Secret ne sera plus affiché. Conserve-le maintenant.")
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
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    from urllib.parse import urlencode, urlparse

    from .auth import ClientStore, CodeStore, TokenStore

    clients_file = os.environ.get("TARKAMCP_CLIENTS_FILE")
    client_store = ClientStore(Path(clients_file) if clients_file else None)
    token_store = TokenStore()
    code_store = CodeStore()

    async def oauth_metadata(request: Request) -> Response:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host_header = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
        issuer = f"{scheme}://{host_header}"
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

    async def oauth_authorize(request: Request) -> Response:
        params = request.query_params
        response_type = params.get("response_type", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        # Pre-redirect validation: until client_id + redirect_uri are trusted,
        # errors must be rendered directly, never redirected (OAuth 2.1 §4.1.2.1).
        if response_type != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if not client_id or not client_store.exists(client_id):
            return JSONResponse({"error": "unauthorized_client"}, status_code=400)
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must be an absolute URL"},
                status_code=400,
            )
        # Only allow http for localhost (dev); everything else must be https.
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must use https"},
                status_code=400,
            )
        if not code_challenge or code_challenge_method != "S256":
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE with S256 is required"},
                status_code=400,
            )

        code = code_store.issue(client_id, redirect_uri, code_challenge, code_challenge_method)

        query = {"code": code}
        if state:
            query["state"] = state
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
        ):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="tarkamcp"'},
            )

        client_id = token_store.validate(authorization[7:])
        if not client_id:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="tarkamcp", error="invalid_token"'},
            )
        return await call_next(request)

    mcp_app = mcp.streamable_http_app()

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/.well-known/oauth-authorization-server", oauth_metadata),
            Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/oauth/register", oauth_register, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_middleware)],
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
