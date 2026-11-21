"""Regression tests for the JMR-00~08 independent audit findings.

Each test pins one audit item so it cannot silently regress:

C-1 credential/PII in review fields, C-2 credential in memory_type/control
reason, C-3 raw exception text persisted, P1-1 legacy PII approval, P1-3
settlement retention, P1-4 env precedence, P1-5 period-spend double counting,
P1-7 scheduler host surface, P1-8 forget invalidation, P1-10 auto-execute audit.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from jarvismind.container import Container
from jarvismind.core.security_gate import CredentialBlockedError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jmr.config import JMRConfig
from jarvismind.jmr.cost_governance import (
    CostGovernance,
    CostGovernanceConfig,
    CostGovernedJMRService,
)
from jarvismind.jmr.errors import JMRPrivacyBlockedError
from jarvismind.jmr.service import JMRService

PHONE = "13800138000"
SECRET = "api_key=abcd1234efgh5678"

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, 'FACT', :content, 5.0, 0.9, 'ACTIVE', "
    ":now, :now, NULL, NULL, :content)"
)


@pytest.fixture()
def p1_manager(manager):
    install_p1_schema(manager)
    return manager


def _seed(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(_INSERT, {"id": memory_id, "content": content, "now": utc_now_iso()})


def _pair(manager, marker: str) -> None:
    _seed(manager, f"M-{marker}-1", f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}")
    _seed(
        manager,
        f"M-{marker}-2",
        f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}，计划 Q3 迁移",
    )


def _service(manager) -> JMRService:
    return JMRService.build(manager=manager, config=JMRConfig())


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


def _audit_blob(manager) -> str:
    return json.dumps(AuditBus(manager).p1_entries(limit=2000), ensure_ascii=False)


def _bridge(manager, service):
    from jarvismind.jmr.bridge import JMRReviewBridge
    from jarvismind.jmr.query import JMRQueryService
    from jarvismind.services.lifecycle_service import LifecycleService

    return JMRReviewBridge(
        manager=manager,
        query=JMRQueryService(manager),
        lifecycle=LifecycleService(manager),
        audit_bus=service.audit_bus,
        projection=service.projection,
    )


def _first_proposal(manager) -> str:
    with manager.session() as session:
        for table, _ in (("memory_summaries", "SUMMARY"), ("memory_corrections", "CORRECTION"), ("memory_merges", "MERGE")):
            row = session.execute(text(f"SELECT id FROM {table} LIMIT 1")).scalar()
            if row:
                return str(row)
    raise AssertionError("no proposal created")


# --------------------------------------------------------------------- C-1
def test_review_reason_with_credential_is_refused(p1_manager):
    _pair(p1_manager, "R1")
    service = _service(p1_manager)
    service.run(task_id="T-R1")
    proposal_id = _first_proposal(p1_manager)
    bridge = _bridge(p1_manager, service)
    with pytest.raises(CredentialBlockedError):
        bridge.review(proposal_id, decision="approve", actor="user", reason=SECRET)
    assert SECRET not in _audit_blob(p1_manager)


def test_review_reason_with_pii_is_refused(p1_manager):
    _pair(p1_manager, "R2")
    service = _service(p1_manager)
    service.run(task_id="T-R2")
    proposal_id = _first_proposal(p1_manager)
    with pytest.raises(JMRPrivacyBlockedError):
        _bridge(p1_manager, service).review(
            proposal_id, decision="approve", actor="user", reason=f"call me {PHONE}"
        )
    assert PHONE not in _audit_blob(p1_manager)


def test_review_corrected_content_with_credential_is_refused(p1_manager):
    _pair(p1_manager, "R3")
    service = _service(p1_manager)
    service.run(task_id="T-R3")
    proposal_id = _first_proposal(p1_manager)
    with pytest.raises(CredentialBlockedError):
        _bridge(p1_manager, service).review(
            proposal_id, decision="approve", actor="user", corrected_content=SECRET
        )
    with p1_manager.session() as session:
        blob = json.dumps(
            session.execute(
                text("SELECT reason FROM memory_corrections")
            ).scalars().all(),
            ensure_ascii=False,
        )
    assert "abcd1234efgh5678" not in blob


# --------------------------------------------------------------------- C-2
def test_credential_memory_type_is_refused_before_task_creation(p1_manager):
    _pair(p1_manager, "M2")
    service = _service(p1_manager)
    with pytest.raises(CredentialBlockedError):
        service.run(task_id="T-M2", memory_type=SECRET)
    assert service.task_registry.get("T-M2") is None
    assert SECRET not in _audit_blob(p1_manager)
    with p1_manager.session() as session:
        stored = session.execute(
            text("SELECT COUNT(*) FROM p1_tasks WHERE metadata_json LIKE '%abcd1234efgh5678%'")
        ).scalar()
    assert stored == 0


def test_credential_pause_reason_is_refused(p1_manager):
    _pair(p1_manager, "P2")
    service = _service(p1_manager)
    service.task_registry.create("T-P2", "JMR_REFLECTION", owner="test")
    with pytest.raises(CredentialBlockedError):
        service.pause("T-P2", reason=SECRET)
    with p1_manager.session() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM p1_task_events WHERE detail_json LIKE '%abcd1234efgh5678%'")
        ).scalar()
    assert count == 0


# --------------------------------------------------------------------- C-3
def test_exception_text_is_redacted_everywhere(p1_manager, monkeypatch):
    _pair(p1_manager, "E3")
    service = _service(p1_manager)

    def boom(_records):
        raise RuntimeError("downstream failed: password=Hunter2SecretValue")

    monkeypatch.setattr(service, "_detect", boom)
    result = service.run(task_id="T-E3")
    assert result.status == "FAILED"
    assert "Hunter2SecretValue" not in result.failure_reason
    task = service.task_registry.get("T-E3")
    assert "Hunter2SecretValue" not in (task.get("failure_reason") or "")
    assert "Hunter2SecretValue" not in _audit_blob(p1_manager)


# --------------------------------------------------------------------- P1-1
def test_legacy_pii_proposal_cannot_be_approved(p1_manager):
    _pair(p1_manager, "L1")
    service = _service(p1_manager)
    service.run(task_id="T-L1")
    proposal_id = _first_proposal(p1_manager)
    with p1_manager.session() as session:
        session.execute(
            text("UPDATE memory_summaries SET content=:c WHERE id=:i"),
            {"c": f"我的手机号是 {PHONE}", "i": proposal_id},
        )
        before = session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar()
    with pytest.raises(JMRPrivacyBlockedError):
        _bridge(p1_manager, service).review(proposal_id, decision="approve", actor="user")
    with p1_manager.session() as session:
        after = session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar()
    assert after == before
    assert PHONE not in _audit_blob(p1_manager)


# --------------------------------------------------------------------- P1-3
def test_settlement_after_ttl_records_actual_spend(p1_manager):
    from tests.test_jmr_cost_governance import MutableClock

    clock = MutableClock(datetime(2026, 5, 1, tzinfo=timezone.utc))
    config = CostGovernanceConfig(
        daily_budget_cny=1.0, reservation_ttl_seconds=1, manage_period_spend=False
    )
    gov = CostGovernance(p1_manager, config, clock=clock)
    decision = gov.reserve_run(task_id="T-TTL", run_id="R-TTL", expected_cost_cny=0.2)
    clock.advance(60)
    settlement = gov.settle_run(decision.reservation_id, actual_cost_cny=0.2)
    assert settlement.settled is True
    assert settlement.actual_cny == "0.200000"


def test_settlement_failure_keeps_reservation(p1_manager, monkeypatch):
    from tests.test_jmr_cost_governance import FakeInner, _gov

    _pair(p1_manager, "SF")
    inner = FakeInner(p1_manager)
    gov = _gov(p1_manager, daily_budget_cny=1.0)

    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(gov, "settle_run", boom)
    wrapper = CostGovernedJMRService(inner, gov)
    result = wrapper.run(task_id="T-SF", run_id="R-SF")
    assert result.status == "SUCCEEDED"
    # The run consumed spend; the reservation must not be dropped.
    assert len(gov.active_reservations()) == 1


# --------------------------------------------------------------------- P1-4
def test_env_budget_overrides_config_defaults(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("JARVISMIND_JMR_COST_DAILY_BUDGET_CNY", "5")
    monkeypatch.setenv("JARVISMIND_JMR_COST_EXPECTED_COST_CNY", "0.2")
    monkeypatch.setenv("JARVISMIND_JMR_COST_MAX_SINGLE_CALL_CNY", "1")
    cfg = CostGovernanceConfig.from_sources(
        SimpleNamespace(llm_budget_cny=0.0, llm_price_in=0.0, llm_price_out=0.0),
        jmr_config=JMRConfig(),
    )
    assert cfg.daily_budget_cny == 5.0
    assert cfg.expected_cost_cny == 0.2
    assert cfg.max_single_call_cny == 1.0


# --------------------------------------------------------------------- P1-5
def test_service_period_spend_is_not_double_counted(p1_manager):
    _pair(p1_manager, "D5")
    service = JMRService.build(
        manager=p1_manager,
        config=JMRConfig(expected_cost_cny=0.02, model_calls_per_stage=1),
    )
    gov = CostGovernance(p1_manager, CostGovernanceConfig(daily_budget_cny=10.0))
    wrapper = CostGovernedJMRService(service, gov)
    result = wrapper.run(task_id="T-D5", run_id="R-D5")
    assert result.status == "SUCCEEDED"
    day = datetime.now(timezone.utc).date().isoformat()
    with p1_manager.session() as session:
        daily = session.execute(
            text("SELECT value FROM system_settings WHERE key=:k"),
            {"k": "p1.cost.daily." + day},
        ).scalar()
    recorded = Decimal(str(json.loads(daily)))
    # Three stages at 0.02 each. The wrapper must not add the run cost again.
    assert recorded == Decimal("0.06"), recorded


# --------------------------------------------------------------------- P1-7
def test_scheduler_endpoints_require_token_and_tick(client, auth_headers, p1_manager):
    from jarvismind.main import create_app  # noqa: F401  (route registration sanity)

    assert client.get("/api/v1/jmr/scheduler").status_code == 401
    assert client.post("/api/v1/jmr/scheduler/tick").status_code == 401

    status = client.get("/api/v1/jmr/scheduler", headers=auth_headers)
    assert status.status_code == 200, status.text
    assert "stats" in status.json()["data"]
    tick = client.post("/api/v1/jmr/scheduler/tick", headers=auth_headers)
    assert tick.status_code == 200, tick.text
    assert "executed" in tick.json()["data"]


def test_container_exposes_scheduler_on_the_governed_service(p1_manager, settings):
    container = Container(settings=settings, manager=p1_manager, logger=None)
    engine = container.jmr_scheduler
    assert isinstance(engine.service, CostGovernedJMRService)


# --------------------------------------------------------------------- P1-8
def test_forget_voids_derived_rows(p1_manager):
    _pair(p1_manager, "F8")
    service = _service(p1_manager)
    service.run(task_id="T-F8")
    with p1_manager.session() as session:
        before = session.execute(
            text("SELECT COUNT(*) FROM memory_summaries WHERE status != 'VOID'")
        ).scalar()
    assert before > 0
    service.store.void_by_source_memory("M-F8-1")
    with p1_manager.session() as session:
        remaining = session.execute(
            text(
                "SELECT COUNT(*) FROM memory_summaries "
                "WHERE status != 'VOID' AND metadata_json LIKE '%M-F8-1%'"
            )
        ).scalar()
        total = session.execute(
            text("SELECT COUNT(*) FROM memory_summaries WHERE status != 'VOID'")
        ).scalar()
    assert remaining == 0
    assert total < before


# --------------------------------------------------------------------- P1-10
def test_auto_execute_toggle_is_audited(client, auth_headers):
    enabled = client.post(
        "/api/v1/jmr/auto-execute/enable",
        json={"acknowledge_risk": True, "rules": ["MERGE"]},
        headers=auth_headers,
    )
    assert enabled.status_code == 200, enabled.text
    disabled = client.post(
        "/api/v1/jmr/auto-execute/disable", headers=auth_headers
    )
    assert disabled.status_code == 200, disabled.text
    actions = [row["action"] for row in AuditBus(client.app.state.container.manager).p1_entries(limit=500)]
    assert any("JMR_AUTO_EXECUTE_ENABLE" in a for a in actions)
    assert any("JMR_AUTO_EXECUTE_DISABLE" in a for a in actions)


# ------------------------------------------------- re-audit (2nd round) fixes
def test_scheduler_register_refuses_credential_memory_type(p1_manager):
    from jarvismind.jmr.scheduler_engine import SchedulerConfig, SchedulerEngine

    engine = SchedulerEngine(p1_manager, service=_service(p1_manager), config=SchedulerConfig())
    with pytest.raises(CredentialBlockedError):
        engine.register_task("T-SCHED-K", memory_type=SECRET)
    with p1_manager.session() as session:
        rows = session.execute(
            text("SELECT COUNT(*) FROM p1_tasks WHERE metadata_json LIKE '%abcd1234efgh5678%'")
        ).scalar()
        settings = session.execute(
            text("SELECT COUNT(*) FROM system_settings WHERE value LIKE '%abcd1234efgh5678%'")
        ).scalar()
    assert rows == 0
    assert settings == 0


def test_title_pii_cannot_be_approved(p1_manager):
    _pair(p1_manager, "T2")
    service = _service(p1_manager)
    service.run(task_id="T-T2")
    proposal_id = _first_proposal(p1_manager)
    with p1_manager.session() as session:
        session.execute(
            text("UPDATE memory_summaries SET content='clean body', title=:t WHERE id=:i"),
            {"t": f"Gateway plan {PHONE}", "i": proposal_id},
        )
        before = session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar()
    with pytest.raises(JMRPrivacyBlockedError):
        _bridge(p1_manager, service).review(proposal_id, decision="approve", actor="user")
    with p1_manager.session() as session:
        after = session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar()
    assert after == before
    assert PHONE not in _audit_blob(p1_manager)


def test_actor_with_pii_is_refused_and_not_audited(p1_manager):
    _pair(p1_manager, "A2")
    service = _service(p1_manager)
    service.run(task_id="T-A2")
    proposal_id = _first_proposal(p1_manager)
    with pytest.raises(JMRPrivacyBlockedError):
        _bridge(p1_manager, service).review(
            proposal_id, decision="approve", actor=f"user {PHONE}"
        )
    assert PHONE not in _audit_blob(p1_manager)


def test_jmr_config_budget_env_value_is_honoured(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv("JARVISMIND_JMR_COST_DAILY_BUDGET_CNY", raising=False)
    monkeypatch.setenv("JARVISMIND_JMR_DAILY_BUDGET_CNY", "5")
    cfg = CostGovernanceConfig.from_sources(
        SimpleNamespace(llm_budget_cny=0.0, llm_price_in=0.0, llm_price_out=0.0),
        jmr_config=JMRConfig(),
    )
    assert cfg.daily_budget_cny == 5.0


def test_resume_marker_consumed_atomically(p1_manager):
    service = _service(p1_manager)
    service.task_registry.create("T-MARK", "JMR_REFLECTION", owner="test")
    service.resume("T-MARK")
    assert service._consume_resume_marker("T-MARK") is True
    assert service._consume_resume_marker("T-MARK") is False


def test_forget_invalidation_uses_the_open_transaction(p1_manager, monkeypatch):
    """Re-audit N-C1: a second session inside the approval transaction deadlocks."""
    from jarvismind.jmr.store import DerivedStore

    _pair(p1_manager, "TX")
    _service(p1_manager).run(task_id="T-TX")
    opened = {"n": 0}
    real_session = p1_manager.session

    def counting_session():
        opened["n"] += 1
        return real_session()

    monkeypatch.setattr(p1_manager, "session", counting_session)
    with p1_manager.session() as outer:
        changed = DerivedStore(p1_manager).void_by_source_memory(
            "M-TX-1", session=outer
        )
        assert changed > 0
        outer.commit()
    # Only the caller's session was opened inside the helper's own call path.
    assert opened["n"] == 1


def test_longer_digit_runs_are_not_treated_as_phone():
    """Re-audit NEW-P3: exercise the tolerant pass, not the exact rules."""
    from jarvismind.core.sensitive import tolerant_categories

    assert "phone_sep" not in tolerant_categories("order 1380013800012345")
    assert "phone_sep" in tolerant_categories("call 13800138000 now")


def test_auto_execute_enable_reverts_when_audit_fails(app, auth_headers, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvismind.governance.audit_bus import AuditBus

    def boom(self, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditBus, "record", boom)
    with TestClient(app, raise_server_exceptions=False) as tolerant:
        response = tolerant.post(
            "/api/v1/jmr/auto-execute/enable",
            json={"acknowledge_risk": True, "rules": ["MERGE"]},
            headers=auth_headers,
        )
        assert response.status_code >= 500
        state = tolerant.get("/api/v1/jmr/auto-execute", headers=auth_headers)
        assert state.json()["data"]["enabled"] is False


def test_real_forget_flow_with_derived_rows_succeeds(settings, p1_manager, auth_headers):
    """Re-audit N-C1: forget must not deadlock or 503 when derived rows exist."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from jarvismind.main import create_app

    enabled = replace(settings, direct_write_enabled=True)
    with TestClient(create_app(enabled, manager=p1_manager), raise_server_exceptions=False) as c:
        ids = []
        for content in (
            "Bifrost 网关采用 PostgreSQL 主从架构",
            "Bifrost 网关采用 PostgreSQL 主从架构，计划迁移",
        ):
            resp = c.post(
                "/api/v1/memories",
                json={"memory_type": "SEMANTIC", "content": content},
                headers=auth_headers,
            )
            assert resp.status_code == 201, resp.text
            ids.append(resp.json()["data"]["id"])

        run = c.app.state.container.jmr_service.run(
            task_id="T-FORGET", run_id="R-FORGET"
        )
        assert run.status == "SUCCEEDED"
        assert _derived_count(p1_manager) > 0

        request = c.post(
            f"/api/v1/memories/{ids[0]}/forget-requests",
            json={"reason": "user asked"},
            headers=auth_headers,
        ).json()["data"]
        approved = c.post(
            f"/api/v1/forget-requests/{request['id']}/approve",
            json={"confirmation_token": request["confirmation_token"]},
            headers=auth_headers,
        )
        assert approved.status_code == 200, approved.text

    with p1_manager.session() as session:
        voided = session.execute(
            text(
                "SELECT COUNT(*) FROM memory_summaries "
                "WHERE status='VOID' AND metadata_json LIKE :p"
            ),
            {"p": f"%{ids[0]}%"},
        ).scalar()
    assert voided > 0


