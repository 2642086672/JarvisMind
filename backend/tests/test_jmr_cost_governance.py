"""JMR-06 persistent cost-governance tests.

Covers the atomic reservation ledger, budget dims, estimates, settlement,
release/expiry, audit, reporting, degradation, the JMR service wrapper and
JMR-05 scheduler integration. Every database is the isolated ``manager``
fixture; no production database is touched and no DDL is added.
"""
from __future__ import annotations

import json
import threading
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from jarvismind.core.security_gate import CredentialBlockedError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.migrations import MIGRATIONS
from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.cost_governor import CostGovernor
from jarvismind.governance.schema import install_p1_schema
from jarvismind.governance.task_registry import (
    CREATED,
    FAILED,
    PAUSED,
    SUCCEEDED,
    TaskRegistry,
)
from jarvismind.jmr.config import JMRConfig
from jarvismind.jmr.cost_governance import (
    CostGovernance,
    CostGovernanceConfig,
    CostGovernedJMRService,
    build_cost_governed_jmr_service,
    DEGRADE,
    OBSERVE,
    REASON_BREAKER,
    REASON_DAILY,
    REASON_DEGRADE_EXHAUSTED,
    REASON_RUN,
    REASON_TASK,
)
from jarvismind.jmr.cost_policy import ALLOW, DENY, HOLD, INVALID_INPUT, UNCONFIGURED
from jarvismind.jmr.scheduler_engine import SchedulerConfig, SchedulerEngine
from jarvismind.jmr.service import JMRService, TASK_TYPE

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, 5.0, 0.9, 'ACTIVE', "
    ":now, :now, NULL, NULL, :search_text)"
)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _cfg(**overrides) -> CostGovernanceConfig:
    base = dict(
        enforcement="auto",
        daily_budget_cny=0.0,
        monthly_budget_cny=0.0,
        task_budget_cny=0.0,
        run_budget_cny=0.0,
        global_budget_cny=0.0,
        expected_cost_cny=0.0,
        expected_tokens=0,
        manage_period_spend=False,
    )
    base.update(overrides)
    return CostGovernanceConfig(**base)


def _gov(manager, *, clock=None, **overrides) -> CostGovernance:
    return CostGovernance(manager, _cfg(**overrides), clock=clock)


def _write(manager, key: str, value) -> None:
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES (:k, :v, :u) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at"
            ),
            {"k": key, "v": payload, "u": utc_now_iso()},
        )


def _read(manager, key: str):
    with manager.session() as session:
        raw = session.execute(
            text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
        ).scalar()
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _audit_rows(manager):
    return AuditBus(manager).p1_entries(limit=2000)


def _task_key(task_id: str) -> str:
    return "p1.cost.task." + task_id


def _run_key(run_id: str) -> str:
    return "p1.cost.batch." + run_id


def _seed_memory(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "memory_type": "JMR_COST",
                "content": content,
                "now": utc_now_iso(),
                "search_text": content,
            },
        )


def _derived_count(manager) -> int:
    with manager.session() as session:
        return int(
            session.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM memory_merges) + "
                    "(SELECT COUNT(*) FROM memory_corrections) + "
                    "(SELECT COUNT(*) FROM memory_summaries)"
                )
            ).scalar()
            or 0
        )


@dataclass
class FakeRunResult:
    """Run result shape used by the fake service.

    Declared locally: the committed JMRRunResult has no ``cost_cny`` field, and
    these tests must not depend on uncommitted JMR-04/07 service code.
    """

    task_id: str
    run_id: str
    status: str
    failure_reason: str = ""
    cost_cny: float = 0.0


@dataclass
class FakeConfig:
    expected_cost_cny: float = 0.1
    expected_tokens: int = 100
    max_memories: int = 100
    model_calls_per_stage: int = 1


class FakeInner:
    def __init__(self, manager) -> None:
        self.manager = manager
        self.task_registry = TaskRegistry(manager)
        self.config = FakeConfig()
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.fail_run = False
        self.cost_cny = 0.3

    def _require_p1_schema(self):
        return None

    def run(self, *, task_id, run_id="", memory_type=None, resume=False):
        task = self.task_registry.get(task_id)
        if task is None:
            self.task_registry.create(task_id, TASK_TYPE, owner="fake")
            task = self.task_registry.get(task_id)
        status = task["status"]
        if status == FAILED and resume:
            self.task_registry.recover(task_id)
            status = CREATED
        if status == PAUSED and resume:
            self.task_registry.resume(task_id)
        if status == CREATED:
            self.task_registry.start(task_id)
        self.calls.append(
            {
                "task_id": task_id,
                "run_id": run_id,
                "resume": resume,
                "expected_cost_cny": self.config.expected_cost_cny,
                "expected_tokens": self.config.expected_tokens,
                "max_memories": self.config.max_memories,
            }
        )
        if self.error is not None:
            raise self.error
        if self.fail_run:
            self.task_registry.fail(task_id, reason="RuntimeError: fake fail")
            # The real service records stage cost before a later stage fails.
            self._record_period(self.cost_cny)
            return FakeRunResult(
                task_id=task_id,
                run_id=run_id,
                status=FAILED,
                failure_reason="RuntimeError: fake fail",
                cost_cny=self.cost_cny,
            )
        self.task_registry.succeed(task_id)
        self._record_period(self.cost_cny)
        return FakeRunResult(
            task_id=task_id,
            run_id=run_id,
            status=SUCCEEDED,
            cost_cny=self.cost_cny,
        )

    def _record_period(self, cost: float) -> None:
        """Mirror JMRService: the service, not the wrapper, owns period spend."""
        if cost <= 0:
            return
        now = utc_now_iso()
        for key in (
            "p1.cost.daily." + now[:10],
            "p1.cost.monthly." + now[:7],
        ):
            current = _read(self.manager, key) or 0.0
            _write(self.manager, key, round(float(current) + float(cost), 6))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_default_config_is_observe_not_unlimited():
    cfg = _cfg()
    assert cfg.configured() is False
    assert cfg.budgets() == {
        "global": 0.0,
        "daily": 0.0,
        "monthly": 0.0,
        "task": 0.0,
        "run": 0.0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_budget_cny": -1},
        {"monthly_budget_cny": float("nan")},
        {"enforcement": "sometimes"},
        {"degrade_threshold_ratio": 1.5},
        {"degrade_reduce_ratio": 0},
        {"reservation_ttl_seconds": 0},
        {"model_prices": {"m": {"input": -1}}},
        {"fallback_models": {"a": "a"}},
        {"fallback_models": {"a": "b", "b": "a"}},
    ],
)
def test_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        _cfg(**kwargs)


