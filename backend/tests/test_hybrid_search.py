"""Tests for Hybrid Search and RRF Fusion in JME-4."""
import pytest

from jarvismind.config import Settings
from jarvismind.core.errors import ValidationError
from jarvismind.database.connection import DatabaseManager
from jarvismind.database.migrations import initialize_database
from jarvismind.schemas.memory import MemoryCreate, MemoryType
from jarvismind.services.embedding import DeterministicEmbeddingProvider, DisabledEmbeddingProvider
from jarvismind.services.hybrid_search_service import HybridSearchService
from jarvismind.services.memory_service import MemoryService
from jarvismind.services.vector_store import VectorStore


@pytest.fixture()
def hybrid_env(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "jarvismind.db"
    settings = Settings(
        env="test",
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        embedding_provider="deterministic",
        embedding_dimension=256,
        qdrant_path_override=":memory:",
        hybrid_rrf_k=60,
        hybrid_vector_weight=1.0,
        hybrid_fts_weight=1.0,
    )
    manager = DatabaseManager(settings)
    initialize_database(manager)

    embedding = DeterministicEmbeddingProvider(dimension=256)
    vector_store = VectorStore(
        path=":memory:",
        collection_name="test_hybrid",
        dimension=256,
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


def test_hybrid_search_both_fts_and_vector(hybrid_env):
    mem_svc = hybrid_env["memory_service"]
    hybrid_svc = hybrid_env["hybrid_service"]

    # 1. Exact match on "舟山" (will match both FTS and vector)
    m1 = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.EPISODIC,
            content="我喜欢去舟山海边拍视频记录生活",
            summary="舟山海边度假摄影",
        )
    )
    # 2. Unrelated memory
    mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.WORKING,
            content="下周二下午需要开省区客服质量管理例会",
        )
    )

    results, meta = hybrid_svc.search(query="舟山")
    assert meta.total >= 1
    assert results[0].memory_id == m1.id
    assert results[0].fts_rank is not None
    assert results[0].vector_rank is not None
    assert "fts5" in results[0].sources
    assert "vector" in results[0].sources
    assert results[0].fusion_rank == 1
    assert results[0].score > 0.0


def test_hybrid_search_rrf_ranking_and_deduplication(hybrid_env):
    mem_svc = hybrid_env["memory_service"]
    hybrid_svc = hybrid_env["hybrid_service"]

    # Memory matching both strongly
    m_both = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="平时喜欢喝手冲咖啡和乌龙茶",
        )
    )
    # Memory matching partially
    m_partial = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="下午茶习惯吃点甜点",
        )
    )

    results, meta = hybrid_svc.search(query="手冲咖啡")
    assert meta.total >= 1
    # Check deduplication: each memory_id appears at most once
    ids = [r.memory_id for r in results]
    assert len(ids) == len(set(ids))

    # m_both must rank first with higher RRF score
    assert results[0].memory_id == m_both.id
    if len(results) > 1:
        assert results[0].rrf_score >= results[1].rrf_score


def test_hybrid_search_long_question_still_finds_topic(hybrid_env):
    """A natural question ANDs every bigram and would match nothing.

    The OR fallback must still surface a memory that shares the topic, so
    "我喜欢喝什么咖啡？" finds "...手冲咖啡".
    """
    mem_svc = hybrid_env["memory_service"]
    hybrid_svc = hybrid_env["hybrid_service"]

    memory = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="以后我更喜欢喝手冲咖啡",
            summary="咖啡偏好",
        )
    )

    results, meta = hybrid_svc.search(query="我喜欢喝什么咖啡？")
    assert meta.total >= 1
    assert any(item.memory_id == memory.id for item in results)


def test_hybrid_search_filtering_type_and_status(hybrid_env):
    mem_svc = hybrid_env["memory_service"]
    hybrid_svc = hybrid_env["hybrid_service"]

    # Active episodic
    m1 = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.EPISODIC,
            content="公司内部研讨会记录总结",
        )
    )
    # Active profile
    m2 = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="公司内部的员工编号为A1024",
        )
    )

    # Filter by memory_type = PROFILE
    res, meta = hybrid_svc.search(query="公司", memory_type="PROFILE")
    assert all(r.memory_type == MemoryType.PROFILE for r in res)
    matched_ids = {r.memory_id for r in res}
    assert m2.id in matched_ids
    assert m1.id not in matched_ids


