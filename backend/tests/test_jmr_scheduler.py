"""P2 JMR-05 · 离线调度判断隔离测试（synthetic in-memory snapshots）。

本文件不访问数据库、不调用 TaskRegistry 的写路径、不启动线程或定时器。
阈值全部由测试显式传入，用于验证行为，不代表权威默认值（AP-17 未批准）。
"""
from __future__ import annotations

import copy
import inspect
import json
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest

from jarvismind.governance import task_registry as registry_module
from jarvismind.governance.task_registry import TaskRegistry
from jarvismind.jmr import scheduler as sched
from jarvismind.jmr.scheduler import (
    ADMIT_BREAKER_TRIPPED,
    ADMIT_CLOSED,
    AUTO_TRIGGER_APPROVED,
    BreakerPolicy,
    BreakerState,
    RETRY_BUDGET_EXHAUSTED,
    RETRY_INVALID,
    RETRY_MANUAL_ELIGIBLE,
    RETRY_NOT_FAILED,
    RETRY_NOT_RETRYABLE,
    RetryPolicy,
    SchedulerPolicyError,
    TIMEOUT_EXCEEDED,
    TIMEOUT_INVALID_TIME,
    TIMEOUT_NOT_RUNNING,
    TIMEOUT_WITHIN,
    TimeoutPolicy,
    TriggerThresholds,
    admit_run,
    auto_recovery_allowed,
    build_plan,
    decide_retry,
    evaluate_trigger,
    failure_code,
    judge_timeout,
    manual_reset,
    order_tasks,
    plan_batches,
    rank_from_detection_chain,
    record_outcome,
)
from jarvismind.governance.task_registry import CANCELLED, CREATED, FAILED, PAUSED, RUNNING, SUCCEEDED

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(seconds_before_now: float) -> str:
    return (NOW - timedelta(seconds=seconds_before_now)).isoformat()


def _task(task_id, *, status=CREATED, created="2026-01-01T00:00:00+00:00",
          updated=None, failure="", metadata=None, rule="MERGE"):
    meta = {"rule": rule} if metadata is None else metadata
    return {
        "task_id": task_id,
        "task_type": "JMR",
        "status": status,
        "created_at": created,
        "updated_at": updated if updated is not None else created,
        "failure_reason": failure,
        "metadata_json": meta,
    }


TIMEOUT = TimeoutPolicy(stage_timeout_seconds=300)
RETRY = RetryPolicy(max_retries=3, retryable_codes=frozenset({"stage_timeout"}))
BREAKER = BreakerPolicy(failure_threshold=3)


# ==========================================================================
# 一、正常任务调度（纯计算，不执行）
# ==========================================================================
def test_normal_plan_orders_batches_and_judges_without_execution():
    tasks = [
        _task("T-B", rule="CONFLICT"),
        _task("T-A", rule="MERGE"),
        _task("T-C", rule="STATUS_CHANGE"),
    ]
    plan = build_plan(
        tasks,
        now=NOW,
        timeout_policy=TIMEOUT,
        retry_policy=RETRY,
        breaker_state=BreakerState(),
        batch_size=2,
        rank_of=lambda t: rank_from_detection_chain(t["metadata_json"]["rule"]),
    )
    assert plan.ordered_task_ids == ("T-A", "T-C", "T-B")
    assert [b.task_ids for b in plan.batches] == [("T-A", "T-C"), ("T-B",)]
    assert plan.admission.allowed is True
    assert plan.executed is False


def test_plan_is_not_a_run_and_never_touches_registry(monkeypatch):
    # 调度判断不直接执行任务：注册表的状态写入入口全部替换为失败断言
    def forbidden(*_a, **_k):
        raise AssertionError("scheduler must not write task state")

    for name in ("create", "start", "succeed", "fail", "transition", "pause",
                 "resume", "cancel", "recover"):
        monkeypatch.setattr(TaskRegistry, name, forbidden)
    tasks = [_task("T-X", status=RUNNING, updated=_ts(900)), _task("T-Y", status=FAILED,
             failure="stage_timeout: x", metadata={"retry_count": 0})]
    plan = build_plan(
        tasks, now=NOW, timeout_policy=TIMEOUT, retry_policy=RETRY,
        breaker_state=BreakerState(), batch_size=10,
        rank_of=lambda t: 0,
    )
    assert plan.executed is False
    # 超时被判断出来，但 cancels_task 恒为 False（只判断，不取消）
    exceeded = [j for j in plan.timeouts if j.status == TIMEOUT_EXCEEDED]
    assert exceeded and all(j.cancels_task is False for j in exceeded)