# ------------------------------------------- NEW-P1/P2 (round 3) regression
def test_p1_gateway_refuses_credential_task_metadata_and_reason(
    manager, settings, auth_headers
):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from jarvismind.governance import deactivate_governance
    from jarvismind.main import create_app

    enabled = replace(settings, p1_governance_enabled=True)
    try:
        with TestClient(create_app(enabled, manager=manager)) as gw:
            created = gw.post(
                "/api/v1/p1/tasks",
                json={
                    "task_id": "T-GW-1",
                    "task_type": "JMR_REFLECTION",
                    "metadata": {"note": SECRET},
                },
                headers=auth_headers,
            )
            assert created.status_code >= 400, created.text
            ok = gw.post(
                "/api/v1/p1/tasks",
                json={"task_id": "T-GW-2", "task_type": "JMR_REFLECTION"},
                headers=auth_headers,
            )
            assert ok.status_code == 200, ok.text
            paused = gw.post(
                "/api/v1/p1/tasks/T-GW-2/pause",
                json={"reason": SECRET},
                headers=auth_headers,
            )
            assert paused.status_code >= 400, paused.text
            cancelled = gw.post(
                "/api/v1/p1/tasks/T-GW-2/cancel",
                json={"reason": SECRET},
                headers=auth_headers,
            )
            assert cancelled.status_code >= 400, cancelled.text
    finally:
        deactivate_governance()


