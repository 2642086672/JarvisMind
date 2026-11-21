"""JMR-05 scheduler engine tests.

The scheduler is pull-based and single-process; these tests cover registration,
dedupe, immediate/delayed/periodic scheduling, priority and aging, pause /
resume / cancel / logical delete, bounded ticks, retries and backoff, stale
RUNNING recovery, batching, statistics, zero-DDL persistence, and a real
``JMRService`` integration run.

Every database is the shared isolated ``manager`` fixture. No production
database, migration entry point or background thread is used.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from jarvismind.core.security_gate import CredentialBlockedError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.migrations import MIGRATIONS
from jarvismind.governance.schema import install_p1_schema
from jarvismind.governance.task_registry import (
    CANCELLED,
    CREATED,
    FAILED,
    PAUSED,
    RUNNING,
    SUCCEEDED,
    TaskRegistry,
)
from jarvismind.jmr.scheduler import SchedulerPolicyError
from jarvismind.jmr.scheduler_engine import (
    SchedulerConfig,
    SchedulerEngine,
    SchedulerStateError,
    SchedulerValidationError,
    deterministic_task_id,
    effective_priority,
    order_due_tasks,
    retry_delay_seconds,
    window_state,
)
from jarvismind.jmr.service import JMRRunResult, JMRService, TASK_TYPE

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES ("
    ":id, :memory_type, :content, 5.0, 0.9, 'ACTIVE', "
    ":now, :now, NULL, NULL, :search_text"
    ")"
)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@dataclass(frozen=True)
class FakeConfig:
    stage_timeout_seconds: int = 120


class FakeService:
    """Minimal JMRService-shaped collaborator for deterministic engine tests."""

    def __init__(self, registry: TaskRegistry) -> None:
        self.registry = registry
        self.task_registry = registry
        self.config = FakeConfig()
        self.behaviors: dict[str, str] = {}
        self.calls: list[dict] = []
        self.on_run = None

    def run(
        self,
        *,
        task_id: str,
        run_id: str = "",
        memory_type=None,
        resume: bool = False,
    ) -> JMRRunResult:
        task = self.registry.get(task_id)
        assert task is not None, task_id
        status = task["status"]
        behavior = self.behaviors.get(task_id, "SUCCEED")
        if behavior == "RAISE_BEFORE_START":
            raise RuntimeError("injected early failure")
        if behavior == "RAISE_VALUE_BEFORE_START":
            raise ValueError("injected non-retryable early failure")
        if behavior == "BLOCK_BEFORE_START":
            return JMRRunResult(
                task_id=task_id,
                run_id=run_id,
                status="BLOCKED",
                failure_reason="breaker_tripped:test",
            )
        if status == FAILED and resume:
            self.registry.recover(task_id)
            status = CREATED
        if status == PAUSED and resume:
            self.registry.resume(task_id)
            status = RUNNING
        if status == CREATED:
            self.registry.start(task_id)

        self.calls.append(
            {
                "task_id": task_id,
                "run_id": run_id,
                "resume": resume,
                "memory_type": memory_type,
                "timeout": getattr(self.config, "stage_timeout_seconds", None),
            }
        )
        if self.on_run is not None:
            self.on_run()

        behavior = self.behaviors.get(task_id, "SUCCEED")
        if behavior == "RAISE_EARLY":
            raise RuntimeError("injected early failure")
        if behavior == "SUCCEED":
            self.registry.succeed(task_id)
            return JMRRunResult(
                task_id=task_id, run_id=run_id, status=SUCCEEDED
            )
        if behavior == "SUCCEED_NO_TRANSITION":
            return JMRRunResult(
                task_id=task_id, run_id=run_id, status=SUCCEEDED
            )
        if behavior == "PAUSE":
            self.registry.pause(task_id, reason="fake_pause")
            return JMRRunResult(task_id=task_id, run_id=run_id, status=PAUSED)
        if behavior == "FAIL":
            self.registry.fail(task_id, reason="RuntimeError: boom")
            return JMRRunResult(
                task_id=task_id,
                run_id=run_id,
                status=FAILED,
                failure_reason="RuntimeError: boom",
            )
        if behavior == "FAIL_NONRETRY":
            self.registry.fail(task_id, reason="permission_denied: no")
            return JMRRunResult(
                task_id=task_id,
                run_id=run_id,
                status=FAILED,
                failure_reason="permission_denied: no",
            )
        if behavior == "BLOCK":
            return JMRRunResult(
                task_id=task_id,
                run_id=run_id,
                status="BLOCKED",
                failure_reason="breaker_tripped:test",
            )
        raise AssertionError(f"unknown fake behavior {behavior!r}")


@pytest.fixture()
def scheduler_env(manager):
    install_p1_schema(manager)
    return manager


@pytest.fixture()
def clock():
    return MutableClock(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))


@pytest.fixture()
def fake(scheduler_env):
    return FakeService(TaskRegistry(scheduler_env))


@pytest.fixture()
def engine(scheduler_env, fake, clock):
    return SchedulerEngine(
        scheduler_env,
        service=fake,
        config=SchedulerConfig(
            max_concurrent=1,
            max_tasks_per_tick=10,
            max_scan_tasks=1000,
            retry_base_seconds=10,
            retry_max_seconds=100,
            retry_multiplier=2.0,
            priority_aging_seconds=60,
            max_priority_boost=5,
            stale_running_seconds=60,
            default_timeout_seconds=30,
            batch_size=2,
            tick_interval_seconds=30,
            max_runtime_seconds=60,
        ),
        clock=clock,
    )


def _status(manager, task_id: str) -> str:
    with manager.session() as session:
        return str(
            session.execute(
                text("SELECT status FROM p1_tasks WHERE task_id=:t"),
                {"t": task_id},
            ).scalar()
            or ""
        )


def _events(manager, task_id: str) -> list[str]:
    with manager.session() as session:
        return [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT event_type FROM p1_task_events "
                    "WHERE task_id=:t ORDER BY created_at, event_id"
                ),
                {"t": task_id},
            )
        ]


def _seed_memory(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "memory_type": "JMR_SCHEDULER",
                "content": content,
                "now": utc_now_iso(),
                "search_text": content,
            },
        )


# ---------------------------------------------------------------------------
# Registration and lifecycle
# ---------------------------------------------------------------------------
def test_engine_starts_no_background_threads(engine, clock):
    before = threading.active_count()
    engine.register_task("T-NOTHREAD", now=clock.now)
    engine.tick(now=clock.now)
    assert threading.active_count() == before
    assert not hasattr(engine, "start")
    assert not hasattr(engine, "stop")


def test_register_task_is_idempotent_and_persisted(engine, clock):
    first = engine.register_task("T-1", now=clock.now)
    second = engine.register_task("T-1", now=clock.now)
    assert first.created is True
    assert second.created is False
    assert _status(engine.manager, "T-1") == CREATED
    meta = engine.store.read_task_meta("T-1")
    assert meta is not None
    assert meta["priority"] == 50
    assert meta["attempts"] == 0
    assert _events(engine.manager, "T-1").count("CREATE") == 1
    with engine.manager.session() as session:
        assert (
            session.execute(text("SELECT COUNT(*) FROM p1_tasks")).scalar() == 1
        )


def test_register_task_dedup_key_returns_existing(engine, clock):
    first = engine.register_task("T-A", dedup_key="same-key", now=clock.now)
    second = engine.register_task("T-B", dedup_key="same-key", now=clock.now)
    assert first.task_id == "T-A"
    assert second.task_id == "T-A"
    assert second.created is False
    assert engine.registry.get("T-B") is None


def test_register_task_adopts_existing_p1_task(engine, clock):
    engine.registry.create("T-ADOPT", TASK_TYPE, owner="external")
    adopted = engine.register_task("T-ADOPT", now=clock.now)
    assert adopted.created is False
    assert adopted.adopted is True
    assert engine.store.read_task_meta("T-ADOPT") is not None


def test_register_task_validates_inputs(engine, clock):
    with pytest.raises(SchedulerValidationError):
        engine.register_task("", now=clock.now)
    with pytest.raises(SchedulerValidationError):
        engine.register_task("T-BAD", priority=101, now=clock.now)
    with pytest.raises(SchedulerValidationError):
        engine.register_task("T-BAD", max_attempts=0, now=clock.now)
    with pytest.raises(SchedulerValidationError):
        engine.register_task("T-BAD", timeout_seconds=0, now=clock.now)
    with pytest.raises(SchedulerValidationError):
        engine.register_task(
            "T-BAD",
            window_start=clock.now + timedelta(hours=1),
            window_end=clock.now,
            now=clock.now,
        )
    with pytest.raises(SchedulerValidationError):
        engine.register_task(
            "T-BAD", due_at=datetime(2026, 1, 1), now=clock.now
        )
    with pytest.raises(SchedulerValidationError):
        engine.register_task("T-BAD", metadata=["not", "a", "dict"], now=clock.now)


# ---------------------------------------------------------------------------
# Execution, delay, priority, pause/cancel/delete
# ---------------------------------------------------------------------------
def test_immediate_task_executes_once(engine, fake, clock):
    engine.register_task("T-RUN", now=clock.now)
    first = engine.tick(now=clock.now)
    assert first.executed == ("T-RUN",)
    assert first.succeeded == ("T-RUN",)
    assert _status(engine.manager, "T-RUN") == SUCCEEDED
    assert len(fake.calls) == 1
    second = engine.tick(now=clock.now)
    assert second.executed == ()
    assert len(fake.calls) == 1
    assert _events(engine.manager, "T-RUN")[:3] == [
        "CREATE",
        "START",
        "SUCCEED",
    ]


def test_delayed_task_waits_for_due_time(engine, fake, clock):
    engine.register_task(
        "T-DELAY", due_at=clock.now + timedelta(seconds=60), now=clock.now
    )
    first = engine.tick(now=clock.now)
    assert first.executed == ()
    assert fake.calls == []
    clock.advance(61)
    second = engine.tick(now=clock.now)
    assert second.executed == ("T-DELAY",)
    assert len(fake.calls) == 1


def test_priority_and_aging_order_due_tasks(engine, fake, clock):
    old_due = clock.now - timedelta(minutes=10)
    engine.register_task(
        "T-LOW", priority=50, due_at=old_due, now=clock.now
    )
    engine.register_task("T-HIGH", priority=51, due_at=clock.now, now=clock.now)

    pure = order_due_tasks(
        [
            {
                "task_id": "T-LOW",
                "meta": {
                    "priority": 50,
                    "due_at": old_due.isoformat(),
                    "created_at": old_due.isoformat(),
                },
            },
            {
                "task_id": "T-HIGH",
                "meta": {
                    "priority": 51,
                    "due_at": clock.now.isoformat(),
                    "created_at": clock.now.isoformat(),
                },
            },
        ],
        now=clock.now,
        config=engine.config,
    )
    assert [item["task_id"] for item in pure] == ["T-LOW", "T-HIGH"]
    assert effective_priority(
        {"priority": 50, "due_at": old_due.isoformat()},
        now=clock.now,
        config=engine.config,
    ) == 55

    first = engine.tick(now=clock.now, max_tasks=1)
    assert first.executed == ("T-LOW",)
    second = engine.tick(now=clock.now, max_tasks=1)
    assert second.executed == ("T-HIGH",)


def test_pause_created_task_then_resume(engine, fake, clock):
    engine.register_task("T-PAUSE", now=clock.now)
    engine.pause_task("T-PAUSE", reason="operator")
    assert engine.tick(now=clock.now).executed == ()
    assert fake.calls == []
    engine.resume_task("T-PAUSE")
    assert engine.tick(now=clock.now).executed == ("T-PAUSE",)


def test_running_task_pause_is_cooperative_and_resumable(engine, fake, clock):
    engine.register_task("T-COOP", now=clock.now, max_attempts=2)
    fake.behaviors["T-COOP"] = "PAUSE"
    first = engine.tick(now=clock.now)
    assert first.executed == ("T-COOP",)
    assert _status(engine.manager, "T-COOP") == PAUSED
    assert engine.store.read_task_meta("T-COOP")["paused"] is True

    fake.behaviors["T-COOP"] = "SUCCEED"
    engine.resume_task("T-COOP")
    second = engine.tick(now=clock.now)
    assert second.executed == ("T-COOP",)
    assert _status(engine.manager, "T-COOP") == SUCCEEDED
    assert fake.calls[-1]["resume"] is True


def test_cancel_and_logical_delete(engine, fake, clock):
    engine.register_task("T-CANCEL", now=clock.now)
    engine.cancel_task("T-CANCEL", reason="stop")
    assert _status(engine.manager, "T-CANCEL") == CANCELLED
    assert engine.tick(now=clock.now).executed == ()
    assert fake.calls == []

    engine.register_task("T-DELETE", now=clock.now)
    result = engine.delete_task("T-DELETE", reason="cleanup")
    assert result["deleted"] is True
    assert _status(engine.manager, "T-DELETE") == CANCELLED
    assert engine.tick(now=clock.now).executed == ()
    assert [item["task_id"] for item in engine.list_tasks()] == ["T-CANCEL"]
    assert {item["task_id"] for item in engine.list_tasks(include_deleted=True)} == {
        "T-CANCEL",
        "T-DELETE",
    }


def test_running_task_cannot_be_deleted(engine, fake, clock):
    engine.register_task("T-RUNNING-DEL", now=clock.now)
    engine.registry.start("T-RUNNING-DEL")
    with pytest.raises(SchedulerStateError):
        engine.delete_task("T-RUNNING-DEL")


def test_force_does_not_override_pause(engine, fake, clock):
    engine.register_task("T-FORCE-PAUSE", now=clock.now)
    engine.pause_task("T-FORCE-PAUSE")
    with pytest.raises(SchedulerStateError):
        engine.execute_task("T-FORCE-PAUSE", force=True, now=clock.now)


def test_cancel_failed_task_is_logical_noop(engine, fake, clock):
    engine.register_task("T-FAIL-CANCEL", now=clock.now)
    fake.behaviors["T-FAIL-CANCEL"] = "FAIL_NONRETRY"
    engine.tick(now=clock.now)
    result = engine.cancel_task("T-FAIL-CANCEL", reason="stop retry")
    assert result["status"] == FAILED
    assert result["cancelled"] is True
    assert result["noop"] is True
    assert engine.store.read_task_meta("T-FAIL-CANCEL")["next_retry_at"] is None


def test_breaker_block_before_start_pauses_not_spins(engine, fake, clock):
    engine.register_task("T-BLOCK", max_attempts=3, now=clock.now)
    fake.behaviors["T-BLOCK"] = "BLOCK_BEFORE_START"
    first = engine.tick(now=clock.now)
    assert first.executed == ("T-BLOCK",)
    assert _status(engine.manager, "T-BLOCK") == CREATED
    meta = engine.store.read_task_meta("T-BLOCK")
    assert meta["paused"] is True
    assert meta["blocked_reason"].startswith("breaker_tripped")
    assert engine.tick(now=clock.now).executed == ()

    fake.behaviors["T-BLOCK"] = "SUCCEED"
    engine.resume_task("T-BLOCK")
    second = engine.tick(now=clock.now)
    assert second.executed == ("T-BLOCK",)
    assert _status(engine.manager, "T-BLOCK") == SUCCEEDED


# ---------------------------------------------------------------------------
# Periodic schedules
# ---------------------------------------------------------------------------
def test_success_result_reconciles_stuck_running(engine, fake, clock):
    engine.register_task("T-RECON", now=clock.now)
    fake.behaviors["T-RECON"] = "SUCCEED_NO_TRANSITION"
    result = engine.tick(now=clock.now)
    assert result.succeeded == ("T-RECON",)
    assert _status(engine.manager, "T-RECON") == SUCCEEDED
    meta = engine.store.read_task_meta("T-RECON")
    assert "status_mismatch" not in meta


def test_schedule_materializes_once_per_occurrence(engine, clock):
    engine.register_schedule(
        "S-ONE", interval_seconds=60, first_run_at=clock.now
    )
    first = engine.tick(now=clock.now)
    assert len(first.materialized) == 1
    second = engine.tick(now=clock.now)
    assert second.materialized == ()
    metas = [
        meta
        for meta in engine.store.list_task_meta()
        if meta.get("schedule_id") == "S-ONE"
    ]
    assert len(metas) == 1

    clock.advance(61)
    third = engine.tick(now=clock.now)
    assert len(third.materialized) == 1
    metas = [
        meta
        for meta in engine.store.list_task_meta()
        if meta.get("schedule_id") == "S-ONE"
    ]
    assert len(metas) == 2
    assert metas[0]["task_id"] != metas[1]["task_id"]
    schedule = engine.list_schedules()[0]
    assert schedule["next_run_at"] > clock.now.isoformat()


def test_schedule_overlap_is_skipped(engine, fake, clock):
    engine.register_schedule(
        "S-OVERLAP", interval_seconds=60, first_run_at=clock.now
    )
    fake.behaviors[deterministic_task_id("S-OVERLAP", clock.now)] = "PAUSE"
    first = engine.tick(now=clock.now)
    assert len(first.materialized) == 1
    paused_id = first.materialized[0]
    assert _status(engine.manager, paused_id) == PAUSED

    clock.advance(61)
    second = engine.tick(now=clock.now)
    assert second.materialized == ()
    schedule = engine.list_schedules()[0]
    assert schedule["skipped_overlap"] >= 1


def test_schedule_missed_occurrences_do_not_storm(engine, clock):
    engine.register_schedule(
        "S-MISSED",
        interval_seconds=60,
        first_run_at=clock.now - timedelta(minutes=10),
    )
    result = engine.tick(now=clock.now)
    assert len(result.materialized) == 1
    schedule = engine.list_schedules()[0]
    assert schedule["missed"] >= 9
    assert schedule["next_run_at"] > clock.now.isoformat()


def test_exhausted_schedule_occurrence_does_not_block_next(engine, fake, clock):
    engine.register_schedule(
        "S-NEXT", interval_seconds=60, first_run_at=clock.now, max_attempts=1
    )
    first_id = deterministic_task_id("S-NEXT", clock.now)
    fake.behaviors[first_id] = "FAIL_NONRETRY"
    first = engine.tick(now=clock.now)
    assert first.materialized == (first_id,)
    assert engine.store.read_task_meta(first_id)["retry_eligible"] is False
    clock.advance(61)
    second = engine.tick(now=clock.now)
    assert len(second.materialized) == 1
    assert second.materialized[0] != first_id


def test_schedule_pause_and_delete(engine, clock):
    engine.register_schedule(
        "S-PAUSE", interval_seconds=60, first_run_at=clock.now, enabled=False
    )
    assert engine.tick(now=clock.now).materialized == ()
    engine.set_schedule_enabled("S-PAUSE", True)
    assert len(engine.tick(now=clock.now).materialized) == 1
    assert engine.delete_schedule("S-PAUSE") is True
    clock.advance(61)
    assert engine.tick(now=clock.now).materialized == ()
    assert engine.delete_schedule("S-PAUSE") is False


def test_deterministic_schedule_task_id(clock):
    first = deterministic_task_id("S-1", clock.now)
    second = deterministic_task_id("S-1", clock.now)
    third = deterministic_task_id("S-1", clock.now + timedelta(seconds=1))
    assert first == second
    assert first != third


# ---------------------------------------------------------------------------
# Retry, timeout, stale RUNNING recovery
# ---------------------------------------------------------------------------
def test_retry_backoff_and_max_attempts(engine, fake, clock):
    engine.register_task(
        "T-RETRY",
        max_attempts=2,
        retry_base_seconds=10,
        retry_max_seconds=100,
        now=clock.now,
    )
    fake.behaviors["T-RETRY"] = "FAIL"
    first = engine.tick(now=clock.now)
    assert first.failed == ("T-RETRY",)
    meta = engine.store.read_task_meta("T-RETRY")
    assert meta["retry_eligible"] is True
    assert meta["next_retry_at"] is not None
    assert meta["attempts"] == 1
    assert meta["retry_delay_seconds"] == 10

    assert engine.tick(now=clock.now).executed == ()
    clock.advance(11)
    second = engine.tick(now=clock.now)
    assert second.executed == ("T-RETRY",)
    assert fake.calls[-1]["resume"] is True
    meta = engine.store.read_task_meta("T-RETRY")
    assert meta["attempts"] == 2
    assert meta["retry_blocked_reason"] == "max_attempts_reached"
    assert meta["next_retry_at"] is None


def test_non_retryable_failure_is_not_scheduled(engine, fake, clock):
    engine.register_task("T-NORETRY", max_attempts=3, now=clock.now)
    fake.behaviors["T-NORETRY"] = "FAIL_NONRETRY"
    result = engine.tick(now=clock.now)
    assert result.failed == ("T-NORETRY",)
    meta = engine.store.read_task_meta("T-NORETRY")
    assert meta["retry_eligible"] is False
    assert meta["next_retry_at"] is None
    assert meta["retry_blocked_reason"] == "not_retryable"


def test_manual_retry_after_budget_is_explicit(engine, fake, clock):
    engine.register_task("T-MANUAL", max_attempts=1, now=clock.now)
    fake.behaviors["T-MANUAL"] = "FAIL"
    engine.tick(now=clock.now)
    assert engine.store.read_task_meta("T-MANUAL")["next_retry_at"] is None

    fake.behaviors["T-MANUAL"] = "SUCCEED"
    engine.resume_task("T-MANUAL")
    result = engine.tick(now=clock.now)
    assert result.executed == ("T-MANUAL",)
    assert _status(engine.manager, "T-MANUAL") == SUCCEEDED
    assert len(fake.calls) == 2


def test_stale_running_task_is_recovered(engine, fake, clock):
    engine.register_task(
        "T-STALE",
        max_attempts=2,
        timeout_seconds=30,
        retry_base_seconds=10,
        now=clock.now,
    )
    engine.registry.start("T-STALE")
    old = (clock.now - timedelta(minutes=5)).isoformat()
    with engine.manager.session() as session:
        session.execute(
            text("UPDATE p1_tasks SET updated_at=:t WHERE task_id='T-STALE'"),
            {"t": old},
        )
    result = engine.tick(now=clock.now)
    assert "T-STALE" in result.stale_recovered
    assert _status(engine.manager, "T-STALE") == FAILED
    meta = engine.store.read_task_meta("T-STALE")
    assert meta["retry_eligible"] is True
    assert meta["next_retry_at"] is not None

    fake.behaviors["T-STALE"] = "SUCCEED"
    clock.advance(11)
    retried = engine.tick(now=clock.now)
    assert retried.executed == ("T-STALE",)
    assert _status(engine.manager, "T-STALE") == SUCCEEDED


def test_timeout_override_is_applied_and_restored(engine, fake, clock):
    engine.register_task("T-TIMEOUT", timeout_seconds=7, now=clock.now)
    engine.tick(now=clock.now)
    assert fake.calls[0]["timeout"] == 7
    assert fake.config.stage_timeout_seconds == 120


def test_early_engine_exception_marks_failed_not_cancelled(engine, fake, clock):
    engine.register_task("T-EARLY", now=clock.now)
    fake.behaviors["T-EARLY"] = "RAISE_BEFORE_START"
    result = engine.tick(now=clock.now)
    # A system error before the service starts is NOT a user cancellation.
    assert _status(engine.manager, "T-EARLY") == FAILED
    assert result.executed == ("T-EARLY",)
    assert result.succeeded == ()
    assert result.failed == ("T-EARLY",)
    meta = engine.store.read_task_meta("T-EARLY")
    assert meta["last_status"] == FAILED
    assert "scheduler_execution_error" in meta["last_failure_reason"]
    # max_attempts=1 stops the retry, but the task stays visible as FAILED.
    assert meta["retry_eligible"] is False
    assert meta["next_retry_at"] is None
    assert _events(engine.manager, "T-EARLY") == ["CREATE", "START", "FAIL"]


def test_transient_infra_crash_is_retried_after_delay(engine, fake, clock):
    engine.register_task(
        "T-INFRA", max_attempts=2, retry_base_seconds=30, now=clock.now
    )
    fake.behaviors["T-INFRA"] = "RAISE_BEFORE_START"
    first = engine.tick(now=clock.now)
    assert _status(engine.manager, "T-INFRA") == FAILED
    assert first.failed == ("T-INFRA",)
    meta = engine.store.read_task_meta("T-INFRA")
    assert meta["retry_eligible"] is True
    assert meta["next_retry_at"]
    # Not lost: retried automatically, but not immediately.
    second = engine.tick(now=clock.now)
    assert "T-INFRA" not in second.executed
    fake.behaviors["T-INFRA"] = "SUCCEED"
    clock.advance(3600)
    third = engine.tick(now=clock.now)
    assert "T-INFRA" in third.succeeded
    assert _status(engine.manager, "T-INFRA") == SUCCEEDED


def test_infra_crash_retry_is_bounded_by_max_attempts(engine, fake, clock):
    engine.register_task(
        "T-INFRA-MAX", max_attempts=2, retry_base_seconds=10, now=clock.now
    )
    fake.behaviors["T-INFRA-MAX"] = "RAISE_BEFORE_START"
    engine.tick(now=clock.now)
    clock.advance(3600)
    engine.tick(now=clock.now)
    meta = engine.store.read_task_meta("T-INFRA-MAX")
    assert meta["attempts"] == 2
    assert meta["retry_eligible"] is False
    assert meta["retry_blocked_reason"] == "max_attempts_reached"
    # A further tick must not execute again: no unbounded retry loop.
    before = len(fake.calls)
    engine.tick(now=clock.now)
    assert len(fake.calls) == before


def test_non_retryable_infra_crash_stays_failed_for_operator(engine, fake, clock):
    engine.register_task("T-INFRA-BAD", max_attempts=3, now=clock.now)
    fake.behaviors["T-INFRA-BAD"] = "RAISE_VALUE_BEFORE_START"
    engine.tick(now=clock.now)
    meta = engine.store.read_task_meta("T-INFRA-BAD")
    assert meta["retry_eligible"] is False
    assert meta["next_retry_at"] is None
    assert meta["retry_blocked_reason"] == "not_retryable"


def test_infra_crash_recovery_survives_engine_restart(scheduler_env, fake, clock):
    engine = SchedulerEngine(
        scheduler_env, service=fake, config=SchedulerConfig(), clock=clock
    )
    engine.register_task(
        "T-INFRA-RESTART", max_attempts=2, retry_base_seconds=30, now=clock.now
    )
    fake.behaviors["T-INFRA-RESTART"] = "RAISE_BEFORE_START"
    engine.tick(now=clock.now)
    assert _status(scheduler_env, "T-INFRA-RESTART") == FAILED

    # A new process/engine reads the same persisted metadata and resumes the
    # bounded retry instead of losing the task.
    restarted = SchedulerEngine(
        scheduler_env, service=fake, config=SchedulerConfig(), clock=clock
    )
    fake.behaviors["T-INFRA-RESTART"] = "SUCCEED"
    clock.advance(3600)
    result = restarted.tick(now=clock.now)
    assert "T-INFRA-RESTART" in result.succeeded


# ---------------------------------------------------------------------------
# Bounds, batching, stats, schema
# ---------------------------------------------------------------------------
def test_tick_respects_max_tasks_and_runtime_budget(engine, fake, clock):
    for index in range(3):
        engine.register_task(f"T-MAX-{index}", now=clock.now)
    first = engine.tick(now=clock.now, max_tasks=2)
    assert len(first.executed) == 2
    assert first.truncated is True
    second = engine.tick(now=clock.now, max_tasks=2)
    assert len(second.executed) == 1

    fake.on_run = lambda: clock.advance(100)
    engine.register_task("T-SLOW-1", now=clock.now)
    engine.register_task("T-SLOW-2", now=clock.now)
    slow = engine.tick(now=clock.now, max_runtime_seconds=50)
    assert len(slow.executed) == 1
    assert slow.truncated is True


def test_batch_partial_failure_and_metadata(engine, fake, clock):
    for task_id in ("T-B1", "T-B2", "T-B3"):
        engine.register_task(task_id, now=clock.now)
    fake.behaviors["T-B2"] = "FAIL_NONRETRY"
    result = engine.run_batch(
        ["T-B1", "T-B2", "T-B3"], batch_size=3, now=clock.now
    )
    assert len(result.batches) == 1
    batch = result.batches[0]
    assert batch.status == "PARTIAL"
    assert [item.result_status for item in batch.outcomes] == [
        SUCCEEDED,
        FAILED,
        SUCCEEDED,
    ]
    for task_id in ("T-B1", "T-B2", "T-B3"):
        meta = engine.store.read_task_meta(task_id)
        assert meta["batch_id"] == batch.batch_id
        assert meta["batch_index"] == 0


def test_batch_recovery_skips_succeeded_and_retries_failed(engine, fake, clock):
    for task_id in ("T-RB1", "T-RB2"):
        engine.register_task(
            task_id, max_attempts=2, retry_base_seconds=10, now=clock.now
        )
    fake.behaviors["T-RB2"] = "FAIL"
    first = engine.run_batch(
        ["T-RB1", "T-RB2"], batch_size=2, now=clock.now
    )
    assert first.batches[0].status == "PARTIAL"
    assert _status(engine.manager, "T-RB1") == SUCCEEDED
    assert _status(engine.manager, "T-RB2") == FAILED

    clock.advance(11)
    fake.behaviors["T-RB2"] = "SUCCEED"
    second = engine.run_batch(
        ["T-RB1", "T-RB2"], batch_size=2, now=clock.now
    )
    assert second.batches[0].status == "SUCCEEDED"
    outcomes = {item.task_id: item for item in second.batches[0].outcomes}
    assert outcomes["T-RB1"].skipped is True
    assert outcomes["T-RB2"].executed is True
    assert _status(engine.manager, "T-RB2") == SUCCEEDED


def test_batch_rejects_duplicate_ids(engine, clock):
    engine.register_task("T-DUP", now=clock.now)
    with pytest.raises(SchedulerPolicyError):
        engine.run_batch(["T-DUP", "T-DUP"], batch_size=2, now=clock.now)


def test_stats_reports_queue_rates_and_last_tick(engine, fake, clock):
    engine.register_task("T-STAT-OK", now=clock.now)
    engine.register_task("T-STAT-FAIL", now=clock.now)
    engine.register_task(
        "T-STAT-FUTURE", due_at=clock.now + timedelta(hours=1), now=clock.now
    )
    fake.behaviors["T-STAT-FAIL"] = "FAIL_NONRETRY"
    clock.advance(1)
    engine.tick(now=clock.now)
    stats = engine.stats()
    assert stats["mode"] == "PULL_SINGLE_PROCESS"
    assert stats["automatic_trigger_started"] is False
    assert stats["succeeded"] == 1
    assert stats["failed"] == 1
    assert stats["created"] == 1
    assert stats["due_now"] == 0
    assert stats["success_rate"] == 0.5
    assert stats["failure_rate"] == 0.5
    assert stats["last_tick"] is not None
    assert stats["duration_samples"] >= 0
    with engine.manager.session() as session:
        session.execute(
            text(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES ('jmr.breaker', '{\"tripped\": true}', :u) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            ),
            {"u": utc_now_iso()},
        )
    assert engine.stats()["breaker_tripped"] is True


def test_list_tasks_due_only(engine, clock):
    engine.register_task("T-DUE", now=clock.now)
    engine.register_task(
        "T-LATER", due_at=clock.now + timedelta(hours=1), now=clock.now
    )
    due = engine.list_tasks(due_only=True)
    assert [item["task_id"] for item in due] == ["T-DUE"]
    assert engine.list_tasks(status=CREATED)[0]["scheduler"]["priority"] == 50


def test_scheduler_writes_no_schema(engine, clock, scheduler_env):
    with scheduler_env.session() as session:
        before = {
            str(row[0])
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    engine.register_schedule("S-SCHEMA", interval_seconds=60, first_run_at=clock.now)
    engine.register_task("T-SCHEMA", now=clock.now)
    engine.tick(now=clock.now)
    with scheduler_env.session() as session:
        after = {
            str(row[0])
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert before == after
    assert MIGRATIONS[-1].version == 6


# ---------------------------------------------------------------------------
# Pure helpers and real service integration
# ---------------------------------------------------------------------------
def test_pure_helpers_validate_and_order():
    with pytest.raises(SchedulerValidationError):
        retry_delay_seconds(0, base_seconds=10, max_seconds=100, multiplier=2)
    assert retry_delay_seconds(1, base_seconds=10, max_seconds=100, multiplier=2) == 10
    assert retry_delay_seconds(2, base_seconds=10, max_seconds=100, multiplier=2) == 20
    assert retry_delay_seconds(9, base_seconds=10, max_seconds=100, multiplier=2) == 100

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert window_state({"window_start": None, "window_end": None}, now=now) == "OPEN"
    assert window_state(
        {"window_start": (now + timedelta(minutes=1)).isoformat()}, now=now
    ) == "EARLY"
    assert window_state(
        {"window_end": (now - timedelta(minutes=1)).isoformat()}, now=now
    ) == "MISSED"


def test_real_jmr_service_integration(scheduler_env, clock):
    _seed_memory(
        scheduler_env, "M-SCHED-1", "Bifrost 网关采用 PostgreSQL 主从架构"
    )
    _seed_memory(
        scheduler_env,
        "M-SCHED-2",
        "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
    )
    service = JMRService.build(manager=scheduler_env)
    real_engine = SchedulerEngine(
        scheduler_env,
        service=service,
        config=SchedulerConfig(
            max_tasks_per_tick=3,
            batch_size=2,
            default_timeout_seconds=30,
            max_runtime_seconds=60,
        ),
        clock=clock,
    )
    registration = real_engine.register_task("T-REAL", now=clock.now)
    assert registration.created is True
    result = real_engine.tick(now=clock.now)
    assert "T-REAL" in result.succeeded
    assert _status(scheduler_env, "T-REAL") == SUCCEEDED
    events = _events(scheduler_env, "T-REAL")
    assert events[:3] == ["CREATE", "START", "SUCCEED"]
    assert real_engine.stats()["succeeded"] == 1


# ---------------------------------------------------------------------------
# JMR-07 identifier guard: credential-shaped scheduler parameters are refused
# before any persistence, and never appear in settings or audit rows.
# ---------------------------------------------------------------------------
def _persisted_text(manager) -> str:
    with manager.session() as session:
        settings = session.execute(
            text("SELECT key || '=' || value FROM system_settings")
        ).scalars().all()
        audit = session.execute(
            text("SELECT action || ' ' || COALESCE(detail_json, '') FROM memory_audit_logs")
        ).scalars().all()
    return "\n".join([*settings, *audit])


def test_register_refuses_credential_shaped_task_id(scheduler_env, engine, clock):
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    with pytest.raises(CredentialBlockedError):
        engine.register_task(secret, now=clock.now)
    assert TaskRegistry(scheduler_env).get(secret) is None
    assert secret not in _persisted_text(scheduler_env)


def test_register_refuses_credential_shaped_batch_dedup_and_metadata(
    scheduler_env, engine, clock
):
    secret = "sk-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"
    with pytest.raises(CredentialBlockedError):
        engine.register_task("T-BATCH-SAFE", batch_id=secret, now=clock.now)
    with pytest.raises(CredentialBlockedError):
        engine.register_task("T-DEDUP-SAFE", dedup_key=secret, now=clock.now)
    with pytest.raises(CredentialBlockedError):
        engine.register_task("T-META-SAFE", metadata={"note": secret}, now=clock.now)
    for task_id in ("T-BATCH-SAFE", "T-DEDUP-SAFE", "T-META-SAFE"):
        assert TaskRegistry(scheduler_env).get(task_id) is None
    assert secret not in _persisted_text(scheduler_env)


# ---------------------------------------------------------------------------
# JMR-07 R-5: key=value credential shapes are refused for caller-authored ids
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_id",
    [
        "api_key=abcd1234efgh5678",
        "token=abcdef123456",
        "access_token=abcd1234efgh",
        "api_token=zzzz1111yyyy2222",
    ],
)
def test_register_refuses_key_value_credential_ids(scheduler_env, engine, clock, bad_id):
    with pytest.raises(CredentialBlockedError):
        engine.register_task(bad_id, now=clock.now)
    assert TaskRegistry(scheduler_env).get(bad_id) is None
    assert bad_id not in _persisted_text(scheduler_env)
    assert bad_id.split("=", 1)[1] not in _persisted_text(scheduler_env)


@pytest.mark.parametrize(
    "good_id",
    [
        "T-OK",
        "batch-2026-01",
        "jmr-reflection-T-1",
        "0f8b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d",
        "3f2a9c1e-7b4d-4e2a-9c3b-1d5e6f7a8b9c",
    ],
)
def test_register_still_accepts_ordinary_identifiers(scheduler_env, engine, clock, good_id):
    registration = engine.register_task(good_id, now=clock.now)
    assert registration.created is True
    assert _status(scheduler_env, good_id) == CREATED
