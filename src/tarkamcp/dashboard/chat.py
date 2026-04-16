"""ChatEngine abstraction.

The route calls ``engine.run(turn)`` which yields :class:`ChatEvent`
instances. The route translates them into SSE frames, persists the final
assistant message, and emits the auxiliary ``done`` / ``title_updated``
events itself.

Two implementations:

- :class:`FakeChatEngine` -- yields a scripted sequence, used in tests.
- :class:`GeminiChatEngine` -- the real google-genai SDK backed engine
  with TarkaMCP wired in as an MCP tool provider.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable

from .conversations import Message, ToolCall

_logger = logging.getLogger("tarkamcp.dashboard.chat")


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Drill into nested ExceptionGroups to return the leaf exception.

    anyio and asyncio wrap concurrent errors in ``ExceptionGroup``; the
    default string representation is ``"unhandled errors in a TaskGroup
    (N sub-exception)"`` which hides the actual failure. We walk the
    chain to expose the most specific underlying error.
    """
    current = exc
    for _ in range(8):  # bounded recursion, exception chains are shallow
        inner = getattr(current, "exceptions", None)
        if not inner:
            return current
        # Prefer the first exception that is itself NOT an ExceptionGroup.
        for sub in inner:
            if not getattr(sub, "exceptions", None):
                return sub
        current = inner[0]
    return current


# ---------------------------------------------------------------------------
# Effort level mapping
# ---------------------------------------------------------------------------

def _is_gemini_3(model: str) -> bool:
    return model.startswith("gemini-3")


# Approximate token budgets mapped from the four effort presets. Used for
# Gemini 2.5 which doesn't accept the `thinking_level` enum (that one is
# Gemini 3+). Numbers err on the higher side so "high" actually has room
# to think; Gemini will clamp to the model ceiling if needed.
_BUDGET_BY_EFFORT = {
    "minimal": 0,
    "low": 1024,
    "medium": 4096,
    "high": 16384,
}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    summary: str


@dataclass
class ToolCallStart:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolCallEnd:
    id: str
    status: str  # ok | error
    preview: str
    duration_ms: int


@dataclass
class ErrorEvent:
    code: str
    message: str


ChatEvent = (
    TextDelta | ThinkingDelta | ToolCallStart | ToolCallEnd | ErrorEvent
)


# ---------------------------------------------------------------------------
# Turn input
# ---------------------------------------------------------------------------

@dataclass
class TurnInput:
    history: list[Message]
    user_text: str
    model: str
    effort: str
    bearer: str
    mcp_url: str  # URL (remote mode) or connect URL (local mode)
    # "local": dashboard opens an MCP ClientSession against mcp_url and
    #         passes the session to Gemini as a tool. Works on any key.
    # "remote": passes an McpServer(url, headers) to Gemini and lets
    #           Google's backend call mcp_url directly. Natively supported
    #           on Gemini 3; requires mcp_url to be publicly reachable.
    mcp_mode: str = "local"


# ---------------------------------------------------------------------------
# Engine protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ChatEngine(Protocol):
    async def run(self, turn: TurnInput) -> AsyncIterator[ChatEvent]:  # pragma: no cover
        ...

    async def title(self, *, model: str, user_text: str) -> str | None:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Fake engine (tests + Stage 2 development)
# ---------------------------------------------------------------------------

@dataclass
class FakeScript:
    events: list[ChatEvent] = field(default_factory=list)
    title_text: str | None = None
    delay_ms: int = 0  # per event, useful to exercise streaming behavior


class FakeChatEngine:
    """An engine that replays a hard-coded sequence of events."""

    def __init__(self, script: FakeScript | None = None) -> None:
        self.script = script or FakeScript()
        self.calls: list[TurnInput] = []
        self.title_calls: list[str] = []

    async def run(self, turn: TurnInput) -> AsyncIterator[ChatEvent]:
        self.calls.append(turn)
        for event in self.script.events:
            if self.script.delay_ms:
                await asyncio.sleep(self.script.delay_ms / 1000)
            yield event

    async def title(self, *, model: str, user_text: str) -> str | None:
        self.title_calls.append(user_text)
        return self.script.title_text


def assemble_assistant_message(
    events: Iterable[ChatEvent],
) -> tuple[str, list[ToolCall], str | None]:
    """Reduce a stream of events into ``(content, tool_calls, thinking)``."""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    pending: dict[str, ToolCall] = {}
    finished: list[ToolCall] = []

    for event in events:
        if isinstance(event, TextDelta):
            content_parts.append(event.text)
        elif isinstance(event, ThinkingDelta):
            thinking_parts.append(event.summary)
        elif isinstance(event, ToolCallStart):
            tc = ToolCall(id=event.id, name=event.name, args=event.args, status="pending")
            pending[event.id] = tc
            finished.append(tc)
        elif isinstance(event, ToolCallEnd):
            tc = pending.get(event.id)
            if tc is None:
                # Server emitted an end without a start (shouldn't happen).
                tc = ToolCall(id=event.id, name="?", args={})
                finished.append(tc)
            tc.status = event.status
            tc.preview = event.preview
            tc.duration_ms = event.duration_ms

    return (
        "".join(content_parts),
        finished,
        "\n".join(thinking_parts) if thinking_parts else None,
    )


