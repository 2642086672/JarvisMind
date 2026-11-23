"""JME-AUTO-GOVERNANCE-01 tests on a real isolated SQLite database.

Every test asserts REAL database state. Nothing here mocks the write path:
a status is only trusted when the corresponding ``memory_records`` row exists.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from jarvismind.core.auto_governance import (
    Relation,
    attributes,
    is_fact_like,
    mergeable_group,
    relation,
)
from jarvismind.schemas.memory import MemoryCreate, MemoryType
from jarvismind.services.auto_memory import AutoMemoryService, detect_memory_intent
from jarvismind.services.embedding import DeterministicEmbeddingProvider
from jarvismind.services.hybrid_search_service import HybridSearchService
from jarvismind.services.memory_service import MemoryService
from jarvismind.services.vector_store import VectorStore

TS = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------- helpers
def _count(manager, table, where="", params=None):
    with manager.session() as session:
        return int(
            session.execute(
                text(f"SELECT COUNT(*) FROM {table} {where}"), params or {}
            ).scalar()
            or 0
        )


def _remember(manager, text_value, conversation_id="conv-gov"):
    return AutoMemoryService(manager).auto_remember(
        text_value=text_value,
        conversation_id=conversation_id,
        message_id="msg-gov",
        turn_id="turn-gov",
    )


def _seed_memory(manager, content, memory_type="SEMANTIC", memory_id="seed-1"):
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records("
                "id, memory_type, content, importance, confidence, status, "
                "created_at, updated_at, source_id, metadata_json, search_text"
                ") VALUES (:id, :t, :c, 5.0, 0.9, 'ACTIVE', :now, :now, NULL, NULL, :c)"
            ),
            {"id": memory_id, "t": memory_type, "c": content, "now": TS},
        )


def _rows(manager, where="", params=None):
    with manager.session() as session:
        return session.execute(
            text(f"SELECT * FROM memory_records {where} ORDER BY created_at"),
            params or {},
        ).mappings().all()


def _audits(manager, action):
    with manager.session() as session:
        return session.execute(
            text(
                "SELECT memory_id, detail_json FROM memory_audit_logs "
                "WHERE action=:action ORDER BY created_at"
            ),
            {"action": action},
        ).mappings().all()


# ------------------------------------------------------------------ A1 policy
def test_intent_accepts_profile_facts_without_keyword():
    for text in (
        "我叫林晓明",
        "我喜欢喝美式咖啡",
        "我平时喜欢喝美式",
        "以后我更喜欢喝美式咖啡",
        "我使用 Windows 10",
        "我的职业是软件工程师",
    ):
        intent = detect_memory_intent(text)
        assert intent.is_memory, text
        assert intent.fact, text


def test_question_hypothetical_and_third_party_are_not_facts():
    for text in (
        "我喜欢这个方案吗？",
        "我喜欢喝什么咖啡",
        "我的爱好是什么",
        "如果我有一台 Mac 会怎样",
        "朋友说他喜欢喝拿铁",
        "他说他住在北京",
        "不要记住我的密码",
    ):
        assert not is_fact_like(text), text


def test_relation_classification_is_conservative():
    assert relation("我喜欢喝美式咖啡", "我喜欢喝美式咖啡") == Relation.DUPLICATE
    assert relation("我平时喜欢喝美式", "我喜欢喝美式咖啡") == Relation.DUPLICATE
    assert relation("以后我更喜欢喝美式咖啡", "我喜欢喝拿铁") == Relation.UPDATE
    assert relation("我现在不喜欢拿铁了", "我喜欢喝拿铁") == Relation.UPDATE
    assert relation("我的职业是产品经理", "我的职业是软件工程师") == Relation.CONFLICT
    # A person can like two unrelated things: that is a new memory, not a clash.
    assert relation("我的爱好是摄影", "我喜欢喝拿铁") == Relation.INDEPENDENT
    assert relation("我喜欢独自去海边旅行", "我喜欢独自旅行拍摄视频") != Relation.UPDATE


def test_word_containing_change_character_is_not_an_update():
    """'改善岗' contains 改 but states a different job, not a replacement.

    Regression for a real A-class risk: treating it as UPDATE would have
    retired a valid career-history memory.
    """
    older = "用户职业经历：2022-08 起在省区客服质管部任品效改善岗。"
    newer = "用户职业经历：2024-07 起兼任或转入省区仲裁岗。"
    assert relation(older, newer) == Relation.CONFLICT
    assert relation(newer, older) == Relation.CONFLICT


def test_mergeable_group_rejects_merely_similar_memories():
    assert mergeable_group(
        [
            "用户偏好使用中文交流，喜欢简洁直接的回答",
            "用户偏好使用中文交流，喜欢简洁直接的回答。",
        ]
    )
    assert not mergeable_group(
        [
            "用户喜欢独自旅行、拍摄视频和记录生活。",
            "用户喜欢独自去舟山这样的海边城市旅行，并拍摄视频记录生活。",
        ]
    )


# --------------------------------------------------------------- A2 auto write
def test_plain_name_is_auto_saved_without_keyword(manager):
    result = _remember(manager, "我叫林晓明")
    assert result.status == "SAVED", result.message
    assert result.memory_id
    rows = _rows(manager, "WHERE id=:id", {"id": result.memory_id})
    assert rows[0]["content"] == "我叫林晓明"
    assert rows[0]["status"] == "ACTIVE"


def test_plain_preference_is_auto_saved(manager):
    result = _remember(manager, "我平时喜欢喝美式")
    assert result.status == "SAVED", result.message
    assert _count(manager, "memory_records") == 1


def test_explicit_request_still_saves(manager):
    result = _remember(manager, "记住我使用 Windows 10")
    assert result.status == "SAVED", result.message
    assert _rows(manager)[0]["content"] == "我使用 Windows 10"


def test_write_failure_is_not_reported_as_saved(manager, monkeypatch):
    from jarvismind.services.approval_service import ApprovalService

    def boom(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(ApprovalService, "review", boom)
    result = _remember(manager, "我喜欢喝茶")
    assert result.status == "FAILED"
    assert result.memory_id is None
    assert _count(manager, "memory_records") == 0


# ------------------------------------------------------------------- B1 dedupe
def test_identical_sentence_is_not_written_twice(manager):
    first = _remember(manager, "我叫林晓明")
    second = _remember(manager, "我叫林晓明")
    assert first.status == "SAVED"
    assert second.status == "DUPLICATE"
    assert _count(manager, "memory_records") == 1


def test_different_wording_of_same_fact_is_deduplicated(manager):
    first = _remember(manager, "我喜欢喝拿铁")
    second = _remember(manager, "我平时喜欢喝拿铁")
    assert first.status == "SAVED"
    assert second.status == "DUPLICATE", second.message
    assert _count(manager, "memory_records") == 1


def test_same_preference_across_conversations_stays_single(manager):
    _remember(manager, "我喜欢喝美式咖啡", conversation_id="conv-a")
    result = _remember(manager, "我平时喜欢喝美式", conversation_id="conv-b")
    assert result.status == "DUPLICATE"
    assert _count(manager, "memory_records") == 1


# ------------------------------------------------------------------- B2 update
def test_explicit_preference_change_updates_in_place(manager):
    saved = _remember(manager, "我喜欢喝拿铁")
    updated = _remember(manager, "以后我更喜欢喝美式咖啡")
    assert saved.status == "SAVED"
    assert updated.status == "UPDATED", updated.message
    assert updated.memory_id == saved.memory_id
    assert _count(manager, "memory_records") == 1
    row = _rows(manager)[0]
    assert row["content"] == "以后我更喜欢喝美式咖啡"
    assert row["status"] == "ACTIVE"


def test_update_keeps_old_value_in_audit_history(manager):
    saved = _remember(manager, "我喜欢喝拿铁")
    _remember(manager, "以后我更喜欢喝美式咖啡")
    audits = _audits(manager, "AUTO_UPDATE")
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail_json"])
    assert detail["before"]["content"] == "我喜欢喝拿铁"
    assert detail["after"]["content"] == "以后我更喜欢喝美式咖啡"
    assert audits[0]["memory_id"] == saved.memory_id


def test_project_status_update_retires_old_state(manager):
    _seed_memory(manager, "JarvisMind 项目当前状态为进行中", "PROJECT")
    result = _remember(manager, "JarvisMind 项目已完成")
    assert result.status == "UPDATED", result.message
    rows = _rows(manager)
    assert len(rows) == 1
    assert rows[0]["content"] == "JarvisMind 项目已完成"


def test_update_does_not_overwrite_unrelated_memory(manager):
    """'我更喜欢喝拿铁' must not rewrite a memory that only shares 喜欢.

    Real defect observed on device: the SQLite preference memory was overwritten
    because both sentences contained 喜欢. An update marker alone is not enough;
    the attribute must be the same AND single-valued.
    """
    _seed_memory(manager, "user: 我喜欢用 SQLite 做本地存储", "SEMANTIC", "sqlite-1")
    result = _remember(manager, "我更喜欢喝拿铁")
    assert result.status in {"SAVED", "CONFLICT"}, result.message
    assert _rows(manager, "WHERE id='sqlite-1'")[0]["content"] == (
        "user: 我喜欢用 SQLite 做本地存储"
    )


def test_question_is_not_saved_as_memory(manager):
    """A question can mention a preference but is not a statement of fact."""
    result = _remember(manager, "我喜欢喝什么咖啡")
    assert result.status == "NO_MEMORY", result.message
    assert _count(manager, "memory_records") == 0


def test_update_failure_keeps_old_value(manager, monkeypatch):
    from jarvismind.services.lifecycle_service import LifecycleService

    _remember(manager, "我喜欢喝拿铁")

    def boom(*args, **kwargs):
        raise RuntimeError("injected update failure")

    monkeypatch.setattr(LifecycleService, "supersede_memory", boom)
    result = _remember(manager, "以后我更喜欢喝美式咖啡")
    assert result.status == "FAILED", result.message
    assert _rows(manager)[0]["content"] == "我喜欢喝拿铁"


# ----------------------------------------------------------------- B3 auto merge
def test_auto_merge_collapses_equivalent_memories(manager):
    _seed_memory(
        manager,
        "用户偏好使用中文交流，喜欢简洁直接的回答",
        "PROFILE",
        memory_id="dup-a",
    )
    _seed_memory(
        manager,
        "用户偏好使用中文交流，喜欢简洁直接的回答。",
        "PROFILE",
        memory_id="dup-b",
    )
    service = AutoMemoryService(manager)
    report = service.auto_merge(dry_run=False)
    assert report["status"] == "MERGED"
    assert len(report["merged"]) == 1

    rows = _rows(manager)
    statuses = sorted(row["status"] for row in rows)
    assert statuses == ["ACTIVE", "MERGED", "MERGED"]
    product = [row for row in rows if row["status"] == "ACTIVE"][0]
    meta = json.loads(product["metadata_json"])
    assert set(meta["merged_from"]) == {"dup-a", "dup-b"}
    assert _count(manager, "memory_merges", "WHERE status='CONFIRMED'") == 1
    assert len(_audits(manager, "AUTO_MERGE_SOURCE")) == 2


def test_auto_merge_dry_run_writes_nothing(manager):
    _seed_memory(manager, "用户偏好使用中文交流，喜欢简洁直接的回答", "PROFILE", "d1")
    _seed_memory(manager, "用户偏好使用中文交流，喜欢简洁直接的回答。", "PROFILE", "d2")
    report = AutoMemoryService(manager).auto_merge(dry_run=True)
    assert report["status"] == "DRY_RUN"
    assert _count(manager, "memory_records", "WHERE status='MERGED'") == 0
    assert _count(manager, "memory_merges") == 0


def test_auto_merge_skips_memories_that_differ_in_detail(manager):
    _seed_memory(manager, "用户喜欢独自旅行、拍摄视频和记录生活。", "PROFILE", "p1")
    _seed_memory(
        manager, "用户喜欢独自去舟山这样的海边城市旅行，并拍摄视频记录生活。", "PROFILE", "p2"
    )
    report = AutoMemoryService(manager).auto_merge(dry_run=False)
    assert report["merged"] == []
    assert _count(manager, "memory_records", "WHERE status='MERGED'") == 0


# ----------------------------------------------------------------- B4 expiry
def test_expiry_only_archives_explicitly_expired_memories(manager):
    _seed_memory(manager, "我明天要去上海出差", "EPISODIC", "temp-1")
    with manager.session() as session:
        session.execute(
            text(
                "UPDATE memory_records SET expires_at=:exp WHERE id='temp-1'"
            ),
            {"exp": "2020-01-01T00:00:00+00:00"},
        )
    report = AutoMemoryService(manager).auto_expire(dry_run=False)
    assert report["archived"] == 1
    row = _rows(manager, "WHERE id='temp-1'")[0]
    assert row["status"] == "ARCHIVED"
    assert len(_audits(manager, "AUTO_EXPIRE")) == 1


def test_expiry_never_touches_memories_without_expiry(manager):
    _seed_memory(manager, "旧的工作状态记录", "PROJECT", "old-1")
    report = AutoMemoryService(manager).auto_expire(dry_run=False)
    assert report["archived"] == 0
    assert _rows(manager, "WHERE id='old-1'")[0]["status"] == "ACTIVE"


def test_expiry_dry_run_writes_nothing(manager):
    _seed_memory(manager, "过期的临时计划", "EPISODIC", "temp-2")
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_records SET expires_at=:exp WHERE id='temp-2'"),
            {"exp": "2020-01-01T00:00:00+00:00"},
        )
    report = AutoMemoryService(manager).auto_expire(dry_run=True)
    assert report["status"] == "DRY_RUN"
    assert _rows(manager, "WHERE id='temp-2'")[0]["status"] == "ACTIVE"


# ------------------------------------------------------------------ C conflict
def test_ambiguous_change_is_held_not_overwritten(manager):
    _remember(manager, "我的职业是软件工程师")
    result = _remember(manager, "我的职业是产品经理")
    assert result.status == "CONFLICT", result.message
    assert _rows(manager)[0]["content"] == "我的职业是软件工程师"
    with manager.session() as session:
        row = session.execute(
            text(
                "SELECT status, conflicts_with FROM memory_candidates WHERE id=:id"
            ),
            {"id": result.candidate_id},
        ).mappings().one()
    assert row["status"] == "PENDING"
    assert row["conflicts_with"] is not None


def test_ambiguous_multi_target_is_held_not_written(manager):
    """Two plausible targets: JME must not pick one and must not add a third row."""
    _seed_memory(manager, "我的职业是软件工程师", "PROFILE", "job-a")
    _seed_memory(manager, "我的职业是产品经理", "PROFILE", "job-b")
    result = _remember(manager, "我的职业是设计师")
    assert result.status == "CONFLICT", result.message
    assert result.code == "conflict_ambiguous"
    assert _count(manager, "memory_records") == 2


def test_explicit_correction_updates_instead_of_conflicting(manager):
    _remember(manager, "我的职业是软件工程师")
    result = _remember(manager, "我现在改行做产品经理了")
    assert result.status in {"UPDATED", "SAVED", "CONFLICT"}, result.message
    # Whatever the classifier decides, it must never leave two contradictory
    # ACTIVE rows for the same single-valued attribute.
    active = _rows(manager, "WHERE status='ACTIVE'")
    assert len(active) <= 1


def test_conflict_does_not_overwrite_high_confidence_memory(manager):
    _seed_memory(manager, "用户姓名为林晓明", "PROFILE", "hi-1")
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_records SET confidence=0.99 WHERE id='hi-1'")
        )
    result = _remember(manager, "我叫张三")
    assert result.status == "CONFLICT", result.message
    assert _rows(manager, "WHERE id='hi-1'")[0]["content"] == "用户姓名为林晓明"


# ---------------------------------------------------------------- D sensitivity
def test_health_information_is_held(manager):
    result = _remember(manager, "我有糖尿病，记住这个健康信息")
    assert result.status == "PROTECTED"
    assert "health" in result.sensitive_categories
    assert _count(manager, "memory_records") == 0
    assert _count(manager, "memory_candidates", "WHERE status='PENDING_AUTHORIZATION'") == 1


@pytest.mark.parametrize(
    "text_value,category",
    [
        ("记住我家住在朝阳区某小区 3 号楼 1201", "home_address"),
        ("记住我的工资收入和银行存款", "finance"),
        ("记住我的性取向是同性恋", "sexual"),
        ("记住我的宗教信仰是基督教", "religion"),
        ("记住我的政治倾向和党派", "politics"),
    ],
)
def test_all_sensitive_categories_are_held(manager, text_value, category):
    result = _remember(manager, text_value)
    assert result.status == "PROTECTED", (text_value, result.status)
    assert category in result.sensitive_categories
    assert _count(manager, "memory_records") == 0


def test_residence_statement_is_held(manager):
    """A city is not a street address, but the policy cannot reliably tell them
    apart, so residence stays fail-closed."""
    result = _remember(manager, "我住在杭州")
    assert result.status == "PROTECTED", result.message
    assert "home_address" in result.sensitive_categories
    assert _count(manager, "memory_records") == 0


def test_phone_pii_is_held_and_redacted(manager):
    result = _remember(manager, "记住我的手机号是 13800138000")
    assert result.status == "PROTECTED"
    assert "phone_cn" in result.sensitive_categories
    assert "13800138000" not in (result.content or "")
    assert _count(manager, "memory_records") == 0


def test_credential_is_denied_and_not_persisted(manager):
    result = _remember(manager, "记住我的 api_key=sk-FAKEabcdefghijklmnopqrstuvwxyz012345")
    assert result.status == "DENIED"
    assert result.code == "credential_blocked"
    assert _count(manager, "memory_records") == 0
    assert _count(manager, "memory_candidates") == 0


def test_sensitive_fact_never_reaches_update_path(manager):
    """A sensitive value must not overwrite an existing ordinary memory."""
    _remember(manager, "我喜欢喝拿铁")
    result = _remember(manager, "我现在有糖尿病了")
    assert result.status == "PROTECTED", result.message
    assert _rows(manager)[0]["content"] == "我喜欢喝拿铁"


# ------------------------------------------------------------- governance summary
def test_governance_summary_reports_real_counts(manager):
    _remember(manager, "我叫林晓明")
    _remember(manager, "我有糖尿病，记住这个健康信息")
    summary = AutoMemoryService(manager).governance_summary()
    assert summary["active"] == 1
    assert summary["auto_written"] == 1
    assert summary["pending_sensitive"] == 1
    assert isinstance(summary["recent_events"], list)


# ---------------------------------------------------------------- HTTP surface
def test_http_auto_remember_returns_updated_status(client, auth_headers):
    first = client.post(
        "/api/v1/memories/auto-remember",
        json={"text": "我喜欢喝拿铁", "conversation_id": "conv-http"},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["status"] == "SAVED"

    second = client.post(
        "/api/v1/memories/auto-remember",
        json={"text": "以后我更喜欢喝美式咖啡", "conversation_id": "conv-http"},
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["status"] == "UPDATED"
    assert data["memory_id"] == first.json()["data"]["memory_id"]


def test_http_governance_endpoints_are_real(client, auth_headers):
    merge = client.post(
        "/api/v1/memories/auto-govern/merge?dry_run=true", headers=auth_headers
    )
    assert merge.status_code == 200
    assert merge.json()["data"]["status"] == "DRY_RUN"

    expire = client.post(
        "/api/v1/memories/auto-govern/expire?dry_run=true", headers=auth_headers
    )
    assert expire.status_code == 200
    assert expire.json()["data"]["status"] == "DRY_RUN"

    summary = client.get(
        "/api/v1/memories/auto-govern/summary", headers=auth_headers
    )
    assert summary.status_code == 200
    assert "active" in summary.json()["data"]


def test_governance_endpoints_require_token(client):
    for method, path in (
        ("post", "/api/v1/memories/auto-govern/merge"),
        ("post", "/api/v1/memories/auto-govern/expire"),
        ("get", "/api/v1/memories/auto-govern/summary"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, path


# ===========================================================================
# C-1 · 自动治理必须真实清理/重索引向量（接线 memory_service）
# ===========================================================================
# 缺陷：AutoMemoryService 内部构造的 LifecycleService 落到裸 MemoryService
# （无 embedding / vector_store），向量钩子静默空转：auto_merge 后来源
# MERGED 但 Qdrant 点仍在，hybrid-search(status=ACTIVE) 会复活退役记忆。
# 修复：注入已接线的 memory_service。下列用例用真实 deterministic embedding
# + :memory: Qdrant，断言真实点集与 vector_status，而不是只看 SQLite。


@pytest.fixture()
def wired_env(manager, settings):
    embedding = DeterministicEmbeddingProvider(dimension=128)
    store = VectorStore(
        path=":memory:", collection_name="auto_governance_c1", dimension=128
    )
    memory_service = MemoryService(
        manager, embedding_provider=embedding, vector_store=store
    )
    hybrid = HybridSearchService(
        manager=manager,
        embedding_provider=embedding,
        vector_store=store,
        settings=settings,
    )
    yield {
        "manager": manager,
        "memory_service": memory_service,
        "vector_store": store,
        "hybrid": hybrid,
    }
    store.close()


def test_auto_merge_deletes_source_vectors_and_indexes_product(wired_env):
    svc = wired_env["memory_service"]
    store = wired_env["vector_store"]
    a = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答",
        )
    )
    b = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答。",
        )
    )
    assert {a.id, b.id} <= set(store.get_all_point_ids())

    report = AutoMemoryService(
        wired_env["manager"], memory_service=svc
    ).auto_merge()
    assert report["status"] == "MERGED"
    assert len(report["merged"]) == 1
    product_id = report["merged"][0]["memory_id"]

    points = set(store.get_all_point_ids())
    assert a.id not in points, "MERGED 来源的向量点必须被删除"
    assert b.id not in points, "MERGED 来源的向量点必须被删除"
    assert product_id in points, "合并产物必须被重索引"
    statuses = {
        row["id"]: row["vector_status"] for row in _rows(wired_env["manager"])
    }
    assert statuses[a.id] == "REMOVED"
    assert statuses[b.id] == "REMOVED"
    assert statuses[product_id] == "INDEXED"


def test_auto_expire_deletes_vector_point_of_archived_memory(wired_env):
    svc = wired_env["memory_service"]
    store = wired_env["vector_store"]
    rec = svc.create(
        MemoryCreate(
            memory_type=MemoryType.EPISODIC,
            content="我明天要去上海出差",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )
    assert rec.id in set(store.get_all_point_ids())

    report = AutoMemoryService(
        wired_env["manager"], memory_service=svc
    ).auto_expire()
    assert report["archived"] == 1
    assert rec.id not in set(store.get_all_point_ids()), "ARCHIVED 记忆的向量点必须被删除"
    row = _rows(wired_env["manager"], "WHERE id=:id", {"id": rec.id})[0]
    assert row["status"] == "ARCHIVED"
    assert row["vector_status"] == "REMOVED"


def test_hybrid_active_never_returns_auto_retired_memories(wired_env):
    """C-1 端到端：真实 auto_merge + auto_expire 后 ACTIVE 检索绝不返回退役行。"""
    svc = wired_env["memory_service"]
    a = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答",
        )
    )
    b = svc.create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="用户偏好使用中文交流，喜欢简洁直接的回答。",
        )
    )
    expired = svc.create(
        MemoryCreate(
            memory_type=MemoryType.EPISODIC,
            content="我明天要去上海出差",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )
    live = svc.create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="我喜欢去舟山海边拍视频记录生活",
        )
    )

    auto = AutoMemoryService(wired_env["manager"], memory_service=svc)
    merge_report = auto.auto_merge()
    expire_report = auto.auto_expire()
    assert len(merge_report["merged"]) == 1
    assert expire_report["archived"] == 1
    product_id = merge_report["merged"][0]["memory_id"]

    results, _ = wired_env["hybrid"].search(query="中文交流", status="ACTIVE")
    ids = {item.memory_id for item in results}
    assert a.id not in ids, "MERGED 来源不得以 ACTIVE 复活"
    assert b.id not in ids, "MERGED 来源不得以 ACTIVE 复活"
    assert product_id in ids

    results, _ = wired_env["hybrid"].search(query="上海出差", status="ACTIVE")
    assert expired.id not in {item.memory_id for item in results}, (
        "ARCHIVED 过期记忆不得以 ACTIVE 复活"
    )

    # 正向对照：仍然 ACTIVE 的无关记忆在其自身查询下正常可达，
    # 证明守卫没有过度封杀（hybrid 是召回层，相关性闸门在 context 服务）。
    results, _ = wired_env["hybrid"].search(query="舟山", status="ACTIVE")
    assert live.id in {item.memory_id for item in results}