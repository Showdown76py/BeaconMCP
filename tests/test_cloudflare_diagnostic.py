"""Cloudflare 401 diagnostic tests.

When an MCP request reaches BeaconMCP without a usable ``Authorization``
header but *with* Cloudflare's ``cf-ray`` edge header, the 401 body gains an
actionable ``hint`` (and the server logs a throttled warning) because a
Cloudflare WAF / Access / Bot-Fight-Mode rule is the overwhelmingly likely
cause. See ``docs/cloudflare.md``.

The 401 status and ``WWW-Authenticate`` header are the caller's job and are
covered where ``auth_middleware`` is exercised; these tests own the body and
the logging contract.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp.__main__ import _CF_EDGE_HEADER, _build_unauthorized_body, _cf_log_state

_HINT_MARKER = "docs/cloudflare.md"


@pytest.fixture(autouse=True)
def reset_log_throttle():
    """The Cloudflare warning is rate-limited via module state; start clean so
    tests don't suppress each other's log lines."""
    _cf_log_state.update(last=0.0, suppressed=0)
    yield
    _cf_log_state.update(last=0.0, suppressed=0)


@pytest.mark.parametrize("error", ["unauthorized", "invalid_token"])
def test_cf_ray_adds_hint_and_logs(error: str, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="beaconmcp"):
        body = _build_unauthorized_body(
            {_CF_EDGE_HEADER: "7d9f0c2a1b3e4f56-AMS"}, error=error
        )

    assert body["error"] == error
    assert _HINT_MARKER in body["hint"]
    assert any("Cloudflare" in r.message for r in caplog.records)


def test_no_cf_ray_keeps_body_minimal_and_quiet(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="beaconmcp"):
        body = _build_unauthorized_body({}, error="unauthorized")

    assert body == {"error": "unauthorized"}
    assert not any("Cloudflare" in r.message for r in caplog.records)


def test_repeated_unauthorized_hits_do_not_flood_the_log(caplog) -> None:
    """A public /mcp is scanned continuously and every hit carries a cf-ray.
    Each one must still get its hint, but the journal must not grow by a line
    per anonymous request.
    """
    headers = {_CF_EDGE_HEADER: "abc-AMS"}
    with caplog.at_level(logging.WARNING, logger="beaconmcp"):
        bodies = [
            _build_unauthorized_body(headers, error="unauthorized")
            for _ in range(200)
        ]

    assert all(_HINT_MARKER in b["hint"] for b in bodies)
    cf_records = [r for r in caplog.records if "Cloudflare" in r.message]
    assert len(cf_records) == 1
    assert _cf_log_state["suppressed"] == 199