# ==========================================================================
# 二、超时判断（边界、缺失、非法、不取消）
# ==========================================================================
def test_timeout_equal_to_limit_is_within():
    task = _task("T-1", status=RUNNING, updated=_ts(300))
    judgement = judge_timeout(task, now=NOW, policy=TIMEOUT)
    assert judgement.status == TIMEOUT_WITHIN
    assert judgement.elapsed_seconds == pytest.approx(300.0)


def test_timeout_strictly_over_limit_is_exceeded():
    task = _task("T-1", status=RUNNING, updated=_ts(300.001))
    judgement = judge_timeout(task, now=NOW, policy=TIMEOUT)
    assert judgement.status == TIMEOUT_EXCEEDED
    assert judgement.reason == "stage_timeout"
    assert judgement.cancels_task is False


def test_timeout_only_applies_to_running_tasks():
    for status in (CREATED, PAUSED, SUCCEEDED, FAILED, CANCELLED):
        judgement = judge_timeout(_task("T-1", status=status, updated=_ts(10_000)),
                                  now=NOW, policy=TIMEOUT)
        assert judgement.status == TIMEOUT_NOT_RUNNING


@pytest.mark.parametrize("updated", [None, "", "not-a-time", "2026-01-01T12:00:00"])
def test_timeout_missing_or_invalid_time_is_never_within(updated):
    task = _task("T-1", status=RUNNING)
    task["updated_at"] = updated
    judgement = judge_timeout(task, now=NOW, policy=TIMEOUT)
    assert judgement.status == TIMEOUT_INVALID_TIME  # 缺失 / 非法 / 无时区，都不判为正常


def test_timeout_future_timestamp_is_invalid_not_within():
    task = _task("T-1", status=RUNNING, updated=(NOW + timedelta(minutes=5)).isoformat())
    assert judge_timeout(task, now=NOW, policy=TIMEOUT).status == TIMEOUT_INVALID_TIME


def test_timeout_does_not_mutate_task():
    task = _task("T-1", status=RUNNING, updated=_ts(9999))
    before = copy.deepcopy(task)
    judge_timeout(task, now=NOW, policy=TIMEOUT)
    assert task == before


def test_timeout_rejects_naive_now_and_missing_policy():
    with pytest.raises(SchedulerPolicyError):
        judge_timeout(_task("T-1", status=RUNNING), now=datetime(2026, 1, 1), policy=TIMEOUT)
    with pytest.raises(TypeError):
        TimeoutPolicy()  # 阈值没有默认值


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "300"])
def test_timeout_policy_rejects_invalid_threshold(bad):
    with pytest.raises(SchedulerPolicyError):
        TimeoutPolicy(stage_timeout_seconds=bad)


# ==========================================================================
# 三、重试策略（次数边界、不可重试、不绕过拒绝、不自动）
# ==========================================================================
def test_retry_within_budget_is_manual_eligible_and_never_automatic():
    decision = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout: x",
                                  metadata={"retry_count": 2}), policy=RETRY)
    assert decision.action == RETRY_MANUAL_ELIGIBLE
    assert decision.automatic is False
    assert decision.retry_count == 2


def test_retry_budget_boundary_is_exhausted_at_max():
    # max_retries=3：计数 0、1、2 可重试；计数 3 起耗尽，不无限重试
    for count in (0, 1, 2):
        d = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout",
                               metadata={"retry_count": count}), policy=RETRY)
        assert d.action == RETRY_MANUAL_ELIGIBLE
    d = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout",
                           metadata={"retry_count": 3}), policy=RETRY)
    assert d.action == RETRY_BUDGET_EXHAUSTED


def test_retry_missing_count_defaults_to_zero_only_for_counting():
    d = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout", metadata={}),
                     policy=RETRY)
    assert d.action == RETRY_MANUAL_ELIGIBLE and d.retry_count == 0


@pytest.mark.parametrize("bad_count", [-1, True, "2", 1.5, None])
def test_retry_invalid_count_is_invalid_not_retryable(bad_count):
    d = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout",
                           metadata={"retry_count": bad_count}), policy=RETRY)
    assert d.action == RETRY_INVALID


