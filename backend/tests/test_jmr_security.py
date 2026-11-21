"""P2 JMR · 安全反例 / 治理态 / 任务与成本验收测试。

对齐 JarvisMind-2.0-P2-JMR-Architecture-Design.md v1.2：
  §12 标准 5/6/7/8/9/10/11/12/14、§20 契约对齐点。

硬性环境规则：
  * 治理态 fixture 用 build_governance(manager, settings, enforce=True)，
    teardown 必须 yield + try/finally: deactivate_governance()（进程级 _ACTIVE 泄漏会污染后续用例）；
  * 失败/暂停/取消注入不给生产代码开测试后门，只用 monkeypatch 控制外部条件；
  * JARVISMIND_* 只在用例内 setenv；不裸调 DatabaseManager / sqlite3.connect / load_settings；
  * 标准 5/14 摘要用独立新连接 ReadOnlyProjection.content_digest()；
  * 不 skip / xfail 任何用例。
"""
from __future__ import annotations

import pytest
from sqlalchemy import column, table, text, update

from jarvismind.database.migrations import (
    MigrationBlocked,
    MigrationRunner,
    initialize_database,
)
from jarvismind.database.models import MemoryRecord
from jarvismind.governance import build_governance, deactivate_governance
from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.config import GovernanceConfig
from jarvismind.governance.cost_governor import CostGovernor
from jarvismind.governance.errors import UnauthorizedWriteError
from jarvismind.governance.identity import (
    CAP_MEMORY_READ,
    CAP_MEMORY_WRITE,
    CAP_TASK_MANAGE,
    TrustedPrincipal,
)
from jarvismind.governance.projection import ReadOnlyProjection
from jarvismind.governance.schema import install_p1_schema
from jarvismind.governance.task_registry import TaskRegistry
from jarvismind.governance.write_governor import OP_CREATE
from jarvismind.jmr import JMRConfig, build_jmr_service
from jarvismind.jmr.provenance import RULES
from jarvismind.jmr.service import JMRService
from jarvismind.schemas.memory import MemoryCreate, MemoryType

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"

DERIVED_TABLES = ("memory_merges", "memory_corrections", "memory_summaries")

_INSERT_SQL = (
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, :importance, :confidence, :status, "
    ":created_at, :updated_at, :source_id, :metadata_json, :search_text)"
)


def _row(memory_id, content, memory_type, created_at):
    return {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "importance": 5.0,
        "confidence": 0.9,
        "status": "ACTIVE",
        "created_at": created_at,
        "updated_at": created_at,
        "source_id": None,
        "metadata_json": None,
        "search_text": content,
    }


def _full_dataset():
    return [
        _row("M-DUP-A", "Bifrost 网关采用 PostgreSQL 主从架构", "JMR_DUP", TS1),
        _row(
            "M-DUP-B",
            "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
            "JMR_DUP",
            TS2,
        ),
        _row("M-ST-1", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS1),
        _row("M-ST-2", "Phoenix 项目当前状态为已完成", "JMR_STATUS", TS2),
        _row("M-CF-1", "Nebula 缓存层使用 Redis Cluster 三主三从部署", "JMR_CONFLICT", TS1),
        _row("M-CF-2", "Nebula 缓存层改用 Memcached 单机方案降低成本", "JMR_CONFLICT", TS2),
        _row("M-RL-1", "Atlas 日志平台采集 Nginx 访问日志", "JMR_RELATED", TS1),
        _row("M-RL-2", "Atlas 日志平台采集应用错误日志", "JMR_RELATED", TS2),
    ]


def _seed(manager, specs):
    with manager.session() as session:
        for spec in specs:
            session.execute(text(_INSERT_SQL), spec)


def _scalar(manager, sql, params=None):
    with manager.session() as session:
        return session.execute(text(sql), params or {}).scalar()


def _rows(manager, sql, params=None):
    with manager.session() as session:
        return [tuple(r) for r in session.execute(text(sql), params or {})]


