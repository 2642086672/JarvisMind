"""JMR configuration.

All limits are externalised through ``JARVISMIND_JMR_*`` environment variables. Tests
must set them inside the test case (``conftest.py`` erases every ``JARVISMIND_*`` at
session start). Invalid values fail fast instead of silently degrading a
detection threshold.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _env_nonneg_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _env_ratio(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not 0.0 < value <= 1.0:
        raise ValueError(f"{name} must be in (0, 1], got {value}")
    return value


def _env_amount(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0.0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


@dataclass(frozen=True)
class JMRConfig:
    """Immutable JMR limits. The detection thresholds are REAL parameters
    (``detectors.py`` implements the comparisons itself), not dead config.
    """

    page_size: int = 100
    max_memories: int = 200
    duplicate_threshold: float = 0.82
    conflict_max_similarity: float = 0.80
    subsumption_threshold: float = 0.65
    status_min_group: int = 2
    algorithm_version: str = "jmr-v1"
    expected_cost_cny: float = 0.0
    expected_tokens: int = 0
    model_calls_per_stage: int = 1
    # JMR-04/05/06/08: operational limits. These are in-memory policy values;
    # they create no database schema and no automatic execution. A value of 0
    # for a budget means "not configured" (never read as unlimited by the
    # cost-policy helpers; the legacy global CostGovernor ledger still applies).
    stage_timeout_seconds: int = 120
    result_ttl_days: int = 30
    max_retries: int = 3
    breaker_threshold: int = 3
    daily_budget_cny: float = 0.0
    monthly_budget_cny: float = 0.0
    max_single_call_cny: float = 0.0

    def __post_init__(self) -> None:
        if self.page_size <= 0 or self.max_memories <= 0:
            raise ValueError("page_size and max_memories must be > 0")
        if self.page_size > 1000:
            # ModuleProjection caps page_size at 1000 (projection.py:136-145).
            raise ValueError("page_size must be <= 1000 (projection hard cap)")
        for name in (
            "duplicate_threshold",
            "conflict_max_similarity",
            "subsumption_threshold",
        ):
            value = float(getattr(self, name))
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        if self.status_min_group < 2:
            raise ValueError("status_min_group must be >= 2")
        if not self.algorithm_version:
            raise ValueError("algorithm_version must not be empty")
        if self.expected_cost_cny < 0 or self.expected_tokens < 0:
            raise ValueError("cost parameters must be >= 0")
        if self.model_calls_per_stage <= 0:
            raise ValueError("model_calls_per_stage must be > 0")
        if self.stage_timeout_seconds <= 0:
            raise ValueError("stage_timeout_seconds must be > 0")
        if self.result_ttl_days <= 0:
            raise ValueError("result_ttl_days must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.breaker_threshold < 1:
            raise ValueError("breaker_threshold must be >= 1")
        for name in ("daily_budget_cny", "monthly_budget_cny", "max_single_call_cny"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")

    @classmethod
    def from_env(cls) -> "JMRConfig":
        return cls(
            page_size=_env_int("JARVISMIND_JMR_PAGE_SIZE", 100),
            max_memories=_env_int("JARVISMIND_JMR_MAX_MEMORIES", 200),
            duplicate_threshold=_env_ratio("JARVISMIND_JMR_DUPLICATE_THRESHOLD", 0.82),
            conflict_max_similarity=_env_ratio(
                "JARVISMIND_JMR_CONFLICT_MAX_SIMILARITY", 0.80
            ),
            subsumption_threshold=_env_ratio("JARVISMIND_JMR_SUBSUMPTION_THRESHOLD", 0.65),
            status_min_group=_env_int("JARVISMIND_JMR_STATUS_MIN_GROUP", 2),
            algorithm_version=(
                os.environ.get("JARVISMIND_JMR_ALGORITHM_VERSION") or "jmr-v1"
            ).strip()
            or "jmr-v1",
            expected_cost_cny=_env_amount("JARVISMIND_JMR_EXPECTED_COST_CNY", 0.0),
            expected_tokens=_env_nonneg_int("JARVISMIND_JMR_EXPECTED_TOKENS", 0),
            model_calls_per_stage=_env_int("JARVISMIND_JMR_MODEL_CALLS_PER_STAGE", 1),
            stage_timeout_seconds=_env_int("JARVISMIND_JMR_STAGE_TIMEOUT_SECONDS", 120),
            result_ttl_days=_env_int("JARVISMIND_JMR_RESULT_TTL_DAYS", 30),
            max_retries=_env_nonneg_int("JARVISMIND_JMR_MAX_RETRIES", 3),
            breaker_threshold=_env_int("JARVISMIND_JMR_BREAKER_THRESHOLD", 3),
            daily_budget_cny=_env_amount("JARVISMIND_JMR_DAILY_BUDGET_CNY", 0.0),
            monthly_budget_cny=_env_amount("JARVISMIND_JMR_MONTHLY_BUDGET_CNY", 0.0),
            max_single_call_cny=_env_amount("JARVISMIND_JMR_MAX_SINGLE_CALL_CNY", 0.0),
        )
