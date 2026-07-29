"""MCP client plumbing shared by the chat engine and the MCP Apps host.

The dashboard wears two hats. It is an MCP *client*: every turn opens a
``ClientSession`` against ``/mcp`` with the operator's bearer and runs the
tool loop. It is also an MCP Apps *host*: when a tool it just called points
at a ``ui://`` resource, it fetches that document and renders it in a
sandboxed iframe, then speaks the postMessage dialect to it.

Both hats need the same three things, which is what lives here: a session
that declares the Apps extension, a way to read a ``ui://`` document, and
the ``_meta.ui.resourceUri`` lookup that ties a tool to its panel.

**On the mcp 1.x pin.** A client announces Apps support through
``ClientCapabilities.extensions``, a field that only exists in mcp 2.0 --
which is why this looked blocked behind the ``<2`` pin. It is not:
``ClientCapabilities`` is declared ``extra="allow"``, so the field rides
onto the wire under exactly the name 2.0 will emit, and the server reads
the same JSON either way. The pin costs us the typed attribute, not the
capability.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

# Extension identifier and content type from the MCP Apps spec (SEP-1865).
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"

# postMessage dialect version the host speaks with the iframe. Matches the
# PROTOCOL_VERSION the panels' bridge.js sends in ui/initialize.
UI_PROTOCOL_VERSION = "2026-01-26"

_UI_CAPABILITY: dict[str, Any] = {"mimeTypes": [APP_MIME_TYPE]}


class AppsClientSession(ClientSession):
    """A ``ClientSession`` that declares the MCP Apps extension.

    ``ClientSession.initialize()`` builds its own ``ClientCapabilities`` and
    takes no hook for extra fields, so rather than reimplement it -- version
    negotiation, the ``notifications/initialized`` follow-up, the capability
    bookkeeping -- we tag the outgoing ``InitializeRequest`` on its way past.
    Everything else stays the SDK's business.
    """

    async def send_request(self, request, *args, **kwargs):  # type: ignore[override]
        root = getattr(request, "root", None)
        if isinstance(root, types.InitializeRequest):
            # extra="allow" on ClientCapabilities: the attribute lands in
            # __pydantic_extra__ and serialises as a real field.
            setattr(
                root.params.capabilities,
                "extensions",
                {UI_EXTENSION_ID: dict(_UI_CAPABILITY)},
            )
        return await super().send_request(request, *args, **kwargs)


@asynccontextmanager
async def open_session(
    mcp_url: str,
    bearer: str,
    *,
    timeout: int = 30,
    sse_read_timeout: int = 300,
) -> AsyncIterator[ClientSession]:
    """Open an initialized Apps-aware session against ``mcp_url``.

    ``streamablehttp_client`` (no underscore) is the variant that threads
    ``headers`` through every request; the other one drops the bearer and
    the handshake 401s on loopback. Same reason as in ``chat.py``.
    """
    async with streamablehttp_client(
        mcp_url,
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
    ) as (read_stream, write_stream, _session_id):
        async with AppsClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def ui_resource_uri(meta: Any) -> str | None:
    """Return the ``ui://`` panel a ``_meta`` block points at, if any.

    Accepts the ``_meta`` of a tool declaration or of a ``CallToolResult``;
    the spec puts the pointer in the same place on both, and a result-level
    one wins when present.
    """
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if not isinstance(ui, dict):
        return None
    uri = ui.get("resourceUri")
    if isinstance(uri, str) and uri.startswith("ui://"):
        return uri
    return None


def ui_resource_uris_by_tool(tools: Any) -> dict[str, str]:
    """Map tool name -> panel URI over a ``list_tools()`` result."""
    out: dict[str, str] = {}
    for tool in tools or []:
        name = getattr(tool, "name", "")
        uri = ui_resource_uri(getattr(tool, "meta", None))
        if name and uri:
            out[name] = uri
    return out


def call_result_to_wire(result: Any) -> dict[str, Any]:
    """Flatten a ``CallToolResult`` into the JSON the iframe expects.

    ``ui/notifications/tool-result`` carries a standard ``CallToolResult``,
    and the panels' ``unwrap()`` reads ``structuredContent`` first and falls
    back to parsing ``content[0].text``. We keep both so a panel renders
    whichever the tool happened to produce.
    """
    content: list[dict[str, Any]] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            content.append({"type": "text", "text": text})
            continue
        mime = getattr(item, "mimeType", None)
        content.append({
            "type": getattr(item, "type", "resource") or "resource",
            "mimeType": mime or "application/octet-stream",
        })
    wire: dict[str, Any] = {
        "content": content,
        "isError": bool(getattr(result, "isError", False)),
    }
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        wire["structuredContent"] = structured
    return wire


class UiResourceError(Exception):
    """A ``ui://`` read failed, or came back as something we won't frame."""


async def read_ui_resource(session: ClientSession, uri: str) -> str:
    """Read a ``ui://`` document, refusing anything that is not an app.

    The MIME check is the gate on the panel route: it is what stops the
    endpoint from being used as a general resource proxy that renders, say,
    ``beaconmcp://infrastructure`` as HTML inside the page.
    """
    if not uri.startswith("ui://"):
        raise UiResourceError("not a ui:// resource")
    try:
        result = await session.read_resource(AnyUrl(uri))
    except Exception as exc:  # noqa: BLE001
        raise UiResourceError(str(exc)) from exc

    for item in getattr(result, "contents", None) or []:
        text = getattr(item, "text", None)
        if text is None:
            continue
        mime = (getattr(item, "mimeType", None) or "").replace(" ", "")
        if mime != APP_MIME_TYPE:
            raise UiResourceError(f"unexpected mime type {mime or '(none)'}")
        return text
    raise UiResourceError("resource has no text content")


# ---------------------------------------------------------------------------
# Document cache
# ---------------------------------------------------------------------------

# Panel documents are static assets read off the server's disk, so a page
# with four panels on it should not mean four round-trips through the MCP
# handshake. Keyed by (mcp_url, uri) because one dashboard build can point
# at different servers across restarts; short TTL so a `beaconmcp update`
# that ships a new panel is picked up without a dashboard restart.
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 32
_cache: dict[tuple[str, str], tuple[float, str]] = {}


def cache_get(mcp_url: str, uri: str) -> str | None:
    entry = _cache.get((mcp_url, uri))
    if entry is None:
        return None
    stored_at, html = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        _cache.pop((mcp_url, uri), None)
        return None
    return html


def cache_put(mcp_url: str, uri: str, html: str) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[(mcp_url, uri)] = (time.monotonic(), html)


def cache_clear() -> None:
    _cache.clear()
