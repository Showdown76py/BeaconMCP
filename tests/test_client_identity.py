"""Client identification for the auth pages (display only)."""

from __future__ import annotations

from beaconmcp.client_identity import ClientIdentity, identify


def test_identify_by_client_info() -> None:
    assert identify(client_info_name="claude-code") == ClientIdentity("claude-code", "Claude Code")
    assert identify(client_info_name="codex-mcp-client").key == "codex"


def test_client_info_wins_over_registered_name() -> None:
    got = identify(client_info_name="claude-code", registered_name="ChatGPT connector")
    assert got.key == "claude-code"


def test_identify_by_redirect_claude_ai() -> None:
    got = identify(redirect_uri="https://claude.ai/api/mcp/auth_callback")
    assert got == ClientIdentity("claude", "Claude")
    assert identify(redirect_uri="https://chatgpt.com/connector_platform_oauth_redirect").key == "chatgpt"


def test_lookalike_redirect_host_not_matched() -> None:
    got = identify(redirect_uri="https://claude.ai.evil.example/api/mcp/auth_callback",
                   registered_name="Tool")
    assert got == ClientIdentity(None, "Tool")


def test_localhost_redirect_falls_back_to_registered_name() -> None:
    got = identify(redirect_uri="http://localhost:53172/callback",
                   registered_name="Claude Code (laptop)")
    assert got.key == "claude-code"
    other = identify(redirect_uri="http://localhost:1/cb", registered_name="oh-my-pi")
    assert other == ClientIdentity(None, "oh-my-pi")


def test_unknown_client_generic() -> None:
    assert identify() == ClientIdentity(None, "Unknown client")
    assert identify(client_info_name="GitHub Copilot") == ClientIdentity(None, "GitHub Copilot")