def test_hybrid_search_pagination(hybrid_env):
    mem_svc = hybrid_env["memory_service"]
    hybrid_svc = hybrid_env["hybrid_service"]

    # Create 5 memories with a common term
    for i in range(5):
        mem_svc.create(
            MemoryCreate(
                memory_type=MemoryType.PROJECT,
                content=f"测试项目条目第{i}项关于架构设计的记录",
            )
        )

    # Page 1, size 2
    p1, meta1 = hybrid_svc.search(query="架构设计", page=1, page_size=2)
    assert len(p1) == 2
    assert meta1.total >= 5
    assert meta1.page == 1
    assert meta1.page_size == 2

    # Page 2, size 2
    p2, meta2 = hybrid_svc.search(query="架构设计", page=2, page_size=2)
    assert len(p2) == 2
    assert meta2.page == 2

    # Ensure no overlap between page 1 and page 2
    p1_ids = {r.memory_id for r in p1}
    p2_ids = {r.memory_id for r in p2}
    assert p1_ids.isdisjoint(p2_ids)


def test_hybrid_search_degradation_when_embedding_unavailable(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "jarvismind.db"
    settings = Settings(
        env="test",
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        embedding_provider="disabled",
        qdrant_path_override=":memory:",
    )
    manager = DatabaseManager(settings)
    initialize_database(manager)

    disabled_embedding = DisabledEmbeddingProvider("Simulated model failure")
    vector_store = VectorStore(path=":memory:", collection_name="deg_coll", dimension=256)
    mem_svc = MemoryService(manager, disabled_embedding, vector_store)
    hybrid_svc = HybridSearchService(manager, disabled_embedding, vector_store, settings)

    try:
        m = mem_svc.create(
            MemoryCreate(
                memory_type=MemoryType.SEMANTIC,
                content="关键词测试降级放行说明",
            )
        )
        # Search should not raise 500 error; it gracefully degrades to FTS5!
        results, meta = hybrid_svc.search(query="降级放行")
        assert meta.degraded_to_fts is True
        assert len(results) >= 1
        assert results[0].memory_id == m.id
        assert results[0].sources == ["fts5"]
    finally:
        vector_store.close()
        manager.dispose()


def test_hybrid_search_empty_query_raises(hybrid_env):
    hybrid_svc = hybrid_env["hybrid_service"]
    with pytest.raises(ValidationError):
        hybrid_svc.search(query="   ")


# ---------------------------------------------------------------------------
# C-1 · 融合阶段对 vector-only 命中强制复核权威 status
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("retired_status", ["MERGED", "ARCHIVED", "DELETED"])
def test_vector_only_hit_cannot_revive_a_retired_memory(hybrid_env, retired_status):
    """孤儿向量点（payload 仍标 ACTIVE）不得让退役记忆以 ACTIVE 复活。

    模拟 C-1 的孤儿形状：只在 SQLite 里退役记忆，不触碰 Qdrant 点。
    FTS 侧因 status 过滤不会命中，vector 侧仍会命中——融合阶段必须
    以 SQLite 权威 status 为准拒绝该行。
    """
    from sqlalchemy import text

    mem_svc = hybrid_env["memory_service"]
    rec = mem_svc.create(
        MemoryCreate(
            memory_type=MemoryType.EPISODIC,
            content="我喜欢去舟山海边拍视频记录生活",
        )
    )
    assert rec.id in hybrid_env["vector_store"].get_all_point_ids()
    with hybrid_env["manager"].session() as session:
        session.execute(
            text("UPDATE memory_records SET status=:s WHERE id=:id"),
            {"s": retired_status, "id": rec.id},
        )

    results, _ = hybrid_env["hybrid_service"].search(query="舟山", status="ACTIVE")
    assert all(item.memory_id != rec.id for item in results), (
        f"{retired_status} 记忆不得以 ACTIVE 身份回到 hybrid-search"
    )

    # 正向对照：请求其真实 status 时仍然可达（守卫是 status 相等，不是封杀）。
    results_matching, _ = hybrid_env["hybrid_service"].search(
        query="舟山", status=retired_status
    )
    assert any(item.memory_id == rec.id for item in results_matching)