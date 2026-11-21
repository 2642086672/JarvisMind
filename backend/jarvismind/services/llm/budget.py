"""LLM spend control.

Spend is tracked in ``system_settings`` so it survives restarts. The period is
a rolling 30-day window; exceeding the configured budget blocks new extraction
calls instead of failing silently or overspending.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import text

from ...core.errors import BudgetExceededError
from ...core.timeutils import parse_iso, utc_now, utc_now_iso
from ...database.connection import DatabaseManager

PERIOD_DAYS = 30
SPENT_KEY = "llm.spent_cny"
PERIOD_KEY = "llm.period_start"


class BudgetGuard:
    def __init__(
        self,
        manager: DatabaseManager,
        *,
        limit_cny: float,
        price_per_1k_input_cny: float = 0.0,
        price_per_1k_output_cny: float = 0.0,
    ) -> None:
        self.manager = manager
        self.limit_cny = max(limit_cny, 0.0)
        self.price_input = price_per_1k_input_cny
        self.price_output = price_per_1k_output_cny

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1000.0 * self.price_input
            + output_tokens / 1000.0 * self.price_output
        )

    def _read(self) -> tuple:
        with self.manager.session() as session:
            spent = float(
                session.execute(
                    text("SELECT value FROM system_settings WHERE key=:k"),
                    {"k": SPENT_KEY},
                ).scalar()
                or 0.0
            )
            period_raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"),
                {"k": PERIOD_KEY},
            ).scalar()
        return spent, period_raw

    def _write(self, spent: float, period_start: str) -> None:
        with self.manager.session() as session:
            for key, value in ((SPENT_KEY, str(spent)), (PERIOD_KEY, period_start)):
                session.execute(
                    text(
                        "INSERT INTO system_settings(key, value, updated_at) "
                        "VALUES (:k, :v, :u) ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at"
                    ),
                    {"k": key, "v": value, "u": utc_now_iso()},
                )

    def status(self) -> dict:
        spent, period_raw = self._read()
        period_start = period_raw or utc_now_iso()
        return {
            "limit_cny": self.limit_cny,
            "spent_cny": round(spent, 6),
            "remaining_cny": round(max(self.limit_cny - spent, 0.0), 6),
            "period_start": period_start,
            "period_days": PERIOD_DAYS,
        }

    def check(self, expected_cost: float = 0.0) -> None:
        """Raise BudgetExceededError when the next call would exceed the limit."""
        spent, period_raw = self._read()
        now = utc_now()
        period_start = period_raw or now.isoformat()
        try:
            started = parse_iso(period_start)
        except ValueError:
            started = now
        if started.tzinfo is None:
            from datetime import timezone

            started = started.replace(tzinfo=timezone.utc)
        if now - started > timedelta(days=PERIOD_DAYS):
            spent = 0.0
            period_start = now.isoformat()
        if self.limit_cny > 0 and spent + expected_cost > self.limit_cny:
            raise BudgetExceededError(
                "LLM budget exhausted",
                detail={
                    "limit_cny": self.limit_cny,
                    "spent_cny": round(spent, 6),
                    "expected_cost_cny": round(expected_cost, 6),
                },
            )
        self._write(spent, period_start)

    def record(self, cost_cny: float) -> None:
        spent, period_raw = self._read()
        period_start = period_raw or utc_now_iso()
        self._write(float(spent) + max(cost_cny, 0.0), period_start)
