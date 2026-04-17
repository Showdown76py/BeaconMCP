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

# Gemini 2.5 Pro cannot disable thinking -- valid range is [128, 32768].
# Passing 0 or a value below 128 deterministically triggers 400/500 from
# the backend. Flash and Flash-Lite accept 0 (disables thinking).
_MIN_BUDGET_BY_MODEL = {
    "gemini-2.5-pro": 128,
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

        # Gemini 2.5: use thinking_budget instead. Clamp to the model's
        # minimum (2.5 Pro requires >=128; passing 0 there causes 500s).
        budget = _BUDGET_BY_EFFORT.get(effort, _BUDGET_BY_EFFORT["low"])
        min_budget = _MIN_BUDGET_BY_MODEL.get(model, 0)
        if budget < min_budget:
            budget = min_budget
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
        # Retry schedule for transient Google 5xx errors. We only retry
        # while nothing has been streamed yet -- mid-stream failures are
        # surfaced as-is because we can't replay partial output. Kept
        # short (2 retries, ~3.5 s total) because persistent 2.5-pro 5xx
        # episodes are not rescued by more retries.
        backoffs = [0.5, 2.0]
        attempt = 0
        while True:
            yielded_any = False
            try:
                async for event in self._run(turn):
                    yielded_any = True
                    yield event
                return
            except BaseException as e:  # noqa: BLE001
                leaf = _unwrap_exception(e)
                traceback.print_exception(e, file=sys.stderr)

                if (
                    not yielded_any
                    and attempt < len(backoffs)
                    and _is_transient_error(leaf)
                ):
                    delay = backoffs[attempt]
                    attempt += 1
                    _logger.warning(
                        "gemini chat transient error (%s); retry %d/%d in %.1fs",
                        leaf, attempt, len(backoffs), delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                _logger.error("gemini chat turn failed: %s", leaf, exc_info=False)
                code, message = _classify_error(leaf, turn.model)
                yield ErrorEvent(code=code, message=message)
                return

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
            # Historically we supported Google's backend-driven MCP
            # (``McpServer`` + ``StreamableHttpTransport``), but that
            # mode is fundamentally incompatible with our TarkaMCP auth
            # setup (OAuth bearer + TOTP + Cloudflare Tunnel): Google's
            # backend fetch of /mcp loses the Authorization header in
            # transit, the MCP handshake returns 401, and the upstream
            # model deterministically emits 500 INTERNAL. We surface a
            # clear error instead of cascading through retries.
            yield ErrorEvent(
                code="remote_mode_disabled",
                message=(
                    "Le mode MCP 'remote' n'est plus supporté (il causait "
                    "des 500 INTERNAL systématiques à cause du flux OAuth). "
                    "Retire TARKAMCP_DASHBOARD_MCP_MODE=remote de ton .env "
                    "et relance le service pour repasser en mode local."
                ),
            )
            return

        # Local mode: open an MCP ClientSession from the dashboard
        # process, and run a MANUAL function-calling loop.
        #
        # We previously passed ``tools=[session]`` and relied on the SDK's
        # Automatic Function Calling path, but that route has two known
        # failure modes for Gemini 2.5 Pro:
        #
        #   - AFC corrupts MCP function names non-deterministically
        #     (googleapis/python-genai#1892)
        #   - Combining thinking_config + MCP + streaming triggers
        #     MALFORMED_FUNCTION_CALL / 500 INTERNAL on the backend
        #     (googleapis/python-genai#2081, #1374)
        #
        # Manual loop: we call ``list_tools()`` ourselves, convert each
        # to a ``FunctionDeclaration`` with ``parameters_json_schema``,
        # stream Gemini's response, execute any ``function_call`` via
        # ``session.call_tool()``, and feed the results back in a new
        # ``generate_content_stream`` call until no function_call remains.
        try:
            from mcp.client.session import ClientSession  # type: ignore
            from mcp.client.streamable_http import (  # type: ignore
                streamablehttp_client,
            )
        except ImportError as e:
            yield ErrorEvent(code="sdk_missing", message=str(e))
            return

        _logger.info(
            "gemini chat: model=%s effort=%s mcp_url=%s",
            turn.model, turn.effort, turn.mcp_url,
        )

        # ``streamablehttp_client`` (no underscore) is the variant that
        # actually threads the ``headers`` dict through every request.
        # Previously we used ``streamable_http_client(http_client=...)``
        # and relied on httpx default-headers propagation, but the MCP
        # client builds fresh requests that don't inherit the defaults,
        # so the Authorization header was being dropped -- producing a
        # hard 401 on /mcp even when calling loopback.
        async with streamablehttp_client(
            turn.mcp_url,
            headers={"Authorization": f"Bearer {turn.bearer}"},
            timeout=30,
            sse_read_timeout=300,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                    try:
                        await session.initialize()
                    except Exception as e:  # noqa: BLE001
                        yield ErrorEvent(
                            code="mcp_init_failed",
                            message=(
                                f"Impossible de se connecter à l'MCP "
                                f"({turn.mcp_url}): {e}. Vérifie que "
                                "TARKAMCP_DASHBOARD_PUBLIC_URL n'est pas "
                                "défini en mode local."
                            ),
                        )
                        return

                    try:
                        tools_result = await session.list_tools()
                    except Exception as e:  # noqa: BLE001
                        yield ErrorEvent(
                            code="mcp_list_tools_failed",
                            message=f"MCP list_tools a échoué: {e}",
                        )
                        return

                    mcp_tools = list(tools_result.tools or [])
                    if not mcp_tools:
                        yield ErrorEvent(
                            code="mcp_no_tools",
                            message=(
                                "L'MCP n'expose aucun outil. Vérifie que le "
                                "bearer est valide et que les modules Proxmox "
                                "sont chargés."
                            ),
                        )
                        return

                    function_decls = [
                        _mcp_tool_to_declaration(t, types) for t in mcp_tools
                    ]
                    tools_cfg = [types.Tool(function_declarations=function_decls)]

                    config = types.GenerateContentConfig(
                        thinking_config=thinking,
                        tools=tools_cfg,
                        automatic_function_calling=(
                            types.AutomaticFunctionCallingConfig(disable=True)
                        ),
                    )
                    client = self._ensure_client()

                    current_contents = list(contents)
                    for round_idx in range(_MAX_TOOL_ROUNDS):
                        stream = await client.aio.models.generate_content_stream(
                            model=turn.model,
                            contents=current_contents,
                            config=config,
                        )

                        model_parts: list = []
                        fc_invocations: list = []  # (fc_part, fc_id)
                        async for chunk in stream:
                            for part in _iter_parts(chunk):
                                text = getattr(part, "text", None)
                                if text:
                                    if getattr(part, "thought", False):
                                        yield ThinkingDelta(summary=text)
                                    else:
                                        yield TextDelta(text=text)
                                    model_parts.append(
                                        types.Part(
                                            text=text,
                                            thought=bool(
                                                getattr(part, "thought", False)
                                            ),
                                        )
                                    )

                                fc = getattr(part, "function_call", None)
                                if fc:
                                    fc_id = (
                                        getattr(fc, "id", None)
                                        or f"fc_{len(seen_tool_ids)}"
                                    )
                                    seen_tool_ids.add(fc_id)
                                    tool_starts[fc_id] = time.monotonic()
                                    args = dict(getattr(fc, "args", None) or {})
                                    fc_invocations.append((fc, fc_id, args))
                                    model_parts.append(part)
                                    yield ToolCallStart(
                                        id=fc_id,
                                        name=getattr(fc, "name", "?"),
                                        args=args,
                                    )

                        if not fc_invocations:
                            return  # model is done

                        current_contents.append(
                            types.Content(role="model", parts=model_parts)
                        )

                        response_parts: list = []
                        for fc, fc_id, args in fc_invocations:
                            name = getattr(fc, "name", "")
                            start = tool_starts.pop(fc_id, time.monotonic())
                            try:
                                result = await session.call_tool(name, args)
                                payload = _mcp_call_result_to_response(result)
                                status = (
                                    "error"
                                    if getattr(result, "isError", False)
                                    else "ok"
                                )
                            except Exception as e:  # noqa: BLE001
                                payload = {"error": str(e)}
                                status = "error"
                            duration = int((time.monotonic() - start) * 1000)

                            yield ToolCallEnd(
                                id=fc_id, status=status,
                                preview=_short_preview(payload),
                                duration_ms=duration,
                            )

                            response_parts.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        id=getattr(fc, "id", None),
                                        name=name,
                                        response=(
                                            payload
                                            if isinstance(payload, dict)
                                            else {"result": payload}
                                        ),
                                    )
                                )
                            )

                        current_contents.append(
                            types.Content(role="user", parts=response_parts)
                        )

                    yield ErrorEvent(
                        code="tool_loop_limit",
                        message=(
                            f"La boucle d'appel d'outils a dépassé "
                            f"{_MAX_TOOL_ROUNDS} tours sans réponse finale."
                        ),
                    )

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


