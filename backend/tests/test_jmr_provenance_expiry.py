"""JMR-04/05/06/07 integration tests on a real isolated SQLite database.

These tests do not mock the database. They exercise TTL, generation, staleness,
same-task concurrency, stage timeout, breaker, cost budget, privacy HOLD and
batch planning through the real service/store.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.schema import install_p1_schema
from jarvismind.governance.task_registry import TaskRegistry
from jarvismind.jmr import JMRConfig, JMRService, build_jmr_service
from jarvismind.jmr.bridge import JMRReviewBridge
from jarvismind.jmr.errors import JMRConcurrencyError, JMRStageTimeoutError
from jarvismind.jmr.query import JMRQueryService
from jarvismind.jmr.store import DerivedStore

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"

_INSERT = (
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, :importance, :confidence, :status, "
    ":created_at, :updated_at, :source_id, :metadata_json, :search_text)"
)


def _row(memory_id, content, memory_type, created_at, updated_at=None):
    return {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "importance": 5.0,
        "confidence": 0.9,
        "status": "ACTIVE",
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "source_id": None,
        "metadata_json": None,
        "search_text": content,
    }


def _seed(manager, rows=None):
    specs = rows or [
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
    ]
    with manager.session() as session:
        for spec in specs:
            session.execute(text(_INSERT), spec)


def _count(manager, table, where=""):
    with manager.session() as session:
        return int(
            session.execute(text(f"SELECT COUNT(*) FROM {table} {where}")).scalar()
            or 0
        )


def _proposal(manager, rule):
    store = DerivedStore(manager)
    rows = store.list_derived()
    for table_key, items in rows.items():
        if table_key == "corrupt":
            continue
        for item in items:
            meta = json.loads(item.get("metadata_json") or "{}")
            if meta.get("rule") == rule:
                return item, meta
    raise AssertionError(f"no {rule} proposal")


@pytest.fixture()
def jmr_db(manager):
    install_p1_schema(manager)
    _seed(manager)
    return manager


def test_provenance_contract_has_all_required_keys(jmr_db):
    service = build_jmr_service(manager=jmr_db)
    result = service.run(task_id="T-PROV", run_id="R-PROV")
    assert result.status == "SUCCEEDED"
    _item, meta = _proposal(jmr_db, "SUMMARY")
    for key in (
        "engine",
        "rule_version",
        "model_provider",
        "model_version",
        "prompt_version",
        "confidence",
        "claim_type",
        "evidence",
        "expires_at",
        "generation",
        "review",
        "sensitivity",
    ):
        assert key in meta, key
    assert meta["engine"] == "local"
    assert meta["model_provider"] == "local"
    assert meta["claim_type"] == "INFERENCE"
    assert meta["generation"] == 1
    assert meta["review"]["status"] == "PENDING"
    assert meta["expires_at"]
    assert meta["evidence"]
    assert all("content" not in item for item in meta["evidence"])


def test_ttl_expiry_voids_only_pending_rows(jmr_db):
    service = build_jmr_service(manager=jmr_db)
    service.run(task_id="T-TTL", run_id="R-TTL")
    item, meta = _proposal(jmr_db, "CONFLICT")
    meta["expires_at"] = "2000-01-01T00:00:00+00:00"
    with jmr_db.session() as session:
        session.execute(
            text("UPDATE memory_corrections SET metadata_json=:m WHERE id=:i"),
            {
                "m": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "i": item["id"],
            },
        )
    assert len(service.list_expired()) == 1
    assert service.expire_due() == 1
    with jmr_db.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_corrections WHERE id=:i"),
            {"i": item["id"]},
        ).scalar()
    assert status == "VOID"
    assert service.list_expired() == []
    assert JMRQueryService(jmr_db).status().expired_pending == 0


def test_generation_increments_when_sources_change_and_stale_write_loses(jmr_db):
    service = build_jmr_service(manager=jmr_db)
    service.run(task_id="T-GEN-1", run_id="R-GEN-1")
    merge_item, old_meta = _proposal(jmr_db, "MERGE")
    proposal_id = merge_item["id"]
    assert old_meta["generation"] == 1
    old_metadata_json = merge_item["metadata_json"]

    with jmr_db.session() as session:
        session.execute(
            text(
                "UPDATE memory_records SET content=:c, updated_at=:u WHERE id=:i"
            ),
            {
                "c": "Bifrost 网关采用 PostgreSQL 主从架构，Q4 计划迁移到 TiDB",
                "u": "2026-10-01T00:00:00+00:00",
                "i": "M-DUP-B",
            },
        )
    service.run(task_id="T-GEN-2", run_id="R-GEN-2")

    updated_item, updated_meta = _proposal(jmr_db, "MERGE")
    assert updated_meta["generation"] == 2
    assert updated_meta["source_watermark"] > old_meta["source_watermark"]

    # Simulate an old in-flight run finishing late with the old watermark.
    store = DerivedStore(jmr_db)
    accepted = store.upsert_merge(
        proposal_id=proposal_id,
        source_memory_ids=old_meta["source_memory_ids"],
        new_content="旧结果不应覆盖新结果",
        new_summary="",
        reason="stale",
        metadata_json=old_metadata_json,
        now="2026-10-02T00:00:00+00:00",
    )
    assert accepted is False
    stored = store.get_derived(proposal_id)
    assert stored is not None
    assert stored["new_content"] != "旧结果不应覆盖新结果"


def test_same_task_concurrent_run_is_rejected(jmr_db):
    service = build_jmr_service(manager=jmr_db)
    registry = TaskRegistry(jmr_db)
    registry.create("T-RUNNING", "JMR_REFLECTION", owner="svc:jmr")
    registry.start("T-RUNNING")
    with pytest.raises(JMRConcurrencyError):
        service.run(task_id="T-RUNNING", run_id="R-RUNNING")
    # JMR-08 audit P1-6: resume=true alone is NOT a concurrency bypass. Only an
    # explicit control-endpoint resume records the one-shot marker.
    with pytest.raises(JMRConcurrencyError):
        service.run(task_id="T-RUNNING", run_id="R-RUNNING-2", resume=True)
    service.resume("T-RUNNING")
    result = service.run(task_id="T-RUNNING", run_id="R-RUNNING-3", resume=True)
    assert result.status == "SUCCEEDED"


def test_stage_timeout_fails_the_task(jmr_db, monkeypatch):
    cfg = JMRConfig(stage_timeout_seconds=1)
    service = JMRService.build(manager=jmr_db, config=cfg)

    def slow_read(_memory_type):
        time.sleep(1.1)
        return []

    monkeypatch.setattr(service, "_read_rows", slow_read)
    result = service.run(task_id="T-TIMEOUT", run_id="R-TIMEOUT")
    assert result.status == "FAILED"
    assert "JMRStageTimeoutError" in result.failure_reason
    with jmr_db.session() as session:
        status = session.execute(
            text("SELECT status FROM p1_tasks WHERE task_id='T-TIMEOUT'")
        ).scalar()
    assert status == "FAILED"


def test_breaker_trips_after_consecutive_failures_and_needs_manual_reset(
    jmr_db, monkeypatch
):
    cfg = JMRConfig(breaker_threshold=2)
    service = JMRService.build(manager=jmr_db, config=cfg)

    def boom(records):
        raise RuntimeError("injected detector failure")

    monkeypatch.setattr(service, "_detect", boom)
    first = service.run(task_id="T-BRK-1", run_id="R-BRK-1")
    second = service.run(task_id="T-BRK-2", run_id="R-BRK-2")
    assert first.status == "FAILED" and second.status == "FAILED"
    third = service.run(task_id="T-BRK-3", run_id="R-BRK-3")
    assert third.status == "BLOCKED"
    assert "breaker_tripped" in third.failure_reason

    reset = service.reset_breaker()
    assert reset["tripped"] is False
    healthy = JMRService.build(manager=jmr_db, config=cfg)
    result = healthy.run(task_id="T-BRK-4", run_id="R-BRK-4")
    assert result.status == "SUCCEEDED"


def test_daily_budget_blocks_before_any_derived_write(jmr_db):
    cfg = JMRConfig(daily_budget_cny=0.01, expected_cost_cny=0.02)
    service = JMRService.build(manager=jmr_db, config=cfg)
    result = service.run(task_id="T-BUDGET", run_id="R-BUDGET")
    assert result.status == "BLOCKED"
    assert "cost_governance_blocked" in result.failure_reason
    assert _count(jmr_db, "memory_merges") == 0
    assert _count(jmr_db, "memory_corrections") == 0
    assert _count(jmr_db, "memory_summaries") == 0


def test_pii_is_held_and_credential_is_denied(jmr_db):
    # Replace the dataset with one PII-only row.
    with jmr_db.session() as session:
        session.execute(text("DELETE FROM memory_records"))
    _seed(
        jmr_db,
        [_row("M-PII", "我的手机号是 13800138000，请记住", "JMR_PII", TS1)],
    )
    service = build_jmr_service(manager=jmr_db)
    result = service.run(task_id="T-PII", run_id="R-PII")
    assert result.status == "SUCCEEDED"
    assert _count(jmr_db, "memory_summaries") == 0
    denied = [
        e
        for e in AuditBus(jmr_db).p1_entries(limit=1000)
        if e["action"] == "P1:JMR_REFLECTION:DENIED"
    ]
    assert denied
    assert any("privacy_hold" in e["detail"].get("failure_reason", "") for e in denied)


def test_tampered_source_id_is_denied_with_audit_row(jmr_db, monkeypatch):
    """Standard 4: an out-of-window source id must produce a DENIED audit row."""
    from jarvismind.jmr.detectors import DuplicateCluster

    service = build_jmr_service(manager=jmr_db)

    def tampered_detect(records):
        cluster = DuplicateCluster(
            memory_ids=("M-DUP-A", "NOT-IN-WINDOW"),
            reason="tampered",
            confidence=0.99,
        )
        return (cluster,), (), (), (), ("t", "c", 0.5)

    monkeypatch.setattr(service, "_detect", tampered_detect)
    result = service.run(task_id="T-TAMPER", run_id="R-TAMPER")
    assert result.status == "SUCCEEDED"
    assert result.created["MERGE"] == 0
    denied = [
        e
        for e in AuditBus(jmr_db).p1_entries(limit=1000)
        if e["action"] == "P1:JMR_REFLECTION:DENIED"
    ]
    assert denied
    assert any(
        "provenance_invalid" in e["detail"].get("failure_reason", "")
        for e in denied
    )


def test_batch_planning_is_deterministic(jmr_db):
    service = build_jmr_service(manager=jmr_db)
    batches = service.plan_batches(["a", "b", "c", "d", "e"], batch_size=2)
    assert len(batches) == 3
    assert len({batch["batch_id"] for batch in batches}) == 3
    assert batches[0]["task_ids"] == ["a", "b"]
    assert batches[1]["task_ids"] == ["c", "d"]
    assert batches[2]["task_ids"] == ["e"]
    again = service.plan_batches(["a", "b", "c", "d", "e"], batch_size=2)
    assert [b["batch_id"] for b in again] == [b["batch_id"] for b in batches]
