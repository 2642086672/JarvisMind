"""P2 JMR-06-DEV-01 · 离线成本治理策略隔离测试（synthetic inputs only）。

本文件不访问数据库、不发起网络请求、不调用真实模型、不启动线程或定时器。
所有预算、单价、阈值都由测试显式传入，用于验证行为，不代表权威默认值（AP-08 未批准）。
"""
from __future__ import annotations

import copy
import socket
import threading
from decimal import Decimal

import pytest

from jarvismind.governance.task_registry import FAILED
from jarvismind.jmr import cost_policy as cp
from jarvismind.jmr.cost_policy import (
    ALLOW,
    DENY,
    HOLD,
    INVALID_INPUT,
    UNCONFIGURED,
    CostInputError,
    CostLedger,
    accumulate,
    accumulate_many,
    compute_call_cost,
    evaluate_anomaly,
    evaluate_budget,
    evaluate_cost_policy,
    to_money,
    to_token_count,
)
from jarvismind.jmr.scheduler import (
    NON_RETRYABLE_CODES,
    RetryPolicy,
    decide_retry,
)
from jarvismind.jmr.sensitive import (
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_HOLD,
    LEVEL_CREDENTIAL,
    LEVEL_PUBLIC,
    LEVEL_SENSITIVE,
    PrivacyDecision,
)

TASK = "task-synthetic-001"
PRICE_IN = "0.002"   # 合成单价：元 / 千输入 Token
PRICE_OUT = "0.004"  # 合成单价：元 / 千输出 Token


def _allow_sec() -> PrivacyDecision:
    return PrivacyDecision(LEVEL_PUBLIC, DECISION_ALLOW, "NONE_DETECTED")


def _base(**overrides):
    kwargs = dict(
        task_id=TASK,
        input_tokens=1000,
        output_tokens=500,
        price_in_per_1k_cny=PRICE_IN,
        price_out_per_1k_cny=PRICE_OUT,
        day_spent_cny="0",
        month_spent_cny="0",
        daily_budget_cny="10",
        monthly_budget_cny="100",
        breaker_tripped=False,
        security_decision=_allow_sec(),
        # 合成阈值，显式传入；不代表权威默认值（AP-08 未批准）
        max_single_call_cny="1",
        anomaly_threshold=3,
        anomaly_count=0,
        task_budget_cny="50",
    )
    kwargs.update(overrides)
    return kwargs


# ------------------------------------------------------------ 成本计算
def test_normal_cost_calculation_matches_budget_guard_formula():
    call = compute_call_cost(
        input_tokens=1000, output_tokens=500,
        price_in_per_1k_cny=PRICE_IN, price_out_per_1k_cny=PRICE_OUT,
    )
    # 与 services/llm/budget.py:BudgetGuard.estimate 同口径：tokens/1000*单价
    assert call.input_cost_cny == Decimal("0.002000")
    assert call.output_cost_cny == Decimal("0.002000")
    assert call.total_cost_cny == Decimal("0.004000")
    assert call.total_tokens == 1500


def test_input_and_output_token_cost_are_separate():
    call = compute_call_cost(
        input_tokens=0, output_tokens=2500,
        price_in_per_1k_cny=PRICE_IN, price_out_per_1k_cny=PRICE_OUT,
    )
    assert call.input_cost_cny == Decimal("0.000000")
    assert call.output_cost_cny == Decimal("0.010000")


def test_multiple_calls_accumulate_without_reset():
    call = compute_call_cost(
        input_tokens=1000, output_tokens=0,
        price_in_per_1k_cny=PRICE_IN, price_out_per_1k_cny=PRICE_OUT,
    )
    ledger = accumulate_many(CostLedger(), [call, call, call])
    assert ledger.calls == 3
    assert ledger.tokens == 3000
    assert ledger.cost_cny == Decimal("0.006000")
    assert ledger.retry_calls == 0


def test_retry_cost_accumulates_into_same_ledger():
    call = compute_call_cost(
        input_tokens=1000, output_tokens=0,
        price_in_per_1k_cny=PRICE_IN, price_out_per_1k_cny=PRICE_OUT,
    )
    before = accumulate(CostLedger(), call)
    after = accumulate(before, call, retry=True)
    assert after.calls == 2
    assert after.retry_calls == 1
    assert after.cost_cny == Decimal("0.004000")
    # 历史不被重置：retry 后仍包含首次调用
    assert after.cost_cny > before.cost_cny