def test_config_from_sources_reads_existing_config():
    jmr = types.SimpleNamespace(
        expected_cost_cny=0.02,
        expected_tokens=500,
        daily_budget_cny=1.5,
        monthly_budget_cny=10.0,
        max_single_call_cny=0.5,
        breaker_threshold=4,
    )
    governor = types.SimpleNamespace(
        config=types.SimpleNamespace(
            task_budget_cny=2.0,
            task_token_limit=1000,
            task_call_limit=5,
            rate_limit_per_minute=2,
            breaker_threshold=3,
        )
    )
    settings = types.SimpleNamespace(
        llm_budget_cny=20.0, llm_price_in=1.0, llm_price_out=2.0
    )
    cfg = CostGovernanceConfig.from_sources(settings, jmr, governor)
    assert cfg.daily_budget_cny == 1.5
    assert cfg.monthly_budget_cny == 10.0
    assert cfg.task_budget_cny == 2.0
    assert cfg.global_budget_cny == 20.0
    assert cfg.expected_tokens == 500
    assert cfg.expected_cost_cny == 0.02
    assert cfg.price_in_per_1k_cny == 1.0
    assert cfg.breaker_threshold == 4


def test_config_manage_period_spend_is_opt_in(monkeypatch):
    """JMR-08 audit P1-5: the service owns period spend; the wrapper must not
    add it again unless the operator explicitly opts in."""
    settings = types.SimpleNamespace(
        llm_budget_cny=0.0, llm_price_in=0.0, llm_price_out=0.0
    )
    monkeypatch.delenv("JARVISMIND_JMR_COST_MANAGE_PERIOD_SPEND", raising=False)
    assert CostGovernanceConfig.from_sources(settings).manage_period_spend is False
    monkeypatch.setenv("JARVISMIND_JMR_COST_MANAGE_PERIOD_SPEND", "1")
    assert CostGovernanceConfig.from_sources(settings).manage_period_spend is True


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------
def test_estimate_uses_model_price_book(manager):
    gov = _gov(manager, model_prices={"m1": {"input": 1.0, "output": 2.0}})
    est = gov.estimate(input_tokens=1000, output_tokens=2000, model="m1")
    assert est.status == ALLOW
    assert est.cost_cny == Decimal("5.000000")
    assert est.source == "model_price_book"


def test_unknown_model_is_not_free(manager):
    gov = _gov(manager, model_prices={"m1": {"input": 1.0, "output": 1.0}})
    est = gov.estimate(input_tokens=1000, model="unknown")
    assert est.status == HOLD
    assert est.reason == "unknown_model"
    assert est.cost_cny is None


def test_missing_price_is_not_free(manager):
    gov = _gov(manager)
    est = gov.estimate(output_tokens=1000)
    assert est.status == HOLD
    assert est.reason == "price_unconfigured"
    assert est.cost_cny is None


def test_configured_expected_cost_is_used(manager):
    gov = _gov(manager, expected_cost_cny=0.25)
    est = gov.estimate()
    assert est.status == ALLOW
    assert est.cost_cny == Decimal("0.250000")
    assert est.source == "configured_expected_cost"


def test_zero_cost_is_declared_not_unknown(manager):
    gov = _gov(manager)
    est = gov.estimate()
    assert est.status == ALLOW
    assert est.cost_cny == Decimal("0")
    assert est.reason == "zero_cost_declared"


@pytest.mark.parametrize("value", [-1, "-2", float("nan"), float("inf"), True])
def test_invalid_estimate_input_is_invalid(manager, value):
    gov = _gov(manager, expected_cost_cny=0.1)
    est = gov.estimate(expected_cost_cny=value)
    assert est.status == INVALID_INPUT
    assert est.cost_cny is None


def test_estimate_precision(manager):
    gov = _gov(manager, price_in_per_1k_cny=0.001, price_out_per_1k_cny=0.002)
    est = gov.estimate(input_tokens=333, output_tokens=777)
    assert est.cost_cny == Decimal("0.001887")


