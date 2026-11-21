"""JSON export tests."""
import json

from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from jarvismind.schemas.source import SourceCreate
from jarvismind.services.memory_service import MemoryService
from jarvismind.services.source_service import SourceService


def _seed(manager):
    sources = SourceService(manager)
    memories = MemoryService(manager)
    source = sources.create(
        SourceCreate(source_type="CHAT", source_name="chat", uri="chat://1")
    )
    memories.create(
        MemoryCreate(
            memory_type=MemoryType.PROJECT,
            content="alpha project note",
            source_id=source.id,
        )
    )
    memories.create(
        MemoryCreate(memory_type=MemoryType.SEMANTIC, content="beta semantic note")
    )
    return source


def test_export_includes_memories_and_sources(manager):
    _seed(manager)
    document = MemoryService(manager).export_memories()
    assert len(document["memories"]) == 2
    assert len(document["sources"]) == 1
    assert document["sources"][0]["source_name"] == "chat"
    project = next(m for m in document["memories"] if m["memory_type"] == "PROJECT")
    assert project["source_id"] == document["sources"][0]["id"]


def test_export_filters_by_type(manager):
    _seed(manager)
    document = MemoryService(manager).export_memories(memory_type="SEMANTIC")
    assert len(document["memories"]) == 1
    assert document["memories"][0]["memory_type"] == "SEMANTIC"
    assert document["sources"] == []


def test_export_filters_by_status(manager):
    _seed(manager)
    service = MemoryService(manager)
    records, _ = service.list(memory_type="SEMANTIC")
    service.delete(records[0].id)
    document = service.export_memories(status="DELETED")
    assert len(document["memories"]) == 1
    assert document["memories"][0]["status"] == "DELETED"


def test_export_contains_no_token(manager):
    _seed(manager)
    document = MemoryService(manager).export_memories()
    text = json.dumps(document)
    assert "export-token-value" not in text
    assert "api_token" not in text


def test_export_document_shape(manager, tmp_path):
    _seed(manager)
    document = MemoryService(manager).export_memories()
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert len(loaded["memories"]) == 2
    assert loaded["memories"][0]["content"]