def test_precision_no_float_drift():
    # 0.1 + 0.2 在 float 中不等于 0.3；Decimal 路径必须精确
    call = compute_call_cost(
        input_tokens=100, output_tokens=0,
        price_in_per_1k_cny="0.001", price_out_per_1k_cny="0",
    )
    total = accumulate_many(CostLedger(), [call] * 3)
    assert total.cost_cny == Decimal("0.000300")
    assert str(total.cost_cny) == "0.000300"


def test_rounding_is_half_up_at_six_places():
    # 1 Token 单价 0.0000005/千 -> 0.0000000005 元，按 ROUND_HALF_UP 到 6 位
    call = compute_call_cost(
        input_tokens=1, output_tokens=0,
        price_in_per_1k_cny="0.0000005", price_out_per_1k_cny="0",
    )
    assert call.input_cost_cny == Decimal("0.000000")
    call2 = compute_call_cost(
        input_tokens=1000, output_tokens=0,
        price_in_per_1k_cny="0.0000005", price_out_per_1k_cny="0",
    )
    assert call2.input_cost_cny == Decimal("0.000001")


def test_float_input_uses_string_form():
    assert to_money(0.1, "x") == Decimal("0.100000")
    assert to_money(0.1 + 0.2, "x") == Decimal("0.300000")


@pytest.mark.parametrize("bad", [-1, -0.01, "-5", float("nan"), float("inf"), "abc", "", True, False, [], {}])
def test_invalid_money_rejected(bad):
    with pytest.raises(CostInputError) as info:
        to_money(bad, "amount")
    assert info.value.kind == "invalid"


def test_missing_money_is_reported_as_missing_not_zero():
    with pytest.raises(CostInputError) as info:
        to_money(None, "amount")
    assert info.value.kind == "missing"


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True, None])
def test_invalid_token_count_rejected(bad):
    if bad is None:
        with pytest.raises(CostInputError) as info:
            to_token_count(bad, "tokens")
        assert info.value.kind == "missing"
    else:
        with pytest.raises(CostInputError):
            to_token_count(bad, "tokens")


def test_missing_price_is_missing_not_free():
    with pytest.raises(CostInputError) as info:
        compute_call_cost(
            input_tokens=10, output_tokens=10,
            price_in_per_1k_cny=None, price_out_per_1k_cny=PRICE_OUT,
        )
    assert info.value.kind == "missing"


def test_missing_cost_data_yields_hold_in_policy():
    decision = evaluate_cost_policy(**_base(input_tokens=None))
    assert decision.status == HOLD
    assert decision.reason_codes == ("cost_data_missing",)
    assert not decision.allow_continue


def test_missing_price_yields_hold_in_policy():
    decision = evaluate_cost_policy(**_base(price_in_per_1k_cny=None))
    assert decision.status == HOLD
    assert decision.reason_codes == ("price_unconfigured",)


def test_negative_token_input_yields_invalid():
    decision = evaluate_cost_policy(**_base(output_tokens=-1))
    assert decision.status == INVALID_INPUT
    assert not decision.allow_continue


def test_negative_price_yields_invalid():
    decision = evaluate_cost_policy(**_base(price_out_per_1k_cny="-0.001"))
    assert decision.status == INVALID_INPUT


# ----------------------------------------------------------- 日 / 月预算
def test_daily_budget_within_limit_is_allow():
    view = evaluate_budget("daily", budget_cny="10", spent_cny="5",
                           call_cost_cny=Decimal("0.004"), unconfigured_reason="x")
    assert view.status == ALLOW
    assert view.remaining_cny == Decimal("5.000000")
    assert view.projected_cny == Decimal("5.004000")


def test_daily_budget_exceeded_is_deny():
    view = evaluate_budget("daily", budget_cny="10", spent_cny="9.999",
                           call_cost_cny=Decimal("0.004"), unconfigured_reason="x")
    assert view.status == DENY
    assert view.reason == "daily_budget_exceeded"


def test_monthly_budget_exceeded_is_deny():
    view = evaluate_budget("monthly", budget_cny="100", spent_cny="100",
                           call_cost_cny=Decimal("0.000001"), unconfigured_reason="x")
    assert view.status == DENY
    assert view.reason == "monthly_budget_exceeded"


