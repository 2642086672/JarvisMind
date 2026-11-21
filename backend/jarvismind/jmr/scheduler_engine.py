"""JMR-05 in-process scheduler engine.

Design boundary
---------------
This is a **pull-based, single-process** scheduler. It persists every schedule
and every per-task decision, but it does not start a thread, timer, signal
handler or daemon. A host (operator, CLI, or a future approved lifespan hook)
calls :meth:`SchedulerEngine.tick`; this keeps the Authority Spec §10 "no second
scheduler daemon" freeze and AP-02's "automatic triggering is not approved"
boundary intact.

Execution still goes through ``JMRService.run``. Therefore every run keeps the
existing provenance revalidation, TTL/status checks, privacy grading, cost
governance and audit path. This module never writes ``memory_records`` or
``memory_candidates`` and never creates schema.

Persistence and compatibility
-----------------------------
``p1_tasks`` / ``p1_task_events`` remain the authoritative state and event log.
Scheduler-only fields (priority, due time, retry deadline, batch id, schedule
id, logical-delete marker) live in ``system_settings`` through
``SchedulerStore``. No DDL is required.

The engine deliberately uses only the stable JMR surface that exists in both
the committed baseline and the advanced JMR-04..07 working tree:
``JMRService.build``, ``JMRService.run``, ``TaskRegistry``, and optional
``getattr`` reads of newer result fields. Per-task stage timeout is applied by
temporarily replacing the service instance's config when that config supports
``stage_timeout_seconds``; otherwise the service's own default is used.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text

from ..core.security_gate import (
    assert_safe_identifier,
    assert_safe_text,
    assert_writable,
)
from ..core.timeutils import parse_iso, utc_now_iso
from ..governance.errors import TaskStateError
from ..governance.schema import P1_TABLES
from ..governance.task_registry import (
    CANCELLED,
    CREATED,
    FAILED,
    PAUSED,
    RUNNING,
    SUCCEEDED,
)
from .scheduler import NON_RETRYABLE_CODES, failure_code, plan_batches
from .scheduler_store import SchedulerStore
from .service import JMRService, TASK_TYPE

DEFAULT_PRIORITY = 50
MIN_PRIORITY = 0
MAX_PRIORITY = 100
MAX_TASK_ID_LENGTH = 128
MAX_TASK_SCAN_LIMIT = 5000

#: Failure codes that may be retried by the scheduler. Unknown codes are not
#: retryable; the service's own refusal/security/cost codes are never retryable.
RETRYABLE_FAILURE_CODES = frozenset(
    {
        "RuntimeError",
        "TimeoutError",
        "ConnectionError",
        "OSError",
        "OperationalError",
        "DatabaseError",
        "JMRStageTimeoutError",
        "stage_timeout",
        "Timeout",
    }
)

#: Engine-level state conflicts are not retryable: the task is already being
#: driven by another caller or is in a terminal state.
SCHEDULER_NON_RETRYABLE_CODES = frozenset(NON_RETRYABLE_CODES) | frozenset(
    {
        "JMRStateConflictError",
        "JMRConcurrencyError",
        "jmr_run_conflict",
        "task_type_conflict",
        "validation_error",
        "not_found",
    }
)


class SchedulerError(RuntimeError):
    """Base class for expected scheduler failures."""


class SchedulerUnavailableError(SchedulerError):
    code = "p1_schema_missing"


class SchedulerValidationError(SchedulerError, ValueError):
    code = "scheduler_validation_error"


class SchedulerConflictError(SchedulerError):
    code = "scheduler_conflict"


class SchedulerStateError(SchedulerError):
    code = "scheduler_state_error"


class SchedulerNotFoundError(SchedulerError):
    code = "scheduler_not_found"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise SchedulerValidationError(f"{name} must be an integer") from exc
    if value < minimum:
        raise SchedulerValidationError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise SchedulerValidationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise SchedulerValidationError(f"{name} must be a finite number >= {minimum}")
    return value


@dataclass(frozen=True)
class SchedulerConfig:
    """Tunables owned by JMR-05, kept out of the shared ``JMRConfig``.

    Defaults are conservative and require no environment values. All values
    are process-local policy; none of them creates schema or starts work.
    """

    max_concurrent: int = 1
    max_tasks_per_tick: int = 10
    max_scan_tasks: int = 1000
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    retry_multiplier: float = 2.0
    priority_aging_seconds: int = 300
    max_priority_boost: int = 10
    stale_running_seconds: int = 600
    default_timeout_seconds: int = 300
    batch_size: int = 5
    tick_interval_seconds: int = 60
    max_runtime_seconds: int = 300

    def __post_init__(self) -> None:
        for name in (
            "max_concurrent",
            "max_tasks_per_tick",
            "max_scan_tasks",
            "retry_base_seconds",
            "retry_max_seconds",
            "stale_running_seconds",
            "default_timeout_seconds",
            "batch_size",
            "tick_interval_seconds",
            "max_runtime_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SchedulerValidationError(f"{name} must be a positive integer")
        if self.max_scan_tasks > MAX_TASK_SCAN_LIMIT:
            raise SchedulerValidationError(
                f"max_scan_tasks must be <= {MAX_TASK_SCAN_LIMIT}"
            )
        if (
            isinstance(self.retry_multiplier, bool)
            or not isinstance(self.retry_multiplier, (int, float))
            or not math.isfinite(float(self.retry_multiplier))
            or float(self.retry_multiplier) < 1.0
        ):
            raise SchedulerValidationError("retry_multiplier must be >= 1.0")
        if (
            isinstance(self.priority_aging_seconds, bool)
            or not isinstance(self.priority_aging_seconds, int)
            or self.priority_aging_seconds < 0
        ):
            raise SchedulerValidationError(
                "priority_aging_seconds must be a non-negative integer"
            )
        if (
            isinstance(self.max_priority_boost, bool)
            or not isinstance(self.max_priority_boost, int)
            or self.max_priority_boost < 0
        ):
            raise SchedulerValidationError(
                "max_priority_boost must be a non-negative integer"
            )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise SchedulerValidationError(
                "retry_max_seconds must be >= retry_base_seconds"
            )

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        return cls(
            max_concurrent=_env_int("JARVISMIND_JMR_SCHEDULER_MAX_CONCURRENT", 1),
            max_tasks_per_tick=_env_int("JARVISMIND_JMR_SCHEDULER_MAX_TASKS_PER_TICK", 10),
            max_scan_tasks=_env_int("JARVISMIND_JMR_SCHEDULER_MAX_SCAN_TASKS", 1000),
            retry_base_seconds=_env_int("JARVISMIND_JMR_SCHEDULER_RETRY_BASE_SECONDS", 30),
            retry_max_seconds=_env_int("JARVISMIND_JMR_SCHEDULER_RETRY_MAX_SECONDS", 3600),
            retry_multiplier=_env_float(
                "JARVISMIND_JMR_SCHEDULER_RETRY_MULTIPLIER", 2.0, minimum=1.0
            ),
            priority_aging_seconds=_env_int(
                "JARVISMIND_JMR_SCHEDULER_PRIORITY_AGING_SECONDS", 300, minimum=0
            ),
            max_priority_boost=_env_int(
                "JARVISMIND_JMR_SCHEDULER_MAX_PRIORITY_BOOST", 10, minimum=0
            ),
            stale_running_seconds=_env_int(
                "JARVISMIND_JMR_SCHEDULER_STALE_RUNNING_SECONDS", 600
            ),
            default_timeout_seconds=_env_int(
                "JARVISMIND_JMR_SCHEDULER_DEFAULT_TIMEOUT_SECONDS", 300
            ),
            batch_size=_env_int("JARVISMIND_JMR_SCHEDULER_BATCH_SIZE", 5),
            tick_interval_seconds=_env_int(
                "JARVISMIND_JMR_SCHEDULER_TICK_INTERVAL_SECONDS", 60
            ),
            max_runtime_seconds=_env_int(
                "JARVISMIND_JMR_SCHEDULER_MAX_RUNTIME_SECONDS", 300
            ),
        )


# ---------------------------------------------------------------------------
# Validation and pure helpers
# ---------------------------------------------------------------------------
def _require_task_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerValidationError("task_id must be a non-empty string")
    task_id = value.strip()
    if len(task_id) > MAX_TASK_ID_LENGTH:
        raise SchedulerValidationError(
            f"task_id must be <= {MAX_TASK_ID_LENGTH} characters"
        )
    # Identifiers are persisted as settings keys and audit detail; a credential
    # shape must be refused before any write (JMR-07).
    assert_safe_identifier(task_id, field="task_id", strict=True)
    return task_id


def _require_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchedulerValidationError("priority must be an integer")
    if not MIN_PRIORITY <= value <= MAX_PRIORITY:
        raise SchedulerValidationError(
            f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}"
        )
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulerValidationError(f"{name} must be a positive integer")
    return value


def _as_aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SchedulerValidationError("now must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SchedulerValidationError(f"{name} must be an ISO timestamp")
    try:
        parsed = parse_iso(value.strip())
    except ValueError as exc:
        raise SchedulerValidationError(f"{name} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise SchedulerValidationError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _parse_timestamp_optional(value: Any, name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    return _parse_timestamp(value, name)


def _iso(value: datetime) -> str:
    return _as_aware(value).isoformat()


def _json_object(value: Any, name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchedulerValidationError(f"{name} must be a JSON object")
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise SchedulerValidationError(f"{name} is not JSON-serialisable") from exc
    return dict(value)


def effective_priority(
    meta: Mapping[str, Any],
    *,
    now: datetime,
    config: SchedulerConfig,
) -> int:
    """Base priority plus bounded aging, so low-priority work cannot starve."""
    try:
        base = int(meta.get("priority", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        base = DEFAULT_PRIORITY
    try:
        due = _parse_timestamp_optional(meta.get("due_at"), "due_at")
        if due is None:
            created = _parse_timestamp_optional(meta.get("created_at"), "created_at")
            due = created or now
    except SchedulerValidationError:
        due = now
    waited = max(0.0, (now - due).total_seconds())
    if config.priority_aging_seconds <= 0 or config.max_priority_boost <= 0:
        return base
    boost = int(waited // config.priority_aging_seconds)
    return base + min(int(config.max_priority_boost), boost)


def order_due_tasks(
    items: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    config: SchedulerConfig,
) -> Tuple[Mapping[str, Any], ...]:
    """Deterministic order: effective priority desc, due, created, task id."""

    def key(item: Mapping[str, Any]):
        meta = item.get("meta") or {}
        task_id = str(item.get("task_id") or meta.get("task_id") or "")
        priority = effective_priority(meta, now=now, config=config)
        due = str(meta.get("due_at") or meta.get("created_at") or "")
        created = str(meta.get("created_at") or "")
        return (-priority, due, created, task_id)

    return tuple(sorted(items, key=key))


def retry_delay_seconds(
    attempts: int,
    *,
    base_seconds: int,
    max_seconds: int,
    multiplier: float,
) -> int:
    """Exponential backoff after the given number of failed attempts."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise SchedulerValidationError("attempts must be a positive integer")
    if base_seconds < 1 or max_seconds < base_seconds:
        raise SchedulerValidationError("invalid retry base/max seconds")
    exponent = max(0, attempts - 1)
    delay = float(base_seconds) * (float(multiplier) ** exponent)
    return int(min(float(max_seconds), max(float(base_seconds), delay)))


