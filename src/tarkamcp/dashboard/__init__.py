"""TarkaMCP web dashboard: login + tokens + (optional) Gemini chat.

The dashboard is mounted under ``/app/*`` on the same Starlette app as the
MCP endpoint. It is always-on unless explicitly disabled via
``TARKAMCP_DASHBOARD_ENABLED=false`` — the Tokens API page stays useful
for users who only want to wire external MCP clients (Gemini web,
ChatGPT, Claude Desktop). The integrated chat panel is gated by
``GEMINI_API_KEY`` on top of that (see :func:`has_chat`).
"""

from __future__ import annotations

import os


def is_enabled() -> bool:
    """Return True if the dashboard should be mounted at all.

    Controlled by ``TARKAMCP_DASHBOARD_ENABLED`` (default on). The Gemini
    key is NOT required here — without it, only ``/app/login`` and
    ``/app/tokens`` are meaningful, and ``/app/chat`` redirects to
    tokens.
    """
    flag = os.environ.get("TARKAMCP_DASHBOARD_ENABLED", "true").strip().lower()
    return flag not in ("0", "false", "no", "off")


def has_chat() -> bool:
    """Return True if the integrated Gemini chat panel should be served.

    Requires the dashboard to be enabled AND ``GEMINI_API_KEY`` set.
    """
    return is_enabled() and bool(os.environ.get("GEMINI_API_KEY"))
