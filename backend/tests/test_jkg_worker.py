"""P0-1 / P0-2 / P0-3 / P0-6 回归：JKG 拉取式 worker、单行租约、超时恢复、并发互斥。

本文件锁定的四件事
------------------
1. **worker 是拉取的，不是守护进程。** ``run_once`` 跑完一轮就返回，进程里不留
   线程/定时器/asyncio 任务。因此「没在工作」和「工作失败」必须是两种不同的
   可观测结果，而不是同一个安静的日志行。
2. **租约是准入票，不是锁。** 拿不到租约是**正常**结果（``ok=True``、
   ``errors=[]``、``skipped=LEASE_HELD``），不是错误；把它做成 4xx/5xx 会让
   「别人正在跑」和「服务挂了」在监控里长得一模一样。
3. **崩溃必须留下证据，但不得永久霸占槽位。** 租约过期即可被抢占，且抢夺者要
   记录 ``stolen_from``；``last_run`` 里仍写着 ``running`` 的那一轮要被改写为
   ``abandoned``。
4. **零 DDL。** 所有状态都在既有 ``system_settings`` 里，测试逐条断言
   ``jkg_worker`` 相关键，并断言没有新表。

关于并发用例的诚实说明
----------------------
真正的并发需要两个进程或两个线程，而本任务**禁止**引入线程（那正是被禁的东西）。
所以第 14 条用「顺序调用 + 让第一个不释放租约」来模拟临界区：把
``JkgLeaseStore.release`` 在第一个 worker 上替换成返回 ``False`` 的不动实现，
于是它跑完仍持有租约，第二个 worker 的 ``acquire`` 必然失败。这比 sleep + 概率
断言更能证明「同一时刻只有一个执行者」，而且完全确定。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg.lifecycle import CURSOR_KEY, LAST_TICK_KEY, JkgLifecycleSync
from jarvismind.jkg.lease import LEASE_KEY, JkgLeaseStore
from jarvismind.jkg.schema import JKG_ENABLED_KEY, present_jkg_tables
from jarvismind.jkg.worker import (
    LAST_FAILED_KEY,
    LAST_RUN_KEY,
    LAST_SUCCESS_KEY,
    REASON_LEASE_EXPIRED,
    SKIP_DISABLED,
    SKIP_LEASE_HELD,
    SKIP_SCHEMA,
    STATE_ABANDONED,
    STATE_RUNNING,
    JkgWorker,
)
from jarvismind.main import create_app

# ---------------------------------------------------------------------------
# 常量与 SQL（模块级只构造语句，不执行任何数据库调用）
# ---------------------------------------------------------------------------
_CONTENT_MARKER = "MKR" + "-P0-1-WORKER-DO-NOT-LEAK"

#: 32 位十六进制、且内含多段 ``1[3-9]\d{9}`` 的 id，用来钉死 ``uuid4``。
#: 用普通 id 做这条断言是恒真的：内容过滤器只改写「长得像手机号」的字符串。
_MANGLE_PRONE_ID = "13800138000138001380013800138001"

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES (:id, 'SEMANTIC', :content, 5.0, 0.9, :status, :now, :now)"
)

_SET_MEMORY_STATUS = text(
    "UPDATE memory_records SET status = :status, updated_at = :now WHERE id = :id"
)

_INSERT_AUDIT = text(
    "INSERT INTO memory_audit_logs"
    "(id, memory_id, action, actor, detail_json, created_at) "
    "VALUES (:id, :memory_id, :action, :actor, :detail, :created_at)"
)

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)

_READ_SETTING = text("SELECT value FROM system_settings WHERE key = :key")

_ALL_SETTINGS = text("SELECT key FROM system_settings ORDER BY key")

_COUNT_ROWS = "SELECT COUNT(*) FROM {}"


# ---------------------------------------------------------------------------
# 辅助（全部在函数体内取 manager，模块级不建立任何数据库连接）
# ---------------------------------------------------------------------------
def _now():
    """Aware UTC ``datetime`` for fixture arithmetic; never a wall-clock sleep."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _enable_graph(manager, value: str = "1") -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _put_setting(manager, key: str, value: str) -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": key, "value": value, "now": utc_now_iso()},
        )


def _get_setting(manager, key: str):
    with manager.session() as session:
        return session.execute(_READ_SETTING, {"key": key}).scalar()


