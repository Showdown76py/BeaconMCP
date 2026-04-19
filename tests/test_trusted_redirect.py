"""Tests for :func:`beaconmcp.auth.is_trusted_redirect_uri`.

The allowlist gates every redirect_uri that reaches :class:`/oauth/authorize`
or the DCR endpoint. If this check ever misfires — false-negative
breaking Assistant; false-positive enabling an attacker's callback — the
whole OAuth surface is at risk. Guard it with explicit cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.auth import is_trusted_redirect_uri


# --- Accepted origins --------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        # Assistant
        "https://assistant.ai/api/organizations/xyz/mcp/callback",
        "https://assistant.com/some/path",
        # ChatGPT family (consumer + enterprise + Codex)
        "https://chatgpt.com/connector_platform_oauth/callback",
        "https://chat.openai.com/oauth/callback",
        "https://platform.openai.com/oauth/callback",
        # Gemini CLI / AI Studio / GCP
        "https://gemini.google.com/oauth/cb",
        "https://aistudio.google.com/oauth",
        "https://console.cloud.google.com/mcp",
        # Mistral
        "https://chat.mistral.ai/connectors/oauth/callback",
        "https://console.mistral.ai/oauth",
        # VS Code surfaces
        "https://vscode.dev/oauth/cb",
        "https://github.dev/oauth/cb",
        # Cursor dashboard
        "https://cursor.com/mcp/oauth/callback",
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
def test_trusted_origins_accepted(uri: str) -> None:
    assert is_trusted_redirect_uri(uri), f"expected trusted: {uri}"


# --- Rejected origins --------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        # Obvious attacker-controlled domains
        "https://evil.example.com/cb",
        "https://attacker.xyz/callback",
        # Typo-squats of real origins
        "https://assistant-ai.com/cb",          # hyphenated fake
        "https://chat.mistral.ai.evil.com/cb",  # subdomain confusion
        "https://chatgpt.co/cb",                # TLD typo
        # Valid-looking but not-whitelisted Google domains
        "https://mail.google.com/oauth/cb",
        "https://accounts.google.com/oauth/cb",
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
        # Loopback-like but on a malicious port: it passes the prefix
        # check on purpose (users pick random ports), the rest is on
        # network-layer controls. This case is TRUSTED — we document
        # it to make the trade-off explicit.
    ],
)
def test_untrusted_origins_rejected(uri: str) -> None:
    assert not is_trusted_redirect_uri(uri), f"should have been rejected: {uri!r}"


def test_none_and_non_string_rejected() -> None:
    assert is_trusted_redirect_uri(None) is False  # type: ignore[arg-type]
    assert is_trusted_redirect_uri(123) is False  # type: ignore[arg-type]
    assert is_trusted_redirect_uri([]) is False  # type: ignore[arg-type]