def test_retry_unparseable_metadata_is_invalid():
    task = _task("T-1", status=FAILED, failure="stage_timeout")
    task["metadata_json"] = "{broken"
    assert decide_retry(task, policy=RETRY).action == RETRY_INVALID


def test_retry_unknown_code_is_not_retryable_by_default():
    d = decide_retry(_task("T-1", status=FAILED, failure="TypeError: boom"), policy=RETRY)
    assert d.action == RETRY_NOT_RETRYABLE
    assert d.reason == "not_in_retryable_set"


def test_retry_empty_allowlist_means_nothing_is_retryable():
    policy = RetryPolicy(max_retries=3, retryable_codes=frozenset())
    d = decide_retry(_task("T-1", status=FAILED, failure="stage_timeout"), policy=policy)
    assert d.action == RETRY_NOT_RETRYABLE


def test_retry_only_applies_to_failed_tasks():
    for status in (CREATED, RUNNING, SUCCEEDED, CANCELLED):
        d = decide_retry(_task("T-1", status=status, failure="stage_timeout"), policy=RETRY)
        assert d.action == RETRY_NOT_FAILED


@pytest.mark.parametrize("refusal", [
    "credential_blocked", "unsafe_metadata", "pii_dual_authorization_required",
    "content_scan_blocked", "provenance_invalid", "cost_governance_blocked",
    "circuit_breaker_tripped", "permission_denied",
])
def test_refusal_codes_are_never_retried(refusal):
    # 安全 / 权限 / 凭证 / 成本拒绝不得通过重试绕过
    d = decide_retry(_task("T-1", status=FAILED, failure=f"{refusal}: detail",
                           metadata={"retry_count": 0}), policy=RETRY)
    assert d.action == RETRY_NOT_RETRYABLE
    assert d.reason == "refusal_code"


def test_refusal_code_cannot_be_added_to_retryable_set():
    with pytest.raises(SchedulerPolicyError):
        RetryPolicy(max_retries=3, retryable_codes=frozenset({"credential_blocked"}))


def test_retry_policy_requires_explicit_arguments():
    with pytest.raises(TypeError):
        RetryPolicy()
    with pytest.raises(TypeError):
        RetryPolicy(max_retries=3)  # retryable_codes 必须显式给出


@pytest.mark.parametrize("bad", [-1, True, 1.0])
def test_retry_policy_rejects_invalid_max(bad):
    with pytest.raises(SchedulerPolicyError):
        RetryPolicy(max_retries=bad, retryable_codes=frozenset())


def test_failure_code_extraction():
    assert failure_code("stage_timeout") == "stage_timeout"
    assert failure_code("credential_blocked: x:y") == "credential_blocked"
    assert failure_code("") == ""
    assert failure_code(None) == ""


# ==========================================================================
# 四、熔断与恢复判断
# ==========================================================================
def test_breaker_does_not_trip_below_threshold():
    state = BreakerState()
    for _ in range(2):
        state = record_outcome(state, FAILED, policy=BREAKER)
    assert state.tripped is False
    assert admit_run(state).allowed is True


def test_breaker_trips_at_threshold():
    state = BreakerState()
    for _ in range(3):
        state = record_outcome(state, FAILED, policy=BREAKER)
    assert state.tripped is True
    admission = admit_run(state)
    assert admission.allowed is False
    assert admission.reason == ADMIT_BREAKER_TRIPPED


def test_success_resets_streak_before_trip():
    state = BreakerState()
    state = record_outcome(state, FAILED, policy=BREAKER)
    state = record_outcome(state, FAILED, policy=BREAKER)
    state = record_outcome(state, SUCCEEDED, policy=BREAKER)
    assert state.consecutive_failures == 0
    state = record_outcome(state, FAILED, policy=BREAKER)
    assert state.tripped is False


def test_cancelled_run_does_not_count_as_failure():
    state = BreakerState(consecutive_failures=2)
    assert record_outcome(state, CANCELLED, policy=BREAKER) == state


def test_tripped_breaker_ignores_success_and_stays_tripped():
    tripped = BreakerState(consecutive_failures=3, tripped=True, reason="consecutive_failures=3")
    assert record_outcome(tripped, SUCCEEDED, policy=BREAKER) == tripped
    assert admit_run(tripped).allowed is False