# Max rounds of function_call / function_response before we give up.
# Each round is one generate_content_stream + one batch of tool calls.
# 10 is comfortably above realistic orchestration depth while still
# bounding run-away loops.
_MAX_TOOL_ROUNDS = 10


def _iter_parts(chunk: Any):
    """Yield every ``Part`` from a google-genai stream chunk."""
    for cand in getattr(chunk, "candidates", None) or []:
        content = getattr(cand, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", None) or []:
            yield part


def _mcp_tool_to_declaration(tool: Any, types_mod: Any) -> Any:
    """Convert an MCP ``Tool`` into a Gemini ``FunctionDeclaration``.

    MCP exposes ``inputSchema`` as a JSON Schema dict which the SDK's
    ``parameters_json_schema`` field accepts directly -- no manual
    translation to ``types.Schema`` needed.
    """
    name = getattr(tool, "name", "")
    description = getattr(tool, "description", "") or ""
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict) or not schema:
        # Gemini requires an object schema. Default to an empty object so
        # tools without declared parameters still validate.
        schema = {"type": "object", "properties": {}}
    return types_mod.FunctionDeclaration(
        name=name,
        description=description,
        parameters_json_schema=schema,
    )


def _mcp_call_result_to_response(result: Any) -> dict:
    """Turn an MCP ``CallToolResult`` into a JSON-serialisable response.

    Gemini's ``FunctionResponse.response`` must be a plain dict. We
    flatten the MCP content list into ``{"content": [...]}`` plus an
    optional ``error`` flag.
    """
    out: dict[str, Any] = {}
    content_items: list = []
    for c in getattr(result, "content", None) or []:
        text = getattr(c, "text", None)
        if text is not None:
            content_items.append({"type": "text", "text": text})
            continue
        data = getattr(c, "data", None)
        mime = getattr(c, "mimeType", None)
        if data is not None:
            content_items.append({
                "type": "binary",
                "mimeType": mime or "application/octet-stream",
            })
    out["content"] = content_items
    if getattr(result, "isError", False):
        out["error"] = True
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        out["structured"] = structured
    return out


