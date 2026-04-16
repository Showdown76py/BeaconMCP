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
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable

from .conversations import Message, ToolCall


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
# Real Gemini engine (wired in Stage 3)
# ---------------------------------------------------------------------------

# Mapping our string effort values to the SDK enum is done lazily inside
# GeminiChatEngine.run() to keep import cost low and to make it easy to
# stub out the SDK in tests.

class GeminiChatEngine:
    """Real-Gemini implementation. Imports google-genai lazily."""

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._client = None  # built on first use

    def _ensure_client(self):
        if self._client is None:
            from google import genai  # type: ignore
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def _to_thinking_level(effort: str):
        from google.genai import types  # type: ignore
        mapping = {
            "minimal": types.ThinkingLevel.MINIMAL,
            "low": types.ThinkingLevel.LOW,
            "medium": types.ThinkingLevel.MEDIUM,
            "high": types.ThinkingLevel.HIGH,
        }
        return mapping.get(effort, types.ThinkingLevel.LOW)

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
            from google.genai import types  # type: ignore
        except ImportError:
            yield ErrorEvent(code="sdk_missing", message="google-genai not installed")
            return

        client = self._ensure_client()
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=self._to_thinking_level(turn.effort),
                include_thoughts=False,
            ),
            tools=[types.Tool(
                mcp_servers=[types.McpServer(
                    name="tarkamcp",
                    streamable_http_transport=types.StreamableHttpTransport(
                        url=turn.mcp_url,
                        headers={"Authorization": f"Bearer {turn.bearer}"},
                    ),
                )],
            )],
        )
        contents = self._build_contents(turn.history, turn.user_text)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue()
        seen_tool_ids: set[str] = set()
        tool_starts: dict[str, float] = {}

        def _producer():
            try:
                stream = client.models.generate_content_stream(
                    model=turn.model, contents=contents, config=config,
                )
                for chunk in stream:
                    self._emit_chunk(chunk, queue, loop, seen_tool_ids, tool_starts)
            except Exception as e:  # noqa: BLE001
                asyncio.run_coroutine_threadsafe(
                    queue.put(ErrorEvent(code="gemini_error", message=str(e))), loop,
                ).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        producer_task = loop.run_in_executor(None, _producer)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            await producer_task

    def _emit_chunk(self, chunk, queue, loop, seen_tool_ids, tool_starts):
        # The SDK shape varies; handle the common fields defensively.
        candidates = getattr(chunk, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    asyncio.run_coroutine_threadsafe(
                        queue.put(TextDelta(text=text)), loop,
                    )
                fc = getattr(part, "function_call", None)
                if fc:
                    fc_id = getattr(fc, "id", None) or f"fc_{len(seen_tool_ids)}"
                    if fc_id not in seen_tool_ids:
                        seen_tool_ids.add(fc_id)
                        tool_starts[fc_id] = time.monotonic()
                        asyncio.run_coroutine_threadsafe(
                            queue.put(ToolCallStart(
                                id=fc_id,
                                name=getattr(fc, "name", "?"),
                                args=dict(getattr(fc, "args", {}) or {}),
                            )), loop,
                        )
                fr = getattr(part, "function_response", None)
                if fr:
                    fr_id = getattr(fr, "id", None) or ""
                    if not fr_id:
                        # Some SDK versions don't echo the id; fall back to
                        # the most recent unresolved one.
                        fr_id = next(iter(reversed(list(tool_starts.keys()))), "")
                    started = tool_starts.pop(fr_id, time.monotonic())
                    duration = int((time.monotonic() - started) * 1000)
                    response = getattr(fr, "response", None) or {}
                    status = "error" if "error" in response else "ok"
                    preview = _short_preview(response)
                    asyncio.run_coroutine_threadsafe(
                        queue.put(ToolCallEnd(
                            id=fr_id, status=status,
                            preview=preview, duration_ms=duration,
                        )), loop,
                    )

    async def title(self, *, model: str, user_text: str) -> str | None:
        try:
            from google.genai import types  # type: ignore
        except ImportError:
            return None

        loop = asyncio.get_running_loop()

        def _call() -> str | None:
            try:
                client = self._ensure_client()
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Content(role="user", parts=[types.Part(text=(
                            "Donne un titre français de 4 mots maximum, sans emoji, "
                            "sans guillemets, sans ponctuation finale, qui résume cette demande:\n\n"
                            f"{user_text}"
                        ))]),
                    ],
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(
                            thinking_level=types.ThinkingLevel.MINIMAL,
                        ),
                    ),
                )
                text = (getattr(resp, "text", "") or "").strip().strip('"').strip()
                return text[:80] or None
            except Exception:  # noqa: BLE001
                return None

        return await loop.run_in_executor(None, _call)


def _short_preview(payload: Any, *, limit: int = 500) -> str:
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, (dict, list)):
        import json as _json
        text = _json.dumps(payload, ensure_ascii=False)[:limit]
        return text
    text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
