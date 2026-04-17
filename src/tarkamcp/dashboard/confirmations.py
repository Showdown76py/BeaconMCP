"""Per-session async confirmation waiters for dangerous tool calls.

Some MCP tools (SSH exec, and any future write-side Proxmox operations)
ship arbitrary shell into a PVE host and must never run automatically
from a Gemini turn. The engine yields a ``ToolConfirmRequired`` event,
then awaits a future from this store; the ``/app/api/chat/confirm``
route resolves the future when the user clicks approve or reject in
the dashboard.

Scoped by ``session_id`` so one logged-in user can't resolve another's
pending call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class _Pending:
    future: asyncio.Future[bool]
    session_id: str


class ConfirmationStore:
    """Maps tool-call ids to pending approval futures."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}

    def create(self, *, call_id: str, session_id: str) -> asyncio.Future[bool]:
        """Register a pending approval. Returns the future the engine awaits."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending[call_id] = _Pending(future=fut, session_id=session_id)
        return fut

    def resolve(self, *, call_id: str, session_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Returns True if the call existed."""
        entry = self._pending.get(call_id)
        if entry is None or entry.session_id != session_id:
            return False
        if entry.future.done():
            return False
        entry.future.set_result(approved)
        self._pending.pop(call_id, None)
        return True

    def cancel(self, call_id: str) -> None:
        """Drop a pending entry without resolving it (stream aborted, etc)."""
        entry = self._pending.pop(call_id, None)
        if entry and not entry.future.done():
            entry.future.cancel()

    def pending_for(self, session_id: str) -> list[str]:
        """Return call ids still waiting for this session's decision."""
        return [
            call_id for call_id, entry in self._pending.items()
            if entry.session_id == session_id
        ]
