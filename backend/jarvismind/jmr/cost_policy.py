"""JMR-06 offline cost governance policy (pure computation, no side effects).

This module decides; it never executes, never writes a ledger, never touches
the database, never calls a model and never changes breaker state. It is an
offline judgement layer that sits next to ``governance.cost_governor``:

* ``CostGovernor`` owns the enforcement path (``enforce`` before a call,
  ``record`` after it, ``anomaly`` / ``trip`` / ``reset_breaker`` for the
  breaker). It is NOT modified and NOT duplicated here.
* This module reproduces only the *arithmetic and policy judgement* that the
  authority documents ask for: per-call token cost, accumulation, daily and
  monthly budget comparison, remaining budget, anomaly detection, degradation
  suggestion, and a structured decision. The caller applies the decision.

Conventions:

* Every budget and threshold is an explicit argument. This module has no defaults for them.
* One spend ledger is used (``p1.cost.*`` and ``llm.spent_cny``). Do not add a second ledger.
* Prices are CNY per 1000 tokens (``price_per_1k_input_cny`` / ``price_per_1k_output_cny``).

Decision vocabulary: ``ALLOW / DENY / HOLD / UNCONFIGURED / INVALID_INPUT``.
Authority documents define ``ALLOW / HOLD / DENY`` in ``sensitive.py``. The two
extra states are required by the JMR-06 task book and are not in conflict with
any authority enum.

Precedence when several dimensions disagree (most restrictive wins):

    INVALID_INPUT > DENY > HOLD > UNCONFIGURED > ALLOW

Security precedence: the cost policy can only narrow a decision. Security
refusals (CREDENTIAL, prompt injection, dual-authorization HOLD) and the
circuit breaker can never be turned into ALLOW by a cost computation.

Evaluation order — security -> breaker -> retry -> call cost -> budgets ->
anomaly -> degradation. A cost-input error raised after the security gate does
NOT discard the verdicts already collected: the decision is still rendered with
the worst status and every collected reason, so a credential refusal can never
surface as a bare "price unconfigured" HOLD. (Added in this batch; before the
fix a missing price downgraded CREDENTIAL-DENY to HOLD.)

Money arithmetic uses ``decimal.Decimal`` with ``ROUND_HALF_UP`` to six decimal
places (matching the six-place rounding used by ``BudgetGuard.status``). Floats
are converted through ``str()`` so binary artefacts do not enter the sums. Each
component is quantised before the sum, so the total is an exact Decimal;
``BudgetGuard.estimate`` sums floats instead, so a budget-boundary comparison
can differ from it by at most one micro-yuan (1e-6 CNY). The Decimal path is
the authoritative one for this module.

Complexity: O(1) per call; no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional, Tuple

from .scheduler import RETRY_MANUAL_ELIGIBLE, RetryDecision
from .sensitive import (
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_HOLD,
    LEVEL_CREDENTIAL,
    PrivacyDecision,
)

ALGORITHM_VERSION = "jmr-cost-v1"

# Decision states (see module docstring).
ALLOW = "ALLOW"
DENY = "DENY"
HOLD = "HOLD"
UNCONFIGURED = "UNCONFIGURED"
INVALID_INPUT = "INVALID_INPUT"

_SEVERITY = {ALLOW: 0, UNCONFIGURED: 1, HOLD: 2, DENY: 3, INVALID_INPUT: 4}

# Reason codes. Prefixes before ":" are stable identifiers for audit.
REASON_OK = "within_policy"
REASON_DAILY_EXCEEDED = "daily_budget_exceeded"
REASON_MONTHLY_EXCEEDED = "monthly_budget_exceeded"
REASON_DAILY_UNCONFIGURED = "daily_budget_unconfigured"
REASON_MONTHLY_UNCONFIGURED = "monthly_budget_unconfigured"
REASON_TASK_UNCONFIGURED = "task_budget_unconfigured"
REASON_TASK_EXCEEDED = "task_budget_exceeded"
REASON_PRICE_UNCONFIGURED = "price_unconfigured"
REASON_COST_MISSING = "cost_data_missing"
REASON_SPENT_MISSING = "spent_data_missing"
REASON_BREAKER_TRIPPED = "circuit_breaker_tripped"
REASON_BREAKER_UNKNOWN = "circuit_breaker_state_unknown"
REASON_SECURITY_MISSING = "security_decision_missing"
REASON_SECURITY_INVALID = "security_decision_invalid"
REASON_ANOMALY_UNCONFIGURED = "anomaly_threshold_unconfigured"
REASON_ANOMALY_DETECTED = "cost_anomaly_detected"
REASON_RETRY_LEDGER_MISSING = "retry_ledger_missing"
REASON_RETRY_TASK_MISMATCH = "retry_task_mismatch"
REASON_DEGRADE_UNCONFIGURED = "degrade_threshold_unconfigured"

_MONEY_QUANT = Decimal("0.000001")
_PER_THOUSAND = Decimal("1000")
_MAX_ANOMALY_THRESHOLD = 10 ** 6


class CostInputError(ValueError):
    """Invalid input. ``kind`` is ``"missing"`` or ``"invalid"``; the message
    names the field only and never echoes the value."""

    def __init__(self, field_name: str, kind: str) -> None:
        super().__init__(f"{field_name}:{kind}")
        self.field_name = field_name
        self.kind = kind


# ------------------------------------------------------------------ primitives
def to_money(value: Any, name: str) -> Decimal:
    """Parse a non-negative finite amount. ``bool`` is rejected explicitly.

    ``None`` raises ``CostInputError(kind="missing")``; everything else that
    cannot be read as a non-negative finite number raises ``kind="invalid"``.
    """
    if value is None:
        raise CostInputError(name, "missing")
    if isinstance(value, bool):
        raise CostInputError(name, "invalid")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise CostInputError(name, "invalid")
            number = Decimal(str(value))  # str() avoids binary float artefacts
        elif isinstance(value, str):
            number = Decimal(value.strip())
        else:
            raise CostInputError(name, "invalid")
    except (InvalidOperation, ValueError):
        raise CostInputError(name, "invalid") from None
    if not number.is_finite() or number < 0:
        raise CostInputError(name, "invalid")
    return number.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def to_token_count(value: Any, name: str) -> int:
    """Parse a non-negative integer token count. Floats and bools are rejected."""
    if value is None:
        raise CostInputError(name, "missing")
    if isinstance(value, bool) or not isinstance(value, int):
        raise CostInputError(name, "invalid")
    if value < 0:
        raise CostInputError(name, "invalid")
    return value


# ----------------------------------------------------------------- cost math
@dataclass(frozen=True)
class CallCost:
    """Cost of one model call, in CNY, with six-place precision."""

    input_tokens: int
    output_tokens: int
    input_cost_cny: Decimal
    output_cost_cny: Decimal

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost_cny(self) -> Decimal:
        return (self.input_cost_cny + self.output_cost_cny).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        )


def compute_call_cost(
    *,
    input_tokens: Any,
    output_tokens: Any,
    price_in_per_1k_cny: Any,
    price_out_per_1k_cny: Any,
) -> CallCost:
    """Input, output and total cost of one call.

    Formula (identical to ``BudgetGuard.estimate``):
    ``cost = tokens / 1000 * price_per_1k``, summed for input and output.
    Prices must be provided explicitly. A missing price raises
    ``CostInputError(kind="missing")``; it is never treated as zero.
    """
    in_tok = to_token_count(input_tokens, "input_tokens")
    out_tok = to_token_count(output_tokens, "output_tokens")
    price_in = to_money(price_in_per_1k_cny, "price_in_per_1k_cny")
    price_out = to_money(price_out_per_1k_cny, "price_out_per_1k_cny")
    input_cost = (Decimal(in_tok) / _PER_THOUSAND * price_in).quantize(
        _MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    output_cost = (Decimal(out_tok) / _PER_THOUSAND * price_out).quantize(
        _MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    return CallCost(in_tok, out_tok, input_cost, output_cost)


@dataclass(frozen=True)
class CostLedger:
    """Immutable per-task accumulation. Mirrors the field names of the
    ``p1.cost.task.{task_id}`` record (``cost_cny`` / ``tokens`` / ``calls``)
    and adds ``retry_calls`` so retry cost stays visible. It is a value object;
    nothing here persists it.

    ``calls`` counts model-call placeholders. Per JMR architecture §5.2, a
    ``model_calls=1`` value is a stage counter, not a real model invocation.
    """

    cost_cny: Decimal = Decimal("0.000000")
    tokens: int = 0
    calls: int = 0
    retry_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cost_cny": str(self.cost_cny),
            "tokens": self.tokens,
            "calls": self.calls,
            "retry_calls": self.retry_calls,
        }


def accumulate(
    ledger: CostLedger,
    call: CallCost,
    *,
    retry: bool = False,
    model_calls: int = 1,
) -> CostLedger:
    """Return a new ledger with one more call added. History is never reset.

    A retry adds to the same ledger it was given. Nothing here resets counters,
    which is what makes the "retry cannot re-earn budget" rule hold.
    """
    if not isinstance(ledger, CostLedger):
        raise CostInputError("ledger", "invalid")
    if isinstance(model_calls, bool) or not isinstance(model_calls, int) or model_calls < 0:
        raise CostInputError("model_calls", "invalid")
    return CostLedger(
        cost_cny=(ledger.cost_cny + call.total_cost_cny).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        ),
        tokens=ledger.tokens + call.total_tokens,
        calls=ledger.calls + model_calls,
        retry_calls=ledger.retry_calls + (model_calls if retry else 0),
    )


def accumulate_many(
    ledger: CostLedger, calls: Iterable[CallCost], *, retry: bool = False
) -> CostLedger:
    """Fold ``accumulate`` over several calls. Order-independent for sums."""
    result = ledger
    for call in calls:
        result = accumulate(result, call, retry=retry)
    return result


# -------------------------------------------------------------- budget views
@dataclass(frozen=True)
class BudgetView:
    dimension: str
    status: str
    reason: str
    budget_cny: Optional[Decimal]
    spent_cny: Optional[Decimal]
    projected_cny: Optional[Decimal]
    remaining_cny: Optional[Decimal]
    usage_ratio: Optional[Decimal]

    def to_dict(self) -> Dict[str, Any]:
        def _s(value: Optional[Decimal]) -> Optional[str]:
            return None if value is None else str(value)

        return {
            "dimension": self.dimension,
            "status": self.status,
            "reason": self.reason,
            "budget_cny": _s(self.budget_cny),
            "spent_cny": _s(self.spent_cny),
            "projected_cny": _s(self.projected_cny),
            "remaining_cny": _s(self.remaining_cny),
            "usage_ratio": _s(self.usage_ratio),
        }


def evaluate_budget(
    dimension: str,
    *,
    budget_cny: Any,
    spent_cny: Any,
    call_cost_cny: Decimal,
    unconfigured_reason: str,
) -> BudgetView:
    """Compare ``spent + this call`` against one explicit budget.

    * budget ``None``  -> UNCONFIGURED. Never read as unlimited.
    * spent ``None``   -> HOLD. Ledger unknown, cannot prove headroom.
    * ``projected > budget`` -> DENY. Equality is allowed, matching
      ``BudgetGuard.check`` (which blocks only on ``>``).
    The caller supplies the spent value for the correct period. This function
    does not decide period boundaries or time zones (see the report's gap list).
    """
    if budget_cny is None:
        return BudgetView(dimension, UNCONFIGURED, unconfigured_reason, None, None, None, None, None)
    budget = to_money(budget_cny, f"{dimension}_budget_cny")
    try:
        spent = to_money(spent_cny, f"{dimension}_spent_cny")
    except CostInputError as exc:
        if exc.kind == "missing":
            return BudgetView(dimension, HOLD, REASON_SPENT_MISSING, budget, None, None, None, None)
        raise
    projected = (spent + call_cost_cny).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    remaining = max(budget - spent, Decimal("0")).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    ratio = (projected / budget).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP) if budget > 0 else None
    if projected > budget:
        reason = {
            "daily": REASON_DAILY_EXCEEDED,
            "monthly": REASON_MONTHLY_EXCEEDED,
            "task": REASON_TASK_EXCEEDED,
        }[dimension]
        return BudgetView(dimension, DENY, reason, budget, spent, projected, remaining, ratio)
    return BudgetView(dimension, ALLOW, REASON_OK, budget, spent, projected, remaining, ratio)


# ----------------------------------------------------------------- anomaly
@dataclass(frozen=True)
class AnomalyView:
    status: str  # ALLOW (no anomaly) / HOLD (anomaly) / UNCONFIGURED
    reason: str
    anomalous_call: bool
    anomaly_count_before: Optional[int]
    would_trip_breaker: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "anomalous_call": self.anomalous_call,
            "anomaly_count_before": self.anomaly_count_before,
            "would_trip_breaker": self.would_trip_breaker,
        }


def evaluate_anomaly(
    *,
    call_cost_cny: Decimal,
    max_single_call_cny: Any,
    anomaly_count: Any,
    anomaly_threshold: Any,
) -> AnomalyView:
    """Single-call cost anomaly, reported in the same terms as ``CostGovernor``.

    ``CostGovernor.anomaly`` increments a counter and trips at
    ``breaker_threshold``. This function only *reports* whether the current
    call would be an anomaly and whether it would reach the threshold. It never
    mutates the counter and never trips or resets the breaker. A detected
    anomaly yields HOLD, which requires a human decision.
    """
    if max_single_call_cny is None or anomaly_threshold is None:
        return AnomalyView(UNCONFIGURED, REASON_ANOMALY_UNCONFIGURED, False, None, False)
    limit = to_money(max_single_call_cny, "max_single_call_cny")
    count = to_token_count(anomaly_count, "anomaly_count")
    if isinstance(anomaly_threshold, bool) or not isinstance(anomaly_threshold, int) \
            or not 1 <= anomaly_threshold <= _MAX_ANOMALY_THRESHOLD:
        raise CostInputError("anomaly_threshold", "invalid")
    anomalous = call_cost_cny > limit
    if not anomalous:
        return AnomalyView(ALLOW, REASON_OK, False, count, False)
    would_trip = count + 1 >= anomaly_threshold
    return AnomalyView(HOLD, REASON_ANOMALY_DETECTED, True, count, would_trip)


# ------------------------------------------------------------ main decision
@dataclass(frozen=True)
class CostPolicyDecision:
    task_id: str
    status: str
    reason_codes: Tuple[str, ...]
    call_cost_cny: Optional[Decimal]
    call_tokens: Optional[int]
    daily: Optional[BudgetView]
    monthly: Optional[BudgetView]
    anomaly: Optional[AnomalyView]
    ledger_after: Optional[CostLedger]
    breaker_tripped: Optional[bool]
    degrade_suggested: bool
    degrade_pending_reason: Optional[str]
    needs_manual: bool
    algorithm_version: str = ALGORITHM_VERSION

    @property
    def allow_continue(self) -> bool:
        """Only a fully ALLOW decision may continue. The caller still has to
        call ``CostGovernor.enforce`` on the real path; this flag does not
        replace that gate."""
        return self.status == ALLOW

    def to_dict(self) -> Dict[str, Any]:
        """Audit-safe view: numbers, codes and task id. No prompts, no
        memory text, no security payload, no credential material."""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "allow_continue": self.allow_continue,
            "call_cost_cny": None if self.call_cost_cny is None else str(self.call_cost_cny),
            "call_tokens": self.call_tokens,
            "daily": None if self.daily is None else self.daily.to_dict(),
            "monthly": None if self.monthly is None else self.monthly.to_dict(),
            "anomaly": None if self.anomaly is None else self.anomaly.to_dict(),
            "ledger_after": None if self.ledger_after is None else self.ledger_after.to_dict(),
            "breaker_tripped": self.breaker_tripped,
            "degrade_suggested": self.degrade_suggested,
            "degrade_pending_reason": self.degrade_pending_reason,
            "needs_manual": self.needs_manual,
            "algorithm_version": self.algorithm_version,
        }


def _worst(statuses: Iterable[str]) -> str:
    result = ALLOW
    for status in statuses:
        if _SEVERITY[status] > _SEVERITY[result]:
            result = status
    return result


def _security_gate(decision: Any) -> Tuple[str, Tuple[str, ...]]:
    """Map a ``PrivacyDecision`` onto a cost-policy status. Only ALLOW passes."""
    if decision is None:
        return HOLD, (REASON_SECURITY_MISSING,)
    if not isinstance(decision, PrivacyDecision):
        return INVALID_INPUT, (REASON_SECURITY_INVALID,)
    if decision.level == LEVEL_CREDENTIAL or decision.decision == DECISION_DENY:
        return DENY, (f"security_denied:{decision.reason_code}",)
    if decision.decision == DECISION_HOLD or decision.requires_dual_authorization:
        return HOLD, (f"security_hold:{decision.reason_code}",)
    if decision.decision == DECISION_ALLOW:
        return ALLOW, ()
    return INVALID_INPUT, (REASON_SECURITY_INVALID,)


def evaluate_cost_policy(
    *,
    task_id: str,
    input_tokens: Any,
    output_tokens: Any,
    price_in_per_1k_cny: Any,
    price_out_per_1k_cny: Any,
    day_spent_cny: Any,
    month_spent_cny: Any,
    daily_budget_cny: Any,
    monthly_budget_cny: Any,
    breaker_tripped: Any,
    security_decision: Any,
    task_ledger: Optional[CostLedger] = None,
    retry_decision: Optional[RetryDecision] = None,
    anomaly_count: Any = 0,
    anomaly_threshold: Any = None,
    max_single_call_cny: Any = None,
    degrade_threshold_ratio: Any = None,
    task_budget_cny: Any = None,
) -> CostPolicyDecision:
    """Compute the offline cost-governance decision for one prospective call.

    Pure. The caller supplies every budget, threshold, spent value and security
    result. Nothing is read from the environment, the database or the clock.

    ``retry_decision`` must come from ``scheduler.decide_retry``. Any refusal
    code is already NOT_RETRYABLE there, so a refusal can never be turned into
    a cost-policy ALLOW. When ``retry_decision`` is given, ``task_ledger``
    (the history of the same task) is required and is carried forward.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        return _assemble(task_id if isinstance(task_id, str) else "", [INVALID_INPUT], ["task_id:invalid"])

    reasons: list = []
    statuses: list = []

    # ----- security first: a refusal must never be reported as a cost problem
    sec_status, sec_reasons = _security_gate(security_decision)
    statuses.append(sec_status)
    reasons.extend(sec_reasons)

    # ----- circuit breaker (read-only input; never reset here)
    if breaker_tripped is None or not isinstance(breaker_tripped, bool):
        statuses.append(HOLD)
        reasons.append(REASON_BREAKER_UNKNOWN)
        tripped_value: Optional[bool] = None
    else:
        tripped_value = breaker_tripped
        if breaker_tripped:
            statuses.append(DENY)
            reasons.append(REASON_BREAKER_TRIPPED)

    # ----- retry: history must be carried, and the decision must be eligible
    ledger_base = task_ledger if task_ledger is not None else CostLedger()
    if retry_decision is not None:
        if task_ledger is None:
            statuses.append(HOLD)
            reasons.append(REASON_RETRY_LEDGER_MISSING)
        if not isinstance(retry_decision, RetryDecision):
            statuses.append(INVALID_INPUT)
            reasons.append("retry_decision:invalid")
            return _assemble(task_id, statuses, reasons, breaker_tripped=tripped_value)
        if retry_decision.task_id != task_id:
            statuses.append(INVALID_INPUT)
            reasons.append(REASON_RETRY_TASK_MISMATCH)
        elif retry_decision.action != RETRY_MANUAL_ELIGIBLE:
            statuses.append(DENY)
            reasons.append(f"retry_not_eligible:{retry_decision.action}")

    # ----- cost of the prospective call. A cost-input problem must NOT erase a
    # security / breaker / retry verdict already collected above.
    try:
        call = compute_call_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            price_in_per_1k_cny=price_in_per_1k_cny,
            price_out_per_1k_cny=price_out_per_1k_cny,
        )
    except CostInputError as exc:
        if exc.field_name.startswith("price_") and exc.kind == "missing":
            statuses.append(HOLD)
            reasons.append(REASON_PRICE_UNCONFIGURED)
        elif exc.kind == "missing":
            statuses.append(HOLD)
            reasons.append(REASON_COST_MISSING)
        else:
            statuses.append(INVALID_INPUT)
            reasons.append(f"{exc.field_name}:{exc.kind}")
        return _assemble(task_id, statuses, reasons, breaker_tripped=tripped_value)

    # ----- budgets (daily and monthly, both mandatory for an ALLOW)
    try:
        ledger_after = accumulate(ledger_base, call, retry=retry_decision is not None)
    except CostInputError:
        statuses.append(INVALID_INPUT)
        reasons.append("task_ledger:invalid")
        return _assemble(task_id, statuses, reasons, call=call, breaker_tripped=tripped_value)
    daily = _budget_or_invalid("daily", daily_budget_cny, day_spent_cny, call.total_cost_cny,
                               REASON_DAILY_UNCONFIGURED)
    monthly = _budget_or_invalid("monthly", monthly_budget_cny, month_spent_cny, call.total_cost_cny,
                                 REASON_MONTHLY_UNCONFIGURED)
    # 任务级累计预算：历史成本（含重试）与本次调用合并后比较，重试不重置历史
    task_view = _budget_or_invalid(
        "task", task_budget_cny, ledger_base.cost_cny, call.total_cost_cny,
        REASON_TASK_UNCONFIGURED,
    )
    for view in (daily, monthly, task_view):
        if isinstance(view, str):
            statuses.append(INVALID_INPUT)
            reasons.append(view)
            return _assemble(
                task_id, statuses, reasons, call=call, ledger_after=ledger_after,
                breaker_tripped=tripped_value,
            )
        statuses.append(view.status)
        if view.status != ALLOW:
            reasons.append(view.reason)

    # ----- anomaly (reported, never trips the breaker)
    try:
        anomaly = evaluate_anomaly(
            call_cost_cny=call.total_cost_cny,
            max_single_call_cny=max_single_call_cny,
            anomaly_count=anomaly_count,
            anomaly_threshold=anomaly_threshold,
        )
    except CostInputError as exc:
        statuses.append(INVALID_INPUT)
        reasons.append(f"{exc.field_name}:{exc.kind}")
        return _assemble(
            task_id, statuses, reasons, call=call, ledger_after=ledger_after,
            breaker_tripped=tripped_value,
        )
    statuses.append(anomaly.status)
    if anomaly.status != ALLOW:
        reasons.append(anomaly.reason)

    # ----- degradation: suggestion only, no level, no target model
    degrade_suggested = False
    degrade_pending: Optional[str] = None
    if degrade_threshold_ratio is None:
        degrade_pending = REASON_DEGRADE_UNCONFIGURED
    else:
        try:
            ratio_limit = to_money(degrade_threshold_ratio, "degrade_threshold_ratio")
        except CostInputError:
            statuses.append(INVALID_INPUT)
            reasons.append("degrade_threshold_ratio:invalid")
            return _assemble(
                task_id, statuses, reasons, call=call, ledger_after=ledger_after,
                breaker_tripped=tripped_value, anomaly=anomaly,
            )
        if not Decimal("0") < ratio_limit <= Decimal("1"):
            statuses.append(INVALID_INPUT)
            reasons.append("degrade_threshold_ratio:invalid")
            return _assemble(
                task_id, statuses, reasons, call=call, ledger_after=ledger_after,
                breaker_tripped=tripped_value, anomaly=anomaly,
            )
        usage = [v.usage_ratio for v in (daily, monthly) if v.usage_ratio is not None]
        if usage and max(usage) >= ratio_limit:
            degrade_suggested = True

    return _assemble(
        task_id,
        statuses,
        reasons,
        call=call,
        daily=daily,
        monthly=monthly,
        anomaly=anomaly,
        ledger_after=ledger_after,
        breaker_tripped=tripped_value,
        degrade_suggested=degrade_suggested,
        degrade_pending=degrade_pending,
    )


