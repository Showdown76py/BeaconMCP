"""Security-related MCP tools (session termination, etc.)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..auth import revoke_current_token


def register_security_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def security_end_session() -> dict:
        """Invalidate the current bearer token.

        Call this as the very last step of a task when the caller has
        finished using the server. After this returns, every subsequent
        request made with the same bearer token will fail with 401, forcing
        a fresh OAuth + 2FA round-trip. Use this to shrink the window during
        which a stolen token could be replayed.

        Do not call this in the middle of a multi-step task; the next tool
        call would be rejected.

        Returns ``{"revoked": true}`` on success, or
        ``{"revoked": false, "reason": "..."}`` if no active token was found
        (e.g. called outside an HTTP request context).
        """
        if revoke_current_token():
            return {
                "revoked": True,
                "grace_seconds": 8,
                "message": (
                    "Bearer token scheduled for revocation. It remains valid "
                    "for ~8s so this response can reach the client; after "
                    "that the next request will need a fresh OAuth + 2FA."
                ),
            }
        return {
            "revoked": False,
            "reason": "no active bearer token in request context",
        }
