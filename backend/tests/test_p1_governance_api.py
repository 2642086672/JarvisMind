"""P1 real application integration: enabled-state HTTP tests.

These prove the governance base is actually wired into the FastAPI application,
not just importable: with JARVISMIND_P1_GOVERNANCE_ENABLED=1 the container activates
the gate, the /api/v1/p1/* surface works, AND the existing /api/v1/memories
write endpoint keeps working because MemoryService was wrapped with
authorized_operation (the failure mode predicted by independent review P2-1).
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jarvismind.governance import deactivate_governance
from jarvismind.main import create_app
from jarvismind.p1_gate import is_active

SERVICE_SUBJECT = "svc:jme-governed-write"


@pytest.fixture()
def governed_client(manager, settings, auth_headers):
    enabled = replace(settings, p1_governance_enabled=True)
    application = create_app(enabled, manager=manager)
    try:
        with TestClient(application) as client:
            yield client, auth_headers, manager
    finally:
        deactivate_governance()


def _count(manager) -> int:
    with manager.session() as session:
        return int(
            session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar() or 0
        )


def test_enabled_status_reports_active_gate(governed_client):
    client, headers, manager = governed_client
    response = client.get("/api/v1/p1/status", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["governance_enabled"] is True
    assert body["gate_active"] is True
    assert body["projection_read_only"]["read_only"] is True
    assert body["orm_gate"]["active"] is True
    assert is_active() is True


def test_governed_http_write_and_projection(governed_client):
    client, headers, manager = governed_client
    client.get("/api/v1/p1/status", headers=headers)  # activate governance
    before = _count(manager)
    response = client.post(
        "/api/v1/p1/memories",
        headers=headers,
        json={
            "memory_type": "SEMANTIC",
            "content": "Written through the P1 HTTP gateway",
            "summary": "integration evidence",
            "task_id": "T-HTTP",
            "run_id": "R-HTTP",
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["principal_id"] == SERVICE_SUBJECT
    assert created["grant_id"]
    assert _count(manager) == before + 1

    listing = client.get("/api/v1/p1/memories?page_size=50", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["read_only"]["read_only"] is True
    assert any(item["id"] == created["id"] for item in body["items"])


def test_legacy_memories_endpoint_still_works_when_governance_is_on(governed_client):
    """P2-1 regression: enabling the gate must not break existing write routes."""
    client, headers, manager = governed_client
    client.get("/api/v1/p1/status", headers=headers)
    before = _count(manager)
    response = client.post(
        "/api/v1/memories",
        headers=headers,
        json={
            "memory_type": "SEMANTIC",
            "content": "legacy route under active governance",
        },
    )
    assert response.status_code in (200, 201), response.text
    assert _count(manager) == before + 1


def test_client_supplied_actor_cannot_become_the_audited_principal(governed_client):
    client, headers, manager = governed_client
    client.get("/api/v1/p1/status", headers=headers)
    response = client.post(
        "/api/v1/p1/memories",
        headers=headers,
        json={
            "memory_type": "SEMANTIC",
            "content": "client labels are data only",
            "metadata": {"actor": "root", "actor_label": "administrator"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["principal_id"] == SERVICE_SUBJECT
    audit = client.get("/api/v1/p1/audit?limit=200", headers=headers).json()
    principals = {e["detail"].get("principal_id") for e in audit["entries"]}
    assert SERVICE_SUBJECT in principals
    assert "root" not in principals


def test_task_lifecycle_over_http(governed_client):
    client, headers, manager = governed_client
    client.get("/api/v1/p1/status", headers=headers)
    created = client.post(
        "/api/v1/p1/tasks",
        headers=headers,
        json={"task_id": "T-HTTP-1", "task_type": "REFLECTION"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "CREATED"  # create() returns the new row

    state = client.get("/api/v1/p1/tasks/T-HTTP-1", headers=headers).json()
    assert state["state"]["status"] == "RUNNING"  # auto-started for valid transitions

    paused = client.post("/api/v1/p1/tasks/T-HTTP-1/pause", headers=headers).json()
    assert paused["to_status"] == "PAUSED"
    resumed = client.post("/api/v1/p1/tasks/T-HTTP-1/resume", headers=headers).json()
    assert resumed["to_status"] == "RUNNING"
    cancelled = client.post("/api/v1/p1/tasks/T-HTTP-1/cancel", headers=headers).json()
    assert cancelled["to_status"] == "CANCELLED"


def test_audit_endpoint_reports_p1_activity(governed_client):
    client, headers, manager = governed_client
    client.get("/api/v1/p1/status", headers=headers)
    client.post(
        "/api/v1/p1/memories",
        headers=headers,
        json={"memory_type": "SEMANTIC", "content": "audit integration"},
    )
    body = client.get("/api/v1/p1/audit?limit=200", headers=headers).json()
    assert body["count"] >= 2
    required = (
        "principal_id",
        "operation",
        "object_ref",
        "trigger",
        "executed_at",
        "status",
        "failure_reason",
        "task_id",
        "run_id",
    )
    for entry in body["entries"]:
        assert all(k in entry["detail"] for k in required)


def test_disabled_governance_returns_409(client, auth_headers):
    """Default settings must NOT activate the gate; legacy suite stays intact."""
    response = client.post(
        "/api/v1/p1/memories",
        headers=auth_headers,
        json={"memory_type": "SEMANTIC", "content": "should be refused"},
    )
    assert response.status_code == 409
    assert is_active() is False


def test_enabled_get_and_fts_rebuild_do_not_break(governed_client):
    """Review P1 E3/E4: GET(touch=True) and index rebuild used to 500."""
    from jarvismind.database.search_index import rebuild_search_index

    client, headers, manager = governed_client
    # create_app activates governance at STARTUP now; no /p1/status warm-up.
    created = client.post(
        "/api/v1/memories",
        headers=headers,
        json={"memory_type": "SEMANTIC", "content": "maintenance regression"},
    )
    assert created.status_code in (200, 201), created.text
    with manager.session() as session:
        memory_id = session.execute(
            text(
                "SELECT id FROM memory_records "
                "WHERE content='maintenance regression' LIMIT 1"
            )
        ).scalar()
    assert memory_id
    got = client.get(f"/api/v1/memories/{memory_id}", headers=headers)
    assert got.status_code == 200, got.text
    assert rebuild_search_index(manager) >= 1


def test_enabled_extract_does_not_return_403(governed_client):
    """Round-3 review P1-1: /memories/extract used to be blocked at candidate create."""
    client, headers, manager = governed_client
    response = client.post(
        "/api/v1/memories/extract",
        headers=headers,
        json={
            "conversation": "我从事物流快运行业，负责省区客服质管和理赔工作。",
            "conversation_id": "conv-p1-enabled",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["counts"]["PENDING"] >= 1


def test_enabled_import_does_not_return_403(governed_client):
    """Round-3 review P1-1: /memories/import used to be blocked at repo.create."""
    client, headers, manager = governed_client
    document = {
        "memories": [
            {
                "id": "p1-import-enabled",
                "memory_type": "SEMANTIC",
                "content": "enabled import path",
                "importance": 5.0,
                "confidence": 0.5,
                "status": "ACTIVE",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "sources": [],
    }
    response = client.post(
        "/api/v1/memories/import", headers=headers, json={"document": document}
    )
    assert response.status_code == 200, response.text
