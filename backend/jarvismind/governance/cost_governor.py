"""P1-C CostGovernor: one cost ledger, reusing the existing BudgetGuard.

``BudgetGuard`` already owns the rolling 30-day spend in ``system_settings``
(keys ``llm.spent_cny`` / ``llm.period_start``). P1 does NOT create a second
ledger. Task/batch/rate/token/call counters live in the SAME ``system_settings``
table under ``p1.cost.*`` keys and are checked before every background call.

Order of checks (all fail-closed when a limit is configured):

    1. circuit breaker (tripped by repeated anomalous calls)
    2. per-minute rate limit
    3. task-level CNY budget
    4. task-level token limit
    5. batch-level call limit
    6. task-level model-call limit
    7. global BudgetGuard.check(expected_cost)  <-- mandatory hard block

``record()`` updates the counters and forwards actual spend to
``BudgetGuard.record`` so the single ledger stays authoritative.
Complexity: O(1) settings reads/writes per call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy import text

from ..core.timeutils import utc_now_iso
from ..services.llm.budget import BudgetGuard
from .config import GovernanceConfig
from .errors import CostBlockedError

_SETTINGS_UPSERT = (
    "INSERT INTO system_settings(key, value, updated_at) VALUES (:k, :v, :u) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
    "updated_at=excluded.updated_at"
)


@dataclass(frozen=True)
class CostCheck:
    allowed: bool
    task_id: str
    run_id: str
    reason: str = ""
    snapshot: Optional[Dict[str, Any]] = None


class CostGovernor:
    def __init__(self, manager, settings, config: Optional[GovernanceConfig] = None) -> None:
        self.manager = manager
        self.settings = settings
        self.config = config or GovernanceConfig()
        # Reuse - not duplicate - the existing LLM spend ledger.
        self.budget = BudgetGuard(
            manager,
            limit_cny=float(getattr(settings, "llm_budget_cny", 0.0) or 0.0),
            price_per_1k_input_cny=float(getattr(settings, "llm_price_in", 0.0) or 0.0),
            price_per_1k_output_cny=float(getattr(settings, "llm_price_out", 0.0) or 0.0),
        )

    def _read(self, key: str, default: Dict[str, Any]) -> Dict[str, Any]:
        with self.manager.session() as session:
            raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
            ).scalar()
        if not raw:
            return dict(default)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return dict(default)
        return parsed if isinstance(parsed, dict) else dict(default)

    def _write(self, key: str, value: Dict[str, Any]) -> None:
        with self.manager.session() as session:
            session.execute(
                text(_SETTINGS_UPSERT),
                {
                    "k": key,
                    "v": json.dumps(value, ensure_ascii=False, sort_keys=True),
                    "u": utc_now_iso(),
                },
            )

    def _breaker(self) -> Dict[str, Any]:
        return self._read("p1.cost.breaker", {"tripped": False, "anomalies": 0, "reason": ""})

    def reservations(self) -> Dict[str, Any]:
        """Read-only snapshot of the shared cost reservation registry.

        Reservations are bookkeeping, not spend. They are written by the
        JMR-06 ``CostGovernance`` layer under the same ``p1.cost.*`` namespace
        and never create a second ledger.
        """
        raw = None
        with self.manager.session() as session:
            raw = session.execute(
                text(
                    "SELECT value FROM system_settings "
                    "WHERE key='p1.cost.reservations'"
                )
            ).scalar()
        if not raw:
            data: Dict[str, Any] = {}
        else:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        active = data.get("active") if isinstance(data.get("active"), dict) else {}
        settled = data.get("settled") if isinstance(data.get("settled"), dict) else {}
        released = data.get("released") if isinstance(data.get("released"), dict) else {}
        total = 0.0
        for item in active.values():
            if not isinstance(item, dict):
                continue
            try:
                total += max(float(item.get("expected_cost_cny", 0.0) or 0.0), 0.0)
            except (TypeError, ValueError):
                continue
        return {
            "active": len(active),
            "settled": len(settled),
            "released": len(released),
            "reserved_cny": round(total, 6),
        }

    def status(self) -> Dict[str, Any]:
        return {
            "global_budget": self.budget.status(),
            "breaker": self._breaker(),
            "reservations": self.reservations(),
            "config": {
                "task_budget_cny": self.config.task_budget_cny,
                "task_token_limit": self.config.task_token_limit,
                "task_call_limit": self.config.task_call_limit,
                "batch_call_limit": self.config.batch_call_limit,
                "rate_limit_per_minute": self.config.rate_limit_per_minute,
                "breaker_threshold": self.config.breaker_threshold,
            },
        }

    def check(
        self,
        *,
        task_id: str,
        run_id: str = "",
        expected_cost_cny: float = 0.0,
        expected_tokens: int = 0,
        model_calls: int = 1,
    ) -> CostCheck:
        breaker = self._breaker()
        if breaker.get("tripped"):
            return CostCheck(False, task_id, run_id, "circuit_breaker_tripped", breaker)

        minute = utc_now_iso()[:16]  # YYYY-MM-DDTHH:MM, UTC
        rate_key = f"p1.cost.rate.{task_id}"
        rate = self._read(rate_key, {"minute": minute, "count": 0})
        if rate.get("minute") != minute:
            rate = {"minute": minute, "count": 0}
        rate_limit = self.config.rate_limit_per_minute
        if rate_limit > 0 and int(rate.get("count", 0)) + model_calls > rate_limit:
            return CostCheck(False, task_id, run_id, "rate_limit_exceeded", rate)

        task_key = f"p1.cost.task.{task_id}"
        task = self._read(
            task_key, {"cost_cny": 0.0, "tokens": 0, "calls": 0}
        )
        cost = float(task.get("cost_cny", 0.0))
        tokens = int(task.get("tokens", 0))
        calls = int(task.get("calls", 0))
        if self.config.task_budget_cny > 0 and cost + expected_cost_cny > self.config.task_budget_cny:
            return CostCheck(False, task_id, run_id, "task_budget_exhausted", task)
        if self.config.task_token_limit > 0 and tokens + expected_tokens > self.config.task_token_limit:
            return CostCheck(False, task_id, run_id, "task_token_limit_exceeded", task)
        if self.config.task_call_limit > 0 and calls + model_calls > self.config.task_call_limit:
            return CostCheck(False, task_id, run_id, "task_call_limit_exceeded", task)

        batch_key = f"p1.cost.batch.{run_id or task_id}"
        batch = self._read(batch_key, {"calls": 0})
        batch_calls = int(batch.get("calls", 0))
        if self.config.batch_call_limit > 0 and batch_calls + model_calls > self.config.batch_call_limit:
            return CostCheck(False, task_id, run_id, "batch_limit_exceeded", batch)

        # The existing global ledger is the last and mandatory checkpoint.
        try:
            self.budget.check(expected_cost_cny)
        except Exception as exc:
            return CostCheck(False, task_id, run_id, f"global_budget:{type(exc).__name__}", self.budget.status())

        return CostCheck(
            True,
            task_id,
            run_id,
            "allowed",
            {"task": task, "batch": batch, "rate": rate, "global": self.budget.status()},
        )

    def enforce(self, **kwargs) -> CostCheck:
        """check() that raises on a block - use this on real background calls."""
        result = self.check(**kwargs)
        if not result.allowed:
            raise CostBlockedError(
                "CostGovernor blocked a background call",
                detail={
                    "code": "cost_governance_blocked",
                    "reason": result.reason,
                    "task_id": result.task_id,
                    "run_id": result.run_id,
                },
            )
        return result

    def record(
        self,
        *,
        task_id: str,
        run_id: str = "",
        cost_cny: float = 0.0,
        tokens: int = 0,
        model_calls: int = 1,
    ) -> Dict[str, Any]:
        task_key = f"p1.cost.task.{task_id}"
        task = self._read(task_key, {"cost_cny": 0.0, "tokens": 0, "calls": 0})
        task["cost_cny"] = float(task.get("cost_cny", 0.0)) + max(cost_cny, 0.0)
        task["tokens"] = int(task.get("tokens", 0)) + max(tokens, 0)
        task["calls"] = int(task.get("calls", 0)) + max(model_calls, 0)
        self._write(task_key, task)

        batch_key = f"p1.cost.batch.{run_id or task_id}"
        batch = self._read(batch_key, {"calls": 0})
        batch["calls"] = int(batch.get("calls", 0)) + max(model_calls, 0)
        self._write(batch_key, batch)

        minute = utc_now_iso()[:16]
        rate_key = f"p1.cost.rate.{task_id}"
        rate = self._read(rate_key, {"minute": minute, "count": 0})
        if rate.get("minute") != minute:
            rate = {"minute": minute, "count": 0}
        rate["count"] = int(rate.get("count", 0)) + max(model_calls, 0)
        self._write(rate_key, rate)

        # Forward actual spend to the ONE existing ledger.
        if cost_cny > 0:
            self.budget.record(cost_cny)
        return {"task": task, "batch": batch, "rate": rate, "global": self.budget.status()}

    def anomaly(self, reason: str) -> Dict[str, Any]:
        """Count an abnormal call; trip the breaker at the configured threshold."""
        breaker = self._breaker()
        breaker["anomalies"] = int(breaker.get("anomalies", 0)) + 1
        breaker["last_reason"] = reason
        threshold = max(self.config.breaker_threshold, 1)
        if breaker["anomalies"] >= threshold:
            breaker["tripped"] = True
            breaker["reason"] = reason
            breaker["tripped_at"] = utc_now_iso()
        self._write("p1.cost.breaker", breaker)
        return breaker

    def trip(self, reason: str) -> Dict[str, Any]:
        breaker = self._breaker()
        breaker.update(
            {"tripped": True, "reason": reason, "tripped_at": utc_now_iso()}
        )
        self._write("p1.cost.breaker", breaker)
        return breaker

    def reset_breaker(self) -> Dict[str, Any]:
        breaker = {"tripped": False, "anomalies": 0, "reason": ""}
        self._write("p1.cost.breaker", breaker)
        return breaker
