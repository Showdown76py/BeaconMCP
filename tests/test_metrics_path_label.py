"""The http_requests path label must come from a closed set (pre-auth safe)."""

from __future__ import annotations

import random
import re
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from test_mcp_http_stack import _build_stack, _client, _headers  # noqa: E402

from beaconmcp.__main__ import _metrics_path_label  # noqa: E402

EXACT = [
    "/", "/mcp", "/health", "/metrics",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/oauth/authorize", "/oauth/authorize/finalize",
    "/oauth/passkey/options", "/oauth/passkey/verify",
    "/oauth/passkey/register/options", "/oauth/passkey/register/verify",
    "/oauth/token", "/oauth/register", "/oauth/logo/generic",
]


def test_random_unknown_paths_collapse_to_other() -> None:
    rng = random.Random(1234)
    labels = set()
    for _ in range(1000):
        seg = "".join(rng.choices(string.ascii_lowercase + string.digits, k=rng.randint(3, 20)))
        labels.add(_metrics_path_label(f"/{seg}/{seg[::-1]}"))
    assert labels == {"other"}


@pytest.mark.parametrize("path", EXACT)
def test_exact_routes_keep_their_label(path: str) -> None:
    assert _metrics_path_label(path) == path


@pytest.mark.parametrize(("path", "label"), [
    ("/mcp/c/SECRETSLUG", "/mcp/c"),
    ("/mcp/c/SECRETSLUG/sub", "/mcp/c"),
    ("/.well-known/oauth-protected-resource/mcp/c/SECRETSLUG",
     "/.well-known/oauth-protected-resource/mcp/c"),
    ("/.well-known/oauth-authorization-server/as/SECRETSLUG",
     "/.well-known/oauth-authorization-server/as"),
    ("/oauth/register/c/SECRETSLUG", "/oauth/register/c"),
    ("/oauth/logo/si/github", "/oauth/logo"),
    ("/oauth/logo/cached/abc.png", "/oauth/logo"),
    ("/transfers/upload/TOKEN", "/transfers/upload"),
    ("/app/api/conversations/42", "/app/api/conversations"),
    ("/app/login", "/app"),
    ("/static/x.css", "/static"),
])
def test_prefixed_paths_drop_the_variable_part(path: str, label: str) -> None:
    got = _metrics_path_label(path)
    assert got == label
    assert "SECRETSLUG" not in got


async def test_unknown_paths_without_token_add_at_most_one_series(tmp_path, monkeypatch) -> None:
    stack = _build_stack(tmp_path, monkeypatch)
    async with _client(stack["app"]) as c:
        for i in range(50):
            await c.get(f"/nope-{i}-xyz", headers=_headers(None))
        text = (await c.get("/metrics", headers=_headers(None))).text
    paths = set(re.findall(
        r'^beaconmcp_http_requests_total\{[^}]*path="([^"]*)"', text, re.M,
    ))
    assert not any("nope" in p for p in paths), paths
    assert "other" in paths


def test_upload_prefix_matches_transfers_constant() -> None:
    from beaconmcp import transfers

    assert _metrics_path_label(transfers.UPLOAD_PATH_PREFIX + "tok") == "/transfers/upload"
