"""Shared response-shaping helpers for BeaconMCP tools.

These helpers exist so individual tool modules don't each reimplement the two
cross-cutting patterns BeaconMCP relies on to stay token-efficient:

* ``filter_fields`` lets callers trim tool output to the keys they need, cutting
  the payload on the wire without forcing the server to ship a separate tool for
  every projection.
* ``parse_since`` lets any time-windowed tool (``proxmox_get_tasks`` etc.)
  accept either a relative duration (``"15m"``, ``"2h"``) or an absolute
  epoch/ISO timestamp.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any


def filter_fields(data: Any, fields: list[str] | None) -> Any:
    """Return ``data`` trimmed to only the keys listed in ``fields``.

    - ``fields`` None / empty  -> data is returned unchanged.
    - dict                    -> returns a new dict with only the requested keys
                                 (missing keys are skipped silently).
    - list of dicts           -> applies the same filter to every element.
    - everything else         -> returned unchanged (ints, strings, None, ...).

    Design notes:
    * Missing keys are silently dropped rather than raising so callers can share
      one ``fields`` list across tools that return slightly different shapes.
    * Nested dicts/lists are kept as-is; this is a single-level projection on
      purpose so callers keep predictable output shape.
    """
    if not fields:
        return data
    keep = set(fields)
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in keep}
    if isinstance(data, list):
        return [
            {k: v for k, v in item.items() if k in keep}
            if isinstance(item, dict)
            else item
            for item in data
        ]
    return data


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def parse_since(value: Any, now: float | None = None) -> int | None:
    """Parse a ``since`` argument into an epoch-seconds lower bound.

    Accepted forms:
    * None / "" / 0       -> returns None (no lower bound).
    * "<n><unit>"         -> duration relative to ``now``. Units: s/m/h/d.
                             e.g. ``"15m"`` -> now - 900.
    * int or numeric str  -> treated as a unix epoch in seconds.
    * ISO-8601 string     -> parsed via ``datetime.fromisoformat``; naive values
                             are interpreted as UTC.

    Raises ``ValueError`` on anything else, so tools can surface a clean error
    to the caller instead of silently misinterpreting input.
    """
    if value in (None, "", 0):
        return None

    current = now if now is not None else time.time()

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        match = _SINCE_RE.match(value)
        if match:
            n = int(match.group(1))
            unit = match.group(2).lower()
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            return int(current - n * mult)

        # Numeric epoch as a string.
        if value.strip().isdigit():
            return int(value.strip())

        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Unrecognized 'since' value {value!r}. "
                "Expected a duration like '15m'/'2h'/'1d', a unix epoch, or an ISO-8601 timestamp."
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    raise ValueError(
        f"Unsupported 'since' type {type(value).__name__}. "
        "Expected str, int, or float."
    )
