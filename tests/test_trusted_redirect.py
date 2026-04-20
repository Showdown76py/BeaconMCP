"""Tests for :func:`beaconmcp.auth.is_trusted_redirect_uri`.

HTTPS redirect trust is sourced from ``server.allowed_origins`` so operator
config drives both CORS and OAuth callback validation. Desktop/CLI callback
forms (custom URI schemes + loopback) are intentionally built-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import is_trusted_redirect_uri


# --- Accepted redirects ------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        # Custom OS URI schemes
        "vscode://ms-vscode.remote/callback",
        "vscode-insiders://ms-vscode.remote/callback",
        "cursor://mcp/callback",
        # Loopback (Codex, Gemini CLI, Mistral Vibe, OpenCode, mcp-remote)
        "http://localhost:54321/callback",
        "http://localhost/callback",
        "http://127.0.0.1:3000/oauth/cb",
        "http://127.0.0.1/cb",
        "http://[::1]:8080/cb",
    ],
)
def test_builtin_non_origin_redirects_accepted(uri: str) -> None:
    assert is_trusted_redirect_uri(uri), f"expected trusted: {uri}"


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://assistant.ai/api/organizations/xyz/mcp/callback",
        "https://chatgpt.com/connector_platform_oauth/callback",
        "https://chat.openai.com/oauth/callback",
        "https://platform.openai.com/oauth/callback",
        "https://gemini.google.com/oauth/cb",
        "https://chat.mistral.ai/connectors/oauth/callback",
        "https://vscode.dev/oauth/cb",
        "https://github.dev/oauth/cb",
        "https://cursor.com/mcp/oauth/callback",
    ],
)
def test_https_redirects_follow_allowed_origins(uri: str) -> None:
    allowed_origins = [
        "https://claude.ai",
        "https://assistant.ai",
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://platform.openai.com",
        "https://gemini.google.com",
        "https://chat.mistral.ai",
        "https://vscode.dev",
        "https://github.dev",
        "https://cursor.com",
    ]
    assert is_trusted_redirect_uri(uri, allowed_origins), f"expected trusted: {uri}"


# --- Rejected redirects ------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        # Obvious attacker-controlled domains
        "https://evil.example.com/cb",
        "https://attacker.xyz/callback",
        # Typo-squats of real origins
        "https://assistant-ai.com/cb",  # hyphenated fake
        "https://chat.mistral.ai.evil.com/cb",  # subdomain confusion
        "https://chatgpt.co/cb",  # TLD typo
        # Valid-looking but not-whitelisted domains
        "https://mail.google.com/oauth/cb",
        "https://accounts.google.com/oauth/cb",
        "https://claude.ai.evil.com/callback",
        # Non-HTTP(S) schemes we don't trust
        "ftp://assistant.ai/cb",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
        # Loopback over HTTPS (not loopback-OAuth per RFC 8252) — we
        # explicitly match `http://` loopback only. Plain-HTTPS
        # loopback should still pass via the generic https rules if
        # ever needed; right now it isn't listed.
        "https://localhost:8080/cb",
        # Empty / whitespace / None-like
        "",
        "   ",
        # Prefix-match evasion: a crafted URL that *starts* with a
        # trusted origin's scheme + host but really lands elsewhere.
        "https://assistant.ai.evil.com/cb",
    ],
)
def test_untrusted_redirects_rejected(uri: str) -> None:
    allowed_origins = [
        "https://assistant.ai",
        "https://chatgpt.com",
        "https://chat.mistral.ai",
        "https://gemini.google.com",
    ]
    assert not is_trusted_redirect_uri(uri, allowed_origins), f"should have been rejected: {uri!r}"


def test_allowed_origins_normalizes_trailing_slash() -> None:
    assert is_trusted_redirect_uri(
        "https://claude.ai/api/mcp/auth_callback",
        ["https://claude.ai"],
    )
    assert is_trusted_redirect_uri(
        "https://claude.ai/api/mcp/auth_callback",
        ["https://claude.ai/"],
    )


def test_none_and_non_string_rejected() -> None:
    assert is_trusted_redirect_uri(None) is False  # type: ignore[arg-type]
    assert is_trusted_redirect_uri(123) is False  # type: ignore[arg-type]
    assert is_trusted_redirect_uri([]) is False  # type: ignore[arg-type]
