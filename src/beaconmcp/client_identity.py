"""Who is on the other end of an OAuth/MCP connection, for display only.

Every signal here is self-declared by the client (clientInfo, redirect URI,
registered name), so nothing in this module may feed a security decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ClientIdentity:
    key: str | None  # known-client key, None when unmatched
    name: str


KNOWN_CLIENTS: dict[str, str] = {
    "claude-code": "Claude Code",
    "claude": "Claude",
    "codex": "Codex",
    "chatgpt": "ChatGPT",
}

# clientInfo.name values seen in the wild, normalised (lowercase, alnum only)
_CLIENT_INFO_NAMES: dict[str, str] = {
    "claudecode": "claude-code",
    "claudeai": "claude",
    "claude": "claude",
    "anthropicclaude": "claude",
    "codex": "codex",
    "codexmcpclient": "codex",
    "openaicodex": "codex",
    "chatgpt": "chatgpt",
    "openaimcp": "chatgpt",
}

# (host, path prefix) of hosted callbacks; loopback never identifies anyone
_REDIRECTS: tuple[tuple[str, str, str], ...] = (
    ("claude.ai", "/api/mcp/auth_callback", "claude"),
    ("claude.com", "/api/mcp/auth_callback", "claude"),
    ("chatgpt.com", "/connector_platform_oauth_redirect", "chatgpt"),
    ("chat.openai.com", "/", "chatgpt"),
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _from_name(value: str) -> str | None:
    n = _norm(value)
    if not n:
        return None
    if n in _CLIENT_INFO_NAMES:
        return _CLIENT_INFO_NAMES[n]
    # registered names often carry a suffix: "Claude Code (laptop)", "ChatGPT connector"
    for prefix, key in (("claudecode", "claude-code"), ("chatgpt", "chatgpt"),
                        ("codex", "codex")):
        if n.startswith(prefix):
            return key
    return None


def identify(
    *, client_info_name: str = "", redirect_uri: str = "", registered_name: str = "",
) -> ClientIdentity:
    key = _from_name(client_info_name)
    if key is None and redirect_uri:
        parsed = urlparse(redirect_uri)
        host = (parsed.hostname or "").lower()
        for r_host, r_path, r_key in _REDIRECTS:
            if host == r_host and parsed.path.startswith(r_path):
                key = r_key
                break
    if key is None:
        key = _from_name(registered_name)
    if key is not None:
        return ClientIdentity(key, KNOWN_CLIENTS[key])
    return ClientIdentity(None, registered_name or client_info_name or "Unknown client")