def test_budget_boundary_equal_is_allowed():
    # 与 BudgetGuard.check 一致：仅超过（>）才拦截，等于允许
    view = evaluate_budget("daily", budget_cny="10", spent_cny="9.996",
                           call_cost_cny=Decimal("0.004"), unconfigured_reason="x")
    assert view.projected_cny == Decimal("10.000000")
    assert view.status == ALLOW


def test_budget_boundary_one_micro_over_is_deny():
    view = evaluate_budget("daily", budget_cny="10", spent_cny="9.996",
                           call_cost_cny=Decimal("0.000001") + Decimal("0.004"),
                           unconfigured_reason="x")
    assert view.status == DENY


def test_unconfigured_budget_is_not_unlimited():
    view = evaluate_budget("daily", budget_cny=None, spent_cny="0",
                           call_cost_cny=Decimal("0.004"),
                           unconfigured_reason="daily_budget_unconfigured")
    assert view.status == UNCONFIGURED
    assert view.budget_cny is None
    assert view.remaining_cny is None


def test_unconfigured_daily_makes_policy_unconfigured_not_allow():
    decision = evaluate_cost_policy(**_base(daily_budget_cny=None))
    assert decision.status == UNCONFIGURED
    assert "daily_budget_unconfigured" in decision.reason_codes
    assert decision.needs_manual
    assert not decision.allow_continue


def test_unconfigured_monthly_makes_policy_unconfigured_not_allow():
    decision = evaluate_cost_policy(**_base(monthly_budget_cny=None))
    assert decision.status == UNCONFIGURED
    assert "monthly_budget_unconfigured" in decision.reason_codes


@pytest.mark.parametrize("bad", ["-1", -5, "abc", True])
def test_invalid_budget_yields_invalid_input(bad):
    decision = evaluate_cost_policy(**_base(daily_budget_cny=bad))
    assert decision.status == INVALID_INPUT
    assert not decision.allow_continue


def test_zero_budget_is_a_real_zero_not_unconfigured():
    # 0 为显式的零预算：任何正成本都超限
    decision = evaluate_cost_policy(**_base(daily_budget_cny="0"))
    assert decision.status == DENY
    assert decision.reason_codes == ("daily_budget_exceeded",)


def test_missing_spent_value_is_hold_not_zero():
    decision = evaluate_cost_policy(**_base(day_spent_cny=None))
    assert decision.status == HOLD
    assert "spent_data_missing" in decision.reason_codes


def test_daily_and_monthly_together_worst_status_wins():
    decision = evaluate_cost_policy(**_base(day_spent_cny="0", month_spent_cny="100"))
    assert decision.status == DENY
    assert "monthly_budget_exceeded" in decision.reason_codes
    assert decision.daily.status == ALLOW
    assert decision.monthly.status == DENY


def test_daily_and_monthly_both_within_is_allow():
    decision = evaluate_cost_policy(**_base())
    assert decision.status == ALLOW
    assert decision.allow_continue
    assert decision.reason_codes[0] == "within_policy"


def test_remaining_budget_is_non_negative():
    view = evaluate_budget("monthly", budget_cny="10", spent_cny="12",
                           call_cost_cny=Decimal("0"), unconfigured_reason="x")
    assert view.remaining_cny == Decimal("0.000000")


# ----------------------------------------------------------- 降级策略
def test_degrade_not_suggested_without_explicit_threshold():
    decision = evaluate_cost_policy(**_base(month_spent_cny="99"))
    assert decision.degrade_suggested is False
    assert decision.degrade_pending_reason == "degrade_threshold_unconfigured"


def test_degrade_suggested_only_when_threshold_reached():
    decision = evaluate_cost_policy(**_base(month_spent_cny="95", degrade_threshold_ratio="0.9"))
    assert decision.degrade_suggested is True
    assert decision.degrade_pending_reason is None
    assert decision.status == ALLOW  # 降级只是建议，不改变放行状态


def test_degrade_threshold_out_of_range_is_invalid():
    for bad in ("0", "1.5", "-0.1"):
        decision = evaluate_cost_policy(**_base(degrade_threshold_ratio=bad))
        assert decision.status == INVALID_INPUT


def test_degrade_never_names_a_model_or_level():
    decision = evaluate_cost_policy(**_base(month_spent_cny="95", degrade_threshold_ratio="0.9"))
    payload = decision.to_dict()
    assert "model" not in str(payload).lower()
    assert "level" not in str(payload).lower()