# ---------------------------------------------------------------------------
# Real Gemini engine
# ---------------------------------------------------------------------------

# The SDK is imported lazily so test suites that only exercise the fake
# engine do not pay the (large) import cost. All google-genai / mcp
# references inside GeminiChatEngine are thus guarded.

class GeminiChatEngine:
    """Gemini-backed engine that orchestrates MCP tool calls in-process.

    We open a local MCP ``ClientSession`` against the TarkaMCP endpoint
    using the user's bearer, and pass that session to google-genai as a
    tool. The SDK auto-discovers the tools, handles function-call /
    function-response bookkeeping, and emits text / function_call /
    function_response parts through the streaming API. This path does
    NOT use the ``McpServer`` remote tool (where Google's backend calls
    our MCP directly) — that feature is preview-gated and returned
    ``PERMISSION_DENIED`` on standard API keys.
    """

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._client = None  # type: ignore[assignment]

    def _ensure_client(self):
        if self._client is None:
            from google import genai  # type: ignore
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def _build_thinking_config(model: str, effort: str):
        from google.genai import types  # type: ignore

        if _is_gemini_3(model):
            mapping = {
                "minimal": types.ThinkingLevel.MINIMAL,
                "low": types.ThinkingLevel.LOW,
                "medium": types.ThinkingLevel.MEDIUM,
                "high": types.ThinkingLevel.HIGH,
            }
            return types.ThinkingConfig(
                thinking_level=mapping.get(effort, types.ThinkingLevel.LOW),
                include_thoughts=False,
            )

        # Gemini 2.5: use thinking_budget instead.
        budget = _BUDGET_BY_EFFORT.get(effort, _BUDGET_BY_EFFORT["low"])
        return types.ThinkingConfig(
            thinking_budget=budget,
            include_thoughts=False,
        )

    @staticmethod
    def _build_contents(history, user_text):
        from google.genai import types  # type: ignore
        contents: list = []
        for msg in history:
            if msg.role == "user" and msg.content:
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=msg.content)],
                ))
            elif msg.role == "assistant" and msg.content:
                contents.append(types.Content(
                    role="model",
                    parts=[types.Part(text=msg.content)],
                ))
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=user_text)],
        ))
        return contents

    async def run(self, turn: TurnInput) -> AsyncIterator[ChatEvent]:
        try:
            async for event in self._run(turn):
                yield event
        except BaseException as e:  # noqa: BLE001
            leaf = _unwrap_exception(e)
            # Write the full traceback to stderr so journalctl has the
            # real cause; the client only gets a short message.
            traceback.print_exception(e, file=sys.stderr)
            _logger.error("gemini chat turn failed: %s", leaf, exc_info=False)
            code, message = _classify_error(leaf, turn.model)
            yield ErrorEvent(code=code, message=message)

    async def _run(self, turn: TurnInput) -> AsyncIterator[ChatEvent]:
        try:
            from google.genai import types  # type: ignore
        except ImportError as e:
            yield ErrorEvent(code="sdk_missing", message=str(e))
            return

        tool_starts: dict[str, float] = {}
        seen_tool_ids: set[str] = set()

        contents = self._build_contents(turn.history, turn.user_text)
        thinking = self._build_thinking_config(turn.model, turn.effort)

        if turn.mcp_mode == "remote":
            # Google's backend calls the MCP endpoint directly. Requires a
            # publicly reachable URL and (on many keys) allowlist access to
            # the mcp_servers feature.
            config = types.GenerateContentConfig(
                thinking_config=thinking,
                tools=[types.Tool(
                    mcp_servers=[types.McpServer(
                        name="tarkamcp",
                        streamable_http_transport=types.StreamableHttpTransport(
                            url=turn.mcp_url,
                            headers={
                                "Authorization": f"Bearer {turn.bearer}",
                            },
                        ),
                    )],
                )],
            )
            client = self._ensure_client()
            stream = await client.aio.models.generate_content_stream(
                model=turn.model, contents=contents, config=config,
            )
            async for chunk in stream:
                for event in _emit_chunk(chunk, tool_starts, seen_tool_ids):
                    yield event
            return

        # Default "local" mode: open an MCP ClientSession from the
        # dashboard process and hand it to the SDK as a tool.
        try:
            import httpx  # type: ignore
            from mcp.client.session import ClientSession  # type: ignore
            from mcp.client.streamable_http import (  # type: ignore
                streamable_http_client,
            )
        except ImportError as e:
            yield ErrorEvent(code="sdk_missing", message=str(e))
            return

        headers = {"Authorization": f"Bearer {turn.bearer}"}
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0, read=120.0),
        ) as http_client:
            async with streamable_http_client(
                turn.mcp_url, http_client=http_client,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    config = types.GenerateContentConfig(
                        thinking_config=thinking,
                        tools=[session],  # type: ignore[list-item]
                    )
                    client = self._ensure_client()
                    stream = await client.aio.models.generate_content_stream(
                        model=turn.model, contents=contents, config=config,
                    )
                    async for chunk in stream:
                        for event in _emit_chunk(
                            chunk, tool_starts, seen_tool_ids,
                        ):
                            yield event

    async def title(self, *, model: str, user_text: str) -> str | None:
        try:
            from google.genai import types  # type: ignore
        except ImportError:
            return None
        client = self._ensure_client()
        try:
            # Force minimal thinking to keep titling fast and cheap.
            resp = await client.aio.models.generate_content(
                model=model,
                contents=[types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        "Donne un titre français de 4 mots maximum, sans emoji, "
                        "sans guillemets, sans ponctuation finale, qui résume cette demande:\n\n"
                        f"{user_text}"
                    ))],
                )],
                config=types.GenerateContentConfig(
                    thinking_config=self._build_thinking_config(model, "minimal"),
                ),
            )
        except Exception:  # noqa: BLE001
            return None
        text = (getattr(resp, "text", "") or "").strip().strip('"').strip()
        return text[:80] or None