def test_tripped_breaker_never_recovers_automatically_over_time():
    tripped = BreakerState(consecutive_failures=3, tripped=True)
    assert auto_recovery_allowed(tripped) is False
    plan = build_plan([], now=NOW + timedelta(days=30), timeout_policy=TIMEOUT,
                      retry_policy=RETRY, breaker_state=tripped, batch_size=1,
                      rank_of=lambda t: 0)
    assert plan.admission.allowed is False


def test_manual_reset_requires_explicit_confirmation():
    tripped = BreakerState(consecutive_failures=3, tripped=True)
    with pytest.raises(SchedulerPolicyError):
        manual_reset(tripped, operator_confirmed=False)
    reset = manual_reset(tripped, operator_confirmed=True)
    assert reset == BreakerState()
    assert admit_run(reset).reason == ADMIT_CLOSED


def test_breaker_policy_requires_threshold():
    with pytest.raises(TypeError):
        BreakerPolicy()
    with pytest.raises(SchedulerPolicyError):
        BreakerPolicy(failure_threshold=0)


# ==========================================================================
# 五、批量调度（边界、完整性、不丢失、不重复）
# ==========================================================================
def test_batches_cover_every_task_exactly_once():
    ids = [f"T-{i:03d}" for i in range(10)]
    batches = plan_batches(ids, batch_size=3)
    flat = [tid for b in batches for tid in b.task_ids]
    assert flat == ids
    assert [len(b.task_ids) for b in batches] == [3, 3, 3, 1]


def test_batch_boundary_exact_multiple_and_single_batch():
    ids = ["A", "B", "C", "D"]
    assert [len(b.task_ids) for b in plan_batches(ids, batch_size=2)] == [2, 2]
    assert len(plan_batches(ids, batch_size=4)) == 1
    assert len(plan_batches(ids, batch_size=100)) == 1


def test_empty_input_yields_no_batches():
    assert plan_batches([], batch_size=5) == ()


def test_duplicate_task_ids_are_rejected_not_merged():
    with pytest.raises(SchedulerPolicyError):
        plan_batches(["A", "B", "A"], batch_size=2)


@pytest.mark.parametrize("bad_size", [0, -1, True, 2.0, None])
def test_batch_size_must_be_positive_int(bad_size):
    with pytest.raises(SchedulerPolicyError):
        plan_batches(["A"], batch_size=bad_size)


def test_batch_size_is_required_keyword():
    with pytest.raises(TypeError):
        plan_batches(["A"])  # type: ignore[call-arg]


def test_batch_ids_are_deterministic_and_distinct():
    ids = ["A", "B", "C", "D", "E"]
    first = plan_batches(ids, batch_size=2)
    second = plan_batches(ids, batch_size=2)
    assert first == second
    assert len({b.batch_id for b in first}) == len(first)


def test_batch_planning_does_not_mutate_input_list():
    ids = ["A", "B", "C"]
    snapshot = list(ids)
    plan_batches(ids, batch_size=2)
    assert ids == snapshot


def test_batching_rejects_blank_task_id():
    with pytest.raises(SchedulerPolicyError):
        plan_batches(["A", "  "], batch_size=2)


# ==========================================================================
# 六、优先级排序（确定性、同级、缺失）
# ==========================================================================
def test_order_by_rank_then_created_then_id():
    tasks = [
        _task("T-3", created="2026-01-01T00:00:03+00:00", rule="CONFLICT"),
        _task("T-1", created="2026-01-01T00:00:02+00:00", rule="MERGE"),
        _task("T-2", created="2026-01-01T00:00:01+00:00", rule="MERGE"),
    ]
    ordered = order_tasks(tasks, rank_of=lambda t: rank_from_detection_chain(t["metadata_json"]["rule"]))
    assert [t["task_id"] for t in ordered] == ["T-2", "T-1", "T-3"]


def test_same_rank_and_same_created_breaks_tie_by_task_id():
    created = "2026-01-01T00:00:00+00:00"
    tasks = [_task("T-B", created=created), _task("T-A", created=created)]
    ordered = order_tasks(tasks, rank_of=lambda t: 0)
    assert [t["task_id"] for t in ordered] == ["T-A", "T-B"]


