"""Per-client usage accounting and budget enforcement.

Two moving parts:

- :class:`UsageMeter` -- stateless pricing calculator. Turns Gemini
  ``usage_metadata`` (prompt/cached/output token counts + model) into a
  USD cost using the public Google AI Studio rate card.
- :class:`UsageStore` -- SQLite-backed ledger + 5h session tracker. One
  row per assistant turn in ``usage_events`` (immutable), plus one
  live-session row per ``client_id`` in ``usage_5h_sessions`` that is
  reset whenever the 5-hour window expires.

Windows:

- **5h session (Anthropic-style)**: a contiguous 5h window that opens on
  the first turn after any inactivity of >=5h. While the window is open,
  turns accumulate into it. Once the window closes (now - started_at >=
  18000s at check time), the next turn resets the window to 0 and starts
  a new one beginning at that moment.
- **Weekly (rolling)**: a trailing 7-day sum over ``usage_events``.

Both caps are configurable via env vars, read in ``__main__`` and passed
into :class:`Budget`. Cap <= 0 means "unlimited".
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

# Rates are in USD per 1M tokens. Pulled from Google AI Studio pricing
# page on 2026-04-17. Keys with ``_hi`` suffixes apply when the prompt
# token count exceeds :data:`_TIER_THRESHOLD`; only the Pro models have
# a high tier in the public rate card.
_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {
        "input": 0.30, "cached": 0.03, "output": 2.50,
    },
    "gemini-2.5-pro": {
        "input": 1.25, "cached": 0.125, "output": 10.00,
        "input_hi": 2.50, "cached_hi": 0.25, "output_hi": 15.00,
    },
    "gemini-3-flash-preview": {
        "input": 0.50, "cached": 0.05, "output": 3.00,
    },
    "gemini-3.1-pro-preview": {
        "input": 2.00, "cached": 0.20, "output": 12.00,
        "input_hi": 4.00, "cached_hi": 0.40, "output_hi": 18.00,
    },
}

# Prompt tokens above this count trigger the Pro models' "long prompt"
# pricing tier.
_TIER_THRESHOLD = 200_000

_FIVE_HOURS_SECS = 5 * 3600
_SEVEN_DAYS_SECS = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------


class UsageMeter:
    """Pure function: (model, token counts) -> cost USD."""

    @staticmethod
    def cost_usd(
        model: str,
        *,
        prompt_tokens: int,
        cached_tokens: int,
        output_tokens: int,
    ) -> float:
        rates = _PRICING.get(model) or _PRICING["gemini-2.5-flash"]
        use_hi = prompt_tokens > _TIER_THRESHOLD and "input_hi" in rates
        in_rate = rates["input_hi"] if use_hi else rates["input"]
        out_rate = rates["output_hi"] if use_hi else rates["output"]
        ca_rate = rates["cached_hi"] if use_hi else rates["cached"]

        # cached_tokens is the subset of prompt_tokens served from cache;
        # bill the remainder at the input rate and cached_tokens at the
        # cached rate (which is ~10x cheaper across the board).
        billable_input = max(0, prompt_tokens - cached_tokens)
        total = (
            billable_input * in_rate
            + cached_tokens * ca_rate
            + output_tokens * out_rate
        )
        return total / 1_000_000


# ---------------------------------------------------------------------------
# Budget config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """Per-client spending caps (global, identical across clients).

    ``0`` (or any non-positive value) disables the cap on that window.
    Units: USD.
    """

    limit_5h_usd: float
    limit_week_usd: float

    @property
    def has_any_limit(self) -> bool:
        return self.limit_5h_usd > 0 or self.limit_week_usd > 0


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class UsageSnapshot:
    """Current usage picture for one client, used by the UI footer."""

    spent_5h_usd: float
    limit_5h_usd: float
    session_5h_started_at: float | None  # None if session window is empty/expired
    session_5h_reset_at: float | None    # started_at + 5h, when UI can expect reset

    spent_week_usd: float
    limit_week_usd: float

    def to_json(self) -> dict[str, Any]:
        return {
            "spent_5h_usd": round(self.spent_5h_usd, 6),
            "limit_5h_usd": self.limit_5h_usd,
            "session_5h_started_at": self.session_5h_started_at,
            "session_5h_reset_at": self.session_5h_reset_at,
            "spent_week_usd": round(self.spent_week_usd, 6),
            "limit_week_usd": self.limit_week_usd,
        }


@dataclass
class BudgetBlock:
    """Returned by ``check_budget`` when a request must be refused."""

    window: str  # "5h" | "week"
    spent_usd: float
    limit_usd: float
    reset_at: float | None  # absolute epoch seconds of window reset, if known


class UsageStore:
    def __init__(self, db: Database, budget: Budget) -> None:
        self._db = db
        self._budget = budget

    @property
    def budget(self) -> Budget:
        return self._budget

    # --- write path -------------------------------------------------------

    def record_turn(
        self,
        *,
        client_id: str,
        conversation_id: str | None,
        message_id: str | None,
        model: str,
        prompt_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cost_usd: float,
        now: float | None = None,
    ) -> None:
        """Append a ledger row and update the 5h session row atomically."""
        ts = now if now is not None else time.time()
        conn = self._db.conn()
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO usage_events (id, client_id, conversation_id,
                                          message_id, ts, model,
                                          prompt_tokens, cached_tokens,
                                          output_tokens, cost_usd)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), client_id, conversation_id, message_id,
                    ts, model, prompt_tokens, cached_tokens, output_tokens,
                    cost_usd,
                ),
            )
            self._apply_to_session(conn, client_id, ts, cost_usd)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _apply_to_session(
        self, conn: Any, client_id: str, ts: float, cost_usd: float,
    ) -> None:
        row = conn.execute(
            "SELECT started_at, last_event_at, cost_usd "
            "  FROM usage_5h_sessions WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO usage_5h_sessions "
                "  (client_id, started_at, last_event_at, cost_usd) "
                "  VALUES (?, ?, ?, ?)",
                (client_id, ts, ts, cost_usd),
            )
            return
        started_at = float(row["started_at"])
        if ts - started_at >= _FIVE_HOURS_SECS:
            # Previous session expired -- start a fresh one at ``ts``.
            conn.execute(
                "UPDATE usage_5h_sessions "
                "  SET started_at = ?, last_event_at = ?, cost_usd = ? "
                "  WHERE client_id = ?",
                (ts, ts, cost_usd, client_id),
            )
        else:
            conn.execute(
                "UPDATE usage_5h_sessions "
                "  SET last_event_at = ?, cost_usd = cost_usd + ? "
                "  WHERE client_id = ?",
                (ts, cost_usd, client_id),
            )

    # --- read path --------------------------------------------------------

    def snapshot(self, client_id: str, *, now: float | None = None) -> UsageSnapshot:
        """Return current usage for ``client_id``.

        The 5h-window figure reflects the Anthropic-style session: if the
        last known session has been dormant for >=5h, we report 0 spent
        (the window is closed and the next turn will open a new one).
        """
        ts = now if now is not None else time.time()
        row = self._db.conn().execute(
            "SELECT started_at, cost_usd "
            "  FROM usage_5h_sessions WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if row is None:
            spent_5h = 0.0
            started_at = None
            reset_at = None
        else:
            started_at = float(row["started_at"])
            if ts - started_at >= _FIVE_HOURS_SECS:
                spent_5h = 0.0
                started_at = None
                reset_at = None
            else:
                spent_5h = float(row["cost_usd"])
                reset_at = started_at + _FIVE_HOURS_SECS

        week_row = self._db.conn().execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total "
            "  FROM usage_events WHERE client_id = ? AND ts >= ?",
            (client_id, ts - _SEVEN_DAYS_SECS),
        ).fetchone()
        spent_week = float(week_row["total"] if week_row else 0.0)

        return UsageSnapshot(
            spent_5h_usd=spent_5h,
            limit_5h_usd=self._budget.limit_5h_usd,
            session_5h_started_at=started_at,
            session_5h_reset_at=reset_at,
            spent_week_usd=spent_week,
            limit_week_usd=self._budget.limit_week_usd,
        )

    # --- enforcement ------------------------------------------------------

    def check_budget(
        self, client_id: str, *, now: float | None = None,
    ) -> BudgetBlock | None:
        """Return a :class:`BudgetBlock` if ``client_id`` is over either
        cap, or ``None`` if the request may proceed.

        Called before handing a turn to the chat engine. We err on the
        side of letting the turn through when both caps are 0 (disabled)
        so that users can run an unmetered setup if they choose.
        """
        if not self._budget.has_any_limit:
            return None
        snap = self.snapshot(client_id, now=now)
        if snap.limit_5h_usd > 0 and snap.spent_5h_usd >= snap.limit_5h_usd:
            return BudgetBlock(
                window="5h",
                spent_usd=snap.spent_5h_usd,
                limit_usd=snap.limit_5h_usd,
                reset_at=snap.session_5h_reset_at,
            )
        if snap.limit_week_usd > 0 and snap.spent_week_usd >= snap.limit_week_usd:
            # Rolling 7 days -> the reset moment isn't a single clock tick,
            # so we leave ``reset_at`` unset; the UI formats this as
            # "sur 7 jours glissants" rather than an absolute time.
            return BudgetBlock(
                window="week",
                spent_usd=snap.spent_week_usd,
                limit_usd=snap.limit_week_usd,
                reset_at=None,
            )
        return None
