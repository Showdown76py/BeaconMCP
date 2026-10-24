"""Who is on the other end of an OAuth/MCP connection, for display only.

Every signal here is self-declared by the client (clientInfo, redirect URI,
registered name), so nothing in this module may feed a security decision.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


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

# client names seen in the wild, normalised (lowercase, alnum only)
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


def identify(*, redirect_uri: str = "", registered_name: str = "") -> ClientIdentity:
    key = None
    if redirect_uri:
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
    return ClientIdentity(None, registered_name or "Unknown client")


# --- logos -------------------------------------------------------------------

_ASSETS = Path(__file__).parent / "assets" / "client_logos"
_SI_DIR = _ASSETS / "simpleicons"

# known clients: bundled Simple Icons slug, or a site whose favicon is fetched
_KNOWN_LOGOS: dict[str, tuple[str, str]] = {
    "claude-code": ("si", "claude"),
    "claude": ("si", "claude"),
    "codex": ("site", "https://chatgpt.com"),
    "chatgpt": ("site", "https://chatgpt.com"),
}

# names that don't normalise straight to their Simple Icons slug
_SI_ALIASES: dict[str, str] = {
    "copilot": "githubcopilot",
    "githubcopilotchat": "githubcopilot",
    "vscodecopilot": "githubcopilot",
    "gemini": "googlegemini",
    "geminicli": "googlegemini",
    "zed": "zedindustries",
    "mistral": "mistralai",
    "lechat": "mistralai",
    "windsurfeditor": "windsurf",
    "huggingchat": "huggingface",
}

_MAX_LOGO_BYTES = 256 * 1024
_CACHE_TTL = 7 * 86400
_NEGATIVE_TTL = 3600
# anyone with a bearer can make the server cache icons: keep it bounded
_MAX_CACHED_LOGOS = 200
_MAX_FAILED = 1000
_RASTER_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "ico": "image/x-icon",
}


def _load_si_meta() -> dict[str, dict]:
    try:
        return json.loads((_SI_DIR / "_meta.json").read_text())
    except (OSError, ValueError):
        return {}


_SI_META = _load_si_meta()


def simple_icons_slug(name: str) -> str | None:
    n = _norm(name)
    if not n:
        return None
    # "Foo connector", "Claude Code (laptop)" -> try the bare product name too
    stripped = re.sub(r"\(.*?\)", "", name)
    candidates = [n, _norm(stripped), _norm(re.sub(r"\bconnector\b", "", stripped, flags=re.I))]
    for c in candidates:
        slug = _SI_ALIASES.get(c, c)
        if slug and slug in _SI_META and (_SI_DIR / f"{slug}.svg").is_file():
            return slug
    return None


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    return None


class UnsafeLogoURL(ValueError):
    pass


async def _public_ip(host: str, port: int) -> str:
    """Resolve ``host`` once and return an address to connect to (SSRF).

    Every address must be public. The caller connects to the returned IP
    itself, so a second lookup can't rebind the name to an internal host.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeLogoURL(f"cannot resolve {host}") from exc
    if not infos:
        raise UnsafeLogoURL(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeLogoURL(f"{host} resolves to a non-public address")
    return str(infos[0][4][0])


async def safe_fetch(url: str, *, transport: Any = None) -> tuple[bytes, str]:
    """Fetch a remote raster logo. Returns ``(bytes, ext)``.

    HTTPS only, public hosts only, same-host redirects only, size-capped,
    and the bytes must sniff as PNG/JPEG/WebP/ICO (never SVG).
    """
    import httpx

    for _ in range(3):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise UnsafeLogoURL("logo URL must be https")
        host, port = parsed.hostname, parsed.port or 443
        ip = await _public_ip(host, port)
        # connect to the checked ip, keep the name for Host, SNI and the cert
        pinned = parsed._replace(netloc=f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}")
        headers = {
            "Accept": "image/*",
            "Host": host if parsed.port is None else f"{host}:{port}",
        }
        async with httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, transport=transport,
        ) as client:
            async with client.stream(
                "GET", urlunparse(pinned), headers=headers,
                extensions={"sni_hostname": host},
            ) as res:
                if res.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(url, res.headers.get("location", ""))
                    if urlparse(target).hostname != parsed.hostname:
                        raise UnsafeLogoURL("cross-host redirect")
                    url = target
                    continue
                if res.status_code != 200:
                    raise UnsafeLogoURL(f"HTTP {res.status_code}")
                data = b""
                async for chunk in res.aiter_bytes():
                    data += chunk
                    if len(data) > _MAX_LOGO_BYTES:
                        raise UnsafeLogoURL("logo too large")
        ext = _sniff(data)
        if ext is None:
            raise UnsafeLogoURL("not a PNG/JPEG/WebP/ICO image")
        return data, ext
    raise UnsafeLogoURL("too many redirects")


