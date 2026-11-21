"""JMR-08 overall acceptance.

Exercises the committed chain end to end through the real application:

    HTTP API -> Container -> JMR service (cost-governed) -> security/privacy
    -> query -> runtime -> cost -> scheduler -> task registry -> audit -> DB

No module is stubbed. The database is the isolated test manager, P1 tables are
installed by the fixture, and the Schema stays at v6 (no DDL added by this
feature).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from jarvismind.container import Container
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.migrations import MIGRATIONS
from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jmr.cost_governance import CostGovernance, CostGovernedJMRService
from jarvismind.jmr.scheduler_engine import SchedulerConfig, SchedulerEngine

PHONE = "13800138000"

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, 'FACT', :content, 5.0, 0.9, 'ACTIVE', "
    ":now, :now, NULL, NULL, :content)"
)


def _seed(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(_INSERT, {"id": memory_id, "content": content, "now": utc_now_iso()})


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


def _clean_pair(manager, marker: str) -> None:
    _seed(manager, f"M-{marker}-1", f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}")
    _seed(
        manager,
        f"M-{marker}-2",
        f"Bifrost 网关采用 PostgreSQL 主从架构 {marker}，计划 Q3 迁移",
    )


@pytest.fixture()
def p1_manager(manager):
    install_p1_schema(manager)
    return manager


@pytest.fixture()
def p1_client(settings, p1_manager):
    from fastapi.testclient import TestClient

    from jarvismind.main import create_app

    with TestClient(create_app(settings, manager=p1_manager)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Container acceptance
# ---------------------------------------------------------------------------
def test_container_exposes_wired_jmr_services(p1_manager, settings):
    container = Container(settings=settings, manager=p1_manager, logger=None)
    service = container.jmr_service
    assert isinstance(service, CostGovernedJMRService)
    assert isinstance(container.jmr_cost_governance, CostGovernance)
    assert container.jmr_query_service is not None
    assert container.jmr_review_bridge is not None
    # The wrapper delegates to the real service, not a copy.
    assert service.task_registry is container.jmr_service.task_registry
    assert service.audit_bus is container.jmr_service.audit_bus


def test_container_privacy_gate_is_active_through_the_wrapper(p1_manager, settings):
    _seed(p1_manager, "M-C-PII-1", f"我的手机号是 {PHONE}，请记住")
    _seed(p1_manager, "M-C-PII-2", f"我的手机号是 {PHONE}，请记住，常用")
    container = Container(settings=settings, manager=p1_manager, logger=None)
    result = container.jmr_service.run(task_id="T-C-PII", run_id="R-C-PII")
    assert result.status == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0


def test_container_scheduler_uses_the_cost_governed_service(p1_manager, settings):
    _clean_pair(p1_manager, "SCHED")
    container = Container(settings=settings, manager=p1_manager, logger=None)
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    engine = SchedulerEngine(
        p1_manager,
        service=container.jmr_service,
        config=SchedulerConfig(
            max_tasks_per_tick=2,
            batch_size=2,
            max_runtime_seconds=30,
            default_timeout_seconds=30,
        ),
        clock=lambda: now,
    )
    engine.register_task("T-C-SCHED", now=now)
    result = engine.tick(now=now)
    assert "T-C-SCHED" in result.succeeded
    assert _derived_count(p1_manager) > 0


# ---------------------------------------------------------------------------
# API acceptance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/v1/jmr/runs"),
        ("get", "/api/v1/jmr/runs/T-X"),
        ("post", "/api/v1/jmr/runs/T-X/pause"),
        ("post", "/api/v1/jmr/runs/T-X/resume"),
        ("post", "/api/v1/jmr/runs/T-X/cancel"),
        ("get", "/api/v1/jmr/proposals"),
        ("get", "/api/v1/jmr/proposals/P-X"),
        ("get", "/api/v1/jmr/conflicts"),
        ("get", "/api/v1/jmr/status"),
        ("get", "/api/v1/jmr/cost"),
        ("get", "/api/v1/jmr/auto-execute"),
    ],
)
def test_every_jmr_endpoint_requires_token(p1_client, method, path):
    response = getattr(p1_client, method)(path)
    assert response.status_code == 401, (path, response.status_code)


def test_cost_report_endpoint_returns_ledger(p1_client, auth_headers):
    response = p1_client.get("/api/v1/jmr/cost?days=30", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ledger"]["mode"] in {"OBSERVE", "ENFORCE"}
    assert "settled_count" in data
    assert "rejections" in data
    assert "by_model" in data


def test_api_run_is_privacy_gated_end_to_end(p1_client, auth_headers, p1_manager):
    _seed(p1_manager, "M-A-PII-1", f"我的手机号是 {PHONE}，请记住")
    _seed(p1_manager, "M-A-PII-2", f"我的手机号是 {PHONE}，请记住，常用")
    response = p1_client.post(
        "/api/v1/jmr/runs",
        json={"task_id": "T-API-PII", "run_id": "R-API-PII"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "SUCCEEDED"
    assert _derived_count(p1_manager) == 0
    status = p1_client.get("/api/v1/jmr/status", headers=auth_headers)
    assert status.status_code == 200
    detail = p1_client.get("/api/v1/jmr/runs/T-API-PII", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "SUCCEEDED"


def test_api_run_produces_queryable_proposals(p1_client, auth_headers, p1_manager):
    _clean_pair(p1_manager, "API")
    response = p1_client.post(
        "/api/v1/jmr/runs",
        json={"task_id": "T-API-OK", "run_id": "R-API-OK"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert _derived_count(p1_manager) > 0
    listing = p1_client.get("/api/v1/jmr/proposals?page=1&page_size=50", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"], listing.text
    proposal_id = listing.json()["items"][0]["proposal_id"]
    detail = p1_client.get(f"/api/v1/jmr/proposals/{proposal_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["proposal_id"] == proposal_id


# ---------------------------------------------------------------------------
# Database / audit acceptance
# ---------------------------------------------------------------------------
def test_schema_stays_at_v6_and_jmr_adds_no_tables(p1_manager):
    with p1_manager.session() as session:
        before = {
            str(row[0])
            for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    _clean_pair(p1_manager, "DDL")
    from jarvismind.jmr.service import JMRService

    JMRService.build(manager=p1_manager).run(task_id="T-DDL")
    with p1_manager.session() as session:
        after = {
            str(row[0])
            for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert before == after
    assert MIGRATIONS[-1].version == 6


def test_audit_chain_covers_run_privacy_and_cost(p1_manager, settings):
    _seed(p1_manager, "M-AUD-PII-1", f"我的手机号是 {PHONE}，请记住")
    _seed(p1_manager, "M-AUD-PII-2", f"我的手机号是 {PHONE}，请记住，常用")
    from jarvismind.jmr.config import JMRConfig
    from jarvismind.jmr.cost_governance import (
        CostGovernanceConfig,
        CostGovernedJMRService,
    )
    from jarvismind.jmr.service import JMRService

    # Enforced mode so the cost reservation/settlement audit chain is exercised.
    service = JMRService.build(manager=p1_manager, config=JMRConfig(expected_cost_cny=0.01))
    governance = CostGovernance(p1_manager, CostGovernanceConfig(daily_budget_cny=1.0))
    CostGovernedJMRService(service, governance).run(task_id="T-AUD", run_id="R-AUD")
    actions = [row["action"] for row in AuditBus(p1_manager).p1_entries(limit=2000)]
    assert any(a.startswith("P1:JMR_COST_RESERVE") for a in actions)
    assert any(a.startswith("P1:JMR_COST_SETTLE") for a in actions)
    assert any(a == "P1:JMR_REFLECTION:DENIED" for a in actions)
    blob = json.dumps(AuditBus(p1_manager).p1_entries(limit=2000), ensure_ascii=False)
    assert PHONE not in blob
    assert "sk-" not in blob
