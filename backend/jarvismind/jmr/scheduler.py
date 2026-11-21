"""JMR-05 offline scheduling judgements.

This module decides; it never executes. Every public function is a pure
computation over task snapshots and explicit policies. It does not write
``p1_tasks``, does not call ``TaskRegistry``, does not start threads, timers or
workers, and does not import the JMR service.

Authorization boundary (AP-02, not approved):

* automatic (scheduled or condition-based) triggering is closed.
  ``AUTO_TRIGGER_APPROVED`` is a module constant and is not configurable;
* a condition that evaluates true is reported as ``condition_met`` only. It is
  never an execution authorization;
* manual resume (``JMRService.run(resume=True)``) remains the only retry path,
  and this module only reports whether a manual retry is eligible.

Thresholds (timeout seconds, retry budget, breaker threshold, batch size,
trigger thresholds) have NO built-in defaults. They are required arguments,
because the architecture values are still proposals pending AP-17. A missing
threshold is a construction error, never an implicit authorization.

Reused, not rebuilt:

* task states and failure semantics come from ``governance.task_registry``;
* timestamps use ``core.timeutils.parse_iso`` (UTC ISO-8601);
* the detection priority chain is read from the approved ``MERGE >
  STATUS_CHANGE > CONFLICT`` definition in ``detectors.py`` (see
  ``rank_from_detection_chain``). That chain governs detection order, not
  task scheduling priority; scheduling priority is still pending.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple

from ..core.timeutils import parse_iso
from ..governance.task_registry import CANCELLED, FAILED, RUNNING, SUCCEEDED

# AP-02 gate. Deliberately a constant, not a parameter: no caller can enable
# automatic triggering through this module.
AUTO_TRIGGER_APPROVED = False

# Failure codes that must never be retried. They are the refusal outcomes of
# the security, privacy and cost gates. Matching is by the prefix before ":".
NON_RETRYABLE_CODES = frozenset({
    "credential_blocked",
    "unsafe_metadata",
    "pii_dual_authorization_required",
    "content_scan_blocked",
    "provenance_invalid",
    "cost_governance_blocked",
    "circuit_breaker_tripped",
    "write_without_authorized_context",
    "permission_denied",
    "forbidden",
    "unauthorized",
})

# Detection order from detectors.py (approved). SUMMARY is not in the chain and
# therefore has no rank: it must not be ordered by guesswork.
DETECTION_CHAIN: Tuple[str, ...] = ("MERGE", "STATUS_CHANGE", "CONFLICT")

TIMEOUT_WITHIN = "WITHIN"
TIMEOUT_EXCEEDED = "EXCEEDED"
TIMEOUT_NOT_RUNNING = "NOT_RUNNING"
TIMEOUT_INVALID_TIME = "INVALID_TIME"

RETRY_MANUAL_ELIGIBLE = "MANUAL_RETRY_ELIGIBLE"
RETRY_NOT_RETRYABLE = "NOT_RETRYABLE"
RETRY_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
RETRY_NOT_FAILED = "NOT_APPLICABLE"
RETRY_INVALID = "INVALID"

ADMIT_CLOSED = "CLOSED"
ADMIT_BREAKER_TRIPPED = "BREAKER_TRIPPED"


class SchedulerPolicyError(ValueError):
    """A policy, snapshot or argument is missing or malformed. Fail-closed."""


# ---------------------------------------------------------------- validation
def _require_positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchedulerPolicyError(f"{name} must be an integer")
    if value < minimum:
        raise SchedulerPolicyError(f"{name} must be >= {minimum}, got {value}")
    return value


def _require_positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchedulerPolicyError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SchedulerPolicyError(f"{name} must be a finite number > 0")
    return number


def _require_nonnegative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchedulerPolicyError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SchedulerPolicyError(f"{name} must be a finite number >= 0")
    return number


def _parse_aware(value: Any) -> datetime:
    """Parse a stored timestamp. Naive or unparseable values are rejected.

    A naive timestamp is never assumed to be UTC: that guess could shift an
    elapsed-time judgement by hours without any visible error.
    """
    if not isinstance(value, str) or not value.strip():
        raise SchedulerPolicyError("timestamp missing")
    try:
        parsed = parse_iso(value.strip())
    except ValueError as exc:
        raise SchedulerPolicyError("timestamp unparseable") from exc
    if parsed.tzinfo is None:
        raise SchedulerPolicyError("timestamp is naive")
    return parsed


def _metadata_of(task: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = task.get("metadata_json")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise SchedulerPolicyError("metadata_json unparseable") from exc
        if isinstance(parsed, dict):
            return parsed
    raise SchedulerPolicyError("metadata_json is not an object")


def _task_id_of(task: Mapping[str, Any]) -> str:
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise SchedulerPolicyError("task_id missing")
    return task_id


# ------------------------------------------------------------------- timeout
@dataclass(frozen=True)
class TimeoutPolicy:
    stage_timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "stage_timeout_seconds",
            _require_positive_number(self.stage_timeout_seconds, "stage_timeout_seconds"),
        )


@dataclass(frozen=True)
class TimeoutJudgement:
    """A judgement only. ``cancels_task`` is always False: cancelling is a
    separate, explicit ``TaskRegistry`` operation that this module never calls."""

    task_id: str
    status: str
    elapsed_seconds: Optional[float]
    reason: str

    @property
    def cancels_task(self) -> bool:
        return False


def judge_timeout(task: Mapping[str, Any], *, now: datetime, policy: TimeoutPolicy) -> TimeoutJudgement:
    """Judge whether a RUNNING task has exceeded its stage timeout.

    The reference time is ``updated_at``, which ``TaskRegistry.transition``
    sets on every state change, so it is the moment the task entered RUNNING.
    Elapsed time strictly greater than the timeout is EXCEEDED; equal is WITHIN.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise SchedulerPolicyError("now must be a timezone-aware datetime")
    task_id = _task_id_of(task)
    if task.get("status") != RUNNING:
        return TimeoutJudgement(task_id, TIMEOUT_NOT_RUNNING, None, "status_not_running")
    try:
        updated = _parse_aware(task.get("updated_at"))
    except SchedulerPolicyError as exc:
        return TimeoutJudgement(task_id, TIMEOUT_INVALID_TIME, None, str(exc))
    elapsed = (now - updated).total_seconds()
    if elapsed < 0:
        return TimeoutJudgement(task_id, TIMEOUT_INVALID_TIME, None, "timestamp_in_future")
    if elapsed > policy.stage_timeout_seconds:
        return TimeoutJudgement(task_id, TIMEOUT_EXCEEDED, elapsed, "stage_timeout")
    return TimeoutJudgement(task_id, TIMEOUT_WITHIN, elapsed, "within_timeout")


