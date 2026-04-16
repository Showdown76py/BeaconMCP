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
    serve_parser = sub.add_parser("serve", help="Start the MCP server")
    serve_parser.add_argument(
        "--http", action="store_true",
        help="Run as Streamable HTTP server (for Claude mobile, ChatGPT, Gemini)",
    )
    serve_parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TARKAMCP_PORT", "8420")),
        help="HTTP port (default: 8420)",
    )
    serve_parser.add_argument(
        "--host", default=os.environ.get("TARKAMCP_HOST", "0.0.0.0"),
        help="HTTP bind address (default: 0.0.0.0)",
    )

    # --- auth create ---
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

    # Default to 'serve' if no subcommand
    if args.command is None:
        # Backward compat: bare `python -m tarkamcp` = stdio serve
        from .server import mcp
        mcp.run(transport="stdio")
        return

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "auth":
        _cmd_auth(args)


def _cmd_serve(args):
    from .server import mcp

    if args.http:
        _run_http(mcp, args.host, args.port)
    else:
        mcp.run(transport="stdio")


def _cmd_auth(args):
    from .auth import ClientStore

    store = ClientStore(args.clients_file)

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
        print("  - ChatGPT / Gemini  : en-tête Authorization: Bearer <token>")
        print("    (obtenir un token : POST /oauth/token avec les credentials)")
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

    from .auth import ClientStore, TokenStore

    clients_file = os.environ.get("TARKAMCP_CLIENTS_FILE")
    client_store = ClientStore(Path(clients_file) if clients_file else None)
    token_store = TokenStore()

    # --- OAuth endpoints ---

    async def oauth_metadata(request: Request) -> Response:
        """RFC 8414 -- OAuth Authorization Server Metadata."""
        # Build issuer from request
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host_header = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
        issuer = f"{scheme}://{host_header}"

        return JSONResponse({
            "issuer": issuer,
            "token_endpoint": f"{issuer}/oauth/token",
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "response_types_supported": ["token"],
        })

    async def oauth_token(request: Request) -> Response:
        """OAuth 2.1 token endpoint -- client_credentials grant."""
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        grant_type = body.get("grant_type", "")
        client_id = body.get("client_id", "")
        client_secret = body.get("client_secret", "")

        if grant_type != "client_credentials":
            return JSONResponse(
                {"error": "unsupported_grant_type", "error_description": "Only client_credentials is supported"},
                status_code=400,
            )

        if not client_store.verify(client_id, client_secret):
            return JSONResponse(
                {"error": "invalid_client", "error_description": "Invalid client_id or client_secret"},
                status_code=401,
            )

        token, expires_in = token_store.issue(client_id)
        return JSONResponse({
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
        })

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "server": "tarkamcp"})

    # --- Auth middleware ---

    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # Public endpoints -- no auth
        if path in ("/health", "/oauth/token", "/.well-known/oauth-authorization-server"):
            return await call_next(request)

        # Check bearer token
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="tarkamcp"'},
            )

        token = authorization[7:]
        client_id = token_store.validate(token)
        if not client_id:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Token is invalid or expired"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="tarkamcp", error="invalid_token"'},
            )

        return await call_next(request)

    # --- App assembly ---

    mcp_app = mcp.streamable_http_app()

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/.well-known/oauth-authorization-server", oauth_metadata),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_middleware)],
    )

    n_clients = len(client_store.list_clients())
    print(f"TarkaMCP HTTP server starting on {host}:{port}")
    print(f"Registered clients: {n_clients}")
    print(f"MCP endpoint:   http://{host}:{port}/mcp")
    print(f"Token endpoint: http://{host}:{port}/oauth/token")
    print(f"Health check:   http://{host}:{port}/health")
    if n_clients == 0:
        print(f"\nNo clients registered! Run: tarkamcp auth create --name 'My Client'")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
