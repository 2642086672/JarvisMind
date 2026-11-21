"""All P1 governance parameters, externalised from code.

``from_env()`` reads JARVISMIND_P1_* variables so an operator can enable or tune the
base without editing source. ``from_settings()`` is the in-process path used by
tests and the application container. Zero means "unlimited" for the numeric
caps, matching the existing BudgetGuard convention (limit_cny=0 => no cap).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


@dataclass(frozen=True)
class GovernanceConfig:
    """Immutable P1 limits. Defaults are fail-open on *limits* but the write
    gate itself is fail-closed: authorization is still mandatory."""

    enabled: bool = False
    grant_ttl_seconds: int = 300
    grant_key_version: int = 1
    task_budget_cny: float = 0.0
    task_token_limit: int = 0
    task_call_limit: int = 0
    batch_call_limit: int = 0
    rate_limit_per_minute: int = 0
    breaker_threshold: int = 3
    projection_page_size: int = 200
    max_projection_page_size: int = 1000

    @classmethod
    def from_env(cls) -> "GovernanceConfig":
        raw = (os.environ.get("JARVISMIND_P1_GOVERNANCE_ENABLED") or "").strip().lower()
        return cls(
            enabled=raw in {"1", "true", "yes", "on"},
            grant_ttl_seconds=_env_int("JARVISMIND_P1_GRANT_TTL_SECONDS", 300),
            grant_key_version=_env_int("JARVISMIND_P1_GRANT_KEY_VERSION", 1),
            task_budget_cny=_env_float("JARVISMIND_P1_TASK_BUDGET_CNY", 0.0),
            task_token_limit=_env_int("JARVISMIND_P1_TASK_TOKEN_LIMIT", 0),
            task_call_limit=_env_int("JARVISMIND_P1_TASK_CALL_LIMIT", 0),
            batch_call_limit=_env_int("JARVISMIND_P1_BATCH_CALL_LIMIT", 0),
            rate_limit_per_minute=_env_int("JARVISMIND_P1_RATE_LIMIT_PER_MINUTE", 0),
            breaker_threshold=_env_int("JARVISMIND_P1_BREAKER_THRESHOLD", 3),
        )

    @classmethod
    def from_settings(cls, settings, override: Optional["GovernanceConfig"] = None):
        """Prefer explicit override, then Settings attributes, then defaults.

        ``Settings`` has no P1 fields yet (we deliberately do not edit it), so
        the attribute lookup is optional and keeps this module drop-in.
        """
        if override is not None:
            return override
        env_cfg = cls.from_env()
        enabled = bool(getattr(settings, "p1_governance_enabled", False)) or env_cfg.enabled
        return cls(
            enabled=enabled,
            grant_ttl_seconds=int(
                getattr(settings, "p1_grant_ttl_seconds", env_cfg.grant_ttl_seconds)
            ),
            grant_key_version=env_cfg.grant_key_version,
            task_budget_cny=float(
                getattr(settings, "p1_task_budget_cny", env_cfg.task_budget_cny)
            ),
            task_token_limit=int(
                getattr(settings, "p1_task_token_limit", env_cfg.task_token_limit)
            ),
            task_call_limit=int(
                getattr(settings, "p1_task_call_limit", env_cfg.task_call_limit)
            ),
            batch_call_limit=int(
                getattr(settings, "p1_batch_call_limit", env_cfg.batch_call_limit)
            ),
            rate_limit_per_minute=int(
                getattr(settings, "p1_rate_limit_per_minute", env_cfg.rate_limit_per_minute)
            ),
            breaker_threshold=env_cfg.breaker_threshold,
        )