def test_degrade_unauthorised_stays_off_even_when_budget_exhausted():
    decision = evaluate_cost_policy(**_base(month_spent_cny="100"))
    assert decision.status == DENY
    assert decision.degrade_suggested is False


# -------------------------------------------------------- 重试成本累计
def _failed_task(code, retry_count=0, task_id=TASK):
    return {
        "task_id": task_id,
        "status": FAILED,
        "failure_reason": f"{code}:detail",
        "metadata_json": {"retry_count": retry_count},
    }


def test_retry_eligible_task_carries_history_and_adds_cost():
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 1), policy=policy)
    history = CostLedger(cost_cny=Decimal("0.300000"), tokens=20000, calls=5)
    decision = evaluate_cost_policy(**_base(task_ledger=history, retry_decision=retry))
    assert decision.status == ALLOW
    assert decision.ledger_after.calls == 6
    assert decision.ledger_after.retry_calls == 1
    assert decision.ledger_after.cost_cny == Decimal("0.304000")
    assert decision.ledger_after.tokens == 21500


def test_retry_does_not_reset_history():
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 0), policy=policy)
    history = CostLedger(cost_cny=Decimal("9.999000"), tokens=0, calls=1)
    # 任务预算显式设为 10：历史 9.999 + 本次 0.004 = 10.003 超限
    decision = evaluate_cost_policy(
        **_base(task_ledger=history, retry_decision=retry, task_budget_cny="10")
    )
    assert decision.ledger_after.cost_cny == Decimal("10.003000")
    assert decision.status == DENY  # 重试不重置历史，历史累计超任务预算
    assert "task_budget_exceeded" in decision.reason_codes


def test_task_budget_unconfigured_is_not_unlimited():
    decision = evaluate_cost_policy(**_base(task_budget_cny=None))
    assert decision.status == UNCONFIGURED
    assert "task_budget_unconfigured" in decision.reason_codes


def test_task_budget_exceeded_by_history_denies():
    # 历史任务成本 + 本次调用超过任务预算 -> DENY（重试不能绕过任务级累计）
    decision = evaluate_cost_policy(
        **_base(task_budget_cny="0.005", task_ledger=CostLedger(cost_cny=Decimal("0.003000")))
    )
    assert decision.status == DENY
    assert "task_budget_exceeded" in decision.reason_codes


def test_retry_requires_history_ledger():
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 0), policy=policy)
    decision = evaluate_cost_policy(**_base(task_ledger=None, retry_decision=retry))
    assert decision.status == HOLD
    assert "retry_ledger_missing" in decision.reason_codes


def test_retry_budget_exhausted_is_denied_by_cost_policy():
    policy = RetryPolicy(max_retries=2, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 2), policy=policy)
    decision = evaluate_cost_policy(
        **_base(task_ledger=CostLedger(), retry_decision=retry)
    )
    assert decision.status == DENY
    assert any(code.startswith("retry_not_eligible") for code in decision.reason_codes)


def test_retry_task_mismatch_is_invalid():
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 0, task_id="other"), policy=policy)
    decision = evaluate_cost_policy(**_base(task_ledger=CostLedger(), retry_decision=retry))
    assert decision.status == INVALID_INPUT
    assert "retry_task_mismatch" in decision.reason_codes


# --------------------------------------------------- 安全拒绝不得被成本放行
@pytest.mark.parametrize("code", sorted(NON_RETRYABLE_CODES))
def test_refusal_codes_are_never_retryable_for_cost_policy(code):
    policy = RetryPolicy(max_retries=5, retryable_codes=frozenset())
    retry = decide_retry(_failed_task(code, 0), policy=policy)
    assert retry.action != "MANUAL_RETRY_ELIGIBLE"
    decision = evaluate_cost_policy(**_base(task_ledger=CostLedger(), retry_decision=retry))
    assert decision.status != ALLOW


def test_credential_refusal_cannot_be_released_by_cost_policy():
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    decision = evaluate_cost_policy(**_base(security_decision=cred, daily_budget_cny="1000"))
    assert decision.status == DENY
    assert "security_denied:CREDENTIAL_PRESENT" in decision.reason_codes


def test_credential_refusal_cannot_be_released_by_degrade():
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    decision = evaluate_cost_policy(
        **_base(security_decision=cred, month_spent_cny="95", degrade_threshold_ratio="0.9")
    )
    assert decision.status == DENY
    assert decision.allow_continue is False