def _emit_chunk(
    chunk: Any,
    tool_starts: dict[str, float],
    seen_tool_ids: set[str],
) -> list[ChatEvent]:
    """Translate a google-genai stream chunk into ChatEvents.

    The SDK shape varies across minor versions; everything is looked up
    defensively. When the SDK auto-executes MCP tools via the local
    ClientSession, we still observe ``function_call`` parts for each
    invocation and ``function_response`` parts for each result.
    """
    out: list[ChatEvent] = []
    candidates = getattr(chunk, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                if getattr(part, "thought", False):
                    out.append(ThinkingDelta(summary=text))
                else:
                    out.append(TextDelta(text=text))

            fc = getattr(part, "function_call", None)
            if fc:
                fc_id = getattr(fc, "id", None) or f"fc_{len(seen_tool_ids)}"
                if fc_id not in seen_tool_ids:
                    seen_tool_ids.add(fc_id)
                    tool_starts[fc_id] = time.monotonic()
                    args = getattr(fc, "args", {}) or {}
                    out.append(ToolCallStart(
                        id=fc_id,
                        name=getattr(fc, "name", "?"),
                        args=dict(args),
                    ))

            fr = getattr(part, "function_response", None)
            if fr:
                fr_id = getattr(fr, "id", None) or ""
                if not fr_id:
                    # Some SDK versions omit the id on the response; fall
                    # back to the most recent unresolved invocation.
                    fr_id = next(
                        iter(reversed(list(tool_starts.keys()))), "",
                    )
                started = tool_starts.pop(fr_id, time.monotonic())
                duration = int((time.monotonic() - started) * 1000)
                response = getattr(fr, "response", None) or {}
                status = "error" if (
                    isinstance(response, dict) and "error" in response
                ) else "ok"
                out.append(ToolCallEnd(
                    id=fr_id, status=status,
                    preview=_short_preview(response),
                    duration_ms=duration,
                ))
    return out


def _classify_error(exc: BaseException, model: str) -> tuple[str, str]:
    """Map a leaf exception to a short user-facing (code, message).

    The dashboard UI renders the message verbatim, so it must be concise
    and actionable in French. Full technical detail lands in journalctl
    via the stderr traceback dump.
    """
    name = type(exc).__name__
    msg = str(exc)

    # Google API 403 usually means "this API key cannot access this
    # model or feature" -- preview models require allowlist access.
    if "PERMISSION_DENIED" in msg or "403" in msg and "caller" in msg.lower():
        is_preview = "preview" in model
        if is_preview:
            return (
                "model_access_denied",
                (
                    f"Ta clé Gemini n'a pas accès à {model} (allowlist Google "
                    "requise pour les modèles preview). Bascule sur "
                    "gemini-2.5-flash ou gemini-2.5-pro via le dropdown en bas "
                    "à gauche — ils sont dispos sur toutes les clés AI Studio."
                ),
            )
        return (
            "permission_denied",
            (
                f"Gemini a refusé la requête sur {model} (403 PERMISSION_DENIED). "
                "Vérifie que ta clé API peut utiliser ce modèle."
            ),
        )

    # 404 on the model -> invalid model ID for the API version.
    if "NOT_FOUND" in msg or "404" in msg and "model" in msg.lower():
        return (
            "model_not_found",
            f"Modèle {model} introuvable côté Gemini. Essaie un autre modèle.",
        )

    # 429 rate limit / quota.
    if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        return (
            "rate_limited",
            "Quota Gemini dépassé. Réessaie dans quelques secondes.",
        )

    # Default: include the exception name to help future triage.
    return ("gemini_error", f"{name}: {msg}")


def _short_preview(payload: Any, *, limit: int = 500) -> str:
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, (dict, list)):
        text = _json.dumps(payload, ensure_ascii=False)[:limit]
        return text
    text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
