"""CRUD for conversations and messages.

Conversations are scoped to a ``client_id``. Messages are stored in the
order they were created. Tool calls are persisted as a JSON blob on the
assistant message itself rather than a child table -- keeps a turn fetch
to a single SELECT + parse.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import Database


VALID_EFFORTS = ("minimal", "low", "medium", "high")
# Stable Gemini 2.5 ship first (wider access), then Gemini 3 preview
# variants for users on the allowlist.
VALID_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
)
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_EFFORT = "low"


@dataclass
class Conversation:
    id: str
    client_id: str
    title: str | None
    model: str
    thinking_effort: str
    created_at: float
    updated_at: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | ok | error
    preview: str | None = None
    duration_ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Message:
    id: str
    conversation_id: str
    role: str  # user | assistant
    content: str | None
    tool_calls: list[ToolCall]
    thinking_summary: str | None
    model: str | None
    effort: str | None
    created_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "tool_calls": [tc.to_json() for tc in self.tool_calls],
            "thinking_summary": self.thinking_summary,
            "model": self.model,
            "effort": self.effort,
            "created_at": self.created_at,
        }


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return str(uuid.uuid4())


def _decode_tool_calls(raw: str | None) -> list[ToolCall]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[ToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            ToolCall(
                id=item.get("id", ""),
                name=item.get("name", ""),
                args=item.get("args") or {},
                status=item.get("status", "ok"),
                preview=item.get("preview"),
                duration_ms=item.get("duration_ms"),
            )
        )
    return out


def _encode_tool_calls(tcs: list[ToolCall]) -> str | None:
    if not tcs:
        return None
    return json.dumps([tc.to_json() for tc in tcs])


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- conversations -----------------------------------------------------

    def create(self, *, client_id: str, model: str, effort: str) -> Conversation:
        if model not in VALID_MODELS:
            model = DEFAULT_MODEL
        if effort not in VALID_EFFORTS:
            effort = DEFAULT_EFFORT
        conv = Conversation(
            id=_new_id(),
            client_id=client_id,
            title=None,
            model=model,
            thinking_effort=effort,
            created_at=_now(),
            updated_at=_now(),
        )
        self._db.conn().execute(
            """
            INSERT INTO conversations (id, client_id, title, model, thinking_effort,
                                       created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (conv.id, conv.client_id, conv.title, conv.model, conv.thinking_effort,
             conv.created_at, conv.updated_at),
        )
        return conv

    def list_for_client(self, client_id: str, *, limit: int = 200) -> list[Conversation]:
        rows = self._db.conn().execute(
            """
            SELECT id, client_id, title, model, thinking_effort, created_at, updated_at
              FROM conversations WHERE client_id = ?
              ORDER BY updated_at DESC
              LIMIT ?
            """,
            (client_id, limit),
        ).fetchall()
        return [Conversation(**dict(r)) for r in rows]

    def get(self, conversation_id: str, *, client_id: str) -> Conversation | None:
        row = self._db.conn().execute(
            """
            SELECT id, client_id, title, model, thinking_effort, created_at, updated_at
              FROM conversations WHERE id = ? AND client_id = ?
            """,
            (conversation_id, client_id),
        ).fetchone()
        if row is None:
            return None
        return Conversation(**dict(row))

    def patch(
        self,
        conversation_id: str,
        *,
        client_id: str,
        title: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> Conversation | None:
        existing = self.get(conversation_id, client_id=client_id)
        if existing is None:
            return None
        new_title = existing.title if title is None else (title or None)
        new_model = existing.model
        if model is not None and model in VALID_MODELS:
            new_model = model
        new_effort = existing.thinking_effort
        if effort is not None and effort in VALID_EFFORTS:
            new_effort = effort
        now = _now()
        self._db.conn().execute(
            """
            UPDATE conversations
               SET title = ?, model = ?, thinking_effort = ?, updated_at = ?
             WHERE id = ? AND client_id = ?
            """,
            (new_title, new_model, new_effort, now, conversation_id, client_id),
        )
        return self.get(conversation_id, client_id=client_id)

    def touch(self, conversation_id: str) -> None:
        self._db.conn().execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )

    def delete(self, conversation_id: str, *, client_id: str) -> bool:
        cur = self._db.conn().execute(
            "DELETE FROM conversations WHERE id = ? AND client_id = ?",
            (conversation_id, client_id),
        )
        return (cur.rowcount or 0) > 0

    # --- messages ----------------------------------------------------------

    def list_messages(self, conversation_id: str) -> list[Message]:
        rows = self._db.conn().execute(
            """
            SELECT id, conversation_id, role, content, tool_calls,
                   thinking_summary, model, effort, created_at
              FROM messages WHERE conversation_id = ?
              ORDER BY created_at ASC, id ASC
            """,
            (conversation_id,),
        ).fetchall()
        return [
            Message(
                id=r["id"],
                conversation_id=r["conversation_id"],
                role=r["role"],
                content=r["content"],
                tool_calls=_decode_tool_calls(r["tool_calls"]),
                thinking_summary=r["thinking_summary"],
                model=r["model"],
                effort=r["effort"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def add_user_message(self, conversation_id: str, content: str) -> Message:
        msg = Message(
            id=_new_id(),
            conversation_id=conversation_id,
            role="user",
            content=content,
            tool_calls=[],
            thinking_summary=None,
            model=None,
            effort=None,
            created_at=_now(),
        )
        self._insert(msg)
        return msg

    def add_assistant_message(
        self,
        conversation_id: str,
        *,
        content: str,
        tool_calls: list[ToolCall],
        thinking_summary: str | None,
        model: str,
        effort: str,
    ) -> Message:
        msg = Message(
            id=_new_id(),
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            thinking_summary=thinking_summary,
            model=model,
            effort=effort,
            created_at=_now(),
        )
        self._insert(msg)
        return msg

    def _insert(self, msg: Message) -> None:
        self._db.conn().execute(
            """
            INSERT INTO messages (id, conversation_id, role, content, tool_calls,
                                  thinking_summary, model, effort, created_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id, msg.conversation_id, msg.role, msg.content,
                _encode_tool_calls(msg.tool_calls),
                msg.thinking_summary, msg.model, msg.effort, msg.created_at,
            ),
        )
        self.touch(msg.conversation_id)