def _setting_keys(manager) -> tuple:
    with manager.session() as session:
        return tuple(str(row[0]) for row in session.execute(_ALL_SETTINGS).all())


def _worker_keys(manager) -> tuple:
    return tuple(k for k in _setting_keys(manager) if k.startswith("jkg.worker"))


def _jkg_table_counts(manager) -> dict:
    """Row counts for the JKG tables **that actually exist**.

    A not-yet-installed table has no count to report, so it is skipped rather
    than counted as zero: the two states mean different things, and a test that
    conflated them would pass against a database that never got the DDL.
    """
    with manager.session() as session:
        present = tuple(present_jkg_tables(session))
        return {
            table: int(
                session.execute(text(_COUNT_ROWS.format(table))).scalar() or 0
            )
            for table in present
        }


def _row_counts(manager) -> dict:
    return _jkg_table_counts(manager)


def _insert_memory(manager, content: str, *, status: str = "ACTIVE") -> str:
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "content": content,
                "status": status,
                "now": utc_now_iso(),
            },
        )
    return memory_id


def _set_memory_status(manager, memory_id: str, status: str) -> None:
    with manager.session() as session:
        session.execute(
            _SET_MEMORY_STATUS,
            {"id": memory_id, "status": status, "now": utc_now_iso()},
        )


def _audit(manager, memory_id: str, action: str, *, detail: str = "") -> str:
    audit_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_AUDIT,
            {
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "actor": "system",
                "detail": detail or None,
                "created_at": utc_now_iso(),
            },
        )
    return audit_id


def _seed_edge(manager, memory_id: str) -> dict:
    writer = JkgCompositionAdapter(manager).write_service()
    left = writer.upsert_node(
        entity_type="PERSON", name="Alice", source_memory_id=memory_id
    )
    right = writer.upsert_node(
        entity_type="PROJECT", name="JarvisMind", source_memory_id=memory_id
    )
    return writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[memory_id],
    )


def _edge_status(manager, edge_id: str) -> str:
    with manager.session() as session:
        return str(
            session.execute(
                text("SELECT status FROM jkg_edges WHERE edge_id = :eid"),
                {"eid": edge_id},
            ).scalar()
        )


def _worker_settings(settings, **overrides):
    """Worker switch ON (and graph switch irrelevant: it lives in the DB row)."""
    base = {"jkg_worker_enabled": True}
    base.update(overrides)
    return replace(settings, **base)


def _build(manager, settings, *, ingest=None):
    """A worker wired the way the container wires it, minus the ingestor by default."""
    lifecycle = JkgLifecycleSync(manager, ingest=ingest)
    return JkgWorker(
        manager,
        adapter=JkgCompositionAdapter(manager),
        lifecycle=lifecycle,
        ingest=ingest,
        config=settings,
    )


@pytest.fixture()
def graph(manager):
    """装表 + 打开图开关：这是「worker 能真的动图」的前置条件。"""
    create_jkg_schema(manager)
    _enable_graph(manager, "1")
    return manager


# ===========================================================================
# A · worker 基本行为
# ===========================================================================
def test_disabled_worker_skips_and_writes_nothing(manager, settings):
    """关闭态：返回 skipped，且**一个键都不写**（记账也不写）。

    关闭态写 ``last_run`` 会让运维看到「跑过」的证据，而实际上什么都没跑。
    这里连 ``system_settings`` 的键集合都整体比对，避免只检查某个键而漏掉新增键。
    """
    create_jkg_schema(manager)
    _enable_graph(manager, "1")
    keys_before = _setting_keys(manager)
    counts_before = _row_counts(manager)

    worker = _build(manager, _worker_settings(settings, jkg_worker_enabled=False))
    result = worker.run_once()

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["skip_reason"] == SKIP_DISABLED
    assert result["state"] == "disabled"
    assert result["errors"] == []
    assert result["run_id"], "即使跳过也要有 run_id，否则无法与日志对齐"
    assert result["wrote"] is False
    assert _setting_keys(manager) == keys_before
    assert _row_counts(manager) == counts_before


