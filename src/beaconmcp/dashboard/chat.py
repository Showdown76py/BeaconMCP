"""ChatEngine abstraction.

The route calls ``engine.run(turn)`` which yields :class:`ChatEvent`
instances. The route translates them into SSE frames, persists the final
assistant message, and emits the auxiliary ``done`` / ``title_updated``
events itself.

Two implementations:

- :class:`FakeChatEngine` -- yields a scripted sequence, used in tests.
- :class:`GeminiChatEngine` -- the real google-genai SDK backed engine
  with BeaconMCP wired in as an MCP tool provider.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Protocol, runtime_checkable

from .conversations import Message, ToolCall

_logger = logging.getLogger("beaconmcp.dashboard.chat")


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
class ToolConfirmRequired:
    """Engine paused waiting for a manual approve/reject click in the UI."""
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ErrorEvent:
    code: str
    message: str


@dataclass
class UsageAccumulated:
    """Total token counts for the turn, summed across all tool rounds.

    Emitted exactly once at the end of a successful (or partially
    successful) turn so the route can compute a USD cost and persist it.
    ``cached_tokens`` is the subset of ``prompt_tokens`` that Gemini
    served from an implicit/explicit cache hit.
    """
    model: str
    prompt_tokens: int
    cached_tokens: int
    output_tokens: int


ChatEvent = (
    TextDelta | ThinkingDelta | ToolCallStart | ToolCallEnd
    | ToolConfirmRequired | ErrorEvent | UsageAccumulated
)


# Tool names that MUST go through a human approval step before we run
# them from a Gemini turn. Anything that can fire arbitrary shell on a
# host or VM (SSH directly, QEMU Guest Agent exec via proxmox_run) belongs
# here -- otherwise a single compromised/confused turn could rm -rf a
# production box. Legacy ``*_exec_command*`` names are kept for
# defense-in-depth in case an older MCP server is still wired up.
# Keep this list tight; every entry adds a modal click to the UX.
_NEEDS_CONFIRMATION: frozenset[str] = frozenset({
    # Current (unified) tools.
    "ssh_run",
    "proxmox_run",
    # Legacy names (pre-unified tools) -- kept defensively.
    "ssh_exec_command",
    "ssh_exec_command_async",
    "proxmox_exec_command",
    "proxmox_exec_command_async",
})


def _tool_call_requires_confirmation(name: str, args: Any) -> bool:
    """Return True when a tool call needs human approval before running.

    The unified ``ssh_run`` / ``proxmox_run`` tools have three call patterns
    (sync start, async start, poll existing by ``exec_id``). Only the start
    patterns actually execute shell; a pure poll call -- where ``exec_id``
    is set and ``command`` is not -- is read-only and must not trigger a
    confirmation modal. We keep the allow-list name-based for everything
    else, then peel off the poll case here.
    """
    if name not in _NEEDS_CONFIRMATION:
        return False
    if name in {"ssh_run", "proxmox_run"} and isinstance(args, dict):
        exec_id = args.get("exec_id")
        command = args.get("command")
        if exec_id and not command:
            return False
    return True


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
    # Optional callback invoked right before executing any tool whose
    # name appears in ``_NEEDS_CONFIRMATION``. It is handed the
    # ``ToolConfirmRequired`` payload (already yielded to the UI) and
    # must return ``True`` for approve / ``False`` for reject. If
    # ``None``, confirmation-gated tools auto-reject.
    confirm_tool: Callable[[ToolConfirmRequired], Awaitable[bool]] | None = None


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
        # UsageAccumulated / ToolConfirmRequired / ErrorEvent don't
        # contribute to the persisted message body.

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

    We open a local MCP ``ClientSession`` against the BeaconMCP endpoint
    using the user's bearer, and run a manual function-calling loop over
    MCP declarations. For Gemini 3 models we also enable the built-in
    Google Search tool and surface its server-side tool invocations in
    the same dashboard tool timeline. This path does NOT use the
    ``McpServer`` remote tool (where Google's backend calls our MCP
    directly) — that feature is preview-gated and returned
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
        server_tool_name_by_id: dict[str, str] = {}

        # Accumulated across every ``generate_content_stream`` round in
        # this turn so the dashboard can charge the client once per turn.
        usage_total_prompt = 0
        usage_total_cached = 0
        usage_total_output = 0
        emitted_visible_text = False

        contents = self._build_contents(turn.history, turn.user_text)
        thinking = self._build_thinking_config(turn.model, turn.effort)

        if turn.mcp_mode == "remote":
            # Historically we supported Google's backend-driven MCP
            # (``McpServer`` + ``StreamableHttpTransport``), but that
            # mode is fundamentally incompatible with our BeaconMCP auth
            # setup (OAuth bearer + TOTP + Cloudflare Tunnel): Google's
            # backend fetch of /mcp loses the Authorization header in
            # transit, the MCP handshake returns 401, and the upstream
            # model deterministically emits 500 INTERNAL. We surface a
            # clear error instead of cascading through retries.
            yield ErrorEvent(
                code="remote_mode_disabled",
                message=(
                    "MCP 'remote' mode is no longer supported (it caused "
                    "systematic 500 INTERNAL errors through the OAuth flow). "
                    "Remove BEACONMCP_DASHBOARD_MCP_MODE=remote from your .env "
                    "and restart the service to fall back to local mode."
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
                        init_result = await session.initialize()
                    except Exception as e:  # noqa: BLE001
                        yield ErrorEvent(
                            code="mcp_init_failed",
                            message=(
                                f"Cannot connect to MCP ({turn.mcp_url}): {e}. "
                                "Make sure BEACONMCP_DASHBOARD_PUBLIC_URL is "
                                "not set when running in local mode."
                            ),
                        )
                        return

                    # The MCP server ships a dynamic ``instructions`` string
                    # describing which capabilities are currently active
                    # (Proxmox N nodes / SSH N hosts / BMC N devices). Pass
                    # it through as Gemini's system_instruction so the model
                    # grounds its answers on what the server actually exposes
                    # instead of hallucinating from tool-name shapes.
                    server_instructions = _compose_system_instruction(
                        (init_result.instructions or "").strip() or None
                    )

                    try:
                        tools_result = await session.list_tools()
                    except Exception as e:  # noqa: BLE001
                        yield ErrorEvent(
                            code="mcp_list_tools_failed",
                            message=f"MCP list_tools failed: {e}",
                        )
                        return

                    mcp_tools = list(tools_result.tools or [])
                    if not mcp_tools:
                        yield ErrorEvent(
                            code="mcp_no_tools",
                            message=(
                                "The MCP server exposes no tools. Check that "
                                "the bearer is valid and that at least one "
                                "capability (proxmox.nodes, ssh.hosts, or "
                                "bmc.devices) is configured in beaconmcp.yaml."
                            ),
                        )
                        return

                    function_decls = [
                        _mcp_tool_to_declaration(t, types) for t in mcp_tools
                    ]
                    tools_cfg = [types.Tool(function_declarations=function_decls)]

                    # Gemini web access: enable built-in Google Search on
                    # Gemini 3+ only. 2.5 models do not reliably support
                    # multi-tool combination in this request path.
                    if _is_gemini_3(turn.model):
                        web_tool = _build_google_search_tool(types)
                        if web_tool is not None:
                            tools_cfg.append(web_tool)

                    config_kwargs: dict[str, Any] = {
                        "system_instruction": server_instructions,
                        "thinking_config": thinking,
                        "tools": tools_cfg,
                        "automatic_function_calling": (
                            types.AutomaticFunctionCallingConfig(disable=True)
                        ),
                    }

                    # Ask Gemini to include server-side tool invocation parts
                    # (tool_call/tool_response) in streamed chunks so the UI
                    # can display web-search calls like regular tool cards.
                    tool_config_cls = getattr(types, "ToolConfig", None)
                    if tool_config_cls is not None:
                        try:
                            config_kwargs["tool_config"] = tool_config_cls(
                                include_server_side_tool_invocations=True,
                            )
                        except TypeError:
                            config_kwargs[
                                "include_server_side_tool_invocations"
                            ] = True

                    try:
                        config = types.GenerateContentConfig(**config_kwargs)
                    except TypeError:
                        config_kwargs.pop(
                            "include_server_side_tool_invocations", None,
                        )
                        config = types.GenerateContentConfig(**config_kwargs)

                    client = self._ensure_client()

                    current_contents = list(contents)
                    for round_idx in range(_MAX_TOOL_ROUNDS):
                        stream = await client.aio.models.generate_content_stream(
                            model=turn.model,
                            contents=current_contents,
                            config=config,
                        )

                        model_parts: list = []
                        fc_invocations: list = []  # (fc_part, fc, fc_id, args)
                        emitted_visible_text_this_round = False
                        last_usage: Any = None
                        async for chunk in stream:
                            um = getattr(chunk, "usage_metadata", None)
                            if um is not None:
                                # Only the final chunk carries totals for
                                # the stream; earlier chunks may surface
                                # partial data. Keep overwriting so we
                                # land on the last value.
                                last_usage = um
                            for part in _iter_parts(chunk):
                                text = getattr(part, "text", None)
                                if text:
                                    if getattr(part, "thought", False):
                                        yield ThinkingDelta(summary=text)
                                    else:
                                        if emitted_visible_text and not emitted_visible_text_this_round:
                                            # Tool rounds often produce a short
                                            # sentence before each call; insert
                                            # a paragraph break so they don't
                                            # visually collapse into one blob.
                                            yield TextDelta(text="\n\n")
                                            model_parts.append(types.Part(text="\n\n"))
                                        yield TextDelta(text=text)
                                        emitted_visible_text = True
                                        emitted_visible_text_this_round = True
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
                                    args = dict(getattr(fc, "args", None) or {})
                                    fc_invocations.append((part, fc, fc_id, args))

                                # Server-side built-in tool invocation
                                # (e.g. Google Search). These are emitted by
                                # Gemini when include_server_side_tool_invocations
                                # is enabled and should be rendered as regular
                                # tool cards in the dashboard.
                                tc = getattr(part, "tool_call", None)
                                if tc:
                                    tc_id = (
                                        getattr(tc, "id", None)
                                        or f"tc_{len(seen_tool_ids)}"
                                    )
                                    if tc_id not in seen_tool_ids:
                                        seen_tool_ids.add(tc_id)
                                        tool_starts[tc_id] = time.monotonic()
                                        tool_name = _tool_name_from_server_tool_type(
                                            getattr(tc, "tool_type", None),
                                        )
                                        server_tool_name_by_id[tc_id] = tool_name
                                        raw_args = _normalize_json_like(
                                            getattr(tc, "args", None)
                                        )
                                        args = raw_args if isinstance(raw_args, dict) else {}
                                        yield ToolCallStart(
                                            id=tc_id,
                                            name=tool_name,
                                            args=args,
                                        )
                                    model_parts.append(part)

                                tr = getattr(part, "tool_response", None)
                                if tr:
                                    tr_id = getattr(tr, "id", None) or ""
                                    tr_id = str(tr_id) if tr_id else f"tr_{len(seen_tool_ids)}"
                                    tool_name = server_tool_name_by_id.get(tr_id)
                                    if not tool_name:
                                        tool_name = _tool_name_from_server_tool_type(
                                            getattr(tr, "tool_type", None),
                                        )
                                    payload = _normalize_json_like(
                                        getattr(tr, "response", None)
                                    )
                                    if tr_id not in seen_tool_ids:
                                        # If we only got a response part, still
                                        # synthesize a start card so the timeline
                                        # remains coherent.
                                        seen_tool_ids.add(tr_id)
                                        yield ToolCallStart(
                                            id=tr_id,
                                            name=tool_name,
                                            args={},
                                        )
                                    start = tool_starts.pop(tr_id, time.monotonic())
                                    status = "error" if _tool_response_is_error(payload) else "ok"
                                    yield ToolCallEnd(
                                        id=tr_id,
                                        status=status,
                                        preview=_short_preview(payload),
                                        duration_ms=int((time.monotonic() - start) * 1000),
                                    )
                                    model_parts.append(part)

                        if last_usage is not None:
                            usage_total_prompt += int(
                                getattr(last_usage, "prompt_token_count", 0) or 0
                            )
                            usage_total_cached += int(
                                getattr(last_usage, "cached_content_token_count", 0) or 0
                            )
                            usage_total_output += int(
                                getattr(last_usage, "candidates_token_count", 0) or 0
                            )

                        if not fc_invocations:
                            yield UsageAccumulated(
                                model=turn.model,
                                prompt_tokens=usage_total_prompt,
                                cached_tokens=usage_total_cached,
                                output_tokens=usage_total_output,
                            )
                            return  # model is done

                        selected_fc = fc_invocations[:_MAX_FUNCTION_CALLS_PER_ROUND]
                        if len(fc_invocations) > len(selected_fc):
                            _logger.info(
                                "gemini chat: model emitted %d function calls in one round; "
                                "executing %d to preserve text/tool interleaving",
                                len(fc_invocations),
                                len(selected_fc),
                            )
                        model_parts.extend(fc_part for fc_part, _fc, _fc_id, _args in selected_fc)

                        current_contents.append(
                            types.Content(role="model", parts=model_parts)
                        )

                        response_parts: list = []
                        for _fc_part, fc, fc_id, args in selected_fc:
                            name = getattr(fc, "name", "")
                            seen_tool_ids.add(fc_id)
                            tool_starts[fc_id] = time.monotonic()
                            yield ToolCallStart(
                                id=fc_id,
                                name=name or "?",
                                args=args,
                            )
                            start = tool_starts.pop(fc_id, time.monotonic())

                            approved = True
                            if _tool_call_requires_confirmation(name, args):
                                req = ToolConfirmRequired(
                                    id=fc_id, name=name, args=args,
                                )
                                yield req  # UI shows approve/reject buttons
                                if turn.confirm_tool is None:
                                    # No callback wired -> auto-reject so
                                    # the engine never runs the tool without
                                    # an explicit green light.
                                    approved = False
                                else:
                                    try:
                                        approved = bool(
                                            await turn.confirm_tool(req)
                                        )
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception:  # noqa: BLE001
                                        approved = False

                            if not approved:
                                payload = {
                                    "error": "user_rejected",
                                    "message": (
                                        "The user rejected this tool call "
                                        "from the dashboard."
                                    ),
                                }
                                status = "rejected"
                                duration = int(
                                    (time.monotonic() - start) * 1000
                                )
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
                                            response=payload,
                                        )
                                    )
                                )
                                continue

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

                    # Even though we couldn't finish, tokens WERE burned
                    # across the rounds; surface them so the client is
                    # charged for what actually hit Google.
                    yield UsageAccumulated(
                        model=turn.model,
                        prompt_tokens=usage_total_prompt,
                        cached_tokens=usage_total_cached,
                        output_tokens=usage_total_output,
                    )
                    yield ErrorEvent(
                        code="tool_loop_limit",
                        message=(
                            f"The tool-call loop exceeded {_MAX_TOOL_ROUNDS} "
                            "rounds without a final reply."
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
                        "Give a 4-word title (max), no emoji, no quotes, no "
                        "trailing punctuation, summarizing this request:\n\n"
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
# Each round is one generate_content_stream + one tool call.
# 50 leaves room for longer orchestrations while still
# bounding run-away loops.
_MAX_TOOL_ROUNDS = 50

# Keep MCP tool orchestration conversational: one tool at a time lets
# the model add a short sentence between calls (Copilot/Assistant style).
_MAX_FUNCTION_CALLS_PER_ROUND = 1


def _compose_system_instruction(server_instructions: str | None) -> str:
    """Compose dashboard system instructions for interleaved tool usage."""
    base = (
        "When tools are needed, never batch multiple MCP function calls in one reply. "
        "Before each tool call, first send one short natural-language sentence "
        "about what you are about to check. Then emit exactly one function_call "
        "and wait for its result before deciding the next step."
    )
    if server_instructions:
        return f"{base}\n\n{server_instructions}"
    return base


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


def _build_google_search_tool(types_mod: Any) -> Any | None:
    """Return a google-genai built-in web-search tool when available.

    SDK type names changed across releases (``GoogleSearch`` vs
    ``ToolGoogleSearch``). We support both to keep the dashboard forward
    compatible with minor library updates.
    """
    tool_cls = getattr(types_mod, "Tool", None)
    if tool_cls is None:
        return None

    for cls_name in ("GoogleSearch", "ToolGoogleSearch", "WebSearch"):
        search_cls = getattr(types_mod, cls_name, None)
        if search_cls is None:
            continue
        try:
            return tool_cls(google_search=search_cls())
        except TypeError:
            continue

    # Older SDK variants expose retrieval style search.
    gsr_cls = getattr(types_mod, "GoogleSearchRetrieval", None)
    if gsr_cls is not None:
        try:
            return tool_cls(google_search_retrieval=gsr_cls())
        except TypeError:
            return None
    return None


def _normalize_json_like(value: Any) -> Any:
    """Best-effort conversion of SDK value objects into plain JSON-ish data."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value

    to_dict = getattr(value, "to_json_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:  # noqa: BLE001
            pass

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:  # noqa: BLE001
            pass

    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {
            str(k): _normalize_json_like(v)
            for k, v in raw.items()
            if not str(k).startswith("_")
        }
    return str(value)


def _normalize_server_tool_type(tool_type: Any) -> str:
    if tool_type is None:
        return "TOOL_TYPE_UNSPECIFIED"
    text = str(tool_type)
    if "." in text:
        text = text.split(".")[-1]
    return text.upper()


def _tool_name_from_server_tool_type(tool_type: Any) -> str:
    """Map SDK server-side tool type enums to stable dashboard names."""
    normalized = _normalize_server_tool_type(tool_type)
    mapping = {
        "GOOGLE_SEARCH_WEB": "google_search_web",
        "GOOGLE_SEARCH_IMAGE": "google_search_image",
        "GOOGLE_MAPS": "google_maps",
        "URL_CONTEXT": "url_context",
        "FILE_SEARCH": "file_search",
    }
    if normalized in mapping:
        return mapping[normalized]
    if normalized.startswith("TOOL_TYPE_"):
        normalized = normalized[len("TOOL_TYPE_") :]
    return normalized.lower() or "server_tool"


def _tool_response_is_error(payload: Any) -> bool:
    """Heuristic: built-in tool responses may encode failure in payload fields."""
    if not isinstance(payload, dict):
        return False

    err = payload.get("error")
    if err:
        return True

    status = str(payload.get("status") or "").lower()
    if status in {"error", "failed", "failure"}:
        return True

    retrieval = _normalize_json_like(payload.get("url_retrieval_status"))
    if isinstance(retrieval, str) and retrieval.endswith("_ERROR"):
        return True
    return False


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
                    f"Your Gemini key does not have access to {model} "
                    "(Google allowlist required for preview models). Switch "
                    "to gemini-2.5-flash or gemini-2.5-pro via the dropdown "
                    "in the bottom-left — those are available on every AI "
                    "Studio key."
                ),
            )
        return (
            "permission_denied",
            (
                f"Gemini rejected the request on {model} (403 PERMISSION_DENIED). "
                "Confirm that your API key is allowed to use this model."
            ),
        )

    # 404 on the model -> invalid model ID for the API version.
    if "NOT_FOUND" in msg or "404" in msg and "model" in msg.lower():
        return (
            "model_not_found",
            f"Model {model} not found by Gemini. Try a different one.",
        )

    # 429 rate limit / quota.
    if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        return (
            "rate_limited",
            "Gemini quota exceeded. Retry in a few seconds.",
        )

    # 500 INTERNAL -- Google-side transient failure. We already retried
    # a few times server-side before surfacing, so this message asks the
    # user to try again rather than pretending retrying wasn't tried.
    if "INTERNAL" in msg and ("500" in msg or "Internal error" in msg):
        return (
            "upstream_internal",
            (
                f"Gemini returned a 500 INTERNAL error on {model} after "
                "several retries. This is on Google's side. Wait 10–20 s "
                "and resend your message; if it persists, switch to another "
                "model."
            ),
        )

    # 503 UNAVAILABLE / 504 DEADLINE_EXCEEDED -- surge / latency.
    if "UNAVAILABLE" in msg or "503" in msg:
        return (
            "upstream_unavailable",
            (
                "Gemini is temporarily unavailable (503). Retry in a few "
                "seconds."
            ),
        )
    if "DEADLINE_EXCEEDED" in msg or "504" in msg:
        return (
            "upstream_timeout",
            "Gemini timed out (504). Retry with a shorter request.",
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
