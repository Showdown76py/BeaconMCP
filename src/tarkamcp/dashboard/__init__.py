"""TarkaMCP web dashboard: login + Gemini chat panels.

The dashboard is mounted under ``/app/*`` on the same Starlette app as the
MCP endpoint. It is opt-in: the routes are only registered when
``GEMINI_API_KEY`` is set (see :func:`is_enabled`).
"""

from __future__ import annotations

import os


def is_enabled() -> bool:
    """Return True if the dashboard should be mounted.

    Requires ``GEMINI_API_KEY`` set, and ``TARKAMCP_DASHBOARD_ENABLED`` not
    set to ``false``.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    flag = os.environ.get("TARKAMCP_DASHBOARD_ENABLED", "true").strip().lower()
    return flag not in ("0", "false", "no", "off")