def test_schema_not_ready_worker_skips_without_creating_tables(manager, settings):
    """启用但未装表：报 schema_not_ready 并跳过，绝不建表。"""
    _enable_graph(manager, "1")
    worker = _build(manager, _worker_settings(settings))

    result = worker.run_once()

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["skip_reason"] == SKIP_SCHEMA
    assert result["state"] == "schema_not_ready"
    assert _worker_keys(manager) == (), "未装表时不应留下任何 worker 记账"
    with manager.session() as session:
        assert tuple(present_jkg_tables(session)) == (), "绝对不得建表"


def test_a_normal_run_consumes_audit_rows_and_advances_the_cursor(graph, settings):
    """正常一轮：读到审计行 → 处理该记忆 → 推进游标。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    assert _get_setting(graph, CURSOR_KEY) is None

    worker = _build(graph, _worker_settings(settings))
    result = worker.run_once()

    assert result["ok"] is True, result
    assert result["skipped"] is False
    assert result["entries_read"] >= 1
    assert result["memories_processed"] >= 1
    assert result["wrote"] is True, "真实一轮必须写游标"
    cursor = json.loads(_get_setting(graph, CURSOR_KEY))
    assert cursor["id"], cursor
    assert result["started_at"] and result["finished_at"]


def test_dry_run_writes_neither_the_graph_nor_the_cursor(graph, settings):
    """dry_run 是预览：图不变、游标不写、worker 记账也不写。

    为了让「不写图」不是一个真空断言，这里先造出**本来会被写掉**的状态：
    记忆已 DELETE、关联边还 ACTIVE。真实一轮会把它标 INVALID。
    """
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, memory_id)
    _set_memory_status(graph, memory_id, "DELETED")
    _audit(graph, memory_id, "DELETE")
    keys_before = _setting_keys(graph)
    assert _edge_status(graph, edge["edge_id"]) == "ACTIVE"

    worker = _build(graph, _worker_settings(settings))
    result = worker.run_once(dry_run=True)

    assert result["ok"] is True, result
    assert result["dry_run"] is True
    assert result["wrote"] is False
    assert result["memories_processed"] >= 1, "预览必须真的算过，否则这条用例没有前提"
    assert _edge_status(graph, edge["edge_id"]) == "ACTIVE"
    assert _get_setting(graph, CURSOR_KEY) is None
    assert _get_setting(graph, LAST_TICK_KEY) is None
    assert _setting_keys(graph) == keys_before
    assert _worker_keys(graph) == ()

    # 对照：同样的一轮真实执行确实写图，证明上面的「没变」不是恒真。
    real = worker.run_once()
    assert real["ok"] is True, real
    assert _edge_status(graph, edge["edge_id"]) == "INVALID"


def test_a_raising_tick_is_reported_and_never_propagates(graph, settings, monkeypatch):
    """tick 抛异常时 run_once 不抛，返回 ok=False + 结构化错误。"""
    worker = _build(graph, _worker_settings(settings))

    def boom(**kwargs):
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(worker._lifecycle, "tick", boom)
    result = worker.run_once()

    assert result["ok"] is False
    assert result["skipped"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["phase"] == "run"
    assert result["errors"][0]["error_type"] == "RuntimeError"
    assert "tick exploded" not in json.dumps(result), "异常正文不得进入返回值"
    # 失败也要放掉租约，否则下一轮会被自己堵住一整个 TTL。
    assert _get_setting(graph, LEASE_KEY) is None
    failed = json.loads(_get_setting(graph, LAST_FAILED_KEY))
    assert failed["run_id"] == result["run_id"]
    assert failed["state"] == "failed"


def test_last_run_and_last_success_are_written_by_a_real_run(graph, settings):
    """一次性钉住两条记录：last_run 是「刚刚发生了什么」，last_success 是「上次成功」。"""
    worker = _build(graph, _worker_settings(settings))
    result = worker.run_once()

    assert result["ok"] is True, result
    last_run = json.loads(_get_setting(graph, LAST_RUN_KEY))
    last_success = json.loads(_get_setting(graph, LAST_SUCCESS_KEY))
    assert last_run["run_id"] == result["run_id"]
    assert last_run["state"] == "completed"
    assert last_run["ok"] is True
    assert last_run["finished_at"]
    assert last_success["run_id"] == result["run_id"]
    assert _get_setting(graph, LAST_FAILED_KEY) is None


def test_status_is_valid_before_the_first_run(manager, settings):
    """从未运行过也要返回完整结构，且不写任何东西。"""
    create_jkg_schema(manager)
    _enable_graph(manager, "1")
    keys_before = _setting_keys(manager)
    worker = _build(manager, _worker_settings(settings))

    status = worker.status()

    assert status["enabled"] is True
    assert status["usable"] is True
    assert status["state"] == "ready"
    assert status["last_run"] is None
    assert status["last_success"] is None
    assert status["last_failed"] is None
    assert status["lease"] is None
    assert status["lease_holder"] == ""
    assert status["lease_expired"] is False
    assert status["pending"] == 0
    assert status["runs_this_process"] == 0
    assert status["batch_limit"] == 200
    assert status["lease_ttl_seconds"] == 300
    assert status["reconcile_every"] == 0
    assert status["ingest_wired"] is False
    assert _setting_keys(manager) == keys_before


def test_status_reports_a_backlog_that_excludes_the_overlap_window(graph, settings):
    """``pending`` 只数游标之后的审计行：重叠窗口里的行已经处理过，不能算待办。"""
    worker = _build(graph, _worker_settings(settings))
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    before = worker.status()
    assert before["pending"] == 0

    _audit(graph, memory_id, "CREATE")
    assert worker.status()["pending"] == 1

    # 跑一轮后游标越过该行；重叠窗口会让 tick 再读一次，但 backlog 必须归零。
    worker.run_once()
    after = worker.status()
    assert after["pending"] == 0, "重叠窗口内的行不算待办"


# ===========================================================================
# B · 租约（P0-2）
# ===========================================================================
def test_acquire_succeeds_and_records_a_complete_document(manager):
    store = JkgLeaseStore(manager)
    outcome = store.acquire("owner-a", 300)

    assert outcome["acquired"] is True
    assert outcome["reason"] == "ACQUIRED"
    assert outcome["stolen_from"] == ""
    lease = store.read()
    assert lease["owner"] == "owner-a"
    assert lease["ttl_seconds"] == 300
    assert lease["acquired_at"] and lease["expires_at"] and lease["heartbeat_at"]
    assert lease["stolen_from"] is None


def test_a_live_lease_refuses_a_second_acquire(manager):
    store = JkgLeaseStore(manager)
    assert store.acquire("owner-a", 300)["acquired"] is True

    second = store.acquire("owner-b", 300)

    assert second["acquired"] is False
    assert second["reason"] == "HELD"
    assert second["holder"] == "owner-a"
    assert store.read()["owner"] == "owner-a"


def test_an_expired_lease_is_taken_over_and_stolen_from_is_recorded(manager):
    """过期抢占必须留下 ``stolen_from``，否则抢夺和首次获取长得一样。"""
    stale_expiry = (_now() - timedelta(hours=1)).isoformat()
    _put_setting(
        manager,
        LEASE_KEY,
        json.dumps(
            {
                "owner": "dead-owner",
                "acquired_at": stale_expiry,
                "expires_at": stale_expiry,
                "heartbeat_at": stale_expiry,
                "ttl_seconds": 300,
                "stolen_from": None,
            }
        ),
    )
    store = JkgLeaseStore(manager)

    outcome = store.acquire("owner-b", 300)

    assert outcome["acquired"] is True
    assert outcome["stolen_from"] == "dead-owner"
    lease = store.read()
    assert lease["owner"] == "owner-b"
    assert lease["stolen_from"] == "dead-owner"


def test_only_the_holder_can_heartbeat(manager):
    store = JkgLeaseStore(manager)
    store.acquire("owner-a", 300)

    assert store.heartbeat("owner-a") is True
    assert store.heartbeat("owner-b") is False
    assert store.read()["owner"] == "owner-a"


def test_heartbeat_extends_expiry_but_refuses_a_lapsed_lease(manager):
    """自己放过期就失去租约：下一手可能已经拿走了，续约等于发出第二张票。"""
    store = JkgLeaseStore(manager)
    store.acquire("owner-a", 300)
    first_expiry = store.read()["expires_at"]

    assert store.heartbeat("owner-a") is True
    assert store.read()["expires_at"] >= first_expiry

    lapsed = (_now() - timedelta(minutes=5)).isoformat()
    _put_setting(
        manager,
        LEASE_KEY,
        json.dumps(
            {
                "owner": "owner-a",
                "acquired_at": lapsed,
                "expires_at": lapsed,
                "heartbeat_at": lapsed,
                "ttl_seconds": 300,
                "stolen_from": None,
            }
        ),
    )
    assert store.heartbeat("owner-a") is False


def test_release_lets_the_next_owner_acquire(manager):
    store = JkgLeaseStore(manager)
    store.acquire("owner-a", 300)

    assert store.release("owner-a") is True
    assert store.read() is None
    assert store.acquire("owner-b", 300)["acquired"] is True


def test_a_non_holder_cannot_release(manager):
    store = JkgLeaseStore(manager)
    store.acquire("owner-a", 300)

    assert store.release("owner-b") is False
    assert store.read()["owner"] == "owner-a"


def test_release_on_an_absent_lease_is_false_not_an_error(manager):
    assert JkgLeaseStore(manager).release("owner-a") is False


def test_a_corrupt_lease_document_is_treated_as_expired(manager):
    """损坏的租约按「已过期」处理：宁可多跑一轮，也不能把 worker 永久卡死。"""
    _put_setting(manager, LEASE_KEY, "{not json")
    store = JkgLeaseStore(manager)

    assert store.read() is None
    assert store.acquire("owner-a", 300)["acquired"] is True


def test_release_is_scoped_to_the_holder_across_two_workers(graph, settings):
    """两个 worker 实例同时 run：第一个不放租约，第二个必须 skipped。"""
    first = _build(graph, _worker_settings(settings))
    second = _build(graph, _worker_settings(settings))
    # 让第一个「跑完但不放手」，精确模拟临界区内仍有执行者的状态。
    # 不能用 sleep，也不能起线程：本任务的硬约束就是不起线程。
    first_released: list = []
    original_release = first._leases.release

    def hold_the_lease(owner: str) -> bool:
        first_released.append(owner)
        return False

    first._leases.release = hold_the_lease  # type: ignore[method-assign]

    ran = first.run_once()
    blocked = second.run_once()

    assert ran["ok"] is True and ran["skipped"] is False, ran
    assert first_released, "第一个必须尝试过释放（哪怕被替换成不动实现）"
    assert blocked["ok"] is True, "拿不到租约是正常结果，不是失败"
    assert blocked["skipped"] is True
    assert blocked["skip_reason"] == SKIP_LEASE_HELD
    assert blocked["errors"] == []
    assert blocked["run_id"] and blocked["run_id"] != ran["run_id"]
    assert JkgLeaseStore(graph).read()["owner"] == ran["owner"]

    # 恢复真实释放后，租约被放掉，下一轮才能继续。
    first._leases.release = original_release  # type: ignore[method-assign]
    assert first._leases.release(ran["owner"]) is True
    resumed = second.run_once()
    assert resumed["ok"] is True and resumed["skipped"] is False, resumed


# ===========================================================================
# C · 超时恢复（P0-3）
# ===========================================================================
def _write_running_run(manager, *, owner: str, run_id: str) -> None:
    _put_setting(
        manager,
        LAST_RUN_KEY,
        json.dumps(
            {
                "run_id": run_id,
                "state": STATE_RUNNING,
                "owner": owner,
                "started_at": (_now() - timedelta(hours=2)).isoformat(),
                "dry_run": False,
                "ok": False,
            }
        ),
    )


def _write_expired_lease(manager, *, owner: str) -> str:
    stale = (_now() - timedelta(hours=1)).isoformat()
    _put_setting(
        manager,
        LEASE_KEY,
        json.dumps(
            {
                "owner": owner,
                "acquired_at": stale,
                "expires_at": stale,
                "heartbeat_at": stale,
                "ttl_seconds": 300,
                "stolen_from": None,
            }
        ),
    )
    return stale


def test_recover_marks_a_run_abandoned_when_its_lease_expired(graph, settings):
    """过期租约 + last_run 仍 running → 标记 abandoned 并写 last_failed。"""
    _write_expired_lease(graph, owner="dead-owner")
    _write_running_run(graph, owner="dead-owner", run_id="dead-run-id")
    worker = _build(graph, _worker_settings(settings))

    result = worker.recover_stale_runs()

    assert result["ok"] is True, result
    assert result["recovered"] is True
    assert result["lease_cleared"] is True
    assert result["abandoned_run_id"] == "dead-run-id"
    assert _get_setting(graph, LEASE_KEY) is None

    last_run = json.loads(_get_setting(graph, LAST_RUN_KEY))
    assert last_run["state"] == STATE_ABANDONED
    assert last_run["finished_at"]
    failed = json.loads(_get_setting(graph, LAST_FAILED_KEY))
    assert failed["run_id"] == "dead-run-id"
    assert failed["reason"] == REASON_LEASE_EXPIRED
    assert failed["finished_at"]


def test_recover_does_not_touch_a_live_lease_or_a_healthy_run(graph, settings):
    """租约未过期时 recover 必须完全不动：不误伤正在跑的那一轮。"""
    store = JkgLeaseStore(graph)
    assert store.acquire("live-owner", 300)["acquired"] is True
    _write_running_run(graph, owner="live-owner", run_id="live-run-id")
    worker = _build(graph, _worker_settings(settings))

    result = worker.recover_stale_runs()

    assert result["ok"] is True, result
    assert result["recovered"] is False
    assert result["lease_cleared"] is False
    assert result["abandoned_run_id"] == ""
    assert JkgLeaseStore(graph).read()["owner"] == "live-owner"
    assert json.loads(_get_setting(graph, LAST_RUN_KEY))["state"] == STATE_RUNNING
    assert _get_setting(graph, LAST_FAILED_KEY) is None


def test_recover_does_not_blame_a_completed_run_under_an_expired_lease(graph, settings):
    """租约过期但 last_run 已是 completed：只清租约，不改写已完成的一轮。"""
    _write_expired_lease(graph, owner="gone-owner")
    _put_setting(
        graph,
        LAST_RUN_KEY,
        json.dumps({"run_id": "done-id", "state": "completed", "owner": "gone-owner"}),
    )
    worker = _build(graph, _worker_settings(settings))

    result = worker.recover_stale_runs()

    assert result["lease_cleared"] is True
    assert result["abandoned_run_id"] == ""
    assert json.loads(_get_setting(graph, LAST_RUN_KEY))["state"] == "completed"
    assert _get_setting(graph, LAST_FAILED_KEY) is None


def test_recovery_lets_the_next_run_proceed(graph, settings):
    """恢复之后必须能正常跑下一轮（否则恢复只是把死锁换了个形态）。"""
    _write_expired_lease(graph, owner="dead-owner")
    _write_running_run(graph, owner="dead-owner", run_id="dead-run-id")
    worker = _build(graph, _worker_settings(settings))

    assert worker.recover_stale_runs()["recovered"] is True
    result = worker.run_once()

    assert result["ok"] is True, result
    assert result["skipped"] is False
    assert result["errors"] == []
    last_run = json.loads(_get_setting(graph, LAST_RUN_KEY))
    assert last_run["run_id"] == result["run_id"]
    assert last_run["state"] == "completed"


def test_a_takeover_also_repairs_the_run_it_displaced(graph, settings):
    """抢占路径与独立恢复共用同一实现：抢夺时就把前一轮标成 abandoned。"""
    _write_expired_lease(graph, owner="dead-owner")
    _write_running_run(graph, owner="dead-owner", run_id="dead-run-id")
    worker = _build(graph, _worker_settings(settings))

    result = worker.run_once()

    assert result["ok"] is True, result
    assert result["skipped"] is False
    failed = json.loads(_get_setting(graph, LAST_FAILED_KEY))
    assert failed["run_id"] == "dead-run-id"
    assert failed["reason"] == REASON_LEASE_EXPIRED


# ===========================================================================
# D · 日志与隐私
# ===========================================================================
def test_the_run_id_in_the_log_matches_the_returned_run_id(
    graph, settings, monkeypatch
):
    """钉死 uuid4 后，日志里的 run_id 必须与返回值逐字一致。

    这里刻意用一个会触发内容过滤器的 id 作钉死值：``_log_payload`` 的存在
    理由正是「进程自己生成的标识符不得被当成用户正文改写」。
    """
    import jarvismind.jkg.worker as worker_module

    monkeypatch.setattr(
        worker_module,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=_MANGLE_PRONE_ID)),
    )
    worker = _build(graph, _worker_settings(settings))

    with _caplog_at(logging.INFO, "jarvismind.jkg.worker") as records:
        result = worker.run_once()

    assert result["run_id"] == _MANGLE_PRONE_ID
    messages = [record.getMessage() for record in records]
    assert messages, "run_once 必须留下日志，否则这条断言没有意义"
    assert any(_MANGLE_PRONE_ID in message for message in messages), messages


def test_the_memory_body_never_reaches_the_log_or_the_result(graph, settings):
    """正文标记既不出现在日志里，也不出现在返回值里。"""
    memory_id = _insert_memory(graph, _CONTENT_MARKER)
    _audit(graph, memory_id, "CREATE")
    worker = _build(graph, _worker_settings(settings))

    with _caplog_at(logging.DEBUG, "jarvismind.jkg.worker") as records:
        result = worker.run_once()

    assert result["memories_processed"] >= 1, "必须真的处理过，否则断言恒真"
    assert records, "必须留下日志"
    for record in records:
        assert _CONTENT_MARKER not in record.getMessage()
    assert _CONTENT_MARKER not in json.dumps(result)
    assert _CONTENT_MARKER not in json.dumps(
        json.loads(_get_setting(graph, LAST_RUN_KEY))
    )


# ===========================================================================
# E · HTTP 表面（P0-4）
# ===========================================================================
def _client_for(settings, manager) -> TestClient:
    return TestClient(create_app(settings, manager=manager))


def test_worker_status_is_200_even_when_disabled(client, auth_headers):
    """disabled 也必须 200：这是调度器唯一能无条件轮询的端点。"""
    response = client.get("/api/v1/jkg/worker/status", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["usable"] is False
    assert body["state"] == "disabled"
    assert body["last_run"] is None
    assert body["lease"] is None


def test_worker_run_is_200_and_skipped_when_disabled(client, auth_headers):
    """disabled 时 POST /worker/run 仍是 200 + skipped=true。

    若改成 503，任何把 5xx 当故障计数的看板都会把「功能开关关着」算成事故。
    """
    response = client.post("/api/v1/jkg/worker/run", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["skipped"] is True
    assert body["skip_reason"] == SKIP_DISABLED
    assert body["errors"] == []


def test_worker_run_executes_through_the_http_surface(manager, settings, auth_headers):
    """启用 + 装表 + 有审计行：POST /worker/run 真的跑一轮并返回计数。"""
    create_jkg_schema(manager)
    _enable_graph(manager, "1")
    memory_id = _insert_memory(manager, "Alice 负责 JarvisMind")
    _audit(manager, memory_id, "CREATE")

    application = create_app(_worker_settings(settings), manager=manager)
    with TestClient(application) as client:
        response = client.post("/api/v1/jkg/worker/run", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["skipped"] is False
        assert body["enabled"] is True
        assert body["state"] == "ready"
        assert body["run_id"]
        assert body["entries_read"] >= 1
        assert body["memories_processed"] >= 1
        assert body["wrote"] is True
        assert body["errors"] == []

        status = client.get("/api/v1/jkg/worker/status", headers=auth_headers)
        assert status.status_code == 200, status.text
        assert status.json()["last_run"]["run_id"] == body["run_id"]

        dry = client.post(
            "/api/v1/jkg/worker/run",
            params={"dry_run": "true", "limit": 5},
            headers=auth_headers,
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True
        assert dry.json()["wrote"] is False


def test_worker_run_is_token_gated(client):
    """无 token → 401，与既有 JKG 端点一致。"""
    assert client.post("/api/v1/jkg/worker/run").status_code == 401
    assert client.get("/api/v1/jkg/worker/status").status_code == 401


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
class _caplog_at:
    """Minimal scoped log capture: no autouse fixture, no global side effect."""

    def __init__(self, level: int, logger: str) -> None:
        self.level = level
        self.logger = logger
        self.records: list = []
        self._handler = None
        self._previous_level = None

    def __enter__(self):
        self.records = []

        class _Sink(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record)

        self._handler = _Sink()
        target = logging.getLogger(self.logger)
        self._previous_level = target.level
        target.setLevel(self.level)
        target.addHandler(self._handler)
        self._target = target
        return self.records

    def __exit__(self, *exc_info):
        assert self._handler is not None
        self._target.removeHandler(self._handler)
        self._target.setLevel(self._previous_level)
        return False
