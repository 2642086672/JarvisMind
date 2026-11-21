"""Initial memory import tests.

The fixture is a SYNTHETIC document. JarvisMind's real personal dataset is not
shipped with JarvisMind; these tests only exercise importer behaviour
(counts, idempotency, dry-run, provenance metadata, search, sensitive-content
rules and near-duplicate handling).
"""
import copy
import json

from jarvismind.services.initial_import import run_import

SYNTHETIC_DOCUMENT = {
    "source_type": "IMPORT",
    "source_name": "Synthetic import fixture",
    "memories": [
        {
            "memory_type": "PROFILE",
            "content": "示例用户偏好简洁的周报格式。",
            "summary": "周报偏好",
            "importance": 6.0,
            "confidence": 0.8,
        },
        {
            "memory_type": "PROFILE",
            "content": "示例用户习惯在上午集中处理邮件。",
            "summary": "工作节奏",
            "importance": 5.0,
            "confidence": 0.7,
        },
        {
            "memory_type": "SEMANTIC",
            "content": "示例用户从事物流快运行业。",
            "summary": "行业",
            "importance": 7.0,
            "confidence": 0.9,
        },
        {
            "memory_type": "SEMANTIC",
            "content": "示例仓库位于华东地区的一个枢纽城市。",
            "summary": "仓储位置",
            "importance": 5.0,
            "confidence": 0.6,
        },
        {
            "memory_type": "PROJECT",
            "content": "示例项目是一个内部记忆系统原型。",
            "summary": "项目概览",
            "importance": 8.0,
            "confidence": 0.9,
            "metadata": {"pending_confirmation": True},
        },
        {
            "memory_type": "PROJECT",
            "content": "示例项目计划在第三季度完成试点。",
            "summary": "项目计划",
            "importance": 7.0,
            "confidence": 0.8,
        },
    ],
}


def _document():
    return copy.deepcopy(SYNTHETIC_DOCUMENT)


def test_import_creates_source_and_memories(manager):
    result = run_import(manager, _document())
    assert result["total"] == 6
    assert result["imported"] == 6
    assert result["skipped_similar"] == 0
    assert result["skipped_identical"] == 0
    assert result["source_type"] == "IMPORT"
    assert result["source_name"] == "Synthetic import fixture"

    from jarvismind.services.memory_service import MemoryService

    records, total = MemoryService(manager).list(page_size=1000)
    assert total == 6
    assert result["by_type"]["PROFILE"] == 2
    assert result["by_type"]["SEMANTIC"] == 2
    assert result["by_type"]["PROJECT"] == 2


def test_import_is_idempotent(manager):
    document = _document()
    first = run_import(manager, document)
    second = run_import(manager, document)
    assert first["imported"] == 6
    assert second["imported"] == 0
    assert second["skipped_identical"] == 6

    from jarvismind.services.memory_service import MemoryService

    assert MemoryService(manager).list(page_size=1000)[1] == 6


def test_dry_run_writes_nothing(manager):
    result = run_import(manager, _document(), dry_run=True)
    assert result["dry_run"] is True
    assert result["imported"] == 6

    from jarvismind.services.memory_service import MemoryService

    assert MemoryService(manager).list(page_size=1000)[1] == 0


def test_import_records_source_and_metadata(manager):
    run_import(manager, _document())
    from jarvismind.services.memory_service import MemoryService

    records, _ = MemoryService(manager).list(page_size=1000)
    for record in records:
        assert record.source_id is not None
        assert record.status == "ACTIVE"
        metadata = json.loads(record.metadata_json or "{}")
        assert metadata["initial_import"] is True
        assert metadata["content_hash"]
        assert "pending_confirmation" in metadata


def test_imported_memories_are_searchable(manager):
    run_import(manager, _document())
    from jarvismind.services.memory_service import MemoryService

    service = MemoryService(manager)
    results, total = service.search(query="周报")
    assert total >= 1
    results, total = service.search(query="记忆系统原型")
    assert total >= 1


def test_no_sensitive_content_is_imported(manager):
    run_import(manager, _document())
    from jarvismind.core.sensitive import contains_sensitive
    from jarvismind.services.memory_service import MemoryService

    records, _ = MemoryService(manager).list(page_size=1000)
    for record in records:
        assert not contains_sensitive(record.content), record.content
        assert not contains_sensitive(record.summary or ""), record.summary


def test_near_duplicate_is_skipped(manager):
    document = _document()
    run_import(manager, document)
    tampered = _document()
    tampered["memories"] = [
        {
            "memory_type": "SEMANTIC",
            "content": "示例用户从事物流快运行业。",
            "summary": "行业",
            "importance": 7.0,
            "confidence": 0.9,
        }
    ]
    result = run_import(manager, tampered)
    assert result["imported"] == 0
    assert result["skipped_similar"] + result["skipped_identical"] == 1