def test_ordering_is_independent_of_input_permutation():
    base = [_task(f"T-{i:02d}", created=f"2026-01-01T00:00:0{i % 3}+00:00",
                  rule=["MERGE", "STATUS_CHANGE", "CONFLICT"][i % 3]) for i in range(9)]
    rank = lambda t: rank_from_detection_chain(t["metadata_json"]["rule"])
    expected = [t["task_id"] for t in order_tasks(base, rank_of=rank)]
    rng = random.Random(7)
    for _ in range(20):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert [t["task_id"] for t in order_tasks(shuffled, rank_of=rank)] == expected


def test_unranked_task_is_an_error_not_a_default_position():
    with pytest.raises(SchedulerPolicyError):
        order_tasks([_task("T-1", rule="SUMMARY")],
                    rank_of=lambda t: rank_from_detection_chain(t["metadata_json"]["rule"]))


def test_detection_chain_order_is_the_approved_one():
    assert rank_from_detection_chain("MERGE") < rank_from_detection_chain("STATUS_CHANGE")
    assert rank_from_detection_chain("STATUS_CHANGE") < rank_from_detection_chain("CONFLICT")
    assert rank_from_detection_chain("SUMMARY") is None
    assert rank_from_detection_chain("BOGUS") is None


def test_ordering_rejects_bool_rank_and_missing_created_at():
    with pytest.raises(SchedulerPolicyError):
        order_tasks([_task("T-1")], rank_of=lambda t: True)
    task = _task("T-1")
    task["created_at"] = ""
    with pytest.raises(SchedulerPolicyError):
        order_tasks([task], rank_of=lambda t: 0)


def test_ordering_does_not_mutate_tasks():
    tasks = [_task("T-2", rule="CONFLICT"), _task("T-1", rule="MERGE")]
    before = copy.deepcopy(tasks)
    order_tasks(tasks, rank_of=lambda t: rank_from_detection_chain(t["metadata_json"]["rule"]))
    assert tasks == before


# ==========================================================================
# 七、触发条件（AP-02 关闭；条件通过 ≠ 授权）
# ==========================================================================
def test_auto_trigger_is_closed_by_constant():
    assert AUTO_TRIGGER_APPROVED is False


def test_condition_met_is_never_execution_authorization():
    thresholds = TriggerThresholds(new_memories=50, interval_hours=24)
    result = evaluate_trigger(new_memory_count=500, hours_since_last_run=999,
                              thresholds=thresholds)
    assert result.condition_met is True
    assert result.execution_authorized is False
    assert result.reason == "AP-02_NOT_APPROVED"


def test_trigger_condition_boundaries():
    thresholds = TriggerThresholds(new_memories=50, interval_hours=24)
    assert evaluate_trigger(new_memory_count=49, hours_since_last_run=None,
                            thresholds=thresholds).condition_met is False
    assert evaluate_trigger(new_memory_count=50, hours_since_last_run=None,
                            thresholds=thresholds).condition_met is True
    assert evaluate_trigger(new_memory_count=0, hours_since_last_run=23.9,
                            thresholds=thresholds).condition_met is False
    assert evaluate_trigger(new_memory_count=0, hours_since_last_run=24,
                            thresholds=thresholds).condition_met is True


def test_trigger_rejects_negative_or_invalid_inputs():
    thresholds = TriggerThresholds(new_memories=50, interval_hours=24)
    with pytest.raises(SchedulerPolicyError):
        evaluate_trigger(new_memory_count=-1, hours_since_last_run=None, thresholds=thresholds)
    with pytest.raises(SchedulerPolicyError):
        evaluate_trigger(new_memory_count=1, hours_since_last_run=-5, thresholds=thresholds)
    with pytest.raises(SchedulerPolicyError):
        evaluate_trigger(new_memory_count=1, hours_since_last_run=float("nan"), thresholds=thresholds)


def test_trigger_thresholds_have_no_defaults():
    with pytest.raises(TypeError):
        TriggerThresholds()
    with pytest.raises(SchedulerPolicyError):
        TriggerThresholds(new_memories=0, interval_hours=24)
    with pytest.raises(SchedulerPolicyError):
        TriggerThresholds(new_memories=50, interval_hours=0)