def is_retryable_failure(failure_reason: Any) -> bool:
    code = failure_code(failure_reason)
    if not code:
        return False
    if code in SCHEDULER_NON_RETRYABLE_CODES:
        return False
    return code in RETRYABLE_FAILURE_CODES


def window_state(meta: Mapping[str, Any], *, now: datetime) -> str:
    """Return OPEN / EARLY / MISSED for a task's optional scheduling window."""
    start = _parse_timestamp_optional(meta.get("window_start"), "window_start")
    end = _parse_timestamp_optional(meta.get("window_end"), "window_end")
    if start is not None and now < start:
        return "EARLY"
    if end is not None and now > end:
        return "MISSED"
    return "OPEN"


def deterministic_task_id(schedule_id: str, occurrence: datetime) -> str:
    digest = hashlib.sha256(
        f"{schedule_id}|{_iso(occurrence)}".encode("utf-8")
    ).hexdigest()[:12]
    prefix = schedule_id[: MAX_TASK_ID_LENGTH - 13]
    return f"{prefix}-{digest}"


# ---------------------------------------------------------------------------
# Result values
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskRegistration:
    task_id: str
    created: bool
    adopted: bool
    status: str
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "created": self.created,
            "adopted": self.adopted,
            "status": self.status,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class TaskRunOutcome:
    task_id: str
    executed: bool
    skipped: bool
    status: str
    result_status: str
    run_id: str
    failure_reason: str
    retry_scheduled: bool
    next_retry_at: Optional[str]
    attempts: int
    duration_seconds: float
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "executed": self.executed,
            "skipped": self.skipped,
            "status": self.status,
            "result_status": self.result_status,
            "run_id": self.run_id,
            "failure_reason": self.failure_reason,
            "retry_scheduled": self.retry_scheduled,
            "next_retry_at": self.next_retry_at,
            "attempts": self.attempts,
            "duration_seconds": round(self.duration_seconds, 6),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True)
class TickResult:
    started_at: str
    finished_at: str
    materialized: Tuple[str, ...] = ()
    stale_recovered: Tuple[str, ...] = ()
    executed: Tuple[str, ...] = ()
    succeeded: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    skipped: Tuple[str, ...] = ()
    retry_scheduled: Tuple[str, ...] = ()
    due_candidates: int = 0
    truncated: bool = False
    runtime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "materialized": list(self.materialized),
            "stale_recovered": list(self.stale_recovered),
            "executed": list(self.executed),
            "succeeded": list(self.succeeded),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "retry_scheduled": list(self.retry_scheduled),
            "due_candidates": self.due_candidates,
            "truncated": self.truncated,
            "runtime_seconds": round(self.runtime_seconds, 6),
        }


@dataclass(frozen=True)
class BatchOutcome:
    batch_id: str
    index: int
    task_ids: Tuple[str, ...]
    outcomes: Tuple[TaskRunOutcome, ...]
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "index": self.index,
            "task_ids": list(self.task_ids),
            "outcomes": [item.to_dict() for item in self.outcomes],
            "status": self.status,
        }


