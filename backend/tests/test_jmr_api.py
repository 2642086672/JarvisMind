"""JMR-02 HTTP surface tests (real isolated SQLite, no mocked service).

Covers:
  * token requirement;
  * ``GET /status`` facts and ``409 p1_schema_missing`` before P1 install;
  * real ``POST /runs`` + proposal/conflict queries;
  * ids/task state are read from the same database, never fabricated.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from jarvismind.governance.schema import install_p1_schema

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"
DERIVED = ("memory_merges", "memory_corrections", "memory_summaries")

_INSERT = (
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


def _seed(manager):
    specs = [
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


def _count(manager, sql):
    with manager.session() as session:
        return int(session.execute(text(sql)).scalar() or 0)


def test_requires_api_token(client):
    resp = client.get("/api/v1/jmr/status")
    assert resp.status_code == 401


def test_status_is_factual_before_p1_install(client, auth_headers):
    resp = client.get("/api/v1/jmr/status", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["p1_schema_installed"] is False
    assert data["llm_online"] is False
    assert data["automatic_execution_authorized"] is False
    assert any("p1_schema_missing" in note for note in data["notes"])


def test_run_without_p1_schema_returns_explicit_409(client, auth_headers):
    resp = client.post(
        "/api/v1/jmr/runs",
        json={"task_id": "T-NO-P1", "run_id": "R-NO-P1"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "p1_schema_missing"
    assert "p1_tasks" in body["error"]["details"]["missing"]


def test_real_run_and_queries(client, auth_headers, manager):
    install_p1_schema(manager)
    _seed(manager)
    resp = client.post(
        "/api/v1/jmr/runs",
        json={"task_id": "T-API-1", "run_id": "R-API-1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "SUCCEEDED"
    assert data["created"]["MERGE"] == 1
    assert data["projection_digest"]
    assert data["expired_voided"] == 0

    task = client.get("/api/v1/jmr/runs/T-API-1", headers=auth_headers)
    assert task.status_code == 200, task.text
    assert task.json()["data"]["status"] == "SUCCEEDED"
    assert task.json()["data"]["event_count"] >= 2

    proposals = client.get(
        "/api/v1/jmr/proposals", params={"page_size": 100}, headers=auth_headers
    )
    assert proposals.status_code == 200, proposals.text
    page = proposals.json()
    assert page["total"] >= 4
    assert {item["rule"] for item in page["items"]} <= {
        "MERGE",
        "CONFLICT",
        "STATUS_CHANGE",
        "SUMMARY",
    }
    first = page["items"][0]
    detail = client.get(
        f"/api/v1/jmr/proposals/{first['proposal_id']}", headers=auth_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["proposal_id"] == first["proposal_id"]

    conflicts = client.get(
        "/api/v1/jmr/conflicts", params={"conflict_class": "WORDING_DIFF"},
        headers=auth_headers,
    )
    assert conflicts.status_code == 200, conflicts.text
    assert conflicts.json()["total"] >= 1


def test_run_response_has_no_hardcoded_example_data(client, auth_headers, manager):
    """Every proposal returned by the API exists in the isolated DB."""
    install_p1_schema(manager)
    _seed(manager)
    client.post(
        "/api/v1/jmr/runs",
        json={"task_id": "T-API-2", "run_id": "R-API-2"},
        headers=auth_headers,
    )
    page = client.get(
        "/api/v1/jmr/proposals", params={"page_size": 100}, headers=auth_headers
    ).json()
    with manager.session() as session:
        ids = set()
        for table in DERIVED:
            ids.update(r[0] for r in session.execute(text(f"SELECT id FROM {table}")))
    assert page["items"]
    for item in page["items"]:
        assert item["proposal_id"] in ids
        meta = None
        with manager.session() as session:
            for table in DERIVED:
                row = session.execute(
                    text(f"SELECT metadata_json FROM {table} WHERE id=:i"),
                    {"i": item["proposal_id"]},
                ).scalar()
                if row is not None:
                    meta = json.loads(row)
                    break
        assert meta is not None
        assert meta["rule"] == item["rule"]
        assert meta["expires_at"]
        assert meta["generation"] >= 1
        assert isinstance(meta["evidence"], list) and meta["evidence"]
        assert all("content" not in item for item in meta["evidence"])
        assert all("content_sha256" in item for item in meta["evidence"])


def test_unknown_task_returns_explicit_404(client, auth_headers, manager):
    install_p1_schema(manager)
    resp = client.get("/api/v1/jmr/runs/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "jmr_not_found"


def test_run_pause_cancel_cycle(client, auth_headers, manager):
    install_p1_schema(manager)
    _seed(manager)
    # Create a task without executing it through the public run endpoint.
    from jarvismind.governance.task_registry import TaskRegistry

    TaskRegistry(manager).create("T-CTRL", "JMR_REFLECTION", owner="svc:jmr")
    TaskRegistry(manager).start("T-CTRL")
    paused = client.post("/api/v1/jmr/runs/T-CTRL/pause", json={"reason": "test"}, headers=auth_headers)
    assert paused.status_code == 200, paused.text
    assert paused.json()["data"]["status"] == "PAUSED"
    # Idempotent pause: no duplicate event.
    again = client.post("/api/v1/jmr/runs/T-CTRL/pause", json={}, headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["data"]["noop"] is True
    resumed = client.post("/api/v1/jmr/runs/T-CTRL/resume", headers=auth_headers)
    assert resumed.status_code == 200
    assert resumed.json()["data"]["status"] == "RUNNING"
    cancelled = client.post("/api/v1/jmr/runs/T-CTRL/cancel", json={"reason": "done"}, headers=auth_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "CANCELLED"
