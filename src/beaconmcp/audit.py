"""Structured audit log for BeaconMCP.

Every auth event (login success/failure, token mint, client revoke) and
every MCP tool invocation can be fed through :func:`emit` to produce a
single line of JSON on the audit sink. The sink defaults to the
``beaconmcp.audit`` logger (which inherits the root config wired in
``__main__._configure_logging``) so operators can point it at a
dedicated file with a standard ``logging`` filter, without the rest of
the code caring how bytes land on disk.

Design:

* One line of JSON per event. Keys are stable so the file can be
  grep'd / shipped to Loki / ingested into Elasticsearch without
  schema maintenance on our side.
* Event timestamps use UTC ISO-8601 with microseconds.
* Secrets are *never* in the payload -- callers pass ``client_id`` and
  high-level tool args, and ``_redact`` masks anything that looks like
  a secret (keys matching ``password``, ``secret``, ``token``, ...).
* Fire-and-forget: emitting never raises. If the underlying logger
  explodes the caller keeps going.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

_logger = logging.getLogger("beaconmcp.audit")

# Argument keys whose values are always masked before emission.
_REDACT_KEYS = frozenset({
    "password", "secret", "token", "token_secret", "client_secret",
    "api_key", "authorization", "totp", "bearer",
})


def _redact(value: Any) -> Any:
    """Walk ``value`` replacing obviously-sensitive leaf values with ``***``."""
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def emit(event: str, **fields: Any) -> None:
    """Write one audit event as a JSON line.

    ``event`` is a short dotted identifier (``tool.call``, ``auth.login``,
    ``auth.token.issue``, ...). Any number of additional keyword fields
    can be attached; they're redacted and merged into the JSON record.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "event": event,
    }
    for k, v in fields.items():
        record[k] = _redact(v)
    try:
        _logger.info(json.dumps(record, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001  -- audit must never break a request
        pass