# ---------------------------------------------------------------------- retry
@dataclass(frozen=True)
class RetryPolicy:
    """``retryable_codes`` must be passed explicitly; it may be empty.

    An empty set means no failure is eligible for retry. The architecture does
    not define which failures are transient, so none is assumed.
    """

    max_retries: int
    retryable_codes: frozenset

    def __post_init__(self) -> None:
        _require_positive_int(self.max_retries, "max_retries", minimum=0)
        codes = frozenset(str(code) for code in self.retryable_codes)
        forbidden = codes & NON_RETRYABLE_CODES
        if forbidden:
            raise SchedulerPolicyError(
                f"refusal codes cannot be made retryable: {sorted(forbidden)}"
            )
        object.__setattr__(self, "retryable_codes", codes)


@dataclass(frozen=True)
class RetryDecision:
    task_id: str
    action: str
    failure_code: str
    retry_count: Optional[int]
    reason: str
    automatic: bool = False  # always False: retries are manual only


def failure_code(failure_reason: Any) -> str:
    """Extract the code part of ``"code:detail"`` failure strings."""
    if not isinstance(failure_reason, str):
        return ""
    return failure_reason.split(":", 1)[0].strip()


def decide_retry(task: Mapping[str, Any], *, policy: RetryPolicy) -> RetryDecision:
    """Classify whether a FAILED task may be retried by a manual resume.

    Refusal codes are checked before anything else, so no retry budget and no
    caller configuration can route a security or permission refusal through
    retry. Unknown codes are not retryable unless explicitly listed.
    """
    task_id = _task_id_of(task)
    if task.get("status") != FAILED:
        return RetryDecision(task_id, RETRY_NOT_FAILED, "", None, "status_not_failed")
    code = failure_code(task.get("failure_reason"))
    try:
        metadata = _metadata_of(task)
    except SchedulerPolicyError as exc:
        return RetryDecision(task_id, RETRY_INVALID, code, None, str(exc))
    raw_count = metadata.get("retry_count", 0)
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        return RetryDecision(task_id, RETRY_INVALID, code, None, "retry_count_invalid")
    if code in NON_RETRYABLE_CODES:
        return RetryDecision(task_id, RETRY_NOT_RETRYABLE, code, raw_count, "refusal_code")
    if code not in policy.retryable_codes:
        return RetryDecision(task_id, RETRY_NOT_RETRYABLE, code, raw_count, "not_in_retryable_set")
    if raw_count >= policy.max_retries:
        return RetryDecision(task_id, RETRY_BUDGET_EXHAUSTED, code, raw_count, "max_retries_reached")
    return RetryDecision(task_id, RETRY_MANUAL_ELIGIBLE, code, raw_count, "manual_resume_only")


