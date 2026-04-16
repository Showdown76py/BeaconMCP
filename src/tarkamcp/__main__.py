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
        _run_http(args.host, args.port)
    else:
        mcp.run(transport="stdio")


def _run_http(host: str, port: int):
    """Run the MCP server over Streamable HTTP with URL-based secret auth."""
    import sys

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    secret = os.environ.get("TARKAMCP_SECRET", "")
    if not secret:
        print(
            "ERROR: TARKAMCP_SECRET is required for HTTP mode.\n"
            "This secret is embedded in the URL to authenticate requests.\n"
            "Generate one with: openssl rand -hex 32",
            file=sys.stderr,
        )
        sys.exit(1)

    # The MCP app from FastMCP
    mcp_app = mcp.streamable_http_app()

    # Health check
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "server": "tarkamcp"})

    # The MCP endpoint lives under /s/<secret>/
    # Anyone without the secret gets a 404 -- the URL IS the key.
    # Example: https://mcp.example.com/s/a1b2c3d4.../mcp
    app = Starlette(
        routes=[
            Route("/health", health),
            Mount(f"/s/{secret}", app=mcp_app),
        ],
    )

    public_path = f"/s/{secret}/mcp"
    print(f"TarkaMCP HTTP server starting on {host}:{port}")
    print(f"MCP endpoint: http://{host}:{port}{public_path}")
    print(f"Health check: http://{host}:{port}/health")
    print(f"\nClients should use the full URL including the secret path.")
    print(f"Treat this URL like a password -- anyone with it has access.")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