def test_sensitive_without_dual_auth_is_hold_not_allow():
    sens = PrivacyDecision(LEVEL_SENSITIVE, DECISION_HOLD, "SENSITIVE_DUAL_AUTH_REQUIRED",
                           requires_dual_authorization=True)
    decision = evaluate_cost_policy(**_base(security_decision=sens))
    assert decision.status == HOLD
    assert not decision.allow_continue


def test_prompt_injection_refusal_cannot_be_retried_through_cost():
    inj = PrivacyDecision(LEVEL_PUBLIC, DECISION_DENY, "PROMPT_INJECTION_SUSPECTED",
                          injection_signals=("role_override",))
    policy = RetryPolicy(max_retries=5, retryable_codes=frozenset())
    retry = decide_retry(_failed_task("prompt_injection_suspected", 0), policy=policy)
    decision = evaluate_cost_policy(**_base(security_decision=inj, task_ledger=CostLedger(),
                                            retry_decision=retry))
    assert decision.status == DENY


def test_missing_security_decision_is_hold():
    decision = evaluate_cost_policy(**_base(security_decision=None))
    assert decision.status == HOLD
    assert "security_decision_missing" in decision.reason_codes


def test_non_privacy_security_object_is_invalid():
    decision = evaluate_cost_policy(**_base(security_decision={"decision": "ALLOW"}))
    assert decision.status == INVALID_INPUT


# ------------------------------------------------------- 熔断与异常识别
def test_breaker_tripped_is_deny_and_not_reset():
    decision = evaluate_cost_policy(**_base(breaker_tripped=True))
    assert decision.status == DENY
    assert "circuit_breaker_tripped" in decision.reason_codes
    assert decision.breaker_tripped is True


def test_breaker_unknown_state_is_hold():
    decision = evaluate_cost_policy(**_base(breaker_tripped=None))
    assert decision.status == HOLD
    assert "circuit_breaker_state_unknown" in decision.reason_codes


def test_breaker_cannot_be_released_by_budget_recovery():
    # 预算宽裕 + 熔断已触发 -> 仍 DENY，策略不修改熔断状态
    decision = evaluate_cost_policy(**_base(breaker_tripped=True, daily_budget_cny="1000000"))
    assert decision.status == DENY


def test_anomaly_detected_is_hold_and_reports_trip_intent_only():
    anomaly = evaluate_anomaly(
        call_cost_cny=Decimal("0.500000"), max_single_call_cny="0.100",
        anomaly_count=2, anomaly_threshold=3,
    )
    assert anomaly.status == HOLD
    assert anomaly.anomalous_call is True
    assert anomaly.would_trip_breaker is True


def test_anomaly_below_threshold_does_not_trip_but_holds():
    anomaly = evaluate_anomaly(
        call_cost_cny=Decimal("0.500000"), max_single_call_cny="0.100",
        anomaly_count=0, anomaly_threshold=3,
    )
    assert anomaly.status == HOLD
    assert anomaly.would_trip_breaker is False


def test_anomaly_unconfigured_is_unconfigured_not_allow():
    decision = evaluate_cost_policy(**_base(max_single_call_cny=None, anomaly_threshold=None))
    assert decision.status == UNCONFIGURED
    assert "anomaly_threshold_unconfigured" in decision.reason_codes


def test_anomaly_invalid_threshold_is_invalid():
    decision = evaluate_cost_policy(**_base(max_single_call_cny="1", anomaly_threshold=0))
    assert decision.status == INVALID_INPUT


def test_anomaly_evaluation_does_not_mutate_counter():
    # 纯函数：同一输入两次调用结果一致，计数器（入参）不被修改
    count = 1
    a = evaluate_anomaly(call_cost_cny=Decimal("1"), max_single_call_cny="0.1",
                         anomaly_count=count, anomaly_threshold=5)
    b = evaluate_anomaly(call_cost_cny=Decimal("1"), max_single_call_cny="0.1",
                         anomaly_count=count, anomaly_threshold=5)
    assert a == b
    assert count == 1


# ------------------------------------------------------- 结构化输出
def test_decision_to_dict_contains_required_fields():
    decision = evaluate_cost_policy(**_base(month_spent_cny="95", degrade_threshold_ratio="0.9"))
    payload = decision.to_dict()
    for key in (
        "task_id", "status", "reason_codes", "allow_continue", "call_cost_cny",
        "call_tokens", "daily", "monthly", "anomaly", "ledger_after",
        "breaker_tripped", "degrade_suggested", "degrade_pending_reason",
        "needs_manual", "algorithm_version",
    ):
        assert key in payload
    assert payload["daily"]["remaining_cny"] is not None
    assert payload["allow_continue"] is True