def _table_count(manager, table_name, where=""):
    return int(_scalar(manager, f"SELECT COUNT(*) FROM {table_name} {where}") or 0)


def _five_counts(manager):
    """标准 8：三张派生表 + memory_audit_logs + p1_task_events 精确行数。"""
    return {
        "memory_merges": _table_count(manager, "memory_merges"),
        "memory_corrections": _table_count(manager, "memory_corrections"),
        "memory_summaries": _table_count(manager, "memory_summaries"),
        "memory_audit_logs": _table_count(manager, "memory_audit_logs"),
        "p1_task_events": _table_count(manager, "p1_task_events"),
    }


def _derived_counts(manager):
    return {t: _table_count(manager, t) for t in DERIVED_TABLES}


def _content_digest(manager):
    """独立新连接摘要（每次 content_digest() 都重开 mode=ro 连接）。"""
    return ReadOnlyProjection(manager).content_digest()


def _jmr_audit_count(manager, action):
    return int(
        _scalar(
            manager,
            "SELECT COUNT(*) FROM memory_audit_logs WHERE action=:a",
            {"a": action},
        )
        or 0
    )


def _gov_cfg():
    """与 test_p1_governance.py:62-74 同量级的宽松上限，避免误伤正常用例。"""
    return GovernanceConfig(
        grant_ttl_seconds=300,
        grant_key_version=1,
        task_budget_cny=100.0,
        task_token_limit=100000,
        task_call_limit=50,
        batch_call_limit=50,
        rate_limit_per_minute=100,
        breaker_threshold=3,
    )


def _cost_limited_service(manager, settings):
    """标准 11：JMRService.__init__（§5 冻结公开 API）注入 task_call_limit=1 的 CostGovernor。"""
    cost = CostGovernor(manager, settings, config=GovernanceConfig(task_call_limit=1))
    return JMRService(
        manager=manager,
        projection=ReadOnlyProjection(manager).for_module("JMR"),
        task_registry=TaskRegistry(manager),
        cost_governor=cost,
        audit_bus=AuditBus(manager),
        config=JMRConfig(),
    )


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def jmr_env(manager, settings):
    install_p1_schema(manager)
    _seed(manager, _full_dataset())
    return manager, settings


@pytest.fixture()
def jmr_governed(manager, settings):
    """治理激活态；基线 seed 在 activate 之前（未激活时 raw INSERT 放行）。"""
    install_p1_schema(manager)
    _seed(
        manager,
        [_row("M-GOV-BASE", "Helios 网关采用 PostgreSQL 主从架构", "SEMANTIC", TS1)],
    )
    gov = build_governance(manager, settings, config=_gov_cfg(), enforce=True)
    principal = TrustedPrincipal.issue(
        settings,
        subject_id="svc:jmr-governed-test",
        principal_type="SERVICE",
        grants=(CAP_MEMORY_READ, CAP_MEMORY_WRITE, CAP_TASK_MANAGE),
    )
    try:
        yield gov, principal, manager, settings
    finally:
        deactivate_governance()