# ---------------------------------------------------------------------------
# Reservations and budgets
# ---------------------------------------------------------------------------
def test_reserve_allows_and_persists(manager):
    gov = _gov(manager, daily_budget_cny=1.0, expected_cost_cny=0.1)
    decision = gov.reserve_run(
        task_id="T-1", run_id="R-1", expected_cost_cny=0.1, model="m", provider="p"
    )
    assert decision.allowed is True
    assert decision.status == ALLOW
    assert decision.reservation_id.startswith("rc-")
    assert len(gov.active_reservations()) == 1
    assert _read(manager, "p1.cost.reservations")["active"]


def test_daily_budget_denies_without_reservation(manager):
    gov = _gov(manager, daily_budget_cny=0.01)
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    assert decision.allowed is False
    assert decision.status == DENY
    assert REASON_DAILY in decision.reason_codes
    assert decision.reservation_id == ""
    assert gov.active_reservations() == []


def test_budget_boundary_equal_is_allowed_and_second_is_denied(manager):
    gov = _gov(manager, daily_budget_cny=0.1)
    first = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    second = gov.reserve_run(task_id="T-2", run_id="R-2", expected_cost_cny=0.1)
    assert first.allowed is True
    assert second.allowed is False
    assert REASON_DAILY in second.reason_codes


def test_active_reservation_is_reused_for_same_task_and_run(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    first = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    second = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    assert first.reservation_id == second.reservation_id
    assert len(gov.active_reservations()) == 1


def test_expired_reservation_is_released(manager):
    clock = MutableClock(datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc))
    gov = _gov(
        manager,
        clock=clock,
        daily_budget_cny=0.1,
        reservation_ttl_seconds=10,
    )
    first = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    assert first.allowed is True
    clock.advance(11)
    second = gov.reserve_run(task_id="T-2", run_id="R-2", expected_cost_cny=0.1)
    assert second.allowed is True
    assert len(gov.active_reservations()) == 1


def test_task_and_run_budget_deny(manager):
    _write(manager, _task_key("T-TASK"), {"cost_cny": 0.9, "tokens": 0, "calls": 0})
    _write(manager, _run_key("R-RUN"), {"cost_cny": 0.9, "calls": 0})
    task_gov = _gov(manager, task_budget_cny=1.0)
    task_decision = task_gov.reserve_run(
        task_id="T-TASK", run_id="R-TASK", expected_cost_cny=0.3
    )
    assert task_decision.allowed is False
    assert REASON_TASK in task_decision.reason_codes

    run_gov = _gov(manager, run_budget_cny=1.0)
    run_decision = run_gov.reserve_run(
        task_id="T-RUN", run_id="R-RUN", expected_cost_cny=0.3
    )
    assert run_decision.allowed is False
    assert REASON_RUN in run_decision.reason_codes


def test_global_budget_denies(manager):
    gov = _gov(manager, global_budget_cny=0.05)
    decision = gov.reserve_run(task_id="T-1", expected_cost_cny=0.2)
    assert decision.allowed is False
    assert "global_budget_exceeded" in decision.reason_codes


def test_breaker_denies(manager):
    _write(manager, "p1.cost.breaker", {"tripped": True, "reason": "test"})
    gov = _gov(manager, daily_budget_cny=10.0)
    decision = gov.reserve_run(task_id="T-1", expected_cost_cny=0.1)
    assert decision.allowed is False
    assert REASON_BREAKER in decision.reason_codes


def test_rate_and_call_limits(manager):
    _write(
        manager,
        "p1.cost.rate.T-RATE",
        {"minute": utc_now_iso()[:16], "count": 2},
    )
    gov = _gov(manager, rate_limit_per_minute=2, task_call_limit=2)
    decision = gov.reserve_run(task_id="T-RATE", expected_calls=1, expected_cost_cny=0.0)
    assert decision.allowed is False
    assert "rate_limit_exceeded" in decision.reason_codes or "task_call_limit_exceeded" in decision.reason_codes


def test_observe_mode_does_not_reserve(manager):
    gov = _gov(manager)
    decision = gov.reserve_run(task_id="T-1", expected_cost_cny=0.0)
    assert decision.allowed is True
    assert decision.status == OBSERVE
    assert decision.reservation_id == ""
    assert gov.active_reservations() == []


def test_configured_but_unknown_price_holds(manager):
    gov = _gov(manager, daily_budget_cny=1.0, expected_tokens=1000)
    decision = gov.reserve_run(
        task_id="T-1",
        expected_cost_cny=None,
        expected_tokens=1000,
    )
    assert decision.allowed is False
    assert decision.status == HOLD
    assert "price_unconfigured" in decision.reason_codes


# JMR-JKG-AUDIT-FIX-01 C-2: characterization of ``enforcement="on"`` when no
# budget is configured. A zero budget is a dimension that is not enforced: it is
# reported UNCONFIGURED and never denies. Only an unknown price (HOLD), a
# configured limit or an open breaker blocks admission (JMR-06 report 19.2).
def test_enforcement_on_with_all_budgets_zero_admits_and_labels_unconfigured(manager):
    gov = _gov(manager, enforcement="on")
    assert gov.should_govern() is True
    decision = gov.reserve_run(task_id="T-1", expected_cost_cny=0.0)
    assert decision.allowed is True
    assert decision.status == ALLOW
    assert decision.reservation_id
    assert set(decision.budgets) == {"global", "daily", "monthly", "task", "run"}
    for name, view in decision.budgets.items():
        assert view["status"] == UNCONFIGURED
        assert view["reason"] == f"{name}_budget_unconfigured"
        assert view["remaining_cny"] is None