def test_structured_output_has_no_sensitive_payload():
    # 安全决策原文、凭证类别等不得进入输出
    sens = PrivacyDecision(LEVEL_SENSITIVE, DECISION_HOLD, "SENSITIVE_DUAL_AUTH_REQUIRED",
                           pii_categories=("phone",), requires_dual_authorization=True)
    text = str(evaluate_cost_policy(**_base(security_decision=sens)).to_dict())
    assert "13800138000" not in text
    assert "phone" not in text


def test_hold_and_deny_disallow_continue():
    assert not evaluate_cost_policy(**_base(day_spent_cny=None)).allow_continue
    assert not evaluate_cost_policy(**_base(month_spent_cny="100")).allow_continue


def test_invalid_task_id_is_invalid():
    decision = evaluate_cost_policy(**_base(task_id="  "))
    assert decision.status == INVALID_INPUT


# ------------------------------------------------------- 确定性 / 不变性
def test_repeated_evaluation_is_deterministic():
    kwargs = _base(month_spent_cny="95", degrade_threshold_ratio="0.9")
    first = evaluate_cost_policy(**kwargs).to_dict()
    for _ in range(50):
        assert evaluate_cost_policy(**kwargs).to_dict() == first


def test_inputs_are_not_mutated():
    history = CostLedger(cost_cny=Decimal("1.000000"), tokens=10, calls=1)
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset({"transient_timeout"}))
    retry = decide_retry(_failed_task("transient_timeout", 0), policy=policy)
    kwargs = _base(task_ledger=history, retry_decision=retry)
    snapshot = copy.deepcopy(kwargs["task_ledger"])
    evaluate_cost_policy(**kwargs)
    assert kwargs["task_ledger"] == snapshot
    assert history == CostLedger(cost_cny=Decimal("1.000000"), tokens=10, calls=1)


def test_ledger_is_immutable():
    ledger = CostLedger()
    with pytest.raises(Exception):
        ledger.calls = 99  # frozen dataclass


# ------------------------------------------------- 隔离性（无 DB / 网络 / 线程）
def test_module_import_has_no_side_effects():
    # 不 reload（避免污染其他测试的模块对象）：只检查模块级源码无可执行副作用
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(cp))
    for node in tree.body:
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            pytest.fail("module-level expression with possible side effect")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
    assert not any(isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Call)
                   for n in tree.body)


def test_no_network_access_during_evaluation(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    decision = evaluate_cost_policy(**_base())
    assert decision.status == ALLOW


def test_no_database_access_during_evaluation(monkeypatch):
    import jarvismind.database.connection as conn_module

    def _forbidden(*args, **kwargs):
        raise AssertionError("database access attempted")

    monkeypatch.setattr(conn_module.DatabaseManager, "__init__", _forbidden, raising=True)
    decision = evaluate_cost_policy(**_base())
    assert decision.status == ALLOW


def test_no_threads_started_during_evaluation(monkeypatch):
    started = []
    original_start = threading.Thread.start

    def _spy(self, *a, **k):
        started.append(self)
        return original_start(self, *a, **k)

    monkeypatch.setattr(threading.Thread, "start", _spy)
    evaluate_cost_policy(**_base())
    assert started == []


def test_no_real_model_or_cost_is_produced():
    # 本模块不 import 任何模型客户端、数据库或成本账本组件（只检查 import 语句，不扫描文档字符串）。
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(cp))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    for forbidden in ("httpx", "requests", "openai", "anthropic", "urllib", "sqlalchemy",
                      "BudgetGuard", "CostGovernor", "DatabaseManager", "socket", "threading"):
        assert not any(forbidden == name or name.endswith("." + forbidden) for name in imported), forbidden


# ============================================================================
# 本批次新增：安全优先不被成本输入错误抹掉 / 解析边界 / 审计可序列化 / 精度
# 背景：修复前，缺少单价或 Token 会让 CREDENTIAL-DENY 与熔断 DENY 退化成
# HOLD(price_unconfigured)，审计里只看得到「成本没配」，看不到「安全已拒绝」。
# ============================================================================
def test_security_deny_survives_missing_price():
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    decision = evaluate_cost_policy(**_base(security_decision=cred, price_in_per_1k_cny=None))
    assert decision.status == DENY  # 不是 HOLD
    assert "security_denied:CREDENTIAL_PRESENT" in decision.reason_codes
    assert "price_unconfigured" in decision.reason_codes  # 两条原因都保留
    assert not decision.allow_continue


