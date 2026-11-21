"""JMR-06 persistent cost governance.

This module turns the pure policy in ``cost_policy.py`` into a persistent,
auditable governance layer. It deliberately does **not** create a second spend
ledger:

* ``CostGovernor`` / ``BudgetGuard`` remain the spend writers. The keys
  ``p1.cost.task.*``, ``p1.cost.batch.*``, ``p1.cost.rate.*``,
  ``llm.spent_cny`` and the JMR daily/monthly keys are read here.
* This layer adds only a bounded **reservation registry** in
  ``system_settings`` (``p1.cost.reservations``). Reservations are not spend;
  they reserve headroom so concurrent runs cannot all pass the same check.
* Every reservation, settlement, release, denial and degradation is written to
  the existing ``memory_audit_logs`` via ``AuditBus`` with value-free fields.

Zero DDL, zero migrations, no memory-table writes.

Concurrency model
-----------------
SQLite has a single writer. ``_atomic()`` first writes a small mutex key, which
acquires the database write lock, then reads/writes the ledger inside the same
transaction. Two processes/threads therefore cannot both observe the same
headroom and reserve it. Expired reservations are released on the next
operation. This is intentionally a single-ledger, SQLite-based design; it does
not claim cross-database or distributed locking.
"""
from __future__ import annotations

import json
import logging
import math
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ..core.security_gate import assert_safe_identifier
from ..core.timeutils import parse_iso, utc_now_iso
from ..governance.audit_bus import (
    STATUS_ALLOWED,
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    AuditBus,
)
from ..governance.task_registry import CANCELLED, CREATED, FAILED, PAUSED, RUNNING, SUCCEEDED
from .config import JMRConfig
from .cost_policy import (
    ALLOW,
    ALGORITHM_VERSION,
    CostInputError,
    DENY,
    HOLD,
    INVALID_INPUT,
    UNCONFIGURED,
    compute_call_cost,
    to_money,
    to_token_count,
)
from .service import JMRRunResult, JMRService, TASK_TYPE
logger = logging.getLogger("jme.jmr.cost_governance")

#: Existing keys. No new ledger is introduced.
RESERVATIONS_KEY = "p1.cost.reservations"
MUTEX_KEY = "p1.cost.governance.mutex"
BREAKER_KEY = "p1.cost.breaker"
GLOBAL_SPENT_KEY = "llm.spent_cny"
GLOBAL_PERIOD_KEY = "llm.period_start"
DAILY_PREFIX = "p1.cost.daily."
MONTHLY_PREFIX = "p1.cost.monthly."
TASK_PREFIX = "p1.cost.task."
RUN_PREFIX = "p1.cost.batch."
RATE_PREFIX = "p1.cost.rate."

JMR_STAGE_COUNT = 3
MAX_RECEIPTS = 500
DEFAULT_RESERVATION_TTL_SECONDS = 600

OBSERVE = "OBSERVE"
DEGRADE = "DEGRADE"

REASON_OBSERVE = "cost_governance_unconfigured"
REASON_OK = "within_policy"
REASON_BREAKER = "circuit_breaker_tripped"
REASON_GLOBAL = "global_budget_exceeded"
REASON_DAILY = "daily_budget_exceeded"
REASON_MONTHLY = "monthly_budget_exceeded"
REASON_TASK = "task_budget_exceeded"
REASON_RUN = "run_budget_exceeded"
REASON_TASK_TOKENS = "task_token_limit_exceeded"
REASON_TASK_CALLS = "task_call_limit_exceeded"
REASON_RATE = "rate_limit_exceeded"
REASON_PRICE = "price_unconfigured"
REASON_UNKNOWN_MODEL = "unknown_model"
REASON_INVALID = "cost_input_invalid"
REASON_STATE = "cost_state_invalid"
REASON_DEGRADE_EXHAUSTED = "degradation_exhausted"
REASON_DEGRADE_APPLIED = "degradation_applied"
REASON_ALREADY_RESERVED = "already_reserved"
REASON_DEGRADE_UNAVAILABLE = "degradation_unavailable"
REASON_ZERO = "zero_cost_declared"
REASON_OVERAGE = "actual_cost_exceeded_reservation"
REASON_SETTLE_MISSING = "reservation_not_found"
REASON_ALREADY_SETTLED = "already_settled"
REASON_AUDIT_FAILED = "cost_audit_failed"

_SETTINGS_UPSERT = (
    "INSERT INTO system_settings(key, value, updated_at) VALUES (:k, :v, :u) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
    "updated_at=excluded.updated_at"
)


class CostGovernanceError(RuntimeError):
    """Base class for persistent cost-governance failures."""

    code = "cost_governance_error"


class CostStateError(CostGovernanceError):
    """Ledger state is missing, unreadable or not transactional."""

    code = "cost_state_invalid"


class CostReservationError(CostGovernanceError):
    """A reservation could not be created or settled."""

    code = "cost_reservation_error"


class CostAuditError(CostGovernanceError):
    """Cost audit could not be written; governance is fail-closed."""

    code = "cost_audit_failed"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return number