def test_enforcement_on_with_all_budgets_zero_admits_priced_spend(manager):
    gov = _gov(
        manager,
        enforcement="on",
        price_in_per_1k_cny=1.0,
        price_out_per_1k_cny=2.0,
        expected_tokens=1000,
    )
    decision = gov.reserve_run(task_id="T-1", expected_tokens=1000)
    assert decision.allowed is True
    assert decision.status == ALLOW
    assert decision.estimate.cost_cny is not None
    assert decision.estimate.cost_cny > 0
    assert all(v["status"] == UNCONFIGURED for v in decision.budgets.values())


def test_enforcement_on_with_all_budgets_zero_still_holds_unknown_price(manager):
    gov = _gov(manager, enforcement="on", expected_tokens=1000)
    decision = gov.reserve_run(
        task_id="T-1", expected_cost_cny=None, expected_tokens=1000
    )
    assert decision.allowed is False
    assert decision.status == HOLD
    assert "price_unconfigured" in decision.reason_codes


def test_period_boundaries_reset_daily_and_monthly(manager):
    clock = MutableClock(datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))
    gov = _gov(
        manager,
        clock=clock,
        daily_budget_cny=0.5,
        monthly_budget_cny=1.0,
    )
    first = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.4)
    second = gov.reserve_run(task_id="T-2", run_id="R-2", expected_cost_cny=0.4)
    assert first.allowed is True
    assert second.allowed is False  # monthly budget would be exceeded

    clock.advance(120)  # April 1st, 00:01
    third = gov.reserve_run(task_id="T-3", run_id="R-3", expected_cost_cny=0.4)
    assert third.allowed is True  # new day and new month


