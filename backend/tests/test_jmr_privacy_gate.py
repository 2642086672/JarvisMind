"""JMR-07 privacy gate tests at the service, scheduler and cost boundaries.

The privacy engine (``app.jmr.sensitive``) is pure and already unit-tested.
These tests pin the integration: no PII or credential may become a derived
proposal through ANY entry point — direct service run, retry, resume, the
cost-governed wrapper, or the scheduler. Audit detail must not carry the raw
value, and exception text must not either.

Fail-closed contract under test:
  * CREDENTIAL -> DENY
  * PII (phone / id card / bank card / email) -> HOLD
  * derived tables (merges / corrections / summaries) receive ZERO rows
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jmr.config import JMRConfig
from jarvismind.jmr.cost_governance import CostGovernance, CostGovernanceConfig, CostGovernedJMRService
from jarvismind.jmr.scheduler_engine import SchedulerConfig, SchedulerEngine
from jarvismind.jmr.sensitive import safe_error_message
from jarvismind.jmr.service import JMRService

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, 'FACT', :content, 5.0, 0.9, 'ACTIVE', "
    ":now, :now, NULL, NULL, :content)"
)

PHONE = "13800138000"
ID_CARD = "110101199003072316"
API_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
PASSWORD = "MyS3cretPassw0rd!"


@pytest.fixture()
def p1_manager(manager):
    install_p1_schema(manager)
    return manager


def _seed(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(
            _INSERT,
            {"id": memory_id, "content": content, "now": utc_now_iso()},
        )


def _derived_count(manager) -> int:
    with manager.session() as session:
        return int(
            session.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM memory_merges) + "
                    "(SELECT COUNT(*) FROM memory_corrections) + "
                    "(SELECT COUNT(*) FROM memory_summaries)"
                )
            ).scalar()
            or 0
        )


def _audit_text(manager) -> str:
    rows = AuditBus(manager).p1_entries(limit=2000)
    return json.dumps(rows, ensure_ascii=False)


def _denied(manager) -> list:
    return [
        row
        for row in AuditBus(manager).p1_entries(limit=2000)
        if row["action"].endswith(":DENIED")
    ]


def _service(manager) -> JMRService:
    return JMRService.build(manager=manager, config=JMRConfig())


def _duplicate_pair(manager, marker: str) -> None:
    """Two near-identical memories so JMR emits a duplicate proposal."""
    _seed(manager, f"M-{marker}-1", f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}")
    _seed(
        manager,
        f"M-{marker}-2",
        f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}，计划 Q3 迁移",
    )


def _pii_pair(manager, marker: str, secret: str) -> None:
    _seed(manager, f"M-{marker}-1", f"我的手机号是 {secret}，请记住 {marker}")
    _seed(manager, f"M-{marker}-2", f"我的手机号是 {secret}，请记住 {marker}，常用")


def test_phone_number_is_held_and_writes_nothing(p1_manager):
    _pii_pair(p1_manager, "PHONE", PHONE)
    result = _service(p1_manager).run(task_id="T-PII-PHONE")
    assert result.status == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0
    denied = _denied(p1_manager)
    assert denied
    assert all("privacy_hold" in (r["detail"].get("failure_reason") or "") for r in denied)


def test_id_card_is_held_and_writes_nothing(p1_manager):
    _pii_pair(p1_manager, "IDCARD", ID_CARD)
    _service(p1_manager).run(task_id="T-PII-IDCARD")
    assert _derived_count(p1_manager) == 0
    assert any("privacy_hold" in (r["detail"].get("failure_reason") or "") for r in _denied(p1_manager))


def test_api_key_is_denied_and_never_persisted(p1_manager):
    _seed(p1_manager, "M-KEY-1", f"服务配置 api key: {API_KEY}")
    _seed(p1_manager, "M-KEY-2", f"服务配置 api key: {API_KEY} 备用")
    _service(p1_manager).run(task_id="T-PII-KEY")
    assert _derived_count(p1_manager) == 0
    denied = _denied(p1_manager)
    assert denied
    assert any("privacy_deny" in (r["detail"].get("failure_reason") or "") for r in denied)
    assert API_KEY not in _audit_text(p1_manager)


def test_password_is_denied_and_never_persisted(p1_manager):
    _seed(p1_manager, "M-PWD-1", f"数据库密码是 {PASSWORD}")
    _seed(p1_manager, "M-PWD-2", f"数据库密码是 {PASSWORD}，请保存")
    _service(p1_manager).run(task_id="T-PII-PWD")
    assert _derived_count(p1_manager) == 0
    assert PASSWORD not in _audit_text(p1_manager)


def test_audit_carries_categories_not_raw_values(p1_manager):
    _pii_pair(p1_manager, "AUDIT", PHONE)
    _service(p1_manager).run(task_id="T-AUDIT")
    text_blob = _audit_text(p1_manager)
    assert PHONE not in text_blob
    assert "phone_cn" in text_blob or "pii_categories" in text_blob


def test_privacy_denial_is_not_bypassed_by_cost_allowance(p1_manager):
    _pii_pair(p1_manager, "COST", PHONE)
    service = _service(p1_manager)
    gov = CostGovernance(p1_manager, CostGovernanceConfig(daily_budget_cny=100.0))
    wrapper = CostGovernedJMRService(service, gov)
    result = wrapper.run(task_id="T-PII-COST", run_id="R-PII-COST")
    assert result.status == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0
    # The cost layer did admit the run; privacy still refused every proposal.
    assert any(
        "JMR_COST_SETTLE" in row["action"]
        for row in AuditBus(p1_manager).p1_entries(limit=2000)
    )


def test_retry_after_privacy_hold_writes_nothing(p1_manager):
    _pii_pair(p1_manager, "RETRY", PHONE)
    service = _service(p1_manager)
    first = service.run(task_id="T-PII-RETRY")
    second = service.run(task_id="T-PII-RETRY", resume=True)
    assert first.status == "SUCCEEDED"
    assert second.status == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0


def test_resume_of_failed_task_after_privacy_hold_writes_nothing(p1_manager):
    _pii_pair(p1_manager, "RESUME", PHONE)
    service = _service(p1_manager)
    registry = service.task_registry
    registry.create("T-PII-RESUME", "JMR_REFLECTION", owner="test")
    registry.start("T-PII-RESUME")
    registry.fail("T-PII-RESUME", reason="simulated infrastructure failure")
    result = service.run(task_id="T-PII-RESUME", resume=True)
    assert result.status == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0
    assert any(
        "privacy_hold" in (r["detail"].get("failure_reason") or "")
        for r in _denied(p1_manager)
    )


def test_scheduler_run_denies_privacy_without_derived_rows(p1_manager):
    _pii_pair(p1_manager, "SCHED", PHONE)
    engine = SchedulerEngine(
        p1_manager,
        service=_service(p1_manager),
        config=SchedulerConfig(
            max_tasks_per_tick=2,
            batch_size=2,
            max_runtime_seconds=30,
            default_timeout_seconds=30,
        ),
        clock=lambda: datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )
    engine.register_task("T-PII-SCHED", now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
    engine.tick(now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
    assert _derived_count(p1_manager) == 0
    assert any(
        "privacy_hold" in (r["detail"].get("failure_reason") or "")
        for r in _denied(p1_manager)
    )


def test_non_sensitive_duplicate_still_proceeds(p1_manager):
    """The gate must not turn into a blanket refusal."""
    _duplicate_pair(p1_manager, "CLEAN")
    result = _service(p1_manager).run(task_id="T-CLEAN")
    assert result.status == "SUCCEEDED"
    assert _derived_count(p1_manager) > 0


def test_exception_message_does_not_leak_pii():
    exc = RuntimeError(f"failed while storing phone {PHONE} and key {API_KEY}")
    message = safe_error_message(exc)
    assert PHONE not in message
    assert API_KEY not in message
