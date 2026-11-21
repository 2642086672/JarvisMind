"""Tests for incremental indexing, consistency check, and full rebuild in JME-4."""
import pytest

from jarvismind.config import Settings
from jarvismind.database.connection import DatabaseManager
from jarvismind.database.migrations import initialize_database
from jarvismind.repositories.memory_repository import MemoryRepository
from jarvismind.schemas.memory import MemoryCreate, MemoryType, MemoryUpdate
from jarvismind.services.embedding import DeterministicEmbeddingProvider
from jarvismind.services.hybrid_search_service import HybridSearchService
from jarvismind.services.memory_service import MemoryService
from jarvismind.services.vector_store import VectorStore


@pytest.fixture()
def lifecycle_env(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "jarvismind.db"
    settings = Settings(
        env="test",
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        embedding_provider="deterministic",
        embedding_dimension=128,
        qdrant_path_override=":memory:",
    )
    manager = DatabaseManager(settings)
    initialize_database(manager)

    embedding = DeterministicEmbeddingProvider(dimension=128)
    vector_store = VectorStore(
        path=":memory:",
        collection_name="lifecycle_coll",
        dimension=128,
    )
    memory_service = MemoryService(
        manager,
        embedding_provider=embedding,
        vector_store=vector_store,
    )
    hybrid_service = HybridSearchService(
        manager=manager,
        embedding_provider=embedding,
        vector_store=vector_store,
        settings=settings,
    )

    yield {
        "manager": manager,
        "embedding": embedding,
        "vector_store": vector_store,
        "memory_service": memory_service,
        "hybrid_service": hybrid_service,
        "settings": settings,
    }
    vector_store.close()
    manager.dispose()


def test_incremental_index_on_create(lifecycle_env):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]
    manager = lifecycle_env["manager"]

    rec = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="日常使用的是iPhone 14 Pro手机",
        )
    )

    # Verify Qdrant point was indexed
    assert v_store.get_status().total_points == 1
    point_ids = v_store.get_all_point_ids()
    assert rec.id in point_ids

    # Verify SQLite vector_status was updated to INDEXED
    with manager.session() as session:
        db_rec = MemoryRepository(session).get_by_id(rec.id)
        assert db_rec.vector_status == "INDEXED"
        assert db_rec.vector_updated_at is not None


def test_incremental_index_on_update(lifecycle_env):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]

    rec = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="最初的记忆内容",
        )
    )
    assert v_store.get_status().total_points == 1

    # Update content
    mem_svc.update(rec.id, MemoryUpdate(content="修改后的全新记忆内容"))
    # Point remains indexed and updated
    assert v_store.get_status().total_points == 1


def test_incremental_index_on_delete(lifecycle_env):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]

    rec = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="待删除的临时记忆",
        )
    )
    assert rec.id in v_store.get_all_point_ids()

    # Soft delete
    mem_svc.delete(rec.id)
    # Point should be removed from vector store
    assert rec.id not in v_store.get_all_point_ids()


def test_index_failure_does_not_break_sqlite(lifecycle_env, monkeypatch):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]
    manager = lifecycle_env["manager"]

    # Force vector upsert to raise an error
    def broken_upsert(*args, **kwargs):
        raise RuntimeError("Simulated Qdrant disk write failure")

    monkeypatch.setattr(v_store, "upsert_memory", broken_upsert)

    # Creation in SQLite must STILL succeed!
    rec = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="即使向量写入失败，核心数据绝不能丢失",
        )
    )
    assert rec.id is not None

    # SQLite vector_status must be marked FAILED
    with manager.session() as session:
        db_rec = MemoryRepository(session).get_by_id(rec.id)
        assert db_rec.content == "即使向量写入失败，核心数据绝不能丢失"
        assert db_rec.vector_status == "FAILED"


def test_full_index_rebuild(lifecycle_env):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]
    hybrid_svc = lifecycle_env["hybrid_service"]
    manager = lifecycle_env["manager"]

    # Create 3 memories
    m1 = mem_svc.create(MemoryCreate(memory_type=MemoryType.SEMANTIC, content="条目一"))
    m2 = mem_svc.create(MemoryCreate(memory_type=MemoryType.SEMANTIC, content="条目二"))
    m3 = mem_svc.create(MemoryCreate(memory_type=MemoryType.SEMANTIC, content="条目三"))

    # Simulate Qdrant index lost/deleted
    v_store.recreate_collection(dimension=128)
    assert v_store.get_status().total_points == 0

    # Run full rebuild
    rebuild_res = hybrid_svc.rebuild_index(force=True)
    assert rebuild_res.total_records >= 3
    assert rebuild_res.indexed >= 3
    assert rebuild_res.failed == 0
    assert rebuild_res.status == "completed"

    # Verify Qdrant has all points again
    assert v_store.get_status().total_points >= 3
    all_points = v_store.get_all_point_ids()
    assert {m1.id, m2.id, m3.id}.issubset(all_points)

    # Verify status in SQLite
    with manager.session() as session:
        repo = MemoryRepository(session)
        counts = repo.count_by_vector_status()
        assert counts.get("INDEXED", 0) >= 3


def test_consistency_check(lifecycle_env):
    mem_svc = lifecycle_env["memory_service"]
    v_store = lifecycle_env["vector_store"]
    hybrid_svc = lifecycle_env["hybrid_service"]

    m1 = mem_svc.create(MemoryCreate(memory_type=MemoryType.SEMANTIC, content="数据A"))
    m2 = mem_svc.create(MemoryCreate(memory_type=MemoryType.SEMANTIC, content="数据B"))

    # Initially consistent
    report1 = hybrid_svc.check_consistency()
    assert report1.is_consistent is True
    assert len(report1.missing_in_qdrant) == 0
    assert len(report1.orphans_in_qdrant) == 0

    # Simulate missing point by deleting from Qdrant
    v_store.delete_memory(m1.id)
    report2 = hybrid_svc.check_consistency()
    assert report2.is_consistent is False
    assert m1.id in report2.missing_in_qdrant

    # Simulate orphan by inserting an unknown ID into Qdrant
    v_store.upsert_memory("orphan-id-999", [0.1] * 128)
    report3 = hybrid_svc.check_consistency()
    assert report3.is_consistent is False
    assert "orphan-id-999" in report3.orphans_in_qdrant