def test_concurrent_reservations_do_not_overspend(manager):
    config = _cfg(daily_budget_cny=1.0, reservation_ttl_seconds=60)
    results: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        gov = CostGovernance(manager, config)
        try:
            barrier.wait(timeout=10)
            decision = gov.reserve_run(
                task_id=f"T-CONC-{index}",
                run_id=f"R-CONC-{index}",
                expected_cost_cny=0.3,
            )
            results.append("ALLOW" if decision.allowed else "DENY")
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(f"{type(exc).__name__}:{exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert len(results) == 8
    assert results.count("ALLOW") == 3
    assert results.count("DENY") == 5
    active = CostGovernance(manager, config).active_reservations()
    assert len(active) == 3
    assert sum(Decimal(item["expected_cost_cny"]) for item in active) == Decimal("0.9")


# ---------------------------------------------------------------------------
# Settlement, release and audit
# ---------------------------------------------------------------------------
def test_settle_records_actual_and_run_ledger(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    settlement = gov.settle_run(
        decision.reservation_id,
        actual_cost_cny=0.15,
        actual_tokens=80,
        actual_calls=1,
        status=SUCCEEDED,
    )
    assert settlement.settled is True
    assert settlement.already_settled is False
    assert settlement.actual_cny == "0.150000"
    assert gov.active_reservations() == []
    assert _read(manager, _run_key("R-1"))["cost_cny"] == 0.15
    assert _read(manager, _run_key("R-1"))["tokens"] == 80
    rows = [r for r in _audit_rows(manager) if r["action"].startswith("P1:JMR_COST_SETTLE")]
    assert len(rows) == 1
    assert rows[0]["detail"]["extra"]["actual_cost_cny"] == "0.150000"
    assert rows[0]["detail"]["extra"]["actual_tokens_source"] == "caller"


def test_settle_is_idempotent_and_does_not_double_count(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    first = gov.settle_run(decision.reservation_id, actual_cost_cny=0.15)
    second = gov.settle_run(decision.reservation_id, actual_cost_cny=0.15)
    assert first.settled is True
    assert second.already_settled is True
    assert _read(manager, _run_key("R-1"))["cost_cny"] == 0.15
    assert len([r for r in _audit_rows(manager) if "JMR_COST_SETTLE" in r["action"]]) == 1


def test_settle_overage_is_audited(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    settlement = gov.settle_run(decision.reservation_id, actual_cost_cny=0.25)
    assert settlement.overage_cny == "0.150000"
    assert settlement.reason == "actual_cost_exceeded_reservation"
    rows = [r for r in _audit_rows(manager) if "JMR_COST_SETTLE" in r["action"]]
    assert rows and rows[0]["detail"]["extra"]["overage"] is True


def test_settle_unknown_reservation_is_reported(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    settlement = gov.settle_run("rc-missing", actual_cost_cny=0.1)
    assert settlement.settled is False
    assert settlement.reason == "reservation_not_found"
    rows = [r for r in _audit_rows(manager) if "JMR_COST_SETTLE" in r["action"]]
    assert rows and rows[0]["action"].endswith(":FAILED")


def test_release_is_idempotent_and_frees_budget(manager):
    gov = _gov(manager, daily_budget_cny=0.1)
    first = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    assert first.allowed is True
    assert gov.release_run(first.reservation_id, reason="cancelled") is True
    assert gov.release_run(first.reservation_id, reason="cancelled") is True
    assert gov.active_reservations() == []
    second = gov.reserve_run(task_id="T-2", run_id="R-2", expected_cost_cny=0.1)
    assert second.allowed is True
    assert gov.settle_run(first.reservation_id, actual_cost_cny=0.0).settled is False


def test_settlement_survives_new_governance_instance(manager):
    config = _cfg(daily_budget_cny=1.0)
    first = CostGovernance(manager, config)
    decision = first.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    restarted = CostGovernance(manager, config)
    assert len(restarted.active_reservations()) == 1
    settlement = restarted.settle_run(
        decision.reservation_id, actual_cost_cny=0.2, status=SUCCEEDED
    )
    assert settlement.settled is True
    assert restarted.active_reservations() == []


def test_settlement_never_writes_period_spend(manager):
    """Re-audit N-P2-2: period spend belongs to the service; the wrapper must
    not add it again even when manage_period_spend=True (legacy flag)."""
    clock = MutableClock(datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc))
    gov = _gov(
        manager,
        clock=clock,
        daily_budget_cny=1.0,
        monthly_budget_cny=2.0,
        manage_period_spend=True,
    )
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.2)
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.2)
    assert _read(manager, "p1.cost.daily.2026-06-15") is None
    assert _read(manager, "p1.cost.monthly.2026-06") is None


def test_task_and_run_status_queries(manager):
    _write(manager, _task_key("T-Q"), {"cost_cny": 0.2, "tokens": 10, "calls": 1})
    gov = _gov(manager, task_budget_cny=1.0, run_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-Q", run_id="R-Q", expected_cost_cny=0.1)
    task_view = gov.task_status("T-Q")
    assert task_view["spent_cny"] == 0.2
    assert task_view["reserved_cny"] == 0.1
    assert task_view["budget"]["status"] == ALLOW
    run_view = gov.run_status("R-Q")
    assert run_view["spent_cny"] == 0.0
    assert run_view["reserved_cny"] == 0.1
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.1)
    assert gov.task_status("T-Q")["reserved_cny"] == 0.0
    assert gov.run_status("R-Q")["spent_cny"] == 0.1


def test_settle_does_not_double_count_task_spend(manager):
    _write(manager, _task_key("T-1"), {"cost_cny": 0.2, "tokens": 10, "calls": 1})
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-1", run_id="R-1", expected_cost_cny=0.1)
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.1)
    assert _read(manager, _task_key("T-1"))["cost_cny"] == 0.2
    assert _read(manager, _run_key("R-1"))["cost_cny"] == 0.1


def test_audit_has_no_memory_text_or_api_key(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(
        task_id="T-1",
        run_id="R-1",
        expected_cost_cny=0.1,
        model="m1",
        provider="p1",
    )
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.1)
    serialised = json.dumps(_audit_rows(manager), ensure_ascii=False)
    assert "SECRET_MEMORY_TEXT" not in serialised
    assert "sk-" not in serialised
    assert "api_key" not in serialised.lower()
    assert "token" in serialised.lower()  # token counts/fields are allowed


def test_denied_decision_is_audited(manager):
    gov = _gov(manager, daily_budget_cny=0.01)
    gov.reserve_run(task_id="T-DENY", run_id="R-DENY", expected_cost_cny=1.0)
    rows = [r for r in _audit_rows(manager) if "JMR_COST_RESERVE" in r["action"]]
    assert rows
    assert rows[0]["action"].endswith(":DENIED")
    assert "daily_budget_exceeded" in rows[0]["detail"]["failure_reason"]


def test_report_aggregates_by_model_provider_and_rejections(manager):
    gov = _gov(manager, daily_budget_cny=10.0, reservation_ttl_seconds=60)
    first = gov.reserve_run(
        task_id="T-1", run_id="R-1", expected_cost_cny=0.2,
        model="m1", provider="p1", task_type="JMR_REFLECTION",
    )
    gov.settle_run(first.reservation_id, actual_cost_cny=0.15, status=SUCCEEDED)
    second = gov.reserve_run(
        task_id="T-2", run_id="R-2", expected_cost_cny=0.2,
        model="m2", provider="p2", task_type="JMR_REFLECTION",
    )
    gov.settle_run(second.reservation_id, actual_cost_cny=0.25, status=FAILED)
    third = gov.reserve_run(task_id="T-3", run_id="R-3", expected_cost_cny=20.2)
    assert third.allowed is False

    report = gov.report(days=365)
    assert report["settled_count"] == 2
    assert report["by_model"]["m1"]["cost_cny"] == 0.15
    assert report["by_model"]["m2"]["cost_cny"] == 0.25
    assert report["by_provider"]["p1"]["settled"] == 1
    assert report["rejection_count"] >= 1
    assert report["denied_count"] >= 1
    assert any("daily_budget_exceeded" in key for key in report["rejections"])
    assert report["total_overage_cny"] == 0.05
    assert report["anomalies"]
    assert report["anomalies"][-1]["overage_cny"] == "0.050000"
    assert report["trend"]


def test_breaker_reset_requires_explicit_confirmation(manager):
    _write(manager, "p1.cost.breaker", {"tripped": True, "reason": "test"})
    gov = _gov(manager, daily_budget_cny=1.0)
    with pytest.raises(Exception):
        gov.reset_breaker()
    result = gov.reset_breaker(operator_confirmed=True)
    assert result["tripped"] is False
    assert _read(manager, "p1.cost.breaker")["tripped"] is False
    rows = [r for r in _audit_rows(manager) if "JMR_COST_BREAKER_RESET" in r["action"]]
    assert rows


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------
def test_degradation_is_applied_when_reduced_cost_fits(manager):
    gov = _gov(
        manager,
        daily_budget_cny=0.1,
        degrade_reduce_ratio=0.5,
    )
    decision = gov.reserve_run(
        task_id="T-DEG", run_id="R-DEG", expected_cost_cny=0.15
    )
    assert decision.allowed is True
    assert decision.status == DEGRADE
    assert decision.degradation is not None
    assert decision.degradation.suggested is True
    assert decision.degradation.reduce_ratio == 0.5
    assert decision.degradation.reduced_cost_cny == Decimal("0.075000")
    assert decision.degradation.security_bypass is False
    assert decision.degradation.privacy_bypass is False
    assert decision.degradation.task_state_bypass is False
    reservation = gov.active_reservations()[0]
    assert reservation["expected_cost_cny"] == "0.075000"


def test_degradation_exhausted_denies(manager):
    gov = _gov(manager, daily_budget_cny=0.05, degrade_reduce_ratio=0.5)
    decision = gov.reserve_run(
        task_id="T-DEG", run_id="R-DEG", expected_cost_cny=0.3
    )
    assert decision.allowed is False
    assert decision.status == DENY
    assert REASON_DEGRADE_EXHAUSTED in decision.reason_codes
    assert decision.reservation_id == ""


def test_degradation_is_suggested_by_threshold(manager):
    gov = _gov(
        manager,
        daily_budget_cny=1.0,
        degrade_threshold_ratio=0.5,
    )
    decision = gov.reserve_run(
        task_id="T-DEG", run_id="R-DEG", expected_cost_cny=0.6
    )
    assert decision.allowed is True
    assert decision.status == ALLOW
    assert decision.degradation is not None
    assert decision.degradation.suggested is True
    assert decision.degradation.applied is False
    assert decision.degradation.reason == "budget_usage_threshold"


def test_fallback_model_is_plan_only(manager):
    gov = _gov(
        manager,
        daily_budget_cny=0.1,
        degrade_reduce_ratio=0.5,
        fallback_models={"expensive": "cheap"},
        model_prices={
            "expensive": {"input": 1.0, "output": 1.0},
            "cheap": {"input": 0.1, "output": 0.1},
        },
    )
    decision = gov.reserve_run(
        task_id="T-DEG",
        run_id="R-DEG",
        expected_cost_cny=0.15,
        model="expensive",
    )
    assert decision.allowed is True
    assert decision.degradation is not None
    assert decision.degradation.fallback_model == "cheap"
    assert decision.degradation.fallback_configured is True
    # The plan never switches providers or bypasses security.
    assert decision.degradation.security_bypass is False


# ---------------------------------------------------------------------------
# Service wrapper
# ---------------------------------------------------------------------------
@pytest.fixture()
def p1_manager(manager):
    install_p1_schema(manager)
    return manager


def test_wrapper_observe_delegates_unchanged(p1_manager):
    inner = FakeInner(p1_manager)
    gov = _gov(p1_manager)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-OBS", run_id="R-OBS")
    assert result.status == SUCCEEDED
    assert len(inner.calls) == 1
    assert gov.active_reservations() == []
    assert _audit_rows(p1_manager) == []


def test_wrapper_denies_before_calling_inner(p1_manager):
    inner = FakeInner(p1_manager)
    gov = _gov(p1_manager, daily_budget_cny=0.01)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-DENY", run_id="R-DENY")
    assert result.status == "BLOCKED"
    assert "cost_governance_blocked" in result.failure_reason
    assert inner.calls == []
    assert inner.task_registry.get("T-DENY")["status"] == FAILED


def test_wrapper_allows_and_settles(p1_manager):
    inner = FakeInner(p1_manager)
    inner.cost_cny = 0.3
    gov = _gov(p1_manager, daily_budget_cny=1.0)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-ALLOW", run_id="R-ALLOW")
    assert result.status == SUCCEEDED
    assert len(inner.calls) == 1
    assert gov.active_reservations() == []
    assert _read(p1_manager, _run_key("R-ALLOW"))["cost_cny"] == 0.3
    rows = [r for r in _audit_rows(p1_manager) if "JMR_COST_SETTLE" in r["action"]]
    assert rows
    assert rows[0]["detail"]["extra"]["actual_tokens_source"] == "unavailable"


def test_wrapper_releases_reservation_on_exception(p1_manager):
    inner = FakeInner(p1_manager)
    inner.error = RuntimeError("boom")
    gov = _gov(p1_manager, daily_budget_cny=1.0)
    wrapper = CostGovernedJMRService(inner, gov)
    with pytest.raises(RuntimeError):
        wrapper.run(task_id="T-EXC", run_id="R-EXC")
    assert gov.active_reservations() == []
    rows = [r for r in _audit_rows(p1_manager) if "JMR_COST_RELEASE" in r["action"]]
    assert rows


def test_wrapper_applies_and_restores_degradation_config(p1_manager):
    inner = FakeInner(p1_manager)
    inner.cost_cny = 0.15
    gov = _gov(p1_manager, daily_budget_cny=0.15, degrade_reduce_ratio=0.5)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-DEG", run_id="R-DEG")
    assert result.status == SUCCEEDED
    assert inner.calls[0]["expected_cost_cny"] == 0.05
    assert inner.calls[0]["expected_tokens"] == 50
    assert inner.calls[0]["max_memories"] == 50
    # The wrapper restores the inner config after the run.
    assert inner.config.expected_cost_cny == 0.1
    assert inner.config.expected_tokens == 100
    assert inner.config.max_memories == 100
    rows = [r for r in _audit_rows(p1_manager) if "JMR_COST_DEGRADE" in r["action"]]
    assert rows and rows[0]["detail"]["extra"]["applied"] is True


def test_wrapper_resume_on_succeeded_task_is_governed(p1_manager):
    inner = FakeInner(p1_manager)
    inner.task_registry.create("T-SUCC", TASK_TYPE, owner="test")
    inner.task_registry.start("T-SUCC")
    inner.task_registry.succeed("T-SUCC")
    gov = _gov(p1_manager, daily_budget_cny=0.01)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-SUCC", run_id="R-SUCC", resume=True)
    assert result.status == "BLOCKED"
    assert inner.calls == []


def test_wrapper_real_service_denies_before_any_write(p1_manager):
    _seed_memory(p1_manager, "M-COST-A", "Bifrost 网关采用 PostgreSQL 主从架构")
    _seed_memory(p1_manager, "M-COST-B", "Bifrost 网关采用 PostgreSQL 主从架构，并计划迁移")
    service = JMRService.build(
        manager=p1_manager,
        config=JMRConfig(expected_cost_cny=0.02),
    )
    gov = CostGovernance(p1_manager, _cfg(daily_budget_cny=0.01))
    wrapper = CostGovernedJMRService(service, gov)
    result = wrapper.run(task_id="T-REAL-DENY", run_id="R-REAL-DENY")
    assert result.status == "BLOCKED"
    assert _derived_count(p1_manager) == 0
    assert service.task_registry.get("T-REAL-DENY")["status"] == FAILED


def test_wrapper_real_service_allows_and_records_cost(p1_manager):
    _seed_memory(p1_manager, "M-COST-1", "Bifrost 网关采用 PostgreSQL 主从架构")
    _seed_memory(
        p1_manager,
        "M-COST-2",
        "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
    )
    service = JMRService.build(
        manager=p1_manager,
        config=JMRConfig(expected_cost_cny=0.02),
    )
    gov = CostGovernance(p1_manager, _cfg(daily_budget_cny=1.0))
    wrapper = CostGovernedJMRService(service, gov)
    result = wrapper.run(task_id="T-REAL-OK", run_id="R-REAL-OK")
    assert result.status == SUCCEEDED
    assert _derived_count(p1_manager) > 0
    assert gov.active_reservations() == []
    assert _read(p1_manager, _run_key("R-REAL-OK"))["cost_cny"] > 0


def test_factory_wires_service_and_governance(p1_manager):
    _seed_memory(p1_manager, "M-FACT-1", "Bifrost 网关采用 PostgreSQL 主从架构")
    _seed_memory(
        p1_manager,
        "M-FACT-2",
        "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
    )
    wrapper, gov = build_cost_governed_jmr_service(
        p1_manager,
        config=JMRConfig(expected_cost_cny=0.02),
        governance_config=_cfg(daily_budget_cny=1.0),
    )
    result = wrapper.run(task_id="T-FACTORY", run_id="R-FACTORY")
    assert result.status == SUCCEEDED
    assert gov.active_reservations() == []
    assert _read(p1_manager, _run_key("R-FACTORY"))["cost_cny"] > 0


def test_cost_gate_does_not_bypass_privacy(p1_manager):
    _seed_memory(
        p1_manager,
        "M-PII-COST",
        "我的手机号是 13800138000，请记住",
    )
    service = JMRService.build(
        manager=p1_manager,
        config=JMRConfig(expected_cost_cny=0.02),
    )
    gov = CostGovernance(p1_manager, _cfg(daily_budget_cny=1.0))
    wrapper = CostGovernedJMRService(service, gov)
    result = wrapper.run(task_id="T-PII-COST", run_id="R-PII-COST")
    assert result.status == SUCCEEDED
    assert _derived_count(p1_manager) == 0
    denied = [
        row
        for row in _audit_rows(p1_manager)
        if row["action"] == "P1:JMR_REFLECTION:DENIED"
    ]
    assert denied
    assert any(
        "privacy_hold" in row["detail"].get("failure_reason", "")
        for row in denied
    )


def test_new_task_cannot_bypass_daily_budget(p1_manager):
    inner = FakeInner(p1_manager)
    inner.cost_cny = 0.3
    gov = _gov(
        p1_manager,
        daily_budget_cny=0.4,
        manage_period_spend=True,
        reservation_ttl_seconds=60,
    )
    wrapper = CostGovernedJMRService(inner, gov)
    first = wrapper.run(task_id="T-NEW-1", run_id="R-NEW-1")
    assert first.status == SUCCEEDED
    second = wrapper.run(task_id="T-NEW-2", run_id="R-NEW-2")
    assert second.status == "BLOCKED"
    assert len(inner.calls) == 1
    assert "daily_budget_exceeded" in second.failure_reason


def test_retry_cannot_bypass_exhausted_budget(p1_manager):
    inner = FakeInner(p1_manager)
    inner.cost_cny = 0.3
    inner.fail_run = True
    gov = _gov(
        p1_manager,
        daily_budget_cny=0.4,
        manage_period_spend=True,
        reservation_ttl_seconds=60,
    )
    wrapper = CostGovernedJMRService(inner, gov)
    first = wrapper.run(task_id="T-RETRY", run_id="R-RETRY-1")
    assert first.status == FAILED
    assert _read(p1_manager, "p1.cost.daily." + datetime.now(timezone.utc).date().isoformat()) == 0.3
    assert inner.task_registry.get("T-RETRY")["status"] == FAILED

    inner.fail_run = False
    second = wrapper.run(task_id="T-RETRY", run_id="R-RETRY-2", resume=True)
    assert second.status == "BLOCKED"
    assert len(inner.calls) == 1


# ---------------------------------------------------------------------------
# JMR-05 scheduler integration and zero DDL
# ---------------------------------------------------------------------------
def test_scheduler_denies_when_cost_gate_blocks(p1_manager):
    inner = FakeInner(p1_manager)
    gov = _gov(p1_manager, daily_budget_cny=0.01)
    wrapper = CostGovernedJMRService(inner, gov)
    engine = SchedulerEngine(
        p1_manager,
        service=wrapper,
        config=SchedulerConfig(
            max_tasks_per_tick=2,
            max_scan_tasks=100,
            batch_size=2,
            max_runtime_seconds=30,
            default_timeout_seconds=30,
        ),
    )
    engine.register_task("T-SCHED-DENY")
    result = engine.tick()
    assert "T-SCHED-DENY" in result.failed
    assert inner.calls == []
    assert _derived_count(p1_manager) == 0


def test_scheduler_settles_cost_on_success(p1_manager):
    inner = FakeInner(p1_manager)
    inner.cost_cny = 0.3
    gov = _gov(p1_manager, daily_budget_cny=1.0)
    wrapper = CostGovernedJMRService(inner, gov)
    engine = SchedulerEngine(
        p1_manager,
        service=wrapper,
        config=SchedulerConfig(
            max_tasks_per_tick=2,
            max_scan_tasks=100,
            batch_size=2,
            max_runtime_seconds=30,
            default_timeout_seconds=30,
        ),
    )
    engine.register_task("T-SCHED-OK")
    result = engine.tick()
    assert "T-SCHED-OK" in result.succeeded
    assert len(inner.calls) == 1
    assert gov.active_reservations() == []
    assert _read(p1_manager, "p1.cost.task.T-SCHED-OK") is None  # inner service owns task spend


def test_cost_governor_exposes_reservation_snapshot(manager, settings):
    gov = _gov(manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(
        task_id="T-GOV", run_id="R-GOV", expected_cost_cny=0.2
    )
    governor = CostGovernor(manager, settings)
    snapshot = governor.reservations()
    assert snapshot["active"] == 1
    assert snapshot["reserved_cny"] == 0.2
    assert governor.status()["reservations"]["active"] == 1
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.2)
    assert governor.reservations()["active"] == 0
    assert governor.reservations()["settled"] == 1


def test_cost_governance_adds_no_tables(p1_manager):
    with p1_manager.session() as session:
        before = {
            str(row[0])
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    gov = _gov(p1_manager, daily_budget_cny=1.0)
    decision = gov.reserve_run(task_id="T-NODDL", run_id="R-NODDL", expected_cost_cny=0.1)
    gov.settle_run(decision.reservation_id, actual_cost_cny=0.1)
    with p1_manager.session() as session:
        after = {
            str(row[0])
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert before == after
    assert MIGRATIONS[-1].version == 6


# ---------------------------------------------------------------------------
# JMR-07 identifier guard: credential-shaped ids never reach state or audit
# ---------------------------------------------------------------------------
_SECRET_TASK_ID = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
_SECRET_RUN_ID = "ghp_" + "a" * 36


def _persisted_text(manager) -> str:
    """Everything JMR-06 persists in settings and audit detail, as one string."""
    with manager.session() as session:
        settings = session.execute(
            text("SELECT key || '=' || value FROM system_settings")
        ).scalars().all()
        audit = session.execute(
            text("SELECT action || ' ' || COALESCE(detail_json, '') FROM memory_audit_logs")
        ).scalars().all()
    return "\n".join([*settings, *audit])


def test_wrapper_refuses_credential_shaped_task_id_in_observe_mode(p1_manager):
    inner = FakeInner(p1_manager)
    wrapper = CostGovernedJMRService(inner, _gov(p1_manager))
    with pytest.raises(CredentialBlockedError):
        wrapper.run(task_id=_SECRET_TASK_ID, run_id="R-SAFE")
    assert inner.calls == []
    assert _SECRET_TASK_ID not in _persisted_text(p1_manager)


def test_wrapper_refuses_credential_shaped_run_id_in_enforce_mode(p1_manager):
    inner = FakeInner(p1_manager)
    wrapper = CostGovernedJMRService(inner, _gov(p1_manager, daily_budget_cny=1.0))
    with pytest.raises(CredentialBlockedError):
        wrapper.run(task_id="T-SAFE", run_id=_SECRET_RUN_ID)
    assert inner.calls == []
    assert wrapper.cost_governance.active_reservations() == []
    assert _SECRET_RUN_ID not in _persisted_text(p1_manager)


def test_reserve_refuses_credential_shaped_run_id_before_any_state(manager):
    gov = _gov(manager, daily_budget_cny=1.0)
    with pytest.raises(CredentialBlockedError):
        gov.reserve_run(task_id="T-SAFE", run_id=_SECRET_RUN_ID, expected_cost_cny=0.1)
    assert gov.active_reservations() == []
    assert _SECRET_RUN_ID not in _persisted_text(manager)