# --------------------------------------------------------------------------
# 注入辅助：只用 monkeypatch 控制外部条件，不给生产代码开测试后门
# --------------------------------------------------------------------------
def _inject_control(monkeypatch, manager, task_id, action):
    """在第一个 CostGovernor.record 阶段之后 pause/cancel（阶段间注入）。

    §9 明确「每阶段后置 record」、阶段前 _check_control() 读状态，因此第一次
    record 之后切换状态，下一阶段必然读到 PAUSED / CANCELLED。
    """
    original = CostGovernor.record
    fired = {"done": False}

    def wrapper(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            registry = TaskRegistry(manager)
            if action == "pause":
                registry.pause(task_id, reason="jmr-test-pause")
            else:
                registry.cancel(task_id, reason="jmr-test-cancel")
        return out

    monkeypatch.setattr(CostGovernor, "record", wrapper)
    return fired


def _inject_detector_failure(monkeypatch):
    """让 detector 抛 RuntimeError；两处 patch 覆盖 from-import / 模块属性两种写法。"""
    import jarvismind.jmr.detectors as det
    import jarvismind.jmr.service as svc

    state = {"n": 0}

    def boom(*args, **kwargs):
        state["n"] += 1
        raise RuntimeError("JMR injected detector failure")

    monkeypatch.setattr(det, "find_duplicate_clusters", boom)
    monkeypatch.setattr(svc, "find_duplicate_clusters", boom, raising=False)
    return state


# ==========================================================================
# 标准 6 · 未授权写入被拒绝（治理激活态）
# ==========================================================================
def test_std6_unauthorized_raw_sql_insert_is_refused(jmr_governed):
    _gov, _principal, manager, _settings = jmr_governed
    before = _table_count(manager, "memory_records")
    with pytest.raises(UnauthorizedWriteError) as caught:
        with manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO memory_records(id, memory_type, content, importance, "
                    "confidence, status, created_at, updated_at) VALUES "
                    "('p1-jmr-bypass','SEMANTIC','bypass attempt',5.0,0.5,'ACTIVE',"
                    "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                )
            )
    assert caught.value.code == "unauthorized_memory_write"
    assert _table_count(manager, "memory_records") == before


def test_std6_unauthorized_core_update_is_refused(jmr_governed):
    _gov, _principal, manager, _settings = jmr_governed
    tbl = table("memory_records", column("id"), column("content"))
    stmt = update(tbl).where(tbl.c.id == "M-GOV-BASE").values(content="hijacked")
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.execute(stmt)
    assert _scalar(
        manager,
        "SELECT content FROM memory_records WHERE id=:i",
        {"i": "M-GOV-BASE"},
    ) == "Helios 网关采用 PostgreSQL 主从架构"


def test_std6_unauthorized_orm_insert_is_refused(jmr_governed):
    _gov, _principal, manager, _settings = jmr_governed
    before = _table_count(manager, "memory_records")
    # pytest.raises 必须包住 session context，否则 commit 会二次抛 PendingRollbackError
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.add(
                MemoryRecord(
                    id="p1-jmr-orm-1",
                    memory_type="SEMANTIC",
                    content="raw orm bypass",
                    importance=5.0,
                    confidence=0.5,
                    status="ACTIVE",
                    created_at=TS1,
                    updated_at=TS1,
                )
            )
            session.flush()
    assert _table_count(manager, "memory_records") == before


# ==========================================================================
# 标准 7 · 授权写入正常工作（完整 TrustedPrincipal + SignedGrant 装配）
# ==========================================================================
def test_std7_authorized_gateway_write_is_active_and_readable_by_jmr(jmr_governed):
    gov, principal, manager, _settings = jmr_governed
    grant = gov.grant_authority.issue(
        principal=principal,
        operation=OP_CREATE,
        object_ref="NEW",
        grants=(CAP_MEMORY_WRITE,),
    )
    record = gov.gateway.create_memory(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="Helios 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
        ),
        principal=principal,
        grant=grant,
        object_ref="NEW",
    )
    assert record.status == "ACTIVE"
    with manager.session() as session:
        assert (
            session.execute(
                text("SELECT status FROM memory_records WHERE id=:i"),
                {"i": record.id},
            ).scalar()
            == "ACTIVE"
        )

    # 治理激活态下 JMR run() 仍能读到该新记忆并产出建议（只读投影走独立 ro 连接）
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD7", run_id="R-STD7")
    assert result.status == "SUCCEEDED"
    assert result.memory_rows >= 2
    assert result.created["MERGE"] >= 1