@dataclass(frozen=True)
class BatchRunResult:
    batch_size: int
    batches: Tuple[BatchOutcome, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "batches": [batch.to_dict() for batch in self.batches],
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class SchedulerEngine:
    """Persistent, pull-based JMR task scheduler.

    All mutating public methods are serialized by an in-process ``RLock``. The
    authoritative duplicate-execution guard is still the P1 state machine:
    ``CREATED -> RUNNING`` can only happen once, even if two processes tick at
    the same time. This is a single-process deployment; cross-process worker
    coordination and true parallel execution are explicitly out of scope.
    """

    def __init__(
        self,
        manager,
        *,
        service: Optional[JMRService] = None,
        store: Optional[SchedulerStore] = None,
        config: Optional[SchedulerConfig] = None,
        clock=None,
    ) -> None:
        self.manager = manager
        self.service = service or JMRService.build(manager=manager)
        self.registry = self.service.task_registry
        self.store = store or SchedulerStore(manager)
        self.config = config or SchedulerConfig.from_env()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    # ------------------------------------------------------------- preconditions
    def _now(self, now: Optional[datetime] = None) -> datetime:
        return _as_aware(now if now is not None else self._clock())

    def _require_p1_schema(self) -> None:
        with self.manager.session() as session:
            names = {
                row[0]
                for row in session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        missing = [name for name in P1_TABLES if name not in names]
        if missing:
            raise SchedulerUnavailableError(
                "P1 task tables are not installed; the scheduler requires them "
                f"(missing: {', '.join(missing)})"
            )

    def _require_task(self, task_id: str) -> Dict[str, Any]:
        task = self.registry.get(task_id)
        if task is None:
            raise SchedulerNotFoundError(f"task {task_id!r} not found")
        if task.get("task_type") != TASK_TYPE:
            raise SchedulerConflictError(
                f"task {task_id!r} belongs to type {task.get('task_type')!r}, "
                f"not {TASK_TYPE!r}"
            )
        return task

    def _service_timeout_seconds(self) -> int:
        value = getattr(
            getattr(self.service, "config", None),
            "stage_timeout_seconds",
            None,
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return int(self.config.default_timeout_seconds)
        return int(value)

    def _default_meta(
        self,
        task_id: str,
        *,
        now: datetime,
        priority: int = DEFAULT_PRIORITY,
        memory_type: Optional[str] = None,
        due_at: Optional[datetime] = None,
        timeout_seconds: Optional[int] = None,
        max_attempts: int = 1,
        dedup_key: str = "",
        batch_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        due = due_at or now
        return {
            "task_id": task_id,
            "meta_version": 1,
            "priority": int(priority),
            "memory_type": memory_type,
            "due_at": _iso(due),
            "created_at": _iso(now),
            "window_start": None,
            "window_end": None,
            "timeout_seconds": int(timeout_seconds or self._service_timeout_seconds()),
            "max_attempts": int(max_attempts),
            "attempts": 0,
            "retry_count": 0,
            "retry_eligible": False,
            "next_retry_at": None,
            "last_run_at": None,
            "last_status": "",
            "last_failure_reason": "",
            "last_run_id": "",
            "paused": False,
            "resume_requested": False,
            "deleted": False,
            "dedup_key": dedup_key,
            "batch_id": batch_id,
            "batch_index": None,
            "schedule_id": "",
            "occurrence": None,
            "extra": dict(metadata or {}),
        }

    # ------------------------------------------------------------------ register
    def register_task(
        self,
        task_id: str,
        *,
        memory_type: Optional[str] = None,
        priority: int = DEFAULT_PRIORITY,
        due_at: Optional[datetime] = None,
        not_before: Optional[datetime] = None,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
        timeout_seconds: Optional[int] = None,
        max_attempts: int = 1,
        retry_base_seconds: Optional[int] = None,
        retry_max_seconds: Optional[int] = None,
        dedup_key: str = "",
        batch_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> TaskRegistration:
        """Create or adopt a persisted JMR task, idempotently."""
        with self._lock:
            self._require_p1_schema()
            self.store.ensure_version()
            task_id = _require_task_id(task_id)
            if memory_type:
                assert_safe_text(memory_type, field="memory_type")
            if batch_id:
                assert_safe_identifier(batch_id, field="batch_id", strict=True)
            if dedup_key:
                assert_safe_identifier(dedup_key, field="dedup_key", strict=True)
            if metadata is not None:
                assert_writable(None, metadata=metadata, field="metadata")
            priority = _require_priority(priority)
            max_attempts = _require_positive_int(max_attempts, "max_attempts")
            if max_attempts > 100:
                raise SchedulerValidationError("max_attempts must be <= 100")
            if timeout_seconds is not None:
                timeout_seconds = _require_positive_int(
                    timeout_seconds, "timeout_seconds"
                )
            if retry_base_seconds is not None:
                retry_base_seconds = _require_positive_int(
                    retry_base_seconds, "retry_base_seconds"
                )
            if retry_max_seconds is not None:
                retry_max_seconds = _require_positive_int(
                    retry_max_seconds, "retry_max_seconds"
                )
            effective_base = (
                retry_base_seconds
                if retry_base_seconds is not None
                else self.config.retry_base_seconds
            )
            effective_max = (
                retry_max_seconds
                if retry_max_seconds is not None
                else self.config.retry_max_seconds
            )
            if effective_max < effective_base:
                raise SchedulerValidationError(
                    "retry_max_seconds must be >= retry_base_seconds"
                )
            current = self._now(now)
            due = _as_aware(due_at) if due_at is not None else current
            if not_before is not None:
                due = max(due, _as_aware(not_before))
            start = _as_aware(window_start) if window_start is not None else None
            end = _as_aware(window_end) if window_end is not None else None
            if start is not None and end is not None and end < start:
                raise SchedulerValidationError("window_end must be >= window_start")
            extra = _json_object(metadata, "metadata")

            if dedup_key:
                for existing in self.store.list_task_meta(
                    limit=self.config.max_scan_tasks
                ):
                    if (
                        existing.get("dedup_key") == dedup_key
                        and not existing.get("deleted")
                    ):
                        existing_id = str(existing.get("task_id") or "")
                        task = self.registry.get(existing_id)
                        if task is not None and task.get("task_type") == TASK_TYPE:
                            return TaskRegistration(
                                existing_id,
                                False,
                                True,
                                str(task.get("status") or ""),
                                dict(existing),
                            )

            task = self.registry.get(task_id)
            existing_meta = self.store.read_task_meta(task_id)
            if task is not None:
                if task.get("task_type") != TASK_TYPE:
                    raise SchedulerConflictError(
                        f"task {task_id!r} already exists with another task_type"
                    )
                if existing_meta is not None:
                    return TaskRegistration(
                        task_id,
                        False,
                        False,
                        str(task.get("status") or ""),
                        dict(existing_meta),
                    )
                # Adopt a task created outside the scheduler (for example the
                # first POST /runs). Defaults are conservative; the caller may
                # not rewrite an existing task's history.
                meta = self._default_meta(
                    task_id,
                    now=current,
                    priority=priority,
                    memory_type=memory_type,
                    due_at=due,
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    dedup_key=dedup_key,
                    batch_id=batch_id,
                    metadata=extra,
                )
                meta["created_at"] = str(task.get("created_at") or _iso(current))
                meta["window_start"] = _iso(start) if start else None
                meta["window_end"] = _iso(end) if end else None
                if retry_base_seconds is not None:
                    meta["retry_base_seconds"] = retry_base_seconds
                if retry_max_seconds is not None:
                    meta["retry_max_seconds"] = retry_max_seconds
                self.store.write_task_meta(meta)
                return TaskRegistration(
                    task_id,
                    False,
                    True,
                    str(task.get("status") or ""),
                    meta,
                )

            self.registry.create(
                task_id,
                TASK_TYPE,
                owner="svc:jmr-scheduler",
                metadata={
                    "scheduler": "jmr-05",
                    "memory_type": memory_type or "",
                    "priority": priority,
                },
            )
            meta = self._default_meta(
                task_id,
                now=current,
                priority=priority,
                memory_type=memory_type,
                due_at=due,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                dedup_key=dedup_key,
                batch_id=batch_id,
                metadata=extra,
            )
            meta["window_start"] = _iso(start) if start else None
            meta["window_end"] = _iso(end) if end else None
            if retry_base_seconds is not None:
                meta["retry_base_seconds"] = retry_base_seconds
            if retry_max_seconds is not None:
                meta["retry_max_seconds"] = retry_max_seconds
            self.store.write_task_meta(meta)
            return TaskRegistration(
                task_id, True, False, CREATED, meta
            )

    # --------------------------------------------------------------- schedules
    def register_schedule(
        self,
        schedule_id: str,
        *,
        interval_seconds: int,
        priority: int = DEFAULT_PRIORITY,
        memory_type: Optional[str] = None,
        first_run_at: Optional[datetime] = None,
        enabled: bool = True,
        timeout_seconds: Optional[int] = None,
        max_attempts: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
        replace_existing: bool = False,
    ) -> Dict[str, Any]:
        """Persist a periodic schedule. Materialization happens only on tick."""
        with self._lock:
            self._require_p1_schema()
            self.store.ensure_version()
            schedule_id = _require_task_id(schedule_id)
            interval = _require_positive_int(interval_seconds, "interval_seconds")
            priority = _require_priority(priority)
            max_attempts = _require_positive_int(max_attempts, "max_attempts")
            if max_attempts > 100:
                raise SchedulerValidationError("max_attempts must be <= 100")
            if timeout_seconds is not None:
                timeout_seconds = _require_positive_int(
                    timeout_seconds, "timeout_seconds"
                )
            if memory_type:
                assert_safe_text(memory_type, field="memory_type")
            if metadata is not None:
                assert_writable(None, metadata=metadata, field="metadata")
            now = self._now()
            next_run = _as_aware(first_run_at) if first_run_at is not None else now
            if not isinstance(enabled, bool):
                raise SchedulerValidationError("enabled must be a boolean")
            schedules = self.store.read_schedules()
            existing = schedules.get(schedule_id)
            if existing is not None and not replace_existing:
                return dict(existing)
            record = {
                "schedule_id": schedule_id,
                "interval_seconds": interval,
                "priority": priority,
                "memory_type": memory_type,
                "timeout_seconds": (
                    int(timeout_seconds)
                    if timeout_seconds is not None
                    else self._service_timeout_seconds()
                ),
                "max_attempts": max_attempts,
                "enabled": enabled,
                "next_run_at": _iso(next_run),
                "last_materialized_at": None,
                "last_task_id": "",
                "missed": 0,
                "skipped_overlap": 0,
                "created_at": (
                    str(existing.get("created_at"))
                    if existing and existing.get("created_at")
                    else _iso(now)
                ),
                "updated_at": _iso(now),
                "metadata": _json_object(metadata, "metadata"),
            }
            schedules[schedule_id] = record
            self.store.write_schedules(schedules)
            return dict(record)

    def list_schedules(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dict(value)
                for _, value in sorted(self.store.read_schedules().items())
            ]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            if not isinstance(enabled, bool):
                raise SchedulerValidationError("enabled must be a boolean")
            schedules = self.store.read_schedules()
            record = schedules.get(schedule_id)
            if record is None:
                raise SchedulerNotFoundError(f"schedule {schedule_id!r} not found")
            record["enabled"] = enabled
            record["updated_at"] = _iso(self._now())
            schedules[schedule_id] = record
            self.store.write_schedules(schedules)
            return dict(record)

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            schedules = self.store.read_schedules()
            if schedule_id not in schedules:
                return False
            del schedules[schedule_id]
            self.store.write_schedules(schedules)
            return True


    # ----------------------------------------------------------------- controls
    def _get_or_create_meta(
        self, task_id: str, task: Mapping[str, Any], *, now: datetime
    ) -> Dict[str, Any]:
        meta = self.store.read_task_meta(task_id)
        if meta is not None:
            return dict(meta)
        created = str(task.get("created_at") or _iso(now))
        meta = self._default_meta(
            task_id,
            now=now,
            due_at=_parse_timestamp(created, "created_at"),
        )
        meta["created_at"] = created
        meta["last_status"] = str(task.get("status") or "")
        self.store.write_task_meta(meta)
        return meta

    def _repair_meta(self) -> int:
        repaired = 0
        now = self._now()
        for task in self.registry.list(limit=self.config.max_scan_tasks):
            if task.get("task_type") != TASK_TYPE:
                continue
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            if self.store.read_task_meta(task_id) is None:
                self._get_or_create_meta(task_id, task, now=now)
                repaired += 1
        return repaired

    def pause_task(self, task_id: str, *, reason: str = "") -> Dict[str, Any]:
        if reason:
            assert_safe_text(reason, field="reason")
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            meta = self._get_or_create_meta(task_id, task, now=self._now())
            status = str(task.get("status") or "")
            if status in (SUCCEEDED, CANCELLED):
                raise SchedulerStateError(f"cannot pause a {status} task")
            if status == RUNNING:
                try:
                    self.registry.pause(task_id, reason=reason or "scheduler_pause")
                except TaskStateError as exc:
                    raise SchedulerStateError(str(exc)) from exc
            meta["paused"] = True
            meta["resume_requested"] = False
            meta["pause_reason"] = reason
            self.store.write_task_meta(meta)
            current = self.registry.get(task_id) or {}
            return {
                "task_id": task_id,
                "status": current.get("status", status),
                "paused": True,
                "noop": status == PAUSED,
            }

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        """Request resumption. Actual execution happens on the next tick.

        PAUSED/FAILED tasks are continued by ``JMRService.run(resume=True)``;
        a scheduler-paused CREATED task simply becomes eligible again.
        """
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            meta = self._get_or_create_meta(task_id, task, now=self._now())
            status = str(task.get("status") or "")
            if status in (SUCCEEDED, CANCELLED):
                raise SchedulerStateError(f"cannot resume a {status} task")
            if meta.get("deleted"):
                raise SchedulerStateError("cannot resume a deleted task")
            meta["paused"] = False
            if status == PAUSED:
                meta["resume_requested"] = True
            elif status == FAILED:
                meta["manual_retry"] = True
                meta["next_retry_at"] = _iso(self._now())
            elif status == CREATED:
                meta["resume_requested"] = False
            self.store.write_task_meta(meta)
            return {
                "task_id": task_id,
                "status": status,
                "resume_requested": bool(
                    meta.get("resume_requested") or meta.get("manual_retry")
                ),
            }

    def cancel_task(self, task_id: str, *, reason: str = "") -> Dict[str, Any]:
        if reason:
            assert_safe_text(reason, field="reason")
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            meta = self._get_or_create_meta(task_id, task, now=self._now())
            status = str(task.get("status") or "")
            noop = status == CANCELLED
            if status in (CANCELLED, SUCCEEDED, FAILED):
                noop = True
            else:
                try:
                    self.registry.cancel(
                        task_id, reason=reason or "scheduler_cancel"
                    )
                except TaskStateError as exc:
                    raise SchedulerStateError(str(exc)) from exc
            meta["paused"] = False
            meta["resume_requested"] = False
            meta["manual_retry"] = False
            meta["next_retry_at"] = None
            meta["cancelled"] = True
            meta["last_status"] = (
                CANCELLED if status not in (SUCCEEDED, FAILED) else status
            )
            self.store.write_task_meta(meta)
            return {
                "task_id": task_id,
                "status": meta["last_status"],
                "noop": noop,
                "cancelled": True,
            }

    def delete_task(self, task_id: str, *, reason: str = "") -> Dict[str, Any]:
        """Logical delete. The P1 row/event history is never physically removed."""
        if reason:
            assert_safe_text(reason, field="reason")
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            meta = self._get_or_create_meta(task_id, task, now=self._now())
            status = str(task.get("status") or "")
            if status == RUNNING:
                raise SchedulerStateError(
                    "cannot delete a RUNNING task; cancel it first"
                )
            if status not in (SUCCEEDED, CANCELLED):
                try:
                    self.registry.cancel(
                        task_id, reason=reason or "scheduler_delete"
                    )
                except TaskStateError:
                    pass
            meta["deleted"] = True
            meta["paused"] = False
            meta["resume_requested"] = False
            meta["manual_retry"] = False
            meta["next_retry_at"] = None
            meta["delete_reason"] = reason
            meta["last_status"] = CANCELLED
            self.store.write_task_meta(meta)
            return {"task_id": task_id, "deleted": True, "status": CANCELLED}

    # --------------------------------------------------------------------- tick
    def _due_items(
        self, now: datetime, *, auto_retry: bool
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        tasks = [
            task
            for task in self.registry.list(limit=self.config.max_scan_tasks)
            if task.get("task_type") == TASK_TYPE
        ]
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            meta = self._get_or_create_meta(task_id, task, now=now)
            if meta.get("deleted"):
                continue
            status = str(task.get("status") or "")
            if status not in (CREATED, PAUSED, FAILED):
                continue
            if meta.get("paused") and not (
                status == PAUSED and meta.get("resume_requested")
            ):
                continue
            try:
                window = window_state(meta, now=now)
            except SchedulerValidationError:
                meta["last_error"] = "invalid_window"
                self.store.write_task_meta(meta)
                continue
            if window == "MISSED":
                self._cancel_with_reason(task_id, "window_missed", meta=meta)
                continue
            if window == "EARLY":
                continue
            try:
                due = _parse_timestamp_optional(meta.get("due_at"), "due_at")
            except SchedulerValidationError:
                meta["last_error"] = "invalid_due_at"
                self.store.write_task_meta(meta)
                continue
            if due is not None and due > now:
                continue

            if status == CREATED:
                items.append(
                    {"task_id": task_id, "meta": meta, "resume": False}
                )
            elif status == PAUSED:
                if meta.get("resume_requested"):
                    items.append(
                        {"task_id": task_id, "meta": meta, "resume": True}
                    )
            elif status == FAILED:
                manual = bool(meta.get("manual_retry"))
                if not manual and not auto_retry:
                    continue
                if not manual:
                    if not meta.get("retry_eligible"):
                        continue
                    try:
                        retry_at = _parse_timestamp_optional(
                            meta.get("next_retry_at"), "next_retry_at"
                        )
                    except SchedulerValidationError:
                        meta["last_error"] = "invalid_next_retry_at"
                        self.store.write_task_meta(meta)
                        continue
                    if retry_at is None or retry_at > now:
                        continue
                    if int(meta.get("attempts", 0)) >= int(
                        meta.get("max_attempts", 1)
                    ):
                        continue
                items.append(
                    {"task_id": task_id, "meta": meta, "resume": True}
                )
        return items

    def tick(
        self,
        *,
        now: Optional[datetime] = None,
        max_tasks: Optional[int] = None,
        max_runtime_seconds: Optional[int] = None,
        auto_retry: bool = True,
    ) -> TickResult:
        """Run one bounded scheduling pass.

        No thread is started. The caller decides when this method is invoked;
        the method never registers a timer or schedules its own next call.
        """
        with self._lock:
            self._require_p1_schema()
            self.store.ensure_version()
            started = self._now(now)
            limit = (
                self.config.max_tasks_per_tick
                if max_tasks is None
                else _require_positive_int(max_tasks, "max_tasks")
            )
            limit = min(limit, self.config.max_tasks_per_tick)
            runtime_limit = (
                self.config.max_runtime_seconds
                if max_runtime_seconds is None
                else _require_positive_int(
                    max_runtime_seconds, "max_runtime_seconds"
                )
            )
            deadline = started + timedelta(seconds=runtime_limit)

            self._repair_meta()
            stale = self._recover_stale_running(started, auto_retry=auto_retry)
            materialized = self._materialize_schedules(started)
            due = self._due_items(started, auto_retry=auto_retry)
            ordered = order_due_tasks(due, now=started, config=self.config)

            executed: List[str] = []
            succeeded: List[str] = []
            failed: List[str] = []
            skipped: List[str] = []
            retry_scheduled: List[str] = []
            truncated = False

            for item in ordered:
                if len(executed) >= limit:
                    truncated = True
                    break
                if self._running_count() >= self.config.max_concurrent:
                    truncated = True
                    break
                if self._now() >= deadline:
                    truncated = True
                    break
                outcome = self._execute_item(item, now=self._now())
                if outcome.skipped:
                    skipped.append(outcome.task_id)
                    continue
                executed.append(outcome.task_id)
                if outcome.result_status == SUCCEEDED:
                    succeeded.append(outcome.task_id)
                elif outcome.result_status in (FAILED, "BLOCKED"):
                    failed.append(outcome.task_id)
                if outcome.retry_scheduled:
                    retry_scheduled.append(outcome.task_id)

            finished = self._now()
            result = TickResult(
                started_at=_iso(started),
                finished_at=_iso(finished),
                materialized=tuple(materialized),
                stale_recovered=tuple(stale),
                executed=tuple(executed),
                succeeded=tuple(succeeded),
                failed=tuple(failed),
                skipped=tuple(skipped),
                retry_scheduled=tuple(retry_scheduled),
                due_candidates=len(ordered),
                truncated=truncated,
                runtime_seconds=max(0.0, (finished - started).total_seconds()),
            )
            self.store.write_last_tick(result.to_dict())
            return result

    def execute_task(
        self,
        task_id: str,
        *,
        force: bool = False,
        run_id: str = "",
        now: Optional[datetime] = None,
        auto_retry: bool = True,
    ) -> TaskRunOutcome:
        """Execute one task immediately, bypassing only the due-time check."""
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            current = self._now(now)
            meta = self._get_or_create_meta(task_id, task, now=current)
            if meta.get("deleted"):
                raise SchedulerStateError("task is logically deleted")
            if meta.get("paused"):
                raise SchedulerStateError("task is paused")
            status = str(task.get("status") or "")
            if status not in (CREATED, PAUSED, FAILED):
                return TaskRunOutcome(
                    task_id=task_id,
                    executed=False,
                    skipped=True,
                    status=status,
                    result_status=status,
                    run_id="",
                    failure_reason="",
                    retry_scheduled=False,
                    next_retry_at=None,
                    attempts=int(meta.get("attempts", 0)),
                    duration_seconds=0.0,
                    meta=meta,
                )
            if not force:
                try:
                    window = window_state(meta, now=current)
                    due = _parse_timestamp_optional(meta.get("due_at"), "due_at")
                except SchedulerValidationError as exc:
                    raise SchedulerStateError(str(exc)) from exc
                if window == "MISSED":
                    self._cancel_with_reason(task_id, "window_missed", meta=meta)
                    raise SchedulerStateError("task scheduling window has passed")
                if window == "EARLY" or (due is not None and due > current):
                    raise SchedulerStateError("task is not due yet")
            item = {
                "task_id": task_id,
                "meta": meta,
                "resume": status in (PAUSED, FAILED),
            }
            if run_id:
                meta["forced_run_id"] = run_id
                self.store.write_task_meta(meta)
            return self._execute_item(item, now=current, auto_retry=auto_retry)


    # ----------------------------------------------------------------- execution
    def _execute_item(
        self,
        item: Mapping[str, Any],
        *,
        now: datetime,
        auto_retry: bool = True,
    ) -> TaskRunOutcome:
        task_id = str(item.get("task_id") or "")
        meta = dict(item.get("meta") or {})
        resume = bool(item.get("resume"))
        task = self.registry.get(task_id)
        if task is None:
            return TaskRunOutcome(
                task_id, False, True, "", "", "", "", False, None, 0, 0.0, meta
            )
        status = str(task.get("status") or "")
        if status not in (CREATED, PAUSED, FAILED):
            return TaskRunOutcome(
                task_id, False, True, status, status, "", "", False, None,
                int(meta.get("attempts", 0)), 0.0, meta,
            )
        if meta.get("deleted") or meta.get("paused"):
            return TaskRunOutcome(
                task_id, False, True, status, status, "", "", False, None,
                int(meta.get("attempts", 0)), 0.0, meta,
            )

        attempts = int(meta.get("attempts", 0)) + 1
        meta["attempts"] = attempts
        forced_run_id = str(meta.pop("forced_run_id", "") or "")
        run_id = forced_run_id or (
            f"{task_id}-{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        )
        self.store.write_task_meta(meta)

        original_config = getattr(self.service, "config", None)
        config_applied = False
        timeout = meta.get("timeout_seconds")
        if (
            timeout is not None
            and original_config is not None
            and hasattr(original_config, "stage_timeout_seconds")
        ):
            try:
                self.service.config = replace(
                    original_config, stage_timeout_seconds=int(timeout)
                )
                config_applied = True
            except (TypeError, ValueError):
                config_applied = False

        started = time.monotonic()
        result = None
        crash: Optional[BaseException] = None
        try:
            result = self.service.run(
                task_id=task_id,
                run_id=run_id,
                memory_type=meta.get("memory_type"),
                resume=resume,
            )
        except TaskStateError as exc:
            meta["attempts"] = max(0, attempts - 1)
            self.store.write_task_meta(meta)
            current = self.registry.get(task_id) or {}
            return TaskRunOutcome(
                task_id, False, True, str(current.get("status") or status),
                str(current.get("status") or status), run_id, str(exc),
                False, None, int(meta.get("attempts", 0)), 0.0, meta,
            )
        except Exception as exc:  # noqa: BLE001 - one task must not kill a pass
            code = str(getattr(exc, "code", "") or "")
            name = type(exc).__name__
            if code in ("jmr_run_conflict", "task_type_conflict") or name in (
                "JMRConcurrencyError",
                "JMRStateConflictError",
            ):
                meta["attempts"] = max(0, attempts - 1)
                self.store.write_task_meta(meta)
                current = self.registry.get(task_id) or {}
                return TaskRunOutcome(
                    task_id, False, True, str(current.get("status") or status),
                    str(current.get("status") or status), run_id, str(exc),
                    False, None, int(meta.get("attempts", 0)), 0.0, meta,
                )
            crash = exc
        finally:
            if config_applied:
                self.service.config = original_config

        duration = max(0.0, time.monotonic() - started)
        if crash is not None:
            result_status = FAILED
            # The leading token is the underlying exception class so the existing
            # retry classifier can treat transient infrastructure errors (locked
            # database, socket, timeout) as retryable; the rest keeps scheduler
            # context for operators.
            failure_reason = (
                f"{type(crash).__name__}:scheduler_execution_error: {crash}"
            )
            current = self.registry.get(task_id) or {}
            if current.get("status") == RUNNING:
                try:
                    self.registry.fail(task_id, reason=failure_reason, run_id=run_id)
                except TaskStateError:
                    pass
            elif current.get("status") == CREATED:
                # The service raised before it could move CREATED -> RUNNING.
                # This is a SYSTEM failure, not a user cancellation. Record a
                # real FAILED transition and let the bounded retry policy below
                # decide. Cancelling here would permanently lose the task on a
                # transient infrastructure error.
                try:
                    self.registry.start(task_id, run_id=run_id)
                    self.registry.fail(task_id, reason=failure_reason, run_id=run_id)
                except TaskStateError:
                    pass
        else:
            result_status = str(getattr(result, "status", "") or "")
            failure_reason = str(getattr(result, "failure_reason", "") or "")

        current = self.registry.get(task_id) or {}
        current_status = str(current.get("status") or "")
        if current_status == RUNNING:
            # Reconcile the P1 state with the result the service reported. A
            # success must never leave a task stuck in RUNNING, and a pause or
            # cancel must be visible to the next reader.
            try:
                if result_status == SUCCEEDED:
                    self.registry.succeed(task_id, run_id=run_id)
                elif result_status == FAILED:
                    self.registry.fail(
                        task_id,
                        reason=failure_reason or "scheduler_failed",
                        run_id=run_id,
                    )
                elif result_status == PAUSED:
                    self.registry.pause(
                        task_id, reason="scheduler_pause", run_id=run_id
                    )
                elif result_status == CANCELLED:
                    self.registry.cancel(
                        task_id, reason="scheduler_cancel", run_id=run_id
                    )
                else:
                    self.registry.fail(
                        task_id,
                        reason=failure_reason or f"unresolved_status:{result_status}",
                        run_id=run_id,
                    )
            except TaskStateError:
                pass
        current = self.registry.get(task_id) or {}
        if result_status == SUCCEEDED and str(current.get("status") or "") != SUCCEEDED:
            meta["status_mismatch"] = (
                f"result=SUCCEEDED,p1={current.get('status') or 'MISSING'}"
            )
            if str(current.get("status") or "") == CREATED:
                # A service that claims success without ever starting is a
                # contract violation. Never spin on it; require an operator.
                meta["paused"] = True
                meta["retry_eligible"] = False

        meta["last_run_at"] = _iso(now)
        meta["last_run_id"] = run_id
        meta["last_status"] = result_status
        meta["last_failure_reason"] = failure_reason
        meta["duration_seconds"] = round(duration, 6)
        meta["paused"] = False
        meta["resume_requested"] = False
        meta["manual_retry"] = False

        retry_scheduled = False
        next_retry_at: Optional[str] = None
        if result_status == SUCCEEDED:
            meta["completed_at"] = _iso(now)
            meta["retry_eligible"] = False
            meta["next_retry_at"] = None
            retry_count = getattr(result, "retry_count", None)
            if isinstance(retry_count, int) and not isinstance(retry_count, bool):
                meta["retry_count"] = max(0, retry_count)
        elif result_status == PAUSED:
            meta["paused"] = True
            meta["next_retry_at"] = None
            meta["retry_eligible"] = False
        elif result_status == CANCELLED:
            meta["next_retry_at"] = None
            meta["retry_eligible"] = False
        elif result_status in (FAILED, "BLOCKED"):
            reported = bool(getattr(result, "retry_eligible", False))
            breaker = bool(getattr(result, "breaker_tripped", False))
            eligible = reported or is_retryable_failure(failure_reason)
            if result_status == "BLOCKED":
                eligible = False
            current = self.registry.get(task_id) or {}
            if result_status == "BLOCKED" and current.get("status") == CREATED:
                # The service refused before it started the task (breaker or
                # admission gate). Pause it so ticks do not spin on it; a human
                # reset + resume is required.
                meta["paused"] = True
                meta["blocked_reason"] = failure_reason or "blocked"
            meta["retry_eligible"] = eligible
            retry_scheduled, next_retry_at = self._apply_retry(
                meta,
                now=now,
                attempts=attempts,
                failure_reason=failure_reason,
                retry_eligible=eligible,
                breaker_tripped=breaker,
                auto_retry=auto_retry,
            )
            meta["breaker_tripped"] = breaker
        else:
            meta["next_retry_at"] = None

        self.store.write_task_meta(meta)
        current = self.registry.get(task_id) or {}
        return TaskRunOutcome(
            task_id=task_id,
            executed=True,
            skipped=False,
            status=str(current.get("status") or result_status),
            result_status=result_status,
            run_id=run_id,
            failure_reason=failure_reason,
            retry_scheduled=retry_scheduled,
            next_retry_at=next_retry_at,
            attempts=attempts,
            duration_seconds=duration,
            meta=meta,
        )

    def _apply_retry(
        self,
        meta: Dict[str, Any],
        *,
        now: datetime,
        attempts: int,
        failure_reason: str,
        retry_eligible: bool,
        breaker_tripped: bool,
        auto_retry: bool,
    ) -> Tuple[bool, Optional[str]]:
        if not auto_retry:
            meta["retry_eligible"] = False
            meta["retry_blocked_reason"] = "auto_retry_disabled"
            meta["next_retry_at"] = None
            return False, None
        if breaker_tripped:
            meta["retry_eligible"] = False
            meta["retry_blocked_reason"] = "breaker_tripped"
            meta["next_retry_at"] = None
            return False, None
        max_attempts = int(meta.get("max_attempts", 1))
        if attempts >= max_attempts:
            meta["retry_eligible"] = False
            meta["retry_blocked_reason"] = "max_attempts_reached"
            meta["next_retry_at"] = None
            return False, None
        if not retry_eligible or not failure_reason:
            meta["retry_eligible"] = False
            meta["retry_blocked_reason"] = "not_retryable"
            meta["next_retry_at"] = None
            return False, None
        base = int(meta.get("retry_base_seconds") or self.config.retry_base_seconds)
        maximum = int(
            meta.get("retry_max_seconds") or self.config.retry_max_seconds
        )
        try:
            delay = retry_delay_seconds(
                attempts,
                base_seconds=base,
                max_seconds=maximum,
                multiplier=self.config.retry_multiplier,
            )
        except SchedulerValidationError:
            meta["retry_eligible"] = False
            meta["retry_blocked_reason"] = "invalid_retry_policy"
            meta["next_retry_at"] = None
            return False, None
        next_at = _iso(now + timedelta(seconds=delay))
        meta["retry_eligible"] = True
        meta["next_retry_at"] = next_at
        meta["retry_blocked_reason"] = ""
        meta["retry_delay_seconds"] = delay
        return True, next_at

    # ------------------------------------------------------------- stale recovery
    def _recover_stale_running(
        self, now: datetime, *, auto_retry: bool = True
    ) -> List[str]:
        recovered: List[str] = []
        tasks = [
            task
            for task in self.registry.list(
                status=RUNNING, limit=self.config.max_scan_tasks
            )
            if task.get("task_type") == TASK_TYPE
        ]
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            meta = self._get_or_create_meta(task_id, task, now=now)
            timeout = int(
                meta.get("timeout_seconds") or self._service_timeout_seconds()
            )
            try:
                updated = _parse_timestamp(task.get("updated_at"), "updated_at")
                elapsed = max(0.0, (now - updated).total_seconds())
            except SchedulerValidationError:
                elapsed = float("inf")
            if elapsed <= timeout:
                continue
            try:
                self.registry.fail(
                    task_id, reason="stage_timeout:stale_running"
                )
            except TaskStateError:
                continue
            attempts = max(1, int(meta.get("attempts", 0)))
            meta["attempts"] = attempts
            meta["last_status"] = FAILED
            meta["last_failure_reason"] = "stage_timeout:stale_running"
            meta["recovered_at"] = _iso(now)
            meta["paused"] = False
            meta["resume_requested"] = False
            self._apply_retry(
                meta,
                now=now,
                attempts=attempts,
                failure_reason="stage_timeout:stale_running",
                retry_eligible=True,
                breaker_tripped=bool(meta.get("breaker_tripped", False)),
                auto_retry=auto_retry,
            )
            self.store.write_task_meta(meta)
            recovered.append(task_id)
        return recovered

    # --------------------------------------------------------------- schedules
    @staticmethod
    def _next_future(occurrence: datetime, interval: int, now: datetime) -> Tuple[datetime, int]:
        if occurrence > now:
            return occurrence, 0
        steps = int((now - occurrence).total_seconds() // interval) + 1
        return occurrence + timedelta(seconds=interval * steps), max(0, steps - 1)

    def _active_schedule_task(self, schedule_id: str) -> Optional[str]:
        for meta in self.store.list_task_meta(limit=self.config.max_scan_tasks):
            if meta.get("schedule_id") != schedule_id or meta.get("deleted"):
                continue
            task = self.registry.get(str(meta.get("task_id") or ""))
            if task is None:
                continue
            if task.get("status") in (CREATED, RUNNING, PAUSED):
                return str(meta.get("task_id"))
            if task.get("status") == FAILED and meta.get("retry_eligible"):
                return str(meta.get("task_id"))
        return None

    def _materialize_schedules(self, now: datetime) -> List[str]:
        schedules = self.store.read_schedules()
        created: List[str] = []
        changed = False
        for schedule_id in sorted(schedules):
            record = dict(schedules[schedule_id])
            if not record.get("enabled"):
                continue
            try:
                next_run = _parse_timestamp(
                    record.get("next_run_at"), "next_run_at"
                )
                interval = _require_positive_int(
                    record.get("interval_seconds"), "interval_seconds"
                )
                priority = _require_priority(
                    record.get("priority", DEFAULT_PRIORITY)
                )
                max_attempts = _require_positive_int(
                    record.get("max_attempts", 1), "max_attempts"
                )
                timeout = record.get("timeout_seconds")
                if timeout is not None:
                    timeout = _require_positive_int(timeout, "timeout_seconds")
            except SchedulerValidationError:
                record["last_error"] = "invalid_schedule_definition"
                schedules[schedule_id] = record
                changed = True
                continue
            if next_run > now:
                continue
            active = self._active_schedule_task(schedule_id)
            if active is not None:
                future, missed = self._next_future(next_run, interval, now)
                record["next_run_at"] = _iso(future)
                record["skipped_overlap"] = int(
                    record.get("skipped_overlap", 0) or 0
                ) + 1
                record["missed"] = int(record.get("missed", 0) or 0) + missed
                record["updated_at"] = _iso(now)
                schedules[schedule_id] = record
                changed = True
                continue

            task_id = deterministic_task_id(schedule_id, next_run)
            if self.registry.get(task_id) is not None:
                future, missed = self._next_future(next_run, interval, now)
                record["next_run_at"] = _iso(future)
                record["missed"] = int(record.get("missed", 0) or 0) + missed
                record["updated_at"] = _iso(now)
                schedules[schedule_id] = record
                changed = True
                continue

            registration = self.register_task(
                task_id,
                memory_type=record.get("memory_type"),
                priority=priority,
                due_at=now,
                timeout_seconds=timeout,
                max_attempts=max_attempts,
                metadata=record.get("metadata") or {},
                now=now,
            )
            meta = dict(registration.meta)
            meta["schedule_id"] = schedule_id
            meta["occurrence"] = _iso(next_run)
            self.store.write_task_meta(meta)

            future, missed = self._next_future(next_run, interval, now)
            record["next_run_at"] = _iso(future)
            record["last_materialized_at"] = _iso(now)
            record["last_task_id"] = task_id
            record["missed"] = int(record.get("missed", 0) or 0) + missed
            record["updated_at"] = _iso(now)
            schedules[schedule_id] = record
            changed = True
            created.append(task_id)
        if changed:
            self.store.write_schedules(schedules)
        return created

    # ------------------------------------------------------------------- batch
    def run_batch(
        self,
        task_ids: Sequence[str],
        *,
        batch_size: Optional[int] = None,
        force: bool = True,
        stop_on_failure: bool = False,
        auto_retry: bool = True,
        now: Optional[datetime] = None,
    ) -> BatchRunResult:
        with self._lock:
            self._require_p1_schema()
            if isinstance(task_ids, (str, bytes)) or not isinstance(
                task_ids, Sequence
            ):
                raise SchedulerValidationError("task_ids must be a sequence")
            ids = [_require_task_id(item) for item in task_ids]
            size = (
                self.config.batch_size
                if batch_size is None
                else _require_positive_int(batch_size, "batch_size")
            )
            batches = plan_batches(ids, batch_size=size)
            outcomes: List[BatchOutcome] = []
            for batch in batches:
                batch_outcomes: List[TaskRunOutcome] = []
                for task_id in batch.task_ids:
                    task = self._require_task(task_id)
                    meta = self._get_or_create_meta(
                        task_id, task, now=self._now(now)
                    )
                    meta["batch_id"] = batch.batch_id
                    meta["batch_index"] = batch.index
                    self.store.write_task_meta(meta)
                    try:
                        outcome = self.execute_task(
                            task_id,
                            force=force,
                            now=now,
                            auto_retry=auto_retry,
                        )
                    except SchedulerStateError as exc:
                        current = self.registry.get(task_id) or {}
                        outcome = TaskRunOutcome(
                            task_id=task_id,
                            executed=False,
                            skipped=True,
                            status=str(current.get("status") or ""),
                            result_status=str(current.get("status") or ""),
                            run_id="",
                            failure_reason=str(exc),
                            retry_scheduled=False,
                            next_retry_at=None,
                            attempts=int(meta.get("attempts", 0)),
                            duration_seconds=0.0,
                            meta=meta,
                        )
                    batch_outcomes.append(outcome)
                succeeded = [
                    item
                    for item in batch_outcomes
                    if item.result_status == SUCCEEDED
                ]
                failed = [
                    item
                    for item in batch_outcomes
                    if item.result_status in (FAILED, "BLOCKED")
                ]
                executed = [item for item in batch_outcomes if item.executed]
                if not batch_outcomes:
                    status = "EMPTY"
                elif len(succeeded) == len(batch_outcomes):
                    status = "SUCCEEDED"
                elif len(failed) == len(batch_outcomes):
                    status = "FAILED"
                elif failed and succeeded:
                    status = "PARTIAL"
                elif executed:
                    status = "PARTIAL"
                else:
                    status = "SKIPPED"
                outcomes.append(
                    BatchOutcome(
                        batch_id=batch.batch_id,
                        index=batch.index,
                        task_ids=batch.task_ids,
                        outcomes=tuple(batch_outcomes),
                        status=status,
                    )
                )
                if stop_on_failure and status != "SUCCEEDED":
                    break
            return BatchRunResult(batch_size=size, batches=tuple(outcomes))

    # ------------------------------------------------------------------- stats
    def _running_count(self) -> int:
        return sum(
            1
            for task in self.registry.list(
                status=RUNNING, limit=self.config.max_scan_tasks
            )
            if task.get("task_type") == TASK_TYPE
        )

    def _cancel_with_reason(
        self, task_id: str, reason: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> None:
        task = self.registry.get(task_id)
        if task is not None and task.get("status") in (
            CREATED,
            RUNNING,
            PAUSED,
        ):
            try:
                self.registry.cancel(task_id, reason=reason)
            except TaskStateError:
                pass
        current_meta = meta if meta is not None else self.store.read_task_meta(task_id)
        if current_meta is None:
            return
        current_meta["last_status"] = CANCELLED
        current_meta["paused"] = False
        current_meta["resume_requested"] = False
        current_meta["next_retry_at"] = None
        current_meta["cancel_reason"] = reason
        self.store.write_task_meta(current_meta)

    def _all_scheduler_tasks(self) -> List[Dict[str, Any]]:
        return [
            task
            for task in self.registry.list(limit=self.config.max_scan_tasks)
            if task.get("task_type") == TASK_TYPE
        ]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            self._require_p1_schema()
            now = self._now()
            tasks = self._all_scheduler_tasks()
            metas = {
                str(meta.get("task_id")): meta
                for meta in self.store.list_task_meta(
                    limit=self.config.max_scan_tasks
                )
            }
            status_counts = {
                CREATED: 0,
                RUNNING: 0,
                PAUSED: 0,
                SUCCEEDED: 0,
                FAILED: 0,
                CANCELLED: 0,
            }
            due = 0
            retrying = 0
            window_missed_pending = 0
            deleted = 0
            scheduler_paused = 0
            overdue_running = 0
            oldest_due_age = 0.0
            for task in tasks:
                status = str(task.get("status") or "")
                if status in status_counts:
                    status_counts[status] += 1
                task_id = str(task.get("task_id") or "")
                meta = metas.get(task_id) or {}
                if meta.get("deleted"):
                    deleted += 1
                    continue
                if meta.get("paused"):
                    if status == CREATED:
                        scheduler_paused += 1
                    continue
                if status == CREATED:
                    try:
                        if window_state(meta, now=now) == "MISSED":
                            window_missed_pending += 1
                            continue
                        due_at = _parse_timestamp_optional(
                            meta.get("due_at"), "due_at"
                        )
                    except SchedulerValidationError:
                        continue
                    if due_at is not None and due_at <= now:
                        due += 1
                        oldest_due_age = max(
                            oldest_due_age, (now - due_at).total_seconds()
                        )
                elif status == PAUSED and meta.get("resume_requested"):
                    due += 1
                elif status == FAILED:
                    if meta.get("retry_eligible") and meta.get("next_retry_at"):
                        try:
                            retry_at = _parse_timestamp(
                                meta.get("next_retry_at"), "next_retry_at"
                            )
                        except SchedulerValidationError:
                            continue
                        retrying += 1
                        if retry_at <= now:
                            due += 1
                            oldest_due_age = max(
                                oldest_due_age, (now - retry_at).total_seconds()
                            )
                elif status == RUNNING:
                    try:
                        updated = _parse_timestamp(
                            task.get("updated_at"), "updated_at"
                        )
                        timeout = int(
                            meta.get("timeout_seconds")
                            or self._service_timeout_seconds()
                        )
                        if (now - updated).total_seconds() > timeout:
                            overdue_running += 1
                    except SchedulerValidationError:
                        overdue_running += 1

            success = status_counts[SUCCEEDED]
            failure = status_counts[FAILED]
            terminal = success + failure
            durations = self._duration_stats()
            schedules = self.store.read_schedules()
            next_schedule = None
            for record in schedules.values():
                if not record.get("enabled"):
                    continue
                candidate = record.get("next_run_at")
                if isinstance(candidate, str) and (
                    next_schedule is None or candidate < next_schedule
                ):
                    next_schedule = candidate
            breaker = self.store.read_breaker()
            return {
                "mode": "PULL_SINGLE_PROCESS",
                "automatic_trigger_started": False,
                "task_total": len(tasks),
                "created": status_counts[CREATED],
                "running": status_counts[RUNNING],
                "paused": status_counts[PAUSED] + scheduler_paused,
                "scheduler_paused": scheduler_paused,
                "succeeded": success,
                "failed": failure,
                "cancelled": status_counts[CANCELLED],
                "deleted": deleted,
                "due_now": due,
                "retrying": retrying,
                "window_missed_pending": window_missed_pending,
                "queue_backlog": due,
                "oldest_due_age_seconds": round(oldest_due_age, 3),
                "overdue_running": overdue_running,
                "success_rate": round(success / terminal, 6) if terminal else 0.0,
                "failure_rate": round(failure / terminal, 6) if terminal else 0.0,
                "average_duration_seconds": durations["average"],
                "max_duration_seconds": durations["max"],
                "duration_samples": durations["samples"],
                "breaker_tripped": bool(breaker.get("tripped", False)),
                "schedules_total": len(schedules),
                "schedules_enabled": sum(
                    1 for item in schedules.values() if item.get("enabled")
                ),
                "next_schedule_at": next_schedule,
                "last_tick": self.store.read_last_tick(),
                "scanned_tasks": len(tasks),
                "truncated": len(tasks) >= self.config.max_scan_tasks,
                "config": {
                    "max_concurrent": self.config.max_concurrent,
                    "max_tasks_per_tick": self.config.max_tasks_per_tick,
                    "retry_base_seconds": self.config.retry_base_seconds,
                    "retry_max_seconds": self.config.retry_max_seconds,
                    "retry_multiplier": self.config.retry_multiplier,
                    "priority_aging_seconds": self.config.priority_aging_seconds,
                    "max_priority_boost": self.config.max_priority_boost,
                    "stale_running_seconds": self.config.stale_running_seconds,
                    "batch_size": self.config.batch_size,
                },
            }

    def _duration_stats(self) -> Dict[str, Any]:
        limit = min(
            MAX_TASK_SCAN_LIMIT,
            max(100, self.config.max_scan_tasks * 4),
        )
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT e.task_id, e.event_type, e.created_at "
                    "FROM p1_task_events e JOIN p1_tasks t ON t.task_id=e.task_id "
                    "WHERE t.task_type=:type AND e.event_type IN ('START','SUCCEED','FAIL') "
                    "ORDER BY e.created_at ASC LIMIT :n"
                ),
                {"n": limit, "type": TASK_TYPE},
            ).mappings().all()
        starts: Dict[str, datetime] = {}
        samples: List[float] = []
        for row in rows:
            event = str(row.get("event_type") or "")
            task_id = str(row.get("task_id") or "")
            try:
                stamp = _parse_timestamp(row.get("created_at"), "created_at")
            except SchedulerValidationError:
                continue
            if event == "START":
                starts[task_id] = stamp
            elif event in ("SUCCEED", "FAIL") and task_id in starts:
                samples.append(max(0.0, (stamp - starts[task_id]).total_seconds()))
        if not samples:
            return {"average": 0.0, "max": 0.0, "samples": 0}
        return {
            "average": round(sum(samples) / len(samples), 6),
            "max": round(max(samples), 6),
            "samples": len(samples),
        }

    # -------------------------------------------------------------------- reads
    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        include_deleted: bool = False,
        due_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._require_p1_schema()
            limit = _require_positive_int(limit, "limit")
            now = self._now()
            metas = {
                str(meta.get("task_id")): meta
                for meta in self.store.list_task_meta(
                    limit=self.config.max_scan_tasks
                )
            }
            items: List[Dict[str, Any]] = []
            for task in self._all_scheduler_tasks():
                if status and task.get("status") != status:
                    continue
                task_id = str(task.get("task_id") or "")
                meta = metas.get(task_id) or {}
                if meta.get("deleted") and not include_deleted:
                    continue
                if due_only and not self._is_due(meta, task, now):
                    continue
                items.append(
                    {
                        **dict(task),
                        "scheduler": meta,
                        "due": self._is_due(meta, task, now),
                        "effective_priority": effective_priority(
                            meta, now=now, config=self.config
                        ),
                    }
                )
            if due_only:
                items.sort(
                    key=lambda item: (
                        -int(item.get("effective_priority", 0)),
                        str((item.get("scheduler") or {}).get("due_at") or ""),
                        str(item.get("task_id") or ""),
                    )
                )
            else:
                items.sort(
                    key=lambda item: (
                        str(item.get("created_at") or ""),
                        str(item.get("task_id") or ""),
                    )
                )
            return items[:limit]

    @staticmethod
    def _is_due(
        meta: Mapping[str, Any], task: Mapping[str, Any], now: datetime
    ) -> bool:
        if meta.get("deleted") or meta.get("paused"):
            return False
        status = str(task.get("status") or "")
        try:
            if status == CREATED:
                if window_state(meta, now=now) != "OPEN":
                    return False
                due = _parse_timestamp_optional(meta.get("due_at"), "due_at")
                return due is None or due <= now
            if status == PAUSED:
                return bool(meta.get("resume_requested"))
            if status == FAILED:
                if meta.get("manual_retry"):
                    return True
                if not meta.get("retry_eligible"):
                    return False
                retry_at = _parse_timestamp_optional(
                    meta.get("next_retry_at"), "next_retry_at"
                )
                return retry_at is not None and retry_at <= now
        except SchedulerValidationError:
            return False
        return False

    def task_detail(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            self._require_p1_schema()
            task_id = _require_task_id(task_id)
            task = self._require_task(task_id)
            meta = self._get_or_create_meta(task_id, task, now=self._now())
            events = self.registry.events(task_id, limit=1000)
            return {
                "task": dict(task),
                "scheduler": dict(meta),
                "events": events,
                "due": self._is_due(meta, task, self._now()),
                "effective_priority": effective_priority(
                    meta, now=self._now(), config=self.config
                ),
            }


__all__ = [
    "DEFAULT_PRIORITY",
    "MIN_PRIORITY",
    "MAX_PRIORITY",
    "RETRYABLE_FAILURE_CODES",
    "SCHEDULER_NON_RETRYABLE_CODES",
    "SchedulerConfig",
    "SchedulerError",
    "SchedulerUnavailableError",
    "SchedulerValidationError",
    "SchedulerConflictError",
    "SchedulerStateError",
    "SchedulerNotFoundError",
    "TaskRegistration",
    "TaskRunOutcome",
    "TickResult",
    "BatchOutcome",
    "BatchRunResult",
    "SchedulerEngine",
    "effective_priority",
    "order_due_tasks",
    "retry_delay_seconds",
    "is_retryable_failure",
    "window_state",
    "deterministic_task_id",
]
