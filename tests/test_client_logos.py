"""Client logo resolution chain and the /oauth/logo routes."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from beaconmcp.auth import ClientStore
from beaconmcp.client_identity import (
    ClientIdentity,
    LogoResolver,
    UnsafeLogoURL,
    safe_fetch,
    simple_icons_slug,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeFetch:
    def __init__(self, data: bytes = PNG, ext: str = "png", fail: bool = False):
        self.calls: list[str] = []
        self.data, self.ext, self.fail = data, ext, fail

    async def __call__(self, url: str):
        self.calls.append(url)
        if self.fail:
            raise UnsafeLogoURL("nope")
        return self.data, self.ext


def test_known_client_uses_bundled_logo(tmp_path: Path) -> None:
    r = LogoResolver(tmp_path)
    assert r.url_for(ClientIdentity("claude-code", "Claude Code")) == "/oauth/logo/si/claude"


async def test_declared_icon_cached_then_served(tmp_path: Path) -> None:
    fetch = FakeFetch()
    r = LogoResolver(tmp_path, fetch=fetch)
    ident = ClientIdentity(None, "oh-my-pi")
    first = r.url_for(ident, declared_icons=["https://ohmypi.dev/icon.png"])
    assert first == "/oauth/logo/generic"
    await r.drain()
    second = r.url_for(ident, declared_icons=["https://ohmypi.dev/icon.png"])
    assert second.startswith("/oauth/logo/cached/") and second.endswith(".png")
    assert fetch.calls == ["https://ohmypi.dev/icon.png"]

    client = TestClient(Starlette(routes=r.routes()))
    res = client.get(second)
    assert res.status_code == 200 and res.content == PNG
    assert res.headers["content-type"] == "image/png"
    assert res.headers["x-content-type-options"] == "nosniff"


async def test_uncached_declared_icon_falls_through_without_blocking(tmp_path: Path) -> None:
    r = LogoResolver(tmp_path, fetch=FakeFetch())
    got = r.url_for(ClientIdentity(None, "GitHub Copilot"), declared_icons=["https://x.dev/i.png"])
    assert got == "/oauth/logo/si/githubcopilot"
    await r.drain()


async def test_failed_fetch_negative_cached(tmp_path: Path) -> None:
    fetch = FakeFetch(fail=True)
    r = LogoResolver(tmp_path, fetch=fetch)
    ident = ClientIdentity(None, "x")
    r.url_for(ident, logo_uri="https://x.dev/i.png")
    await r.drain()
    r.url_for(ident, logo_uri="https://x.dev/i.png")
    await r.drain()
    assert len(fetch.calls) == 1


def test_data_uri_icon_used_directly(tmp_path: Path) -> None:
    r = LogoResolver(tmp_path, fetch=FakeFetch())
    uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
    got = r.url_for(ClientIdentity(None, "x"), declared_icons=[uri])
    assert got.startswith("/oauth/logo/cached/")


def test_data_uri_svg_refused(tmp_path: Path) -> None:
    r = LogoResolver(tmp_path, fetch=FakeFetch())
    uri = "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=x>").decode()
    assert r.url_for(ClientIdentity(None, "x"), declared_icons=[uri]) == "/oauth/logo/generic"


def test_simple_icons_slug_normalisation() -> None:
    assert simple_icons_slug("GitHub Copilot") == "githubcopilot"
    assert simple_icons_slug("Copilot") == "githubcopilot"
    assert simple_icons_slug("Gemini CLI") == "googlegemini"
    assert simple_icons_slug("Cursor (work laptop)") == "cursor"
    assert simple_icons_slug("Zed connector") == "zedindustries"
    assert simple_icons_slug("oh-my-pi") is None
    assert simple_icons_slug("") is None


async def test_favicon_from_website_url(tmp_path: Path) -> None:
    fetch = FakeFetch(data=b"\x00\x00\x01\x00" + b"\x00" * 20, ext="ico")
    r = LogoResolver(tmp_path, fetch=fetch)
    ident = ClientIdentity(None, "oh-my-pi")
    r.url_for(ident, website_url="https://ohmypi.dev/docs")
    await r.drain()
    assert fetch.calls == ["https://ohmypi.dev/favicon.ico"]
    assert r.url_for(ident, website_url="https://ohmypi.dev").endswith(".ico")


async def test_chatgpt_uses_site_favicon(tmp_path: Path) -> None:
    fetch = FakeFetch()
    r = LogoResolver(tmp_path, fetch=fetch)
    r.url_for(ClientIdentity("chatgpt", "ChatGPT"))
    await r.drain()
    assert fetch.calls == ["https://chatgpt.com/favicon.ico"]


def test_generic_fallback(tmp_path: Path) -> None:
    r = LogoResolver(tmp_path)
    assert r.url_for(ClientIdentity(None, "oh-my-pi")) == "/oauth/logo/generic"
    assert r.url_for(ClientIdentity(None, "x"), website_url="http://insecure.dev") == "/oauth/logo/generic"


def test_si_route_colors_and_rejects_unknown(tmp_path: Path) -> None:
    client = TestClient(Starlette(routes=LogoResolver(tmp_path).routes()))
    res = client.get("/oauth/logo/si/claude")
    assert res.status_code == 200 and res.headers["content-type"].startswith("image/svg+xml")
    assert "fill:#D97757" in res.text
    dark = client.get("/oauth/logo/si/githubcopilot").text
    assert "prefers-color-scheme:dark" in dark
    assert client.get("/oauth/logo/si/nope").status_code == 404
    assert client.get("/oauth/logo/si/..%2Fgeneric").status_code == 404
    assert client.get("/oauth/logo/cached/../../etc").status_code == 404
    assert client.get("/oauth/logo/generic").status_code == 200


@pytest.mark.parametrize("url", [
    "http://example.com/i.png",
    "https://127.0.0.1/i.png",
    "https://localhost/i.png",
    "https://10.0.0.1/i.png",
    "https://[::1]/i.png",
    "ftp://example.com/i.png",
])
async def test_safe_fetch_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(UnsafeLogoURL):
        await safe_fetch(url)


def test_dcr_stores_logo_and_client_uri(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "clients.json")
    owner, _, _ = store.create("owner")
    cid, _ = store.create_dynamic(
        owner_client_id=owner, name="x", registration_source="chatgpt:s",
        logo_uri="https://ohmypi.dev/logo.png", client_uri="https://ohmypi.dev",
    )
    reloaded = ClientStore(tmp_path / "clients.json").get(cid)
    assert reloaded.logo_uri == "https://ohmypi.dev/logo.png"
    assert reloaded.client_uri == "https://ohmypi.dev"