# ==========================================================================
# 标准 5 · 治理激活态下派生隔离（独立新连接摘要 + 逐字比对）
# ==========================================================================
def test_std5_governed_run_does_not_touch_original_memory(jmr_governed):
    _gov, _principal, manager, _settings = jmr_governed
    before_digest = _content_digest(manager)
    before_rows = _rows(manager, "SELECT content FROM memory_records ORDER BY id")

    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-GOV5", run_id="R-GOV5")
    assert result.status == "SUCCEEDED"
    assert _content_digest(manager) == before_digest
    assert _rows(manager, "SELECT content FROM memory_records ORDER BY id") == before_rows


# ==========================================================================
# 标准 8 · 重复任务幂等
# ==========================================================================
def test_std8_resume_false_returns_existing_result_without_side_effects(jmr_env):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    first = service.run(task_id="T-IDEM", run_id="R-1")
    assert first.status == "SUCCEEDED"
    counts = _five_counts(manager)

    second = service.run(task_id="T-IDEM", run_id="R-1", resume=False)
    assert second.status == "SUCCEEDED"
    # 5 张表前后精确行数不变（直接返回既有结果，不重放副作用）
    assert _five_counts(manager) == counts
    creates = [
        r
        for r in _rows(
            manager,
            "SELECT event_type FROM p1_task_events WHERE task_id=:t",
            {"t": "T-IDEM"},
        )
        if r[0] == "CREATE"
    ]
    assert len(creates) == 1  # 无第二个 CREATE 事件


def test_std8_resume_true_upsert_skips_and_counts_skipped(jmr_env):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    first = service.run(task_id="T-IDEM2", run_id="R-1")
    assert first.status == "SUCCEEDED"
    derived_before = _derived_counts(manager)
    five_before = _five_counts(manager)
    proposal_audit_before = _jmr_audit_count(manager, "P1:JMR_PROPOSAL:SUCCEEDED")

    result = service.run(task_id="T-IDEM2", run_id="R-2", resume=True)
    assert result.status == "SUCCEEDED"
    assert result.skipped_idempotent > 0  # 由真实 upsert 去重产生
    assert _derived_counts(manager) == derived_before
    # Standard 8 literal criterion: all five tables keep their exact row counts.
    assert _five_counts(manager) == five_before
    # 建议审计不重复追加（§10「非 VOID -> 返回 False，不追加重复行/审计」）
    assert _jmr_audit_count(manager, "P1:JMR_PROPOSAL:SUCCEEDED") == proposal_audit_before
    # resume=True 仍走 start()/succeed()（§10），会新增 START/SUCCEED 事件，
    # 故此处判据用「无第二个 CREATE 事件」而非 p1_task_events 总行数不变。
    creates = [
        r
        for r in _rows(
            manager,
            "SELECT event_type FROM p1_task_events WHERE task_id=:t AND event_type='CREATE'",
            {"t": "T-IDEM2"},
        )
    ]
    assert len(creates) == 1


# ==========================================================================
# 标准 9 · 任务暂停 / 恢复 / 取消
# ==========================================================================
def test_std9_pause_then_resume_succeeds(jmr_env, monkeypatch):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    fired = _inject_control(monkeypatch, manager, "T-PAUSE", "pause")

    paused = service.run(task_id="T-PAUSE", run_id="R-PAUSE")
    assert paused.status == "PAUSED"
    assert fired["done"] is True
    derived_paused = _derived_counts(manager)
    # The first pause injection fires after Stage 1 cost recording, before any
    # read/detect/write: a paused run must not leave half-written proposals.
    assert derived_paused == {
        "memory_merges": 0,
        "memory_corrections": 0,
        "memory_summaries": 0,
    }

    # JMR-08 audit P1-6: resuming a RUNNING task requires the explicit control
    # endpoint, which records a one-shot resume marker. A bare registry resume
    # (or a concurrent resume=true) must still conflict.
    service.resume("T-PAUSE")
    monkeypatch.undo()  # 撤销 record 注入，避免恢复后再次 pause
    resumed = service.run(task_id="T-PAUSE", run_id="R-PAUSE-2", resume=True)
    assert resumed.status == "SUCCEEDED"
    # Resuming from a pre-write pause is a normal full run over the fixture
    # dataset: exactly one merge, three corrections and one summary.
    after_resume = _derived_counts(manager)
    assert after_resume == {
        "memory_merges": 1,
        "memory_corrections": 3,
        "memory_summaries": 1,
    }
    # The real "do not write twice" proof is a SECOND resume: deterministic
    # upserts must skip every existing proposal and leave row counts unchanged.
    again = service.run(task_id="T-PAUSE", run_id="R-PAUSE-3", resume=True)
    assert again.status == "SUCCEEDED"
    assert again.skipped_idempotent > 0
    assert _derived_counts(manager) == after_resume