# -------------------------------------------------------------------- breaker
@dataclass(frozen=True)
class BreakerPolicy:
    failure_threshold: int

    def __post_init__(self) -> None:
        _require_positive_int(self.failure_threshold, "failure_threshold", minimum=1)


@dataclass(frozen=True)
class BreakerState:
    consecutive_failures: int = 0
    tripped: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str


def record_outcome(
    state: BreakerState, status: str, *, policy: BreakerPolicy
) -> BreakerState:
    """Advance the breaker by one finished run. Pure; returns a new state.

    * FAILED increments the consecutive count and trips at the threshold.
    * SUCCEEDED resets the count, but only while not tripped.
    * Once tripped, nothing in this function clears the state. Only
      ``manual_reset`` does.
    * CANCELLED and other statuses are not counted as failures and leave the
      streak unchanged. The architecture does not define them as failures.
    """
    if state.tripped:
        return state
    if status == SUCCEEDED:
        return BreakerState()
    if status == FAILED:
        count = state.consecutive_failures + 1
        if count >= policy.failure_threshold:
            return BreakerState(count, True, f"consecutive_failures={count}")
        return BreakerState(count, False, "")
    if status == CANCELLED:
        return state
    return state


def admit_run(state: BreakerState) -> Admission:
    """Whether a new run may be admitted. Admission is not execution."""
    if state.tripped:
        return Admission(False, ADMIT_BREAKER_TRIPPED)
    return Admission(True, ADMIT_CLOSED)


def auto_recovery_allowed(state: BreakerState) -> bool:
    """A tripped breaker never recovers automatically (Breakdown JMR-05 §10 ④)."""
    return not state.tripped


def manual_reset(state: BreakerState, *, operator_confirmed: bool) -> BreakerState:
    """The only path back to CLOSED. Reserved for an explicit human action.

    The scheduler never calls this. ``operator_confirmed`` is a guard against
    accidental calls, not an authentication mechanism; the real identity check
    belongs to the server-side caller.
    """
    if operator_confirmed is not True:
        raise SchedulerPolicyError("manual reset requires explicit operator confirmation")
    return BreakerState()


# ------------------------------------------------------------------- batching
@dataclass(frozen=True)
class Batch:
    batch_id: str
    index: int
    task_ids: Tuple[str, ...]


def plan_batches(task_ids: Sequence[str], *, batch_size: int, namespace: str = "jmr") -> Tuple[Batch, ...]:
    """Split task ids into ordered, non-overlapping batches.

    Every id appears exactly once across all batches. Duplicate ids are
    rejected instead of silently merged. Batch ids are derived from the batch
    contents, so the same input always yields the same ids.
    """
    size = _require_positive_int(batch_size, "batch_size")
    ids = list(task_ids)
    seen = set()
    for task_id in ids:
        if not isinstance(task_id, str) or not task_id.strip():
            raise SchedulerPolicyError("task id must be a non-empty string")
        if task_id in seen:
            raise SchedulerPolicyError(f"duplicate task id: {task_id}")
        seen.add(task_id)
    batches = []
    for index, start in enumerate(range(0, len(ids), size)):
        chunk = tuple(ids[start:start + size])
        digest = hashlib.sha256("\x1f".join(chunk).encode("utf-8")).hexdigest()[:12]
        batches.append(Batch(f"{namespace}-b{index:04d}-{digest}", index, chunk))
    return tuple(batches)


