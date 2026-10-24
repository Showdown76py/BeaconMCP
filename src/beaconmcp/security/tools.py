"""Security-related MCP tools (session termination, etc.)."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..auth import lapse_current_session, revoke_current_token


def register_security_tools(mcp: MCPServer) -> None:
    @mcp.tool()
    def security_end_session(sign_out: bool = False) -> dict:
        """End the current session when the task is done.

        Call this as the very last step of a task. For an OAuth sign-in
        (Claude Code, claude.ai, ChatGPT...) it ends the human session but
        keeps the connection: the next tool call asks the user to verify
        again with a passkey or a 6-digit code before anything runs. Use this
        to shrink the window during which a stolen token could be replayed.

        Pass ``sign_out=True`` only when the user explicitly asks to sign out
        completely: the bearer is revoked and the client has to go through
        the authorization page again. The user is asked to confirm first
        when the client supports it. Other bearers (API tokens) are always
        revoked.

        Do not call this in the middle of a multi-step task.

        Returns ``{"revoked": false, "reverify_required": true}`` when the
        session was ended, ``{"revoked": true}`` when the bearer was revoked,
        or ``{"revoked": false, "reason": "..."}`` if no active token was
        found (e.g. called outside an HTTP request context).
        """
        if not sign_out and lapse_current_session():
            return {
                "revoked": False,
                "reverify_required": True,
                "message": (
                    "Session ended. The connection stays, but the next tool "
                    "call will ask the user to verify again with a passkey "
                    "or a 6-digit code."
                ),
            }
        if revoke_current_token():
            return {
                "revoked": True,
                "grace_s": 8,
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