def test_std9_cancel_is_terminal_and_writes_no_new_rows(jmr_env, monkeypatch):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    fired = _inject_control(monkeypatch, manager, "T-CANCEL", "cancel")

    cancelled = service.run(task_id="T-CANCEL", run_id="R-CANCEL")
    assert cancelled.status == "CANCELLED"
    assert fired["done"] is True
    with manager.session() as session:
        assert (
            session.execute(
                text("SELECT status FROM p1_tasks WHERE task_id=:t"),
                {"t": "T-CANCEL"},
            ).scalar()
            == "CANCELLED"
        )
    derived_after_cancel = _derived_counts(manager)

    # 重复 cancel 返回 noop=True，不追加事件
    repeat = TaskRegistry(manager).cancel("T-CANCEL", reason="again")
    assert repeat.noop is True

    monkeypatch.undo()
    again = service.run(task_id="T-CANCEL", run_id="R-CANCEL-2", resume=True)
    assert again.status == "CANCELLED"
    assert _derived_counts(manager) == derived_after_cancel  # 终态不新增派生行


# ==========================================================================
# 标准 10 · 失败恢复
# ==========================================================================
def test_std10_detector_failure_then_recover_resume(jmr_env, monkeypatch):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    state = _inject_detector_failure(monkeypatch)

    failed = service.run(task_id="T-FAIL", run_id="R-FAIL")
    assert failed.status == "FAILED"
    assert failed.failure_reason
    assert state["n"] >= 1
    assert _jmr_audit_count(manager, "P1:JMR_REFLECTION:FAILED") >= 1
    rows_after_failure = _derived_counts(manager)

    monkeypatch.undo()  # detector 恢复正常
    TaskRegistry(manager).recover("T-FAIL")  # FAILED -> CREATED，只重置状态
    resumed = service.run(task_id="T-FAIL", run_id="R-FAIL-2", resume=True)
    assert resumed.status == "SUCCEEDED"
    rows_after_resume = _derived_counts(manager)
    assert sum(rows_after_resume.values()) >= sum(rows_after_failure.values())

    again = service.run(task_id="T-FAIL", run_id="R-FAIL-3", resume=True)
    assert again.status == "SUCCEEDED"
    assert _derived_counts(manager) == rows_after_resume  # 不重复写


# ==========================================================================
# 标准 11 · 成本限制
# ==========================================================================
def test_std11_cost_limit_blocks_run(jmr_env):
    manager, settings = jmr_env
    service = _cost_limited_service(manager, settings)
    digest_before = _content_digest(manager)
    result = service.run(task_id="T-COST", run_id="R-COST")
    assert result.status == "BLOCKED"
    assert result.failure_reason
    assert _content_digest(manager) == digest_before  # 原始记忆全列 digest 不变