def test_evaluate_trigger_has_no_enable_parameter():
    params = set(inspect.signature(evaluate_trigger).parameters)
    assert params == {"new_memory_count", "hours_since_last_run", "thresholds"}
    assert not any(word in name for name in params for word in ("enable", "auto", "approve"))


def test_default_plan_without_trigger_has_no_trigger_evaluation():
    plan = build_plan([], now=NOW, timeout_policy=TIMEOUT, retry_policy=RETRY,
                      breaker_state=BreakerState(), batch_size=1, rank_of=lambda t: 0)
    assert plan.trigger is None


def test_scheduler_has_no_background_execution_imports():
    source = inspect.getsource(sched)
    for forbidden in ("threading", "asyncio", "sched", "apscheduler", "signal",
                      "multiprocessing", "concurrent", "httpx", "requests", "socket"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def test_planning_starts_no_threads_or_timers():
    # 不 reload 模块（reload 会换掉异常类对象，影响其它用例的 pytest.raises）
    before = threading.active_count()
    build_plan([_task("T-1", status=RUNNING, updated=_ts(10))], now=NOW,
               timeout_policy=TIMEOUT, retry_policy=RETRY, breaker_state=BreakerState(),
               batch_size=1, rank_of=lambda t: 0)
    assert threading.active_count() == before


# ==========================================================================
# 八、缺失字段与非法输入
# ==========================================================================
def test_missing_task_id_fails_closed():
    task = _task("T-1")
    del task["task_id"]
    with pytest.raises(SchedulerPolicyError):
        judge_timeout(task, now=NOW, policy=TIMEOUT)
    with pytest.raises(SchedulerPolicyError):
        order_tasks([task], rank_of=lambda t: 0)


def test_build_plan_requires_every_argument():
    with pytest.raises(TypeError):
        build_plan([], now=NOW)  # type: ignore[call-arg]


def test_build_plan_rejects_naive_now():
    with pytest.raises(SchedulerPolicyError):
        build_plan([_task("T-1", status=RUNNING)], now=datetime(2026, 1, 1),
                   timeout_policy=TIMEOUT, retry_policy=RETRY,
                   breaker_state=BreakerState(), batch_size=1, rank_of=lambda t: 0)


def test_non_object_metadata_json_is_invalid_for_retry():
    task = _task("T-1", status=FAILED, failure="stage_timeout")
    task["metadata_json"] = json.dumps([1, 2, 3])
    assert decide_retry(task, policy=RETRY).action == RETRY_INVALID


# ==========================================================================
# 九、结构化结果与多次调用一致性
# ==========================================================================
def test_plan_is_deterministic_across_repeated_calls():
    tasks = [
        _task("T-1", status=RUNNING, updated=_ts(100), rule="MERGE"),
        _task("T-2", status=FAILED, failure="stage_timeout",
              metadata={"rule": "STATUS_CHANGE", "retry_count": 1}),
        _task("T-3", status=RUNNING, updated=_ts(9000), rule="CONFLICT"),
    ]
    kwargs = dict(now=NOW, timeout_policy=TIMEOUT, retry_policy=RETRY,
                  breaker_state=BreakerState(), batch_size=2,
                  rank_of=lambda t: rank_from_detection_chain(t["metadata_json"]["rule"]))
    first = build_plan(tasks, **kwargs)
    for _ in range(5):
        assert build_plan(tasks, **kwargs) == first


def test_judgements_are_structured_values():
    judgement = judge_timeout(_task("T-1", status=RUNNING, updated=_ts(400)),
                              now=NOW, policy=TIMEOUT)
    assert judgement.task_id == "T-1"
    assert judgement.status == TIMEOUT_EXCEEDED
    assert judgement.reason == "stage_timeout"
    assert judgement.elapsed_seconds == pytest.approx(400.0)


def test_retry_decision_reports_failure_code_and_reason():
    d = decide_retry(_task("T-1", status=FAILED, failure="credential_blocked: x"), policy=RETRY)
    assert (d.task_id, d.failure_code, d.reason) == ("T-1", "credential_blocked", "refusal_code")
    assert d.automatic is False


def test_module_does_not_redefine_registry_states():
    assert sched.SUCCEEDED is registry_module.SUCCEEDED
    assert sched.RUNNING is registry_module.RUNNING
    assert sched.FAILED is registry_module.FAILED
    assert sched.CANCELLED is registry_module.CANCELLED
