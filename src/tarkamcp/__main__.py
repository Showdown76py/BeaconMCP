import argparse
import os

from dotenv import load_dotenv

# load_dotenv MUST run before importing server, because server.py
# triggers Config.from_env() at import time
load_dotenv()

from .server import mcp  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="TarkaMCP - Proxmox MCP Server")
    parser.add_argument(
        "--http", action="store_true",
        help="Run as Streamable HTTP server (for Claude mobile, ChatGPT, Gemini)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("TARKAMCP_PORT", "8420")),
        help="HTTP port (default: 8420, or TARKAMCP_PORT env var)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("TARKAMCP_HOST", "0.0.0.0"),
        help="HTTP bind address (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    if args.http:
        # Streamable HTTP mode for remote clients
        _run_http(args.host, args.port)
    else:
        # stdio mode for Claude Code / Gemini CLI
        mcp.run(transport="stdio")


def _run_http(host: str, port: int):
    """Run the MCP server over Streamable HTTP with bearer token auth."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount

    auth_token = os.environ.get("TARKAMCP_AUTH_TOKEN", "")
    if not auth_token:
        import sys
        print(
            "ERROR: TARKAMCP_AUTH_TOKEN is required for HTTP mode.\n"
            "Generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(1)

    # Get the ASGI app from FastMCP
    mcp_app = mcp.streamable_http_app()

    async def auth_middleware(request: Request, call_next):
        # Health check endpoint -- no auth needed
        if request.url.path == "/health":
            return JSONResponse({"status": "ok", "server": "tarkamcp"})

        # Check bearer token
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or authorization[7:] != auth_token:
            return JSONResponse(
                {"error": "Invalid or missing bearer token"},
                status_code=401,
            )
        return await call_next(request)

    # Wrap MCP app with auth
    from starlette.middleware.base import BaseHTTPMiddleware

    app = Starlette(
        routes=[Mount("/", app=mcp_app)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_middleware)],
    )

    print(f"TarkaMCP HTTP server starting on {host}:{port}")
    print(f"MCP endpoint: http://{host}:{port}/mcp")
    print(f"Health check: http://{host}:{port}/health")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
