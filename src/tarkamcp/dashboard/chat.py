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
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable

from .conversations import Message, ToolCall


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
    mcp_url: str  # public URL for the MCP endpoint


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
            import httpx  # type: ignore
            from google.genai import types  # type: ignore
            from mcp.client.session import ClientSession  # type: ignore
            from mcp.client.streamable_http import streamable_http_client  # type: ignore
        except ImportError as e:  # noqa: BLE001
            yield ErrorEvent(code="sdk_missing", message=str(e))
            return

        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {turn.bearer}"}
        tool_starts: dict[str, float] = {}
        seen_tool_ids: set[str] = set()

        try:
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
                            thinking_config=self._build_thinking_config(
                                turn.model, turn.effort,
                            ),
                            tools=[session],  # type: ignore[list-item]
                        )
                        contents = self._build_contents(turn.history, turn.user_text)

                        stream = await client.aio.models.generate_content_stream(
                            model=turn.model, contents=contents, config=config,
                        )
                        async for chunk in stream:
                            for event in _emit_chunk(chunk, tool_starts, seen_tool_ids):
                                yield event
        except Exception as e:  # noqa: BLE001
            yield ErrorEvent(code="gemini_error", message=str(e))

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


def _short_preview(payload: Any, *, limit: int = 500) -> str:
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, (dict, list)):
        text = _json.dumps(payload, ensure_ascii=False)[:limit]
        return text
    text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