def test_security_deny_survives_missing_token_data():
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    decision = evaluate_cost_policy(**_base(security_decision=cred, input_tokens=None))
    assert decision.status == DENY
    assert "security_denied:CREDENTIAL_PRESENT" in decision.reason_codes
    assert "cost_data_missing" in decision.reason_codes


def test_breaker_trip_survives_missing_cost_input():
    decision = evaluate_cost_policy(**_base(breaker_tripped=True, price_out_per_1k_cny=None))
    assert decision.status == DENY
    assert "circuit_breaker_tripped" in decision.reason_codes


def test_invalid_cost_input_outranks_security_deny_but_keeps_both_reasons():
    # INVALID_INPUT 严重度最高（输入本身不可信），但安全原因不得被吞掉
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    decision = evaluate_cost_policy(**_base(security_decision=cred, price_out_per_1k_cny="-1"))
    assert decision.status == INVALID_INPUT
    assert "security_denied:CREDENTIAL_PRESENT" in decision.reason_codes
    assert not decision.allow_continue


def test_partial_decision_reports_no_invented_cost():
    cred = PrivacyDecision(LEVEL_CREDENTIAL, DECISION_DENY, "CREDENTIAL_PRESENT")
    payload = evaluate_cost_policy(**_base(security_decision=cred, price_in_per_1k_cny=None)).to_dict()
    assert payload["status"] == DENY
    assert payload["call_cost_cny"] is None  # 不编造 0 成本
    assert payload["daily"] is None and payload["monthly"] is None
    assert payload["degrade_suggested"] is False


def test_policy_level_daily_budget_exceeded():
    decision = evaluate_cost_policy(**_base(day_spent_cny="9.999"))
    assert decision.status == DENY
    assert decision.reason_codes == ("daily_budget_exceeded",)
    assert decision.daily.status == DENY
    assert decision.monthly.status == ALLOW  # 月维度独立判断


def test_policy_level_monthly_boundary_equal_is_allowed():
    # 月预算 100，已花 99.996 + 本次 0.004 = 100.000 恰好等于 -> 允许
    decision = evaluate_cost_policy(**_base(month_spent_cny="99.996"))
    assert decision.monthly.projected_cny == Decimal("100.000000")
    assert decision.status == ALLOW


def test_decision_to_dict_is_json_serializable():
    # 审计/日志只能吃 JSON：Decimal 必须已转字符串
    import json

    payload = evaluate_cost_policy(**_base(month_spent_cny="95", degrade_threshold_ratio="0.9")).to_dict()
    text = json.dumps(payload, ensure_ascii=False)  # 不抛 TypeError 即为通过
    assert '"status": "ALLOW"' in text
    assert payload["degrade_suggested"] is True


@pytest.mark.parametrize("bad", [True, False, 1.0, "3", 0, -1, 10 ** 7])
def test_anomaly_threshold_rejects_non_int_or_out_of_range(bad):
    decision = evaluate_cost_policy(**_base(anomaly_threshold=bad))
    assert decision.status == INVALID_INPUT


def test_accumulate_rejects_bad_model_calls():
    call = compute_call_cost(
        input_tokens=10, output_tokens=0,
        price_in_per_1k_cny=PRICE_IN, price_out_per_1k_cny=PRICE_OUT,
    )
    for bad in (-1, 1.5, True, "1"):
        with pytest.raises(CostInputError):
            accumulate(CostLedger(), call, model_calls=bad)


def test_precision_matches_hand_computed_decimal():
    # 1234 输入 Token @ 0.0017/千 = 0.0020978 -> 六位 HALF_UP 0.002098；7 次 = 0.014686
    call = compute_call_cost(
        input_tokens=1234, output_tokens=0,
        price_in_per_1k_cny="0.0017", price_out_per_1k_cny="0",
    )
    assert call.input_cost_cny == Decimal("0.002098")
    total = accumulate_many(CostLedger(), [call] * 7)
    assert total.cost_cny == Decimal("0.014686")
    # 同一输入重复计算必须完全一致（无累计漂移）
    for _ in range(20):
        assert accumulate_many(CostLedger(), [call] * 7).cost_cny == total.cost_cny