def test_std11_blocked_run_writes_failed_audit_and_preserves_digest(jmr_env):
    manager, settings = jmr_env
    service = _cost_limited_service(manager, settings)
    digest_before = _content_digest(manager)
    contents_before = _rows(manager, "SELECT content FROM memory_records ORDER BY id")

    result = service.run(task_id="T-COST2", run_id="R-COST2")
    assert result.status == "BLOCKED"
    failed = [
        e
        for e in AuditBus(manager).p1_entries(limit=1000)
        if e["action"] == "P1:JMR_REFLECTION:FAILED"
    ]
    assert failed
    assert any(
        "cost_governance_blocked" in e["detail"].get("failure_reason", "")
        for e in failed
    )
    assert _content_digest(manager) == digest_before
    assert _rows(manager, "SELECT content FROM memory_records ORDER BY id") == contents_before


# ==========================================================================
# 标准 12 · 审计记录（完整 action 断言，不只前缀）
# ==========================================================================
def test_std12_audit_actions_and_extra_fields_are_complete(jmr_env):
    manager, _ = jmr_env
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-AUDIT", run_id="R-AUDIT")

    entries = AuditBus(manager).p1_entries(limit=1000)
    jmr = [e for e in entries if e["action"].startswith("P1:JMR_")]
    proposals = [e for e in jmr if e["action"] == "P1:JMR_PROPOSAL:SUCCEEDED"]
    reflection = [e for e in jmr if e["action"] == "P1:JMR_REFLECTION:SUCCEEDED"]
    assert len(proposals) == sum(result.created.values())  # 每条建议一条
    assert len(reflection) == 1

    with manager.session() as session:
        proposal_ids = set()
        for t in DERIVED_TABLES:
            for r in session.execute(text(f"SELECT id FROM {t}")):
                proposal_ids.add(r[0])

    for e in proposals:
        d = e["detail"]
        assert d["object_ref"] in proposal_ids  # 与实际 proposal_id 一致
        extra = d.get("extra", {})
        for key in (
            "rule",
            "proposal_id",
            "source_memory_ids",
            "task_id",
            "run_id",
            "algorithm_version",
        ):
            assert key in extra
        assert extra["rule"] in RULES
        assert extra["proposal_id"] == d["object_ref"]

    for e in proposals + reflection:
        d = e["detail"]
        assert d["principal_id"] == "svc:jmr"
        assert d["principal_type"] == "SERVICE"
        assert d["trigger"] == "JMR"
        assert d["task_id"] == "T-AUDIT"
        assert d["run_id"] == "R-AUDIT"


def test_std12_denied_audit_row_exists_for_credential_content(manager):
    """凭证反例：assert_writable 命中 -> 跳过该建议 + DENIED 审计（§11 / 标准 12）。"""
    install_p1_schema(manager)
    _seed(
        manager,
        [
            _row(
                "M-CRED-1",
                "JMR 审计拒绝反例 AKIA" "1234567890ABCDEF 用于验证 DENIED 路径",
                "JMR_CRED",
                TS1,
            ),
            _row(
                "M-CRED-2",
                "JMR 审计拒绝反例 AKIA" "1234567890ABCDEF 副本记录",
                "JMR_CRED",
                TS2,
            ),
        ],
    )
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-DENIED", run_id="R-DENIED")

    denied = [
        e
        for e in AuditBus(manager).p1_entries(limit=1000)
        if e["action"] in ("P1:JMR_PROPOSAL:DENIED", "P1:JMR_REFLECTION:DENIED")
        and e["detail"].get("status") == "DENIED"
    ]
    assert denied  # 不只「无异常」，拒绝行确实存在
    assert all(e["detail"].get("failure_reason") for e in denied)


# ==========================================================================
# 标准 14 · 四入口仍抛 MigrationBlocked
# ==========================================================================
@pytest.mark.real_migration_gate
def test_std14_four_migration_entry_points_still_blocked(manager):
    runner = MigrationRunner(manager)
    with pytest.raises(MigrationBlocked):
        runner.apply_pending()
    with pytest.raises(MigrationBlocked):
        runner.initialize()
    with pytest.raises(MigrationBlocked):
        runner.rollback_v6()
    with pytest.raises(MigrationBlocked):
        initialize_database(manager)