@dataclass(frozen=True)
class CostGovernanceConfig:
    """All JMR-06 knobs. Zero means "not configured" (not unlimited)."""

    enforcement: str = "auto"  # auto / on / off
    daily_budget_cny: float = 0.0
    monthly_budget_cny: float = 0.0
    task_budget_cny: float = 0.0
    run_budget_cny: float = 0.0
    global_budget_cny: float = 0.0
    max_single_call_cny: float = 0.0
    task_token_limit: int = 0
    task_call_limit: int = 0
    rate_limit_per_minute: int = 0
    breaker_threshold: int = 3
    price_in_per_1k_cny: float = 0.0
    price_out_per_1k_cny: float = 0.0
    expected_tokens: int = 0
    expected_cost_cny: float = 0.0
    degrade_threshold_ratio: float = 0.0
    degrade_reduce_ratio: float = 0.5
    reservation_ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS
    trip_on_overage: bool = False
    manage_period_spend: bool = False
    model_prices: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    fallback_models: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        enforcement = str(self.enforcement).strip().lower()
        if enforcement not in {"auto", "on", "off"}:
            raise ValueError("enforcement must be auto, on or off")
        object.__setattr__(self, "enforcement", enforcement)
        for name in (
            "daily_budget_cny",
            "monthly_budget_cny",
            "task_budget_cny",
            "run_budget_cny",
            "global_budget_cny",
            "max_single_call_cny",
            "price_in_per_1k_cny",
            "price_out_per_1k_cny",
            "expected_cost_cny",
        ):
            object.__setattr__(self, name, _finite_nonnegative(getattr(self, name), name))
        for name in ("task_token_limit", "task_call_limit", "rate_limit_per_minute"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if isinstance(self.breaker_threshold, bool) or not isinstance(
            self.breaker_threshold, int
        ) or self.breaker_threshold < 1:
            raise ValueError("breaker_threshold must be a positive integer")
        if isinstance(self.expected_tokens, bool) or not isinstance(
            self.expected_tokens, int
        ) or self.expected_tokens < 0:
            raise ValueError("expected_tokens must be a non-negative integer")
        if not 0.0 <= float(self.degrade_threshold_ratio) <= 1.0:
            raise ValueError("degrade_threshold_ratio must be in [0, 1]")
        object.__setattr__(
            self, "degrade_threshold_ratio", float(self.degrade_threshold_ratio)
        )
        if not 0.0 < float(self.degrade_reduce_ratio) < 1.0:
            raise ValueError("degrade_reduce_ratio must be in (0, 1)")
        object.__setattr__(
            self, "degrade_reduce_ratio", float(self.degrade_reduce_ratio)
        )
        if isinstance(self.reservation_ttl_seconds, bool) or not isinstance(
            self.reservation_ttl_seconds, int
        ) or self.reservation_ttl_seconds < 1:
            raise ValueError("reservation_ttl_seconds must be a positive integer")
        for name in ("trip_on_overage", "manage_period_spend"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        prices = self._normalise_prices(self.model_prices)
        fallbacks = self._normalise_fallbacks(self.fallback_models)
        object.__setattr__(self, "model_prices", prices)
        object.__setattr__(self, "fallback_models", fallbacks)

    @staticmethod
    def _normalise_prices(raw: Any) -> Dict[str, Dict[str, float]]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("model_prices must be an object")
        out: Dict[str, Dict[str, float]] = {}
        for model, prices in raw.items():
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model_prices keys must be non-empty strings")
            if not isinstance(prices, Mapping):
                raise ValueError("model_prices values must be objects")
            input_price = _finite_nonnegative(
                prices.get("input", prices.get("price_in_per_1k_cny", 0.0)),
                "model_prices.input",
            )
            output_price = _finite_nonnegative(
                prices.get("output", prices.get("price_out_per_1k_cny", 0.0)),
                "model_prices.output",
            )
            out[model] = {"input": input_price, "output": output_price}
        return out

    @staticmethod
    def _normalise_fallbacks(raw: Any) -> Dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise ValueError("fallback_models must be an object")
        out: Dict[str, str] = {}
        for source, target in raw.items():
            if not isinstance(source, str) or not source.strip():
                raise ValueError("fallback_models keys must be non-empty strings")
            if not isinstance(target, str) or not target.strip():
                raise ValueError("fallback_models values must be non-empty strings")
            if source == target:
                raise ValueError("fallback_models must not be self-referential")
            out[source] = target
        for source in out:
            seen = {source}
            cursor = out.get(source)
            while cursor:
                if cursor in seen:
                    raise ValueError("fallback_models must not contain cycles")
                seen.add(cursor)
                cursor = out.get(cursor)
        return out

    def configured(self) -> bool:
        return any(
            (
                self.daily_budget_cny > 0,
                self.monthly_budget_cny > 0,
                self.task_budget_cny > 0,
                self.run_budget_cny > 0,
                self.global_budget_cny > 0,
                self.max_single_call_cny > 0,
                self.task_token_limit > 0,
                self.task_call_limit > 0,
                self.rate_limit_per_minute > 0,
                self.price_in_per_1k_cny > 0,
                self.price_out_per_1k_cny > 0,
                self.expected_tokens > 0,
                bool(self.model_prices),
            )
        )

    def budgets(self) -> Dict[str, float]:
        return {
            "global": self.global_budget_cny,
            "daily": self.daily_budget_cny,
            "monthly": self.monthly_budget_cny,
            "task": self.task_budget_cny,
            "run": self.run_budget_cny,
        }

    @classmethod
    def from_env(cls) -> "CostGovernanceConfig":
        return cls(
            enforcement=os.environ.get("JARVISMIND_JMR_COST_ENFORCEMENT", "auto"),
            daily_budget_cny=_env_float("JARVISMIND_JMR_COST_DAILY_BUDGET_CNY", 0.0),
            monthly_budget_cny=_env_float("JARVISMIND_JMR_COST_MONTHLY_BUDGET_CNY", 0.0),
            task_budget_cny=_env_float("JARVISMIND_JMR_COST_TASK_BUDGET_CNY", 0.0),
            run_budget_cny=_env_float("JARVISMIND_JMR_COST_RUN_BUDGET_CNY", 0.0),
            global_budget_cny=_env_float("JARVISMIND_JMR_COST_GLOBAL_BUDGET_CNY", 0.0),
            max_single_call_cny=_env_float("JARVISMIND_JMR_COST_MAX_SINGLE_CALL_CNY", 0.0),
            task_token_limit=_env_int("JARVISMIND_JMR_COST_TASK_TOKEN_LIMIT", 0, minimum=0),
            task_call_limit=_env_int("JARVISMIND_JMR_COST_TASK_CALL_LIMIT", 0, minimum=0),
            rate_limit_per_minute=_env_int(
                "JARVISMIND_JMR_COST_RATE_LIMIT_PER_MINUTE", 0, minimum=0
            ),
            breaker_threshold=_env_int("JARVISMIND_JMR_COST_BREAKER_THRESHOLD", 3),
            price_in_per_1k_cny=_env_float("JARVISMIND_JMR_COST_PRICE_IN_PER_1K_CNY", 0.0),
            price_out_per_1k_cny=_env_float("JARVISMIND_JMR_COST_PRICE_OUT_PER_1K_CNY", 0.0),
            expected_tokens=_env_int("JARVISMIND_JMR_COST_EXPECTED_TOKENS", 0, minimum=0),
            expected_cost_cny=_env_float("JARVISMIND_JMR_COST_EXPECTED_COST_CNY", 0.0),
            degrade_threshold_ratio=_env_float(
                "JARVISMIND_JMR_COST_DEGRADE_THRESHOLD_RATIO", 0.0
            ),
            degrade_reduce_ratio=_env_float(
                "JARVISMIND_JMR_COST_DEGRADE_REDUCE_RATIO", 0.5
            ),
            reservation_ttl_seconds=_env_int(
                "JARVISMIND_JMR_COST_RESERVATION_TTL_SECONDS", DEFAULT_RESERVATION_TTL_SECONDS
            ),
            trip_on_overage=(
                os.environ.get("JARVISMIND_JMR_COST_TRIP_ON_OVERAGE", "").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            manage_period_spend=(
                os.environ.get("JARVISMIND_JMR_COST_MANAGE_PERIOD_SPEND", "").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            model_prices=_env_json("JARVISMIND_JMR_COST_MODEL_PRICES_JSON", {}),
            fallback_models=_env_json("JARVISMIND_JMR_COST_FALLBACK_MODELS_JSON", {}),
        )

    @classmethod
    def from_sources(
        cls,
        settings: Any = None,
        jmr_config: Any = None,
        governor: Any = None,
        *,
        override: Optional["CostGovernanceConfig"] = None,
    ) -> "CostGovernanceConfig":
        """Build from existing configuration, using getattr for compatibility.

        ``JMRConfig`` in the committed baseline has no daily/monthly fields;
        the advanced JMR-04..07 working tree does. Both work here.
        """
        if override is not None:
            return override
        env = cls.from_env()
        governor_config = getattr(governor, "config", None)

        def env_set(*names: str) -> bool:
            """True when at least one of these env vars is explicitly set."""
            for name in names:
                raw = os.environ.get(name)
                if raw is not None and raw.strip() != "":
                    return True
            return False

        def pick(name: str, default: Any = 0.0, env_names: tuple = ()) -> Any:
            # JMR-08 audit P1-4: an explicitly configured environment variable
            # must win over a config default. Otherwise a deployment that sets
            # JARVISMIND_JMR_COST_* would silently enforce nothing, because the
            # container always passes a JMRConfig.
            if env_names and env_set(*env_names):
                return default
            for source in (jmr_config, governor_config, settings):
                if source is not None and hasattr(source, name):
                    return getattr(source, name)
            return default

        def env_number(names: tuple, default: float) -> float:
            """Value of the first explicitly set env var in ``names``."""
            for name in names:
                raw = os.environ.get(name)
                if raw is None or not raw.strip():
                    continue
                try:
                    value = float(raw.strip())
                except ValueError as exc:
                    raise ValueError(f"{name} must be a number") from exc
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{name} must be a finite number >= 0")
                return value
            return default

        daily_env = env_number(
            ("JARVISMIND_JMR_COST_DAILY_BUDGET_CNY", "JARVISMIND_JMR_DAILY_BUDGET_CNY"),
            env.daily_budget_cny,
        )
        monthly_env = env_number(
            ("JARVISMIND_JMR_COST_MONTHLY_BUDGET_CNY", "JARVISMIND_JMR_MONTHLY_BUDGET_CNY"),
            env.monthly_budget_cny,
        )
        expected_env = env_number(
            ("JARVISMIND_JMR_COST_EXPECTED_COST_CNY",), env.expected_cost_cny
        )
        single_env = env_number(
            ("JARVISMIND_JMR_COST_MAX_SINGLE_CALL_CNY", "JARVISMIND_JMR_MAX_SINGLE_CALL_CNY"),
            env.max_single_call_cny,
        )
        return cls(
            enforcement=env.enforcement,
            daily_budget_cny=float(
                pick(
                    "daily_budget_cny",
                    daily_env,
                    ("JARVISMIND_JMR_COST_DAILY_BUDGET_CNY", "JARVISMIND_JMR_DAILY_BUDGET_CNY"),
                )
                or 0.0
            ),
            monthly_budget_cny=float(
                pick(
                    "monthly_budget_cny",
                    monthly_env,
                    ("JARVISMIND_JMR_COST_MONTHLY_BUDGET_CNY", "JARVISMIND_JMR_MONTHLY_BUDGET_CNY"),
                )
                or 0.0
            ),
            task_budget_cny=float(
                pick(
                    "task_budget_cny",
                    env.task_budget_cny,
                    ("JARVISMIND_JMR_COST_TASK_BUDGET_CNY",),
                )
                or 0.0
            ),
            run_budget_cny=float(
                pick(
                    "run_budget_cny",
                    env.run_budget_cny,
                    ("JARVISMIND_JMR_COST_RUN_BUDGET_CNY",),
                )
                or 0.0
            ),
            global_budget_cny=float(
                getattr(settings, "llm_budget_cny", env.global_budget_cny) or 0.0
            ),
            max_single_call_cny=float(
                pick(
                    "max_single_call_cny",
                    single_env,
                    ("JARVISMIND_JMR_COST_MAX_SINGLE_CALL_CNY", "JARVISMIND_JMR_MAX_SINGLE_CALL_CNY"),
                )
                or 0.0
            ),
            task_token_limit=int(
                pick("task_token_limit", env.task_token_limit) or 0
            ),
            task_call_limit=int(
                pick("task_call_limit", env.task_call_limit) or 0
            ),
            rate_limit_per_minute=int(
                pick("rate_limit_per_minute", env.rate_limit_per_minute) or 0
            ),
            breaker_threshold=int(
                pick("breaker_threshold", env.breaker_threshold) or env.breaker_threshold
            ),
            price_in_per_1k_cny=float(
                getattr(settings, "llm_price_in", env.price_in_per_1k_cny) or 0.0
            ),
            price_out_per_1k_cny=float(
                getattr(settings, "llm_price_out", env.price_out_per_1k_cny) or 0.0
            ),
            expected_tokens=int(
                pick("expected_tokens", env.expected_tokens) or 0
            ),
            expected_cost_cny=float(
                pick(
                    "expected_cost_cny",
                    expected_env,
                    ("JARVISMIND_JMR_COST_EXPECTED_COST_CNY",),
                )
                or 0.0
            ),
            degrade_threshold_ratio=env.degrade_threshold_ratio,
            degrade_reduce_ratio=env.degrade_reduce_ratio,
            reservation_ttl_seconds=env.reservation_ttl_seconds,
            trip_on_overage=env.trip_on_overage,
            # JMR-08 audit P1-5: the service already writes p1.cost.daily/monthly
            # per stage. Writing them again on settlement double-counts spend.
            # Period accounting therefore belongs to the service unless the
            # operator explicitly opts in (legacy/compatibility mode).
            manage_period_spend=env.manage_period_spend,
            model_prices=env.model_prices,
            fallback_models=env.fallback_models,
        )


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostEstimate:
    status: str
    cost_cny: Optional[Decimal]
    input_tokens: int
    output_tokens: int
    input_cost_cny: Optional[Decimal]
    output_cost_cny: Optional[Decimal]
    source: str
    reason: str
    model: str = ""
    provider: str = ""

    def to_dict(self) -> Dict[str, Any]:
        def _s(value: Optional[Decimal]) -> Optional[str]:
            return None if value is None else str(value)

        return {
            "status": self.status,
            "cost_cny": _s(self.cost_cny),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_cny": _s(self.input_cost_cny),
            "output_cost_cny": _s(self.output_cost_cny),
            "source": self.source,
            "reason": self.reason,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class DegradationPlan:
    suggested: bool
    applied: bool
    reason: str
    level: int
    reduce_ratio: float
    reduced_tokens: int
    reduced_cost_cny: Optional[Decimal]
    fallback_model: Optional[str]
    fallback_configured: bool
    security_bypass: bool = False
    privacy_bypass: bool = False
    task_state_bypass: bool = False

    @property
    def max_level(self) -> int:
        return 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suggested": self.suggested,
            "applied": self.applied,
            "reason": self.reason,
            "level": self.level,
            "max_level": self.max_level,
            "reduce_ratio": self.reduce_ratio,
            "reduced_tokens": self.reduced_tokens,
            "reduced_cost_cny": (
                None if self.reduced_cost_cny is None else str(self.reduced_cost_cny)
            ),
            "fallback_model": self.fallback_model,
            "fallback_configured": self.fallback_configured,
            "security_bypass": self.security_bypass,
            "privacy_bypass": self.privacy_bypass,
            "task_state_bypass": self.task_state_bypass,
        }


@dataclass(frozen=True)
class CostDecision:
    task_id: str
    run_id: str
    status: str
    allowed: bool
    reason_codes: Tuple[str, ...]
    reservation_id: str
    estimate: CostEstimate
    budgets: Dict[str, Dict[str, Any]]
    degradation: Optional[DegradationPlan]
    enforcement: str
    audit_id: str = ""
    algorithm_version: str = ALGORITHM_VERSION

    @property
    def allow_continue(self) -> bool:
        return self.allowed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "status": self.status,
            "allowed": self.allowed,
            "allow_continue": self.allow_continue,
            "reason_codes": list(self.reason_codes),
            "reservation_id": self.reservation_id,
            "estimate": self.estimate.to_dict(),
            "budgets": self.budgets,
            "degradation": None if self.degradation is None else self.degradation.to_dict(),
            "enforcement": self.enforcement,
            "audit_id": self.audit_id,
            "algorithm_version": self.algorithm_version,
        }


@dataclass(frozen=True)
class SettlementResult:
    reservation_id: str
    settled: bool
    already_settled: bool
    status: str
    estimated_cny: str
    actual_cny: str
    overage_cny: str
    reason: str
    audit_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "settled": self.settled,
            "already_settled": self.already_settled,
            "status": self.status,
            "estimated_cny": self.estimated_cny,
            "actual_cny": self.actual_cny,
            "overage_cny": self.overage_cny,
            "reason": self.reason,
            "audit_id": self.audit_id,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class CostGovernance:
    """Persistent, atomic reservation + audit layer for JMR cost governance."""

    def __init__(
        self,
        manager,
        config: Optional[CostGovernanceConfig] = None,
        *,
        audit_bus: Optional[AuditBus] = None,
        clock=None,
    ) -> None:
        self.manager = manager
        self.config = config or CostGovernanceConfig.from_env()
        self.audit_bus = audit_bus or AuditBus(manager)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --------------------------------------------------------------- helpers
    def _now(self, now: Optional[datetime] = None) -> datetime:
        value = now if now is not None else self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CostStateError("now must be a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _atomic(self):
        """Serialize writers and keep every ledger change in one transaction.

        The first statement is a write to a mutex key. SQLite acquires its
        single writer lock at that point, so a concurrent caller cannot read
        the same headroom before the first caller commits.
        """
        try:
            with self.manager.session() as session:
                try:
                    session.execute(text("PRAGMA busy_timeout=5000"))
                    session.execute(
                        text(_SETTINGS_UPSERT),
                        {
                            "k": MUTEX_KEY,
                            "v": json.dumps({"at": utc_now_iso()}, sort_keys=True),
                            "u": utc_now_iso(),
                        },
                    )
                except OperationalError as exc:
                    raise CostStateError(
                        "cost ledger is not available (system_settings missing or locked)"
                    ) from exc
                yield session
        except OperationalError as exc:
            raise CostStateError("cost ledger transaction failed") from exc

    @staticmethod
    def _read_raw(session, key: str) -> Optional[str]:
        return session.execute(
            text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
        ).scalar()

    @classmethod
    def _read_json(cls, session, key: str, default: Any) -> Any:
        raw = cls._read_raw(session, key)
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _read_number(cls, session, key: str, default: float = 0.0) -> float:
        raw = cls._read_raw(session, key)
        if raw is None or raw == "":
            return default
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = raw
        try:
            value = float(parsed)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and value >= 0 else default

    @staticmethod
    def _write_raw(session, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True
        )
        session.execute(
            text(_SETTINGS_UPSERT),
            {"k": key, "v": payload, "u": utc_now_iso()},
        )

    @staticmethod
    def _empty_reservations() -> Dict[str, Any]:
        return {"active": {}, "settled": {}, "released": {}}

    @classmethod
    def _load_reservations(cls, session) -> Dict[str, Any]:
        raw = cls._read_json(session, RESERVATIONS_KEY, None)
        if not isinstance(raw, dict):
            return cls._empty_reservations()
        out = cls._empty_reservations()
        for bucket in ("active", "settled", "released"):
            value = raw.get(bucket)
            if isinstance(value, dict):
                out[bucket] = {
                    str(key): dict(item)
                    for key, item in value.items()
                    if isinstance(item, dict)
                }
        return out

    @classmethod
    def _save_reservations(cls, session, data: Dict[str, Any]) -> None:
        # Keep receipts bounded; active reservations are never pruned by count.
        settled = data.get("settled") or {}
        released = data.get("released") or {}
        for bucket in (settled, released):
            if len(bucket) > MAX_RECEIPTS:
                ordered = sorted(
                    bucket.items(),
                    key=lambda pair: str(pair[1].get("settled_at") or pair[1].get("released_at") or ""),
                )
                for key, _ in ordered[: len(bucket) - MAX_RECEIPTS]:
                    bucket.pop(key, None)
        cls._write_raw(session, RESERVATIONS_KEY, data)

    @classmethod
    def _expire(cls, data: Dict[str, Any], now: datetime) -> List[str]:
        expired: List[str] = []
        active = data.get("active") or {}
        for reservation_id in list(active):
            record = active[reservation_id]
            try:
                expires = parse_iso(str(record.get("expires_at") or ""))
            except ValueError:
                expires = now - timedelta(seconds=1)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires > now:
                continue
            record = dict(record)
            record["status"] = "EXPIRED"
            record["released_at"] = cls._iso(now)
            record["reason"] = "reservation_expired"
            data.setdefault("released", {})[reservation_id] = record
            active.pop(reservation_id, None)
            expired.append(reservation_id)
        return expired

    @staticmethod
    def _sum_active(
        active: Mapping[str, Mapping[str, Any]],
        field: str,
        *,
        task_id: str = "",
        run_id: str = "",
        day: str = "",
        month: str = "",
    ) -> Decimal:
        total = Decimal("0")
        for record in active.values():
            if task_id and str(record.get("task_id") or "") != task_id:
                continue
            if run_id and str(record.get("run_id") or "") != run_id:
                continue
            if day and str(record.get("day") or "") != day:
                continue
            if month and str(record.get("month") or "") != month:
                continue
            try:
                total += Decimal(str(record.get(field) or "0"))
            except Exception:
                continue
        return total

    @staticmethod
    def _budget_view(
        name: str,
        *,
        budget: float,
        spend: float,
        reserved: float,
        expected: float,
        reason: str,
    ) -> Dict[str, Any]:
        if budget <= 0:
            return {
                "dimension": name,
                "status": UNCONFIGURED,
                "reason": f"{name}_budget_unconfigured",
                "budget_cny": 0.0,
                "spent_cny": round(spend, 6),
                "reserved_cny": round(reserved, 6),
                "projected_cny": round(spend + reserved + expected, 6),
                "remaining_cny": None,
                "usage_ratio": None,
            }
        projected = spend + reserved + expected
        remaining = max(budget - spend - reserved, 0.0)
        ratio = projected / budget if budget > 0 else None
        status = DENY if projected > budget else ALLOW
        return {
            "dimension": name,
            "status": status,
            "reason": reason if status == DENY else REASON_OK,
            "budget_cny": round(budget, 6),
            "spent_cny": round(spend, 6),
            "reserved_cny": round(reserved, 6),
            "projected_cny": round(projected, 6),
            "remaining_cny": round(remaining, 6),
            "usage_ratio": None if ratio is None else round(ratio, 6),
        }

    @staticmethod
    def _result_status(value: Any) -> str:
        text_value = str(value or "").strip().upper()
        if text_value in {SUCCEEDED, FAILED, CANCELLED, PAUSED, RUNNING}:
            return text_value
        if text_value in {"BLOCKED", "COST_BLOCKED"}:
            return FAILED
        return SUCCEEDED if text_value == "ALLOW" else (text_value or "UNKNOWN")


    # --------------------------------------------------------------- estimate
    def _price_for(self, model: str) -> Tuple[float, float, str]:
        model = str(model or "").strip()
        if model and self.config.model_prices:
            prices = self.config.model_prices.get(model)
            if prices is None:
                raise CostInputError("model", "invalid")
            return float(prices["input"]), float(prices["output"]), "model_price_book"
        if self.config.price_in_per_1k_cny > 0 or self.config.price_out_per_1k_cny > 0:
            return (
                float(self.config.price_in_per_1k_cny),
                float(self.config.price_out_per_1k_cny),
                "global_price",
            )
        return 0.0, 0.0, "unconfigured"

    def estimate(
        self,
        *,
        input_tokens: Any = 0,
        output_tokens: Any = 0,
        expected_cost_cny: Any = None,
        model: str = "",
        provider: str = "",
    ) -> CostEstimate:
        """Estimate one call. Missing price is HOLD, never free."""
        try:
            in_tokens = to_token_count(input_tokens, "input_tokens")
            out_tokens = to_token_count(output_tokens, "output_tokens")
            configured_cost = (
                to_money(expected_cost_cny, "expected_cost_cny")
                if expected_cost_cny is not None
                else to_money(self.config.expected_cost_cny, "expected_cost_cny")
            )
        except CostInputError as exc:
            return CostEstimate(
                INVALID_INPUT, None, 0, 0, None, None, "invalid", f"{exc.field_name}:{exc.kind}",
                model=model, provider=provider,
            )

        try:
            price_in, price_out, source = self._price_for(model)
        except CostInputError:
            return CostEstimate(
                HOLD, None, in_tokens, out_tokens, None, None,
                "unconfigured", REASON_UNKNOWN_MODEL, model=model, provider=provider,
            )

        if price_in > 0 or price_out > 0:
            call = compute_call_cost(
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                price_in_per_1k_cny=price_in,
                price_out_per_1k_cny=price_out,
            )
            cost = max(call.total_cost_cny, configured_cost)
            return CostEstimate(
                ALLOW, cost, in_tokens, out_tokens,
                call.input_cost_cny, call.output_cost_cny,
                source, REASON_OK, model=model, provider=provider,
            )

        if configured_cost > 0:
            return CostEstimate(
                ALLOW, configured_cost, in_tokens, out_tokens,
                None, None, "configured_expected_cost", REASON_OK,
                model=model, provider=provider,
            )

        if in_tokens + out_tokens == 0:
            # The caller explicitly declares a zero-cost stage. This is not an
            # unknown price; it is a configuration statement.
            return CostEstimate(
                ALLOW, Decimal("0"), 0, 0, None, None,
                "zero_cost_declared", REASON_ZERO,
                model=model, provider=provider,
            )

        return CostEstimate(
            HOLD, None, in_tokens, out_tokens, None, None,
            "unconfigured", REASON_PRICE, model=model, provider=provider,
        )

    def should_govern(self) -> bool:
        return self.config.enforcement != "off" and (
            self.config.configured() or self.config.enforcement == "on"
        )

    # ---------------------------------------------------------------- reserve
    def _degradation_plan(
        self,
        *,
        expected_cost: Decimal,
        expected_tokens: int,
        model: str,
        reason: str,
        suggested: bool,
        applied: bool = False,
    ) -> DegradationPlan:
        ratio = float(self.config.degrade_reduce_ratio)
        reduced_cost = (expected_cost * Decimal(str(1.0 - ratio))).quantize(
            Decimal("0.000001")
        )
        reduced_tokens = max(0, int(expected_tokens * (1.0 - ratio)))
        fallback = self.config.fallback_models.get(model) if model else None
        return DegradationPlan(
            suggested=suggested,
            applied=applied,
            reason=reason,
            level=1,
            reduce_ratio=ratio,
            reduced_tokens=reduced_tokens,
            reduced_cost_cny=reduced_cost,
            fallback_model=fallback,
            fallback_configured=fallback is not None,
        )

    def _evaluate_checks(
        self,
        *,
        expected_cost: Decimal,
        expected_tokens: int,
        expected_calls: int,
        task_id: str,
        run_id: str,
        day: str,
        month: str,
        task: Mapping[str, Any],
        run: Mapping[str, Any],
        rate: Mapping[str, Any],
        active: Mapping[str, Mapping[str, Any]],
        spends: Mapping[str, float],
        breaker: Mapping[str, Any],
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
        budgets: Dict[str, Dict[str, Any]] = {}
        reasons: List[str] = []

        if bool(breaker.get("tripped")):
            reasons.append(REASON_BREAKER)

        global_view = self._budget_view(
            "global",
            budget=self.config.global_budget_cny,
            spend=float(spends.get("global", 0.0)),
            reserved=float(self._sum_active(active, "expected_cost_cny")),
            expected=float(expected_cost),
            reason=REASON_GLOBAL,
        )
        daily_view = self._budget_view(
            "daily",
            budget=self.config.daily_budget_cny,
            spend=float(spends.get("daily", 0.0)),
            reserved=float(self._sum_active(active, "expected_cost_cny", day=day)),
            expected=float(expected_cost),
            reason=REASON_DAILY,
        )
        monthly_view = self._budget_view(
            "monthly",
            budget=self.config.monthly_budget_cny,
            spend=float(spends.get("monthly", 0.0)),
            reserved=float(self._sum_active(active, "expected_cost_cny", month=month)),
            expected=float(expected_cost),
            reason=REASON_MONTHLY,
        )
        try:
            task_spend = float(task.get("cost_cny", 0.0) or 0.0)
        except (TypeError, ValueError):
            task_spend = 0.0
        task_view = self._budget_view(
            "task",
            budget=self.config.task_budget_cny,
            spend=task_spend,
            reserved=float(self._sum_active(active, "expected_cost_cny", task_id=task_id)),
            expected=float(expected_cost),
            reason=REASON_TASK,
        )
        try:
            run_spend = float(run.get("cost_cny", 0.0) or 0.0)
        except (TypeError, ValueError):
            run_spend = 0.0
        run_view = self._budget_view(
            "run",
            budget=self.config.run_budget_cny,
            spend=run_spend,
            reserved=float(self._sum_active(active, "expected_cost_cny", run_id=run_id)),
            expected=float(expected_cost),
            reason=REASON_RUN,
        )
        budgets.update(
            {
                "global": global_view,
                "daily": daily_view,
                "monthly": monthly_view,
                "task": task_view,
                "run": run_view,
            }
        )
        for view in budgets.values():
            if view["status"] == DENY:
                reasons.append(view["reason"])

        if self.config.task_token_limit > 0:
            try:
                task_tokens = int(task.get("tokens", 0) or 0)
            except (TypeError, ValueError):
                task_tokens = 0
            reserved_tokens = 0
            for record in active.values():
                if str(record.get("task_id") or "") == task_id:
                    try:
                        reserved_tokens += int(record.get("expected_tokens", 0) or 0)
                    except (TypeError, ValueError):
                        pass
            if task_tokens + reserved_tokens + expected_tokens > self.config.task_token_limit:
                reasons.append(REASON_TASK_TOKENS)

        if self.config.task_call_limit > 0:
            try:
                task_calls = int(task.get("calls", 0) or 0)
            except (TypeError, ValueError):
                task_calls = 0
            if task_calls + expected_calls > self.config.task_call_limit:
                reasons.append(REASON_TASK_CALLS)

        if self.config.rate_limit_per_minute > 0:
            try:
                rate_count = int(rate.get("count", 0) or 0)
            except (TypeError, ValueError):
                rate_count = 0
            if rate.get("minute") != utc_now_iso()[:16]:
                rate_count = 0
            if rate_count + expected_calls > self.config.rate_limit_per_minute:
                reasons.append(REASON_RATE)

        return budgets, list(dict.fromkeys(reasons))

    def reserve_run(
        self,
        *,
        task_id: str,
        run_id: str = "",
        expected_cost_cny: Any = None,
        expected_tokens: Any = None,
        expected_calls: int = 1,
        input_tokens: Any = 0,
        output_tokens: Any = 0,
        model: str = "",
        provider: str = "",
        task_type: str = TASK_TYPE,
        source: str = "service",
        now: Optional[datetime] = None,
    ) -> CostDecision:
        """Reserve run headroom atomically; never calls a model."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise CostInputError("task_id", "invalid")
        assert_safe_identifier(task_id, field="task_id", strict=True)
        if run_id:
            assert_safe_identifier(run_id, field="run_id", strict=True)
        if isinstance(expected_calls, bool) or not isinstance(expected_calls, int) or expected_calls < 1:
            raise CostInputError("expected_calls", "invalid")
        if expected_tokens is None:
            expected_tokens = self.config.expected_tokens
        try:
            expected_tokens = to_token_count(expected_tokens, "expected_tokens")
        except CostInputError as exc:
            return self._decision_without_state(
                task_id, run_id, INVALID_INPUT, (f"{exc.field_name}:{exc.kind}",),
                model=model, provider=provider,
            )

        current = self._now(now)
        estimate = self.estimate(
            input_tokens=input_tokens,
            output_tokens=(
                output_tokens
                if (int(input_tokens or 0) + int(output_tokens or 0)) > 0
                else expected_tokens
            ),
            expected_cost_cny=expected_cost_cny,
            model=model,
            provider=provider,
        )
        if not self.should_govern():
            return CostDecision(
                task_id=task_id,
                run_id=run_id,
                status=OBSERVE,
                allowed=True,
                reason_codes=(REASON_OBSERVE,),
                reservation_id="",
                estimate=estimate,
                budgets={},
                degradation=None,
                enforcement=self.config.enforcement,
            )

        if estimate.status == INVALID_INPUT:
            return self._decision_without_state(
                task_id, run_id, INVALID_INPUT, (estimate.reason,),
                model=model, provider=provider,
            )
        if estimate.status == HOLD:
            return self._decision_without_state(
                task_id, run_id, HOLD, (estimate.reason,),
                model=model, provider=provider, estimate=estimate,
            )

        expected_cost = estimate.cost_cny or Decimal("0")
        day = current.date().isoformat()
        month = current.strftime("%Y-%m")
        effective_run_id = run_id or task_id

        with self._atomic() as session:
            reservations = self._load_reservations(session)
            expired = self._expire(reservations, current)
            active = reservations["active"]

            # One active reservation per (task, run) is idempotent. A retry
            # after settlement/release creates a new reservation.
            for existing_id, existing in active.items():
                if (
                    str(existing.get("task_id") or "") == task_id
                    and str(existing.get("run_id") or "") == effective_run_id
                ):
                    existing_plan = existing.get("degradation")
                    plan = None
                    if isinstance(existing_plan, dict):
                        try:
                            plan = DegradationPlan(
                                suggested=bool(existing_plan.get("suggested")),
                                applied=bool(existing_plan.get("applied")),
                                reason=str(existing_plan.get("reason") or ""),
                                level=int(existing_plan.get("level", 1)),
                                reduce_ratio=float(existing_plan.get("reduce_ratio", 0.5)),
                                reduced_tokens=int(existing_plan.get("reduced_tokens", 0)),
                                reduced_cost_cny=(
                                    Decimal(str(existing_plan.get("reduced_cost_cny")))
                                    if existing_plan.get("reduced_cost_cny") is not None
                                    else None
                                ),
                                fallback_model=existing_plan.get("fallback_model"),
                                fallback_configured=bool(existing_plan.get("fallback_configured")),
                            )
                        except (TypeError, ValueError):
                            plan = None
                    return CostDecision(
                        task_id=task_id,
                        run_id=run_id,
                        status=DEGRADE if plan is not None else ALLOW,
                        allowed=True,
                        reason_codes=(REASON_ALREADY_RESERVED,),
                        reservation_id=existing_id,
                        estimate=estimate,
                        budgets={},
                        degradation=plan,
                        enforcement=self.config.enforcement,
                    )

            daily_spend = self._read_number(session, DAILY_PREFIX + day, 0.0)
            monthly_spend = self._read_number(session, MONTHLY_PREFIX + month, 0.0)
            global_spend = self._read_number(session, GLOBAL_SPENT_KEY, 0.0)
            period_raw = self._read_raw(session, GLOBAL_PERIOD_KEY)
            if period_raw:
                try:
                    started = parse_iso(period_raw)
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if current - started > timedelta(days=30):
                        global_spend = 0.0
                except ValueError:
                    pass

            task = self._read_json(session, TASK_PREFIX + task_id, {})
            if not isinstance(task, dict):
                task = {}
            run = self._read_json(session, RUN_PREFIX + effective_run_id, {})
            if not isinstance(run, dict):
                run = {}
            rate = self._read_json(session, RATE_PREFIX + task_id, {})
            if not isinstance(rate, dict):
                rate = {}
            breaker = self._read_json(session, BREAKER_KEY, {})
            if not isinstance(breaker, dict):
                breaker = {}

            budgets, reasons = self._evaluate_checks(
                expected_cost=expected_cost,
                expected_tokens=expected_tokens,
                expected_calls=expected_calls,
                task_id=task_id,
                run_id=effective_run_id,
                day=day,
                month=month,
                task=task,
                run=run,
                rate=rate,
                active=active,
                spends={
                    "daily": daily_spend,
                    "monthly": monthly_spend,
                    "global": global_spend,
                },
                breaker=breaker,
            )

            degradation: Optional[DegradationPlan] = None
            status = ALLOW
            reservation_cost = expected_cost
            reservation_tokens = expected_tokens
            reservation_calls = expected_calls

            if reasons:
                # Try one explicit, bounded degradation step before denying.
                plan = self._degradation_plan(
                    expected_cost=expected_cost,
                    expected_tokens=expected_tokens,
                    model=model,
                    reason=REASON_DEGRADE_EXHAUSTED,
                    suggested=False,
                )
                reduced_budgets, reduced_reasons = self._evaluate_checks(
                    expected_cost=plan.reduced_cost_cny or Decimal("0"),
                    expected_tokens=plan.reduced_tokens,
                    expected_calls=min(expected_calls, 1),
                    task_id=task_id,
                    run_id=effective_run_id,
                    day=day,
                    month=month,
                    task=task,
                    run=run,
                    rate=rate,
                    active=active,
                    spends={
                        "daily": daily_spend,
                        "monthly": monthly_spend,
                        "global": global_spend,
                    },
                    breaker=breaker,
                )
                if reduced_reasons:
                    reasons = list(dict.fromkeys([*reasons, *reduced_reasons, REASON_DEGRADE_EXHAUSTED]))
                    status = DENY
                else:
                    budgets = reduced_budgets
                    degradation = replace(
                        plan,
                        suggested=True,
                        reason=REASON_DEGRADE_APPLIED,
                        applied=False,
                    )
                    status = DEGRADE
                    reservation_cost = plan.reduced_cost_cny or Decimal("0")
                    reservation_tokens = plan.reduced_tokens
                    reservation_calls = 1
                    reasons = [REASON_DEGRADE_APPLIED]
            elif self.config.degrade_threshold_ratio > 0:
                usages = [
                    float(view["usage_ratio"])
                    for view in budgets.values()
                    if view.get("usage_ratio") is not None
                ]
                if usages and max(usages) >= self.config.degrade_threshold_ratio:
                    degradation = self._degradation_plan(
                        expected_cost=expected_cost,
                        expected_tokens=expected_tokens,
                        model=model,
                        reason="budget_usage_threshold",
                        suggested=True,
                    )

            allowed = status in (ALLOW, DEGRADE)
            reservation_id = ""
            if allowed:
                reservation_id = f"rc-{uuid.uuid4().hex}"
                record = {
                    "reservation_id": reservation_id,
                    "task_id": task_id,
                    "run_id": effective_run_id,
                    "task_type": task_type,
                    "source": source,
                    "model": model,
                    "provider": provider,
                    "expected_cost_cny": str(reservation_cost),
                    "expected_tokens": reservation_tokens,
                    "expected_calls": reservation_calls,
                    "day": day,
                    "month": month,
                    "created_at": self._iso(current),
                    "expires_at": self._iso(
                        current + timedelta(seconds=self.config.reservation_ttl_seconds)
                    ),
                    "status": "ACTIVE",
                    "degradation": None if degradation is None else degradation.to_dict(),
                }
                active[reservation_id] = record
                self._save_reservations(session, reservations)

            reason_codes = tuple(dict.fromkeys(reasons)) if reasons else (REASON_OK,)
            if status == OBSERVE:
                reason_codes = (REASON_OBSERVE,)
            audit_id = self._audit(
                session,
                operation="JMR_COST_RESERVE",
                status=STATUS_ALLOWED if allowed else STATUS_DENIED,
                task_id=task_id,
                run_id=run_id,
                failure_reason="" if allowed else ",".join(reason_codes),
                extra={
                    "decision": status,
                    "reservation_id": reservation_id,
                    "estimated_cost_cny": str(estimate.cost_cny or "0"),
                    "reserved_cost_cny": str(reservation_cost),
                    "expected_tokens": reservation_tokens,
                    "expected_calls": reservation_calls,
                    "model": model,
                    "provider": provider,
                    "task_type": task_type,
                    "source": source,
                    "reason_codes": list(reason_codes),
                    "budgets": budgets,
                    "degradation": None if degradation is None else degradation.to_dict(),
                    "enforcement": self.config.enforcement,
                },
            )
            return CostDecision(
                task_id=task_id,
                run_id=run_id,
                status=status,
                allowed=allowed,
                reason_codes=reason_codes,
                reservation_id=reservation_id,
                estimate=estimate,
                budgets=budgets,
                degradation=degradation,
                enforcement=self.config.enforcement,
                audit_id=audit_id,
            )

    def _decision_without_state(
        self,
        task_id: str,
        run_id: str,
        status: str,
        reasons: Tuple[str, ...],
        *,
        model: str = "",
        provider: str = "",
        estimate: Optional[CostEstimate] = None,
    ) -> CostDecision:
        estimate = estimate or self.estimate(model=model, provider=provider)
        return CostDecision(
            task_id=task_id,
            run_id=run_id,
            status=status,
            allowed=False,
            reason_codes=tuple(dict.fromkeys(reasons)),
            reservation_id="",
            estimate=estimate,
            budgets={},
            degradation=None,
            enforcement=self.config.enforcement,
        )


    # ---------------------------------------------------------------- settle
    def _audit(
        self,
        session,
        *,
        operation: str,
        status: str,
        task_id: str,
        run_id: str,
        failure_reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        try:
            return self.audit_bus.record(
                principal_id="svc:jmr-cost",
                principal_type="SERVICE",
                operation=operation,
                object_ref=task_id or "GLOBAL",
                trigger="JMR",
                status=status,
                failure_reason=failure_reason,
                task_id=task_id,
                run_id=run_id,
                extra=extra,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001 - audit must not be silent
            raise CostAuditError("cost audit could not be written") from exc

    def settle_run(
        self,
        reservation_id: str,
        *,
        actual_cost_cny: Any = None,
        actual_tokens: Any = 0,
        actual_calls: Any = 0,
        status: str = SUCCEEDED,
        retry: bool = False,
        tokens_source: str = "caller",
        now: Optional[datetime] = None,
    ) -> SettlementResult:
        """Consume a reservation once and write the run-level actual cost.

        Task/global/period spend is owned by the existing service/CostGovernor
        path; this method never adds it again. It writes only the run dimension
        (``p1.cost.batch.*.cost_cny``).
        """
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise CostReservationError("reservation_id is required")
        current = self._now(now)
        try:
            actual_tokens = to_token_count(actual_tokens, "actual_tokens")
            actual_calls = to_token_count(actual_calls, "actual_calls")
        except CostInputError:
            raise
        result_status = self._result_status(status)

        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            active = reservations["active"]
            record = active.get(reservation_id)
            if record is None:
                settled = reservations["settled"].get(reservation_id)
                if settled is not None:
                    return SettlementResult(
                        reservation_id,
                        True,
                        True,
                        str(settled.get("result_status") or settled.get("status") or ""),
                        str(settled.get("expected_cost_cny") or "0"),
                        str(settled.get("actual_cost_cny") or "0"),
                        str(settled.get("overage_cny") or "0"),
                        REASON_ALREADY_SETTLED,
                        str(settled.get("audit_id") or ""),
                    )
                released = reservations["released"].get(reservation_id)
                if released is not None and (
                    str(released.get("status") or "") != "EXPIRED"
                ):
                    return SettlementResult(
                        reservation_id,
                        False,
                        False,
                        str(released.get("status") or "RELEASED"),
                        str(released.get("expected_cost_cny") or "0"),
                        "0",
                        "0",
                        "reservation_released",
                    )
                if released is not None:
                    # JMR-08 audit P2: a run that outlived the reservation TTL
                    # still consumed spend. Settle the expired reservation
                    # instead of silently dropping the actual cost.
                    reservations["released"].pop(reservation_id, None)
                    record = dict(released)
                else:
                    audit_id = self._audit(
                        session,
                        operation="JMR_COST_SETTLE",
                        status=STATUS_FAILED,
                        task_id="",
                        run_id="",
                        failure_reason=REASON_SETTLE_MISSING,
                        extra={
                            "reservation_id": reservation_id,
                            "reason": REASON_SETTLE_MISSING,
                        },
                    )
                    return SettlementResult(
                        reservation_id, False, False, "MISSING", "0", "0", "0",
                        REASON_SETTLE_MISSING, audit_id,
                    )

            try:
                if actual_cost_cny is None:
                    actual = Decimal(str(record.get("expected_cost_cny") or "0"))
                else:
                    actual = to_money(actual_cost_cny, "actual_cost_cny")
                reserved = Decimal(str(record.get("expected_cost_cny") or "0"))
                expected_tokens = int(record.get("expected_tokens", 0) or 0)
            except (CostInputError, ValueError) as exc:
                raise CostReservationError("reservation record is invalid") from exc

            overage = max(actual - reserved, Decimal("0"))
            active.pop(reservation_id, None)
            settled_record = dict(record)
            settled_record.update(
                {
                    "status": "SETTLED",
                    "result_status": result_status,
                    "settled_at": self._iso(current),
                    "actual_cost_cny": str(actual),
                    "actual_tokens": actual_tokens,
                    "actual_calls": actual_calls,
                    "overage_cny": str(overage),
                    "retry": bool(retry),
                }
            )

            run_key = RUN_PREFIX + str(record.get("run_id") or record.get("task_id") or "")
            run = self._read_json(session, run_key, {})
            if not isinstance(run, dict):
                run = {}
            try:
                run_cost = float(run.get("cost_cny", 0.0) or 0.0)
            except (TypeError, ValueError):
                run_cost = 0.0
            try:
                run_tokens = int(run.get("tokens", 0) or 0)
            except (TypeError, ValueError):
                run_tokens = 0
            run["cost_cny"] = round(run_cost + float(actual), 6)
            run["tokens"] = run_tokens + actual_tokens
            if retry:
                run["retry_calls"] = int(run.get("retry_calls", 0) or 0) + actual_calls
            self._write_raw(session, run_key, run)

            # Re-audit N-P2-2: the service already records period spend per
            # stage. Adding the run total here double-counts it, so this layer
            # never writes daily/monthly keys. ``manage_period_spend`` is kept
            # only for configuration compatibility and is ignored.

            if overage > 0 and self.config.trip_on_overage:
                breaker = self._read_json(session, BREAKER_KEY, {})
                if not isinstance(breaker, dict):
                    breaker = {}
                breaker["tripped"] = True
                breaker["reason"] = REASON_OVERAGE
                breaker["tripped_at"] = self._iso(current)
                self._write_raw(session, BREAKER_KEY, breaker)
                settled_record["breaker_tripped"] = True

            reservations["settled"][reservation_id] = settled_record
            self._save_reservations(session, reservations)

            audit_id = self._audit(
                session,
                operation="JMR_COST_SETTLE",
                status=STATUS_SUCCEEDED if result_status == SUCCEEDED else STATUS_FAILED,
                task_id=str(record.get("task_id") or ""),
                run_id=str(record.get("run_id") or ""),
                failure_reason="" if result_status == SUCCEEDED else f"run_status:{result_status}",
                extra={
                    "reservation_id": reservation_id,
                    "estimated_cost_cny": str(reserved),
                    "actual_cost_cny": str(actual),
                    "overage_cny": str(overage),
                    "reserved_tokens": expected_tokens,
                    "actual_tokens": actual_tokens,
                    "actual_tokens_source": str(tokens_source or "caller")[:64],
                    "actual_calls": actual_calls,
                    "model": str(record.get("model") or ""),
                    "provider": str(record.get("provider") or ""),
                    "task_type": str(record.get("task_type") or ""),
                    "result_status": result_status,
                    "retry": bool(retry),
                    "overage": overage > 0,
                    "degradation": record.get("degradation"),
                },
            )
            settled_record["audit_id"] = audit_id
            reservations["settled"][reservation_id] = settled_record
            self._save_reservations(session, reservations)
            return SettlementResult(
                reservation_id,
                True,
                False,
                result_status,
                str(reserved),
                str(actual),
                str(overage),
                REASON_OK if overage == 0 else REASON_OVERAGE,
                audit_id,
            )

    def release_run(
        self,
        reservation_id: str,
        *,
        reason: str = "released",
        now: Optional[datetime] = None,
    ) -> bool:
        """Release an unused reservation. Idempotent; never touches spend."""
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise CostReservationError("reservation_id is required")
        current = self._now(now)
        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            active = reservations["active"]
            record = active.pop(reservation_id, None)
            if record is None:
                return reservation_id in reservations["settled"] or reservation_id in reservations["released"]
            released = dict(record)
            released.update(
                {
                    "status": "RELEASED",
                    "released_at": self._iso(current),
                    "reason": str(reason or "released")[:200],
                }
            )
            reservations["released"][reservation_id] = released
            self._save_reservations(session, reservations)
            self._audit(
                session,
                operation="JMR_COST_RELEASE",
                status=STATUS_SUCCEEDED,
                task_id=str(record.get("task_id") or ""),
                run_id=str(record.get("run_id") or ""),
                extra={
                    "reservation_id": reservation_id,
                    "reason": str(reason or "released")[:200],
                    "reserved_cost_cny": str(record.get("expected_cost_cny") or "0"),
                },
            )
            return True

    def record_degradation(
        self,
        *,
        reservation_id: str,
        task_id: str,
        run_id: str,
        plan: DegradationPlan,
        applied: bool,
    ) -> str:
        """Audit a degradation that the caller actually applied."""
        current = self._now()
        with self._atomic() as session:
            audit_id = self._audit(
                session,
                operation="JMR_COST_DEGRADE",
                status=STATUS_SUCCEEDED,
                task_id=task_id,
                run_id=run_id,
                extra={
                    "reservation_id": reservation_id,
                    "applied": bool(applied),
                    "plan": plan.to_dict(),
                    "recorded_at": self._iso(current),
                },
            )
        return audit_id

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        current = self._now()
        day = current.date().isoformat()
        month = current.strftime("%Y-%m")
        if not self.should_govern():
            return {
                "enabled": self.config.enforcement != "off",
                "configured": False,
                "mode": "OBSERVE",
                "reason": REASON_OBSERVE,
                "budgets": self.config.budgets(),
                "spend": {},
                "reserved": {},
                "reservations": {"active": 0, "settled": 0, "released": 0},
                "breaker": {},
                "config": self._config_view(),
                "algorithm_version": ALGORITHM_VERSION,
            }
        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            self._save_reservations(session, reservations)
            active = reservations["active"]
            daily_spend = self._read_number(session, DAILY_PREFIX + day, 0.0)
            monthly_spend = self._read_number(session, MONTHLY_PREFIX + month, 0.0)
            global_spend = self._read_number(session, GLOBAL_SPENT_KEY, 0.0)
            breaker = self._read_json(session, BREAKER_KEY, {})
            if not isinstance(breaker, dict):
                breaker = {}
            budgets = {
                name: self._budget_view(
                    name,
                    budget=budget,
                    spend={
                        "global": global_spend,
                        "daily": daily_spend,
                        "monthly": monthly_spend,
                        "task": 0.0,
                        "run": 0.0,
                    }[name],
                    reserved=float(
                        self._sum_active(
                            active,
                            "expected_cost_cny",
                            day=day if name == "daily" else "",
                            month=month if name == "monthly" else "",
                        )
                    ),
                    expected=0.0,
                    reason={
                        "global": REASON_GLOBAL,
                        "daily": REASON_DAILY,
                        "monthly": REASON_MONTHLY,
                        "task": REASON_TASK,
                        "run": REASON_RUN,
                    }[name],
                )
                for name, budget in self.config.budgets().items()
            }
            reserved_total = sum(
                (Decimal(str(item.get("expected_cost_cny") or "0")) for item in active.values()),
                Decimal("0"),
            )
            return {
                "enabled": True,
                "configured": self.config.configured(),
                "mode": "ENFORCE",
                "budgets": budgets,
                "spend": {
                    "global_cny": round(global_spend, 6),
                    "daily_cny": round(daily_spend, 6),
                    "monthly_cny": round(monthly_spend, 6),
                    "reserved_cny": round(float(reserved_total), 6),
                },
                "reserved": {"total_cny": round(float(reserved_total), 6)},
                "reservations": {
                    "active": len(active),
                    "settled": len(reservations["settled"]),
                    "released": len(reservations["released"]),
                },
                "breaker": breaker,
                "config": self._config_view(),
                "algorithm_version": ALGORITHM_VERSION,
            }

    def _config_view(self) -> Dict[str, Any]:
        return {
            "enforcement": self.config.enforcement,
            "daily_budget_cny": self.config.daily_budget_cny,
            "monthly_budget_cny": self.config.monthly_budget_cny,
            "task_budget_cny": self.config.task_budget_cny,
            "run_budget_cny": self.config.run_budget_cny,
            "global_budget_cny": self.config.global_budget_cny,
            "max_single_call_cny": self.config.max_single_call_cny,
            "task_token_limit": self.config.task_token_limit,
            "task_call_limit": self.config.task_call_limit,
            "rate_limit_per_minute": self.config.rate_limit_per_minute,
            "breaker_threshold": self.config.breaker_threshold,
            "degrade_threshold_ratio": self.config.degrade_threshold_ratio,
            "degrade_reduce_ratio": self.config.degrade_reduce_ratio,
            "reservation_ttl_seconds": self.config.reservation_ttl_seconds,
            "trip_on_overage": self.config.trip_on_overage,
            "model_price_count": len(self.config.model_prices),
        }

    def active_reservations(self) -> List[Dict[str, Any]]:
        current = self._now()
        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            self._save_reservations(session, reservations)
            return sorted(
                (dict(item) for item in reservations["active"].values()),
                key=lambda item: str(item.get("created_at") or ""),
            )

    def task_status(self, task_id: str) -> Dict[str, Any]:
        """Single-task spend/reservation view (read-only)."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        current = self._now()
        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            self._save_reservations(session, reservations)
            task = self._read_json(session, TASK_PREFIX + task_id, {})
            if not isinstance(task, dict):
                task = {}
            reserved = self._sum_active(
                reservations["active"], "expected_cost_cny", task_id=task_id
            )
            try:
                spend = float(task.get("cost_cny", 0.0) or 0.0)
            except (TypeError, ValueError):
                spend = 0.0
            view = self._budget_view(
                "task",
                budget=self.config.task_budget_cny,
                spend=spend,
                reserved=float(reserved),
                expected=0.0,
                reason=REASON_TASK,
            )
            return {
                "task_id": task_id,
                "spent_cny": round(spend, 6),
                "reserved_cny": round(float(reserved), 6),
                "tokens": int(task.get("tokens", 0) or 0),
                "calls": int(task.get("calls", 0) or 0),
                "budget": view,
            }

    def run_status(self, run_id: str) -> Dict[str, Any]:
        """Single-run spend/reservation view (read-only)."""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        current = self._now()
        with self._atomic() as session:
            reservations = self._load_reservations(session)
            self._expire(reservations, current)
            self._save_reservations(session, reservations)
            run = self._read_json(session, RUN_PREFIX + run_id, {})
            if not isinstance(run, dict):
                run = {}
            reserved = self._sum_active(
                reservations["active"], "expected_cost_cny", run_id=run_id
            )
            try:
                spend = float(run.get("cost_cny", 0.0) or 0.0)
            except (TypeError, ValueError):
                spend = 0.0
            view = self._budget_view(
                "run",
                budget=self.config.run_budget_cny,
                spend=spend,
                reserved=float(reserved),
                expected=0.0,
                reason=REASON_RUN,
            )
            return {
                "run_id": run_id,
                "spent_cny": round(spend, 6),
                "reserved_cny": round(float(reserved), 6),
                "tokens": int(run.get("tokens", 0) or 0),
                "calls": int(run.get("calls", 0) or 0),
                "budget": view,
            }

    # ------------------------------------------------------------------ report
    def report(self, *, days: int = 30) -> Dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3660:
            raise ValueError("days must be an integer in [1, 3660]")
        current = self._now()
        since = (current - timedelta(days=days)).isoformat()
        try:
            with self.manager.session() as session:
                rows = session.execute(
                    text(
                        "SELECT action, detail_json, created_at FROM memory_audit_logs "
                        "WHERE action LIKE 'P1:JMR_COST_%' AND created_at >= :since "
                        "ORDER BY created_at ASC LIMIT 20000"
                    ),
                    {"since": since},
                ).mappings().all()
        except OperationalError as exc:
            raise CostStateError("audit table is not available") from exc

        total_cost = Decimal("0")
        total_estimated = Decimal("0")
        total_overage = Decimal("0")
        by_model: Dict[str, Dict[str, Any]] = {}
        by_provider: Dict[str, Dict[str, Any]] = {}
        by_task_type: Dict[str, Dict[str, Any]] = {}
        rejections: Dict[str, int] = {}
        denied_count = 0
        degradation_count = 0
        degradation_suggested_count = 0
        settled_count = 0
        trend: Dict[str, Dict[str, Any]] = {}
        anomalies: List[Dict[str, Any]] = []

        for row in rows:
            action = str(row.get("action") or "")
            try:
                detail = json.loads(row.get("detail_json") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(detail, dict):
                continue
            extra = detail.get("extra")
            if not isinstance(extra, dict):
                extra = {}
            created = str(row.get("created_at") or "")
            date = created[:10]
            trend.setdefault(date, {"date": date, "cost_cny": 0.0, "settled": 0, "denied": 0})
            if action.endswith(":DENIED"):
                denied_count += 1
                degradation = extra.get("degradation")
                if isinstance(degradation, dict) and degradation.get("suggested"):
                    degradation_suggested_count += 1
                reason_codes = extra.get("reason_codes") or []
                for reason in reason_codes if isinstance(reason_codes, list) else [reason_codes]:
                    rejections[str(reason)] = rejections.get(str(reason), 0) + 1
                trend[date]["denied"] += 1
                continue
            if "JMR_COST_DEGRADE" in action:
                degradation_count += 1
            if "JMR_COST_RESERVE" in action:
                degradation = extra.get("degradation")
                if isinstance(degradation, dict) and degradation.get("suggested"):
                    degradation_suggested_count += 1
            if "JMR_COST_SETTLE" not in action or not action.endswith(":SUCCEEDED") and not action.endswith(":FAILED"):
                continue
            settled_count += 1
            try:
                actual = Decimal(str(extra.get("actual_cost_cny") or "0"))
                estimated = Decimal(str(extra.get("estimated_cost_cny") or "0"))
                overage = Decimal(str(extra.get("overage_cny") or "0"))
            except Exception:
                continue
            total_cost += actual
            total_estimated += estimated
            total_overage += overage
            model = str(extra.get("model") or "unknown")
            provider = str(extra.get("provider") or "unknown")
            task_type = str(extra.get("task_type") or "unknown")
            for bucket, key in ((by_model, model), (by_provider, provider), (by_task_type, task_type)):
                item = bucket.setdefault(
                    key, {"cost_cny": 0.0, "estimated_cny": 0.0, "settled": 0, "overage_cny": 0.0}
                )
                item["cost_cny"] = round(item["cost_cny"] + float(actual), 6)
                item["estimated_cny"] = round(item["estimated_cny"] + float(estimated), 6)
                item["settled"] += 1
                item["overage_cny"] = round(item["overage_cny"] + float(overage), 6)
            trend[date]["cost_cny"] = round(trend[date]["cost_cny"] + float(actual), 6)
            trend[date]["settled"] += 1
            if overage > 0:
                anomalies.append(
                    {
                        "reservation_id": str(extra.get("reservation_id") or ""),
                        "actual_cny": str(actual),
                        "estimated_cny": str(estimated),
                        "overage_cny": str(overage),
                        "model": model,
                        "provider": provider,
                        "created_at": created,
                    }
                )

        return {
            "days": days,
            "settled_count": settled_count,
            "total_cost_cny": float(total_cost),
            "total_estimated_cny": float(total_estimated),
            "total_overage_cny": float(total_overage),
            "by_model": by_model,
            "by_provider": by_provider,
            "by_task_type": by_task_type,
            "rejections": rejections,
            "rejection_count": sum(rejections.values()),
            "denied_count": denied_count,
            "degradation_count": degradation_count,
            "degradation_suggested_count": degradation_suggested_count,
            "trend": [trend[key] for key in sorted(trend)],
            "anomalies": anomalies[-100:],
            "ledger": self.status(),
            "algorithm_version": ALGORITHM_VERSION,
        }

    # ------------------------------------------------------------------ breaker
    def reset_breaker(self, *, operator_confirmed: bool = False) -> Dict[str, Any]:
        if operator_confirmed is not True:
            raise CostGovernanceError("manual cost-breaker reset requires explicit confirmation")
        current = self._now()
        with self._atomic() as session:
            breaker = {"tripped": False, "anomalies": 0, "reason": ""}
            self._write_raw(session, BREAKER_KEY, breaker)
            self._audit(
                session,
                operation="JMR_COST_BREAKER_RESET",
                status=STATUS_SUCCEEDED,
                task_id="GLOBAL",
                run_id="",
                extra={"operator_confirmed": True, "reset_at": self._iso(current)},
            )
        return breaker


# ---------------------------------------------------------------------------
# Service wrapper
# ---------------------------------------------------------------------------
class CostGovernedJMRService:
    """Wrap ``JMRService`` with run-level cost admission and settlement.

    The inner service remains the only executor and the only writer of
    ``memory_records`` / derived tables. This wrapper never reads memory text.
    Default (unconfigured) behaviour is OBSERVE and delegates unchanged, so the
    existing JMR tests and API contracts are preserved.
    """

    def __init__(self, inner: JMRService, governance: CostGovernance) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_governance", governance)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def config(self):
        return self._inner.config

    @config.setter
    def config(self, value) -> None:
        # JMR-05 temporarily replaces the stage timeout; the replacement must
        # land on the real service, not on this proxy.
        self._inner.config = value

    @property
    def cost_governance(self) -> CostGovernance:
        return self._governance

    def run(
        self,
        *,
        task_id: str,
        run_id: str = "",
        memory_type: Optional[str] = None,
        resume: bool = False,
    ) -> JMRRunResult:
        # Identifier guard runs before the OBSERVE branch: observe mode still
        # persists task/run ids through the inner service (JMR-07).
        assert_safe_identifier(task_id, field="task_id", strict=True)
        if run_id:
            assert_safe_identifier(run_id, field="run_id", strict=True)
        governance = self._governance
        if not governance.should_govern():
            return self._inner.run(
                task_id=task_id, run_id=run_id, memory_type=memory_type, resume=resume
            )

        checker = getattr(self._inner, "_require_p1_schema", None)
        if callable(checker):
            checker()

        task = None
        registry = getattr(self._inner, "task_registry", None)
        if registry is not None:
            task = registry.get(task_id)
            if task is not None and task.get("task_type") != TASK_TYPE:
                # Let the inner service raise its normal state conflict.
                return self._inner.run(
                    task_id=task_id, run_id=run_id, memory_type=memory_type, resume=resume
                )
        task_status = str(task.get("status") or "") if task else ""
        will_execute = (
            task is None
            or task_status == CREATED
            or (resume and task_status in (PAUSED, FAILED, SUCCEEDED))
        )
        if not will_execute:
            return self._inner.run(
                task_id=task_id, run_id=run_id, memory_type=memory_type, resume=resume
            )

        cfg = self._inner.config
        stages = JMR_STAGE_COUNT
        expected_cost = float(getattr(cfg, "expected_cost_cny", 0.0) or 0.0) * stages
        expected_tokens = int(getattr(cfg, "expected_tokens", 0) or 0) * stages
        expected_calls = int(getattr(cfg, "model_calls_per_stage", 1) or 1) * stages
        try:
            decision = governance.reserve_run(
                task_id=task_id,
                run_id=run_id,
                expected_cost_cny=expected_cost,
                expected_tokens=expected_tokens,
                expected_calls=expected_calls,
                task_type=TASK_TYPE,
                source="jmr_service",
            )
        except CostGovernanceError as exc:
            logger.exception("cost admission failed for task=%s", task_id)
            self._fail_task_for_cost(
                task_id, run_id, (f"cost_state_invalid:{getattr(exc, 'code', 'error')}",), resume
            )
            return self._blocked_result(
                task_id, run_id, (f"cost_state_invalid:{getattr(exc, 'code', 'error')}",)
            )
        except CostInputError as exc:
            reasons = (f"{exc.field_name}:{exc.kind}",)
            self._fail_task_for_cost(task_id, run_id, reasons, resume)
            return self._blocked_result(task_id, run_id, reasons)

        if not decision.allowed:
            self._fail_task_for_cost(
                task_id, run_id, decision.reason_codes, resume
            )
            return self._blocked_result(task_id, run_id, decision.reason_codes)

        original_config = self._inner.config
        applied = False
        try:
            if decision.status == DEGRADE and decision.degradation is not None:
                degraded = self._degraded_config(original_config, decision.degradation)
                if degraded is None:
                    governance.release_run(
                        decision.reservation_id, reason="degradation_unavailable"
                    )
                    reasons = (REASON_DEGRADE_UNAVAILABLE,)
                    self._fail_task_for_cost(task_id, run_id, reasons, resume)
                    return self._blocked_result(task_id, run_id, reasons)
                self._inner.config = degraded
                applied = True
                governance.record_degradation(
                    reservation_id=decision.reservation_id,
                    task_id=task_id,
                    run_id=run_id,
                    plan=decision.degradation,
                    applied=True,
                )

            result = self._inner.run(
                task_id=task_id, run_id=run_id, memory_type=memory_type, resume=resume
            )
        except Exception:
            governance.release_run(decision.reservation_id, reason="exception")
            raise
        finally:
            if applied:
                self._inner.config = original_config

        actual = float(getattr(result, "cost_cny", 0.0) or 0.0)
        result_status = str(getattr(result, "status", "") or "")
        settled_calls = 1 if decision.status == DEGRADE else expected_calls
        if actual <= 0 and result_status == SUCCEEDED:
            # The committed JMR baseline has no cost_cny field. Using the
            # admitted estimate is the only truthful fallback; it is recorded
            # as an estimate and the reservation overage is still checked.
            actual = float(decision.estimate.cost_cny or 0.0)
        try:
            governance.settle_run(
                decision.reservation_id,
                actual_cost_cny=actual,
                actual_tokens=0,
                actual_calls=settled_calls,
                status=result_status,
                retry=bool(resume),
                tokens_source="unavailable",
            )
        except Exception:  # noqa: BLE001 - the run already happened
            # JMR-08 audit P1-3: do NOT release here. The run already consumed
            # spend; releasing would erase it. The reservation stays recorded
            # (and can be settled later, including after TTL expiry) and the
            # failure is visible in the logs.
            logger.exception(
                "cost settlement failed for task=%s reservation=%s; "
                "reservation kept for retry",
                task_id,
                decision.reservation_id,
            )
        return result

    def _fail_task_for_cost(
        self,
        task_id: str,
        run_id: str,
        reasons: Iterable[str],
        resume: bool,
    ) -> None:
        registry = getattr(self._inner, "task_registry", None)
        if registry is None:
            return
        reason = "cost_governance_blocked:" + ",".join(dict.fromkeys(str(x) for x in reasons))
        try:
            task = registry.get(task_id)
            if task is None:
                registry.create(
                    task_id,
                    TASK_TYPE,
                    owner="svc:jmr-cost",
                    run_id=run_id,
                    metadata={"cost_governance": "denied"},
                )
                task = registry.get(task_id)
            if task is None or task.get("task_type") != TASK_TYPE:
                return
            status = str(task.get("status") or "")
            if status == CREATED:
                registry.start(task_id, run_id=run_id)
                registry.fail(task_id, reason=reason, run_id=run_id)
            elif status == FAILED and resume:
                registry.recover(task_id, run_id=run_id)
                registry.start(task_id, run_id=run_id)
                registry.fail(task_id, reason=reason, run_id=run_id)
            elif status == PAUSED and resume:
                registry.resume(task_id, run_id=run_id)
                registry.fail(task_id, reason=reason, run_id=run_id)
            elif status == RUNNING and resume:
                registry.fail(task_id, reason=reason, run_id=run_id)
        except Exception:  # noqa: BLE001 - denial must still return a result
            logger.exception("cost denial could not update task=%s", task_id)

    @staticmethod
    def _blocked_result(
        task_id: str, run_id: str, reasons: Iterable[str]
    ) -> JMRRunResult:
        reason = "cost_governance_blocked:" + ",".join(
            dict.fromkeys(str(x) for x in reasons)
        )
        return JMRRunResult(
            task_id=task_id,
            run_id=run_id,
            status="BLOCKED",
            failure_reason=reason,
        )

    @staticmethod
    def _degraded_config(config: Any, plan: DegradationPlan) -> Optional[Any]:
        updates: Dict[str, Any] = {}
        ratio = float(plan.reduce_ratio)
        if not 0.0 < ratio < 1.0:
            return None
        if hasattr(config, "max_memories"):
            try:
                updates["max_memories"] = max(1, int(config.max_memories * (1.0 - ratio)))
            except (TypeError, ValueError):
                return None
        if hasattr(config, "expected_tokens"):
            try:
                updates["expected_tokens"] = max(
                    0, int(config.expected_tokens * (1.0 - ratio))
                )
            except (TypeError, ValueError):
                return None
        if hasattr(config, "expected_cost_cny"):
            try:
                updates["expected_cost_cny"] = max(
                    0.0, float(config.expected_cost_cny) * (1.0 - ratio)
                )
            except (TypeError, ValueError):
                return None
        if hasattr(config, "model_calls_per_stage"):
            updates["model_calls_per_stage"] = 1
        if not updates:
            return None
        try:
            return replace(config, **updates)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Factory (clean integration point for container / host wiring)
# ---------------------------------------------------------------------------
def build_cost_governed_jmr_service(
    manager,
    *,
    config: Optional[JMRConfig] = None,
    governance_config: Optional[CostGovernanceConfig] = None,
    settings: Any = None,
) -> Tuple[CostGovernedJMRService, CostGovernance]:
    """Build the normal JMR service and wrap it in cost governance.

    This is the additive integration point: it does not modify ``container``
    or any other owner's uncommitted file. A host that already constructs
    ``JMRService.build`` can switch to this factory without changing the
    execution path.
    """
    service = JMRService.build(manager=manager, config=config)
    source_settings = (
        settings
        if settings is not None
        else getattr(getattr(service, "cost_governor", None), "settings", None)
    )
    governance_config = governance_config or CostGovernanceConfig.from_sources(
        source_settings,
        jmr_config=service.config,
        governor=getattr(service, "cost_governor", None),
    )
    governance = CostGovernance(manager, governance_config)
    return CostGovernedJMRService(service, governance), governance


__all__ = [
    "build_cost_governed_jmr_service",
    "CostGovernance",
    "CostGovernedJMRService",
    "CostGovernanceConfig",
    "CostEstimate",
    "CostDecision",
    "DegradationPlan",
    "SettlementResult",
    "CostGovernanceError",
    "CostStateError",
    "CostReservationError",
    "CostAuditError",
    "OBSERVE",
    "DEGRADE",
    "REASON_OBSERVE",
    "REASON_BREAKER",
    "REASON_GLOBAL",
    "REASON_DAILY",
    "REASON_MONTHLY",
    "REASON_TASK",
    "REASON_RUN",
    "REASON_TASK_TOKENS",
    "REASON_TASK_CALLS",
    "REASON_RATE",
    "REASON_PRICE",
    "REASON_UNKNOWN_MODEL",
    "REASON_OVERAGE",
]