# ------------------------------------------------------------------ ordering
def rank_from_detection_chain(rule: Any) -> Optional[int]:
    """Rank a derived rule by the approved detection chain. None if unranked."""
    if rule in DETECTION_CHAIN:
        return DETECTION_CHAIN.index(rule)
    return None


def order_tasks(
    tasks: Iterable[Mapping[str, Any]],
    *,
    rank_of: Callable[[Mapping[str, Any]], Optional[int]],
) -> Tuple[Mapping[str, Any], ...]:
    """Order tasks by (rank, created_at, task_id). Fully deterministic.

    ``rank_of`` is supplied by the caller. This module does not invent priority
    levels. A task without a rank is an error, not a default position.
    """
    keyed = []
    for task in tasks:
        task_id = _task_id_of(task)
        created = task.get("created_at")
        if not isinstance(created, str) or not created:
            raise SchedulerPolicyError(f"created_at missing for {task_id}")
        rank = rank_of(task)
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise SchedulerPolicyError(f"unranked task: {task_id}")
        keyed.append(((rank, created, task_id), task))
    keyed.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in keyed)


# -------------------------------------------------------------------- trigger
@dataclass(frozen=True)
class TriggerThresholds:
    new_memories: int
    interval_hours: float

    def __post_init__(self) -> None:
        _require_positive_int(self.new_memories, "new_memories")
        object.__setattr__(
            self, "interval_hours", _require_positive_number(self.interval_hours, "interval_hours")
        )


@dataclass(frozen=True)
class TriggerEvaluation:
    condition_met: bool
    execution_authorized: bool
    reason: str


def evaluate_trigger(
    *,
    new_memory_count: int,
    hours_since_last_run: Optional[float],
    thresholds: TriggerThresholds,
) -> TriggerEvaluation:
    """Report whether a trigger condition holds. It never authorizes execution."""
    count = _require_positive_int(new_memory_count, "new_memory_count", minimum=0)
    met = count >= thresholds.new_memories
    if hours_since_last_run is not None:
        hours = _require_nonnegative_number(hours_since_last_run, "hours_since_last_run")
        met = met or hours >= thresholds.interval_hours
    return TriggerEvaluation(
        condition_met=met,
        execution_authorized=AUTO_TRIGGER_APPROVED,
        reason="AP-02_NOT_APPROVED",
    )


# ----------------------------------------------------------------------- plan
@dataclass(frozen=True)
class SchedulePlan:
    ordered_task_ids: Tuple[str, ...]
    batches: Tuple[Batch, ...]
    timeouts: Tuple[TimeoutJudgement, ...]
    retries: Tuple[RetryDecision, ...]
    admission: Admission
    trigger: Optional[TriggerEvaluation]
    executed: bool = False  # always False: planning never executes


def build_plan(
    tasks: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    timeout_policy: TimeoutPolicy,
    retry_policy: RetryPolicy,
    breaker_state: BreakerState,
    batch_size: int,
    rank_of: Callable[[Mapping[str, Any]], Optional[int]],
    trigger: Optional[TriggerEvaluation] = None,
) -> SchedulePlan:
    """Combine every judgement into one value. Same inputs, same plan.

    Inputs are read, never modified. The result contains no references back to
    the registry, so acting on it is a separate, explicit step.
    """
    snapshot = tuple(tasks)
    ordered = order_tasks(snapshot, rank_of=rank_of)
    ordered_ids = tuple(_task_id_of(task) for task in ordered)
    batches = plan_batches(ordered_ids, batch_size=batch_size)
    timeouts = tuple(judge_timeout(task, now=now, policy=timeout_policy) for task in ordered)
    retries = tuple(
        decide_retry(task, policy=retry_policy) for task in ordered if task.get("status") == FAILED
    )
    return SchedulePlan(
        ordered_task_ids=ordered_ids,
        batches=batches,
        timeouts=timeouts,
        retries=retries,
        admission=admit_run(breaker_state),
        trigger=trigger,
    )
