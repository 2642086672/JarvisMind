"""HTTP API tests for JME-4 endpoints."""
import pytest
from conftest import TEST_TOKEN

from jarvismind.schemas.memory import MemoryCreate, MemoryType


def test_hybrid_search_unauthorized(client):
    resp = client.get("/api/v1/memories/hybrid-search?q=test")
    assert resp.status_code == 401


def test_hybrid_search_invalid_token(client):
    resp = client.get(
        "/api/v1/memories/hybrid-search?q=test",
        headers={"X-API-Token": "wrong-token"},
    )
    assert resp.status_code == 401


def test_hybrid_search_api_success(client, auth_headers, app):
    container = app.state.container
    container.memory_service.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="我主要负责省区客服质管与理赔",
            summary="工作职责",
        )
    )

    resp = client.get(
        "/api/v1/memories/hybrid-search?q=客服质管",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert body["meta"]["total"] >= 1
    first = body["data"][0]
    assert "memory_id" in first
    assert "content" in first
    assert "sources" in first
    assert "rrf_score" in first
    assert "score" in first


def test_embedding_status_api(client, auth_headers):
    resp = client.get("/api/v1/embedding/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "provider" in body
    assert "model_name" in body
    assert "dimension" in body
    assert "is_available" in body
    assert body["dimension"] > 0


def test_index_status_api(client, auth_headers):
    resp = client.get("/api/v1/memories/index/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "collection_name" in body
    assert "storage_path" in body
    assert "total_points" in body
    assert "status" in body
    assert "sqlite_status_counts" in body


def test_index_rebuild_api(client, auth_headers, app):
    container = app.state.container
    container.memory_service.create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="用于测试全量重建的测试记忆",
        )
    )

    resp = client.post(
        "/api/v1/memories/index/rebuild",
        json={"force": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total_records"] >= 1
    assert body["indexed"] >= 1
    assert body["failed"] == 0
    assert body["status"] == "completed"


def test_index_consistency_api(client, auth_headers):
    resp = client.get("/api/v1/memories/index/consistency", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert "is_consistent" in body
    assert "sqlite_active_count" in body
    assert "qdrant_points_count" in body
    assert isinstance(body["missing_in_qdrant"], list)
    assert isinstance(body["orphans_in_qdrant"], list)