def _budget_or_invalid(
    dimension: str,
    budget: Any,
    spent: Any,
    call_cost: Decimal,
    unconfigured_reason: str,
):
    """Return a BudgetView, or an error string when the input is invalid."""
    try:
        return evaluate_budget(
            dimension,
            budget_cny=budget,
            spent_cny=spent,
            call_cost_cny=call_cost,
            unconfigured_reason=unconfigured_reason,
        )
    except CostInputError as exc:
        return f"{exc.field_name}:{exc.kind}"


def _assemble(
    task_id: str,
    statuses: Iterable[str],
    reasons: Iterable[str],
    *,
    call: Optional[CallCost] = None,
    daily: Optional[BudgetView] = None,
    monthly: Optional[BudgetView] = None,
    anomaly: Optional[AnomalyView] = None,
    ledger_after: Optional[CostLedger] = None,
    breaker_tripped: Optional[bool] = None,
    degrade_suggested: bool = False,
    degrade_pending: Optional[str] = None,
) -> CostPolicyDecision:
    """Single exit point: worst status wins, reasons de-duplicated in order.

    Partial decisions (a cost-input error raised after the security gate ran)
    still carry the security / breaker verdict, so a refusal is never reported
    as a mere cost problem.
    """
    status = _worst(statuses)
    reason_codes = list(dict.fromkeys(reasons))
    if status == ALLOW:
        reason_codes.insert(0, REASON_OK)
    return CostPolicyDecision(
        task_id=task_id,
        status=status,
        reason_codes=tuple(reason_codes),
        call_cost_cny=None if call is None else call.total_cost_cny,
        call_tokens=None if call is None else call.total_tokens,
        daily=daily,
        monthly=monthly,
        anomaly=anomaly,
        ledger_after=ledger_after,
        breaker_tripped=breaker_tripped,
        degrade_suggested=degrade_suggested,
        degrade_pending_reason=degrade_pending,
        needs_manual=status in (HOLD, UNCONFIGURED),
    )