@functools.lru_cache(maxsize=64)
def _render_si(slug: str) -> str | None:
    """Bundled Simple Icons SVG tinted with its brand colour, None if unknown."""
    meta = _SI_META.get(slug)
    path = _SI_DIR / f"{slug}.svg"
    if meta is None or not re.fullmatch(r"[a-z0-9]+", slug) or not path.is_file():
        return None
    hexcolor = meta.get("hex", "888888")
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) < 60
    # near-black brand marks would vanish on the dark card
    style = f"path{{fill:#{hexcolor}}}" + (
        "@media (prefers-color-scheme:dark){path{fill:#ececec}}" if dark else ""
    )
    return path.read_text().replace("<title>", f"<style>{style}</style><title>", 1)


class LogoResolver:
    """Pick a logo URL for a client without ever blocking on the network.

    Order: known client, DCR ``logo_uri``, bundled
    Simple Icons by name, favicon of the client's site, generic. Remote
    candidates are fetched in the background and served from a disk cache
    so the browser never talks to a third party.
    """

    def __init__(
        self, cache_dir: Path | None,
        fetch: Callable[[str], Awaitable[tuple[bytes, str]]] = safe_fetch,
    ) -> None:
        self._cache_dir = cache_dir
        self._fetch = fetch
        self._failed: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _cached(self, url: str) -> str | None:
        if self._cache_dir is None:
            return None
        key = self._key(url)
        for ext in _RASTER_TYPES:
            path = self._cache_dir / f"{key}.{ext}"
            try:
                if time.time() - path.stat().st_mtime < _CACHE_TTL:
                    return f"/oauth/logo/cached/{key}.{ext}"
            except OSError:
                continue
        return None

    def _store(self, url: str, data: bytes, ext: str) -> str | None:
        if self._cache_dir is None:
            return None
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        aged: list[tuple[float, Path]] = []
        for path in self._cache_dir.iterdir():
            try:
                aged.append((path.stat().st_mtime, path))
            except FileNotFoundError:  # another fill pruned it meanwhile
                continue
        aged.sort()
        for _, stale in aged[: max(0, len(aged) - _MAX_CACHED_LOGOS + 1)]:
            stale.unlink(missing_ok=True)
        key = self._key(url)
        (self._cache_dir / f"{key}.{ext}").write_bytes(data)
        return f"/oauth/logo/cached/{key}.{ext}"

    def _remote(self, url: str) -> str | None:
        """Cached URL for ``url`` now, or None after scheduling a fetch."""
        if not url.startswith("https://") or self._cache_dir is None:
            return None
        cached = self._cached(url)
        if cached:
            return cached
        if time.time() - self._failed.get(url, 0) < _NEGATIVE_TTL or url in self._inflight:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._inflight.add(url)
        task = loop.create_task(self._fill(url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def _fill(self, url: str) -> None:
        try:
            data, ext = await self._fetch(url)
            self._store(url, data, ext)
        except Exception:  # noqa: BLE001 -- any failure just means "no logo"
            if len(self._failed) >= _MAX_FAILED:
                self._failed.clear()
            self._failed[url] = time.time()
        finally:
            self._inflight.discard(url)

    async def drain(self) -> None:
        """Wait for background fetches (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def url_for(
        self, identity: ClientIdentity, *, logo_uri: str = "", website_url: str = "",
    ) -> str:
        sites: list[str] = []
        if identity.key in _KNOWN_LOGOS:
            kind, value = _KNOWN_LOGOS[identity.key]
            if kind == "si":
                return f"/oauth/logo/si/{value}"
            sites.append(value)
        if logo_uri:
            got = self._remote(logo_uri)
            if got:
                return got
        slug = simple_icons_slug(identity.name)
        if slug:
            return f"/oauth/logo/si/{slug}"
        if website_url:
            sites.append(website_url)
        for site in sites:
            parsed = urlparse(site)
            if parsed.scheme == "https" and parsed.hostname:
                got = self._remote(f"https://{parsed.hostname}/favicon.ico")
                if got:
                    return got
        return "/oauth/logo/generic"

    def routes(self) -> list[Route]:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        }

        async def si(request: Request) -> Response:
            svg = _render_si(request.path_params["slug"])
            if svg is None:
                return Response(status_code=404)
            return Response(svg, media_type="image/svg+xml", headers=headers)

        async def generic(_request: Request) -> Response:
            return Response(
                (_ASSETS / "generic.svg").read_text(), media_type="image/svg+xml",
                headers=headers,
            )

        async def cached(request: Request) -> Response:
            name = request.path_params["name"]
            m = re.fullmatch(r"([0-9a-f]{32})\.(png|jpg|webp|ico)", name)
            if not m or self._cache_dir is None:
                return Response(status_code=404)
            path = self._cache_dir / name
            if not path.is_file():
                return Response(status_code=404)
            return Response(
                path.read_bytes(), media_type=_RASTER_TYPES[m.group(2)], headers=headers,
            )

        return [
            Route("/oauth/logo/si/{slug}", si),
            Route("/oauth/logo/generic", generic),
            Route("/oauth/logo/cached/{name}", cached),
        ]
