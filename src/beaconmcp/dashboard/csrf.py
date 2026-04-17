"""Double-submit CSRF cookie helpers.

Pattern: server sets a JS-readable cookie ``beaconmcp_csrf_token`` and the
client must echo its value via the ``X-CSRF-Token`` header (or the
``csrf_token`` form field for the login POST). Comparison is constant time.
"""

from __future__ import annotations

import hmac
import secrets

from starlette.requests import Request

CSRF_COOKIE = "beaconmcp_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_token(request: Request) -> str | None:
    return request.cookies.get(CSRF_COOKIE)


async def verify(request: Request) -> bool:
    """Return True if the request carries a matching CSRF token.

    Reads the cookie value and compares it against either:

    - The ``X-CSRF-Token`` header (for JSON / fetch requests), or
    - The ``csrf_token`` form field (for plain ``<form>`` POSTs).
    """
    cookie = cookie_token(request)
    if not cookie:
        return False

    submitted = request.headers.get(CSRF_HEADER)
    if not submitted:
        # Form fallback. Must be careful not to consume the body twice.
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            submitted = value if isinstance(value, str) else None

    if not submitted:
        return False
    return hmac.compare_digest(cookie, submitted)