def _is_transient_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a retryable Google 5xx / network blip."""
    msg = str(exc)
    if "INTERNAL" in msg and ("500" in msg or "Internal error" in msg):
        return True
    if "UNAVAILABLE" in msg or "503" in msg:
        return True
    if "DEADLINE_EXCEEDED" in msg or "504" in msg:
        return True
    name = type(exc).__name__
    if name in {"ReadTimeout", "WriteTimeout", "ConnectTimeout", "ConnectError"}:
        return True
    return False


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

    # 500 INTERNAL -- Google-side transient failure. We already retried
    # a few times server-side before surfacing, so this message asks the
    # user to try again rather than pretending retrying wasn't tried.
    if "INTERNAL" in msg and ("500" in msg or "Internal error" in msg):
        return (
            "upstream_internal",
            (
                f"Gemini a renvoyé une erreur interne (500) sur {model} "
                "malgré plusieurs tentatives. C'est côté Google, pas toi. "
                "Attends 10–20 s et renvoie ton message — si ça persiste, "
                "bascule sur un autre modèle."
            ),
        )

    # 503 UNAVAILABLE / 504 DEADLINE_EXCEEDED -- surge / latency.
    if "UNAVAILABLE" in msg or "503" in msg:
        return (
            "upstream_unavailable",
            (
                "Gemini est temporairement indisponible (503). Réessaie "
                "dans quelques secondes."
            ),
        )
    if "DEADLINE_EXCEEDED" in msg or "504" in msg:
        return (
            "upstream_timeout",
            "Gemini a dépassé le délai (504). Réessaie avec une question plus courte.",
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