def test_scheduler_siblings_refuse_credentials(p1_manager):
    from jarvismind.jmr.scheduler_engine import SchedulerConfig, SchedulerEngine

    engine = SchedulerEngine(
        p1_manager, service=_service(p1_manager), config=SchedulerConfig()
    )
    with pytest.raises(CredentialBlockedError):
        engine.register_schedule("S-BAD", interval_seconds=60, memory_type=SECRET)
    with pytest.raises(CredentialBlockedError):
        engine.register_schedule("S-BAD2", interval_seconds=60, metadata={"note": SECRET})
    engine.register_task("T-SIB", now=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
    with pytest.raises(CredentialBlockedError):
        engine.pause_task("T-SIB", reason=SECRET)
    with pytest.raises(CredentialBlockedError):
        engine.cancel_task("T-SIB", reason=SECRET)
    with pytest.raises(CredentialBlockedError):
        engine.delete_task("T-SIB", reason=SECRET)
    with p1_manager.session() as session:
        settings_hits = session.execute(
            text(
                "SELECT COUNT(*) FROM system_settings "
                "WHERE value LIKE '%abcd1234efgh5678%'"
            )
        ).scalar()
        event_hits = session.execute(
            text(
                "SELECT COUNT(*) FROM p1_task_events "
                "WHERE detail_json LIKE '%abcd1234efgh5678%'"
            )
        ).scalar()
    assert settings_hits == 0
    assert event_hits == 0


def test_p1_gateway_refuses_credential_task_type_and_run_id(
    manager, settings, auth_headers
):
    """Round-4 NEW-P1-residual / NEW-P4: task_type, run_id and the memory
    endpoint's task_id/run_id must not persist credentials."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from jarvismind.governance import deactivate_governance
    from jarvismind.main import create_app

    enabled = replace(settings, p1_governance_enabled=True)
    try:
        with TestClient(create_app(enabled, manager=manager)) as gw:
            bad_type = gw.post(
                "/api/v1/p1/tasks",
                json={"task_id": "T-R4-A", "task_type": SECRET},
                headers=auth_headers,
            )
            assert bad_type.status_code >= 400, bad_type.text
            bad_run = gw.post(
                "/api/v1/p1/tasks",
                json={"task_id": "T-R4-B", "run_id": SECRET},
                headers=auth_headers,
            )
            assert bad_run.status_code >= 400, bad_run.text
            bad_memory = gw.post(
                "/api/v1/p1/memories",
                json={
                    "task_id": SECRET,
                    "memory_type": "SEMANTIC",
                    "content": "clean content",
                },
                headers=auth_headers,
            )
            assert bad_memory.status_code >= 400, bad_memory.text
    finally:
        deactivate_governance()
    with manager.session() as session:
        secret = "abcd1234efgh5678"
        hits = session.execute(
            text(
                "SELECT COUNT(*) FROM p1_tasks "
                "WHERE task_type LIKE :p OR metadata_json LIKE :p "
                "OR task_id LIKE :p OR pause_reason LIKE :p "
                "OR cancel_reason LIKE :p OR failure_reason LIKE :p"
            ),
            {"p": f"%{secret}%"},
        ).scalar()
        events = session.execute(
            text(
                "SELECT COUNT(*) FROM p1_task_events "
                "WHERE detail_json LIKE :p OR task_id LIKE :p OR run_id LIKE :p"
            ),
            {"p": f"%{secret}%"},
        ).scalar()
        audit = session.execute(
            text("SELECT COUNT(*) FROM memory_audit_logs WHERE detail_json LIKE :p"),
            {"p": f"%{secret}%"},
        ).scalar()
    assert hits == 0
    assert events == 0
    assert audit == 0


def test_p1_gateway_rejects_non_string_reason(manager, settings, auth_headers):
    """Re-audit NEW-R5-1: a non-string reason must be 422, never 500."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from jarvismind.governance import deactivate_governance
    from jarvismind.main import create_app

    enabled = replace(settings, p1_governance_enabled=True)
    try:
        with TestClient(create_app(enabled, manager=manager)) as gw:
            created = gw.post(
                "/api/v1/p1/tasks",
                json={"task_id": "T-R5-1", "task_type": "JMR_REFLECTION"},
                headers=auth_headers,
            )
            assert created.status_code == 200, created.text
            for action in ("pause", "cancel"):
                for bad in ({"k": "v"}, 123, ["a"]):
                    response = gw.post(
                        f"/api/v1/p1/tasks/T-R5-1/{action}",
                        json={"reason": bad},
                        headers=auth_headers,
                    )
                    assert response.status_code == 422, (
                        action,
                        bad,
                        response.status_code,
                    )
    finally:
        deactivate_governance()
