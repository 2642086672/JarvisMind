"""JME-5.8 import/export tests."""
from __future__ import annotations

import json

from jarvismind.repositories.memory_repository import MemoryRepository


def _create(client, headers, content, mtype="SEMANTIC", **kw):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": mtype, "content": content, **kw},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_export_document_shape_and_no_secrets(client, auth_headers):
    source = client.post(
        "/api/v1/memory-sources",
        json={"source_type": "CHAT", "source_name": "chat", "uri": "chat://1"},
        headers=auth_headers,
    ).json()["data"]
    _create(client, auth_headers, "export me", source_id=source["id"])
    document = client.get("/api/v1/memories/export", headers=auth_headers).json()
    assert document["format"] == "jme-export"
    assert document["schema_version"] == 5
    assert document["counts"]["memories"] == 1
    assert document["counts"]["sources"] == 1
    assert document["memories"][0]["source_id"] == source["id"]
    text = json.dumps(document)
    assert "X-API-Token" not in text
    assert "api_token" not in text


def test_preview_import_reports_duplicates(client, auth_headers):
    existing = _create(client, auth_headers, "已有的记忆内容")
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": existing["id"],
                "memory_type": "SEMANTIC",
                "content": "已有的记忆内容",
                "status": "ACTIVE",
            },
            {
                "id": "new-id-1",
                "memory_type": "SEMANTIC",
                "content": "量子计算研究笔记",
                "status": "ACTIVE",
            },
        ],
        "sources": [],
    }
    preview = client.post(
        "/api/v1/memories/import/preview",
        json={"document": document},
        headers=auth_headers,
    ).json()["data"]
    assert preview["valid"] is True
    assert preview["would_import"] == 1
    assert preview["would_skip"] == 1
    assert existing["id"] in preview["duplicates_by_id"]


def test_import_additive_and_safe(client, auth_headers, manager):
    keep = _create(client, auth_headers, "保留的原有记忆")
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "imported-1",
                "memory_type": "PROJECT",
                "content": "导入的项目记忆",
                "status": "ACTIVE",
            }
        ],
        "sources": [
            {
                "id": "imported-source",
                "source_type": "CHAT",
                "source_name": "imported",
                "uri": "chat://import",
            }
        ],
    }
    result = client.post(
        "/api/v1/memories/import",
        json={"document": document},
        headers=auth_headers,
    ).json()["data"]
    assert result["imported_memories"] == 1
    assert result["imported_sources"] == 1
    assert result["vector_indexed"] == 1

    with manager.session() as session:
        repo = MemoryRepository(session)
        assert repo.get_by_id(keep["id"]) is not None
        imported = repo.get_by_id("imported-1")
        assert imported is not None
        assert imported.status == "ACTIVE"
        assert imported.vector_status == "INDEXED"
        assert repo.source_exists("imported-source")


def test_reimport_skips_duplicates(client, auth_headers):
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "dup-1",
                "memory_type": "SEMANTIC",
                "content": "重复记忆一",
                "status": "ACTIVE",
            }
        ],
        "sources": [],
    }
    first = client.post(
        "/api/v1/memories/import", json={"document": document}, headers=auth_headers
    ).json()["data"]
    assert first["imported_memories"] == 1
    second = client.post(
        "/api/v1/memories/import", json={"document": document}, headers=auth_headers
    ).json()["data"]
    assert second["imported_memories"] == 0
    assert second["skipped_duplicates"] == 1


def test_invalid_document_is_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/import",
        json={"document": {"memories": [{"id": "x"}]}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_import_skips_orphan_source_reference(client, auth_headers, manager):
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "orphan-1",
                "memory_type": "SEMANTIC",
                "content": "孤儿来源记忆",
                "source_id": "missing-source",
                "status": "ACTIVE",
            }
        ],
        "sources": [],
    }
    result = client.post(
        "/api/v1/memories/import", json={"document": document}, headers=auth_headers
    ).json()["data"]
    assert result["imported_memories"] == 1
    assert result["skipped_orphans"] == 1
    with manager.session() as session:
        assert (
            MemoryRepository(session).get_by_id("orphan-1").source_id is None
        )


def test_lifecycle_round_trip(client, auth_headers):
    first = _create(client, auth_headers, "总结来源一")
    second = _create(client, auth_headers, "总结来源二")
    summary_id = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    ).json()["data"]["id"]
    client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "approve"},
        headers=auth_headers,
    )
    document = client.get("/api/v1/memories/export", headers=auth_headers).json()
    assert document["counts"]["summaries"] == 1
    assert document["counts"]["lifecycle_reviews"] == 0

    # Import into the same database would skip duplicates; use a fresh manager
    # via the app's import endpoint with a remapped document instead.
    replacements = ["量子计算研究笔记甲", "生物化学实验记录乙", "天文物理观测数据丙"]
    for item, replacement in zip(document["memories"], replacements):
        item["id"] = item["id"] + "-copy"
        item["content"] = replacement
    for item in document["summaries"]:
        item["id"] = item["id"] + "-copy"
        item["memory_ids"] = [mid + "-copy" for mid in item["memory_ids"]]
    result = client.post(
        "/api/v1/memories/import", json={"document": document}, headers=auth_headers
    ).json()["data"]
    assert result["imported_memories"] == 3
    assert result["imported_summaries"] == 1
