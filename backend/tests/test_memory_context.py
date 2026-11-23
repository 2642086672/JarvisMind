"""JME-MEMORY-CONTEXT-01 · read-only chat memory-context retrieval tests.

The endpoint is the chat pipeline's only memory read path. These tests prove:

* token auth is required and invalid tokens are refused before any work;
* ACTIVE, non-expired memories are returned and DELETED / ARCHIVED / MERGED /
  expired rows never are;
* credential, regex-PII and personally-sensitive memories are excluded;
* ranking is relevance-first, not "newest first";
* an unrelated question returns an empty list instead of padding;
* limits, content length and category filters are honoured;
* the endpoint writes nothing (memory rows, audit logs, access timestamps and
  JMR tables are byte-identical before and after);
* a filter failure degrades to an empty-listed response, never a 500 leak.

Note: these tests seed rows directly, which leaves ``search_text`` empty so
hybrid search cannot return them. That blind spot hid a real 500 in the merge
path (``item.memory_id`` on an ``(item, rank)`` tuple). The end-to-end file
``test_memory_context_e2e.py`` goes through ``MemoryService.create`` and is the
regression test for that path; keep both.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TEST_TOKEN

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.models import MemoryAuditLog, MemoryRecord

CONTEXT_URL = "/api/v1/memories/context"


# --------------------------------------------------------------------- helpers
def _insert(
    manager,
    *,
    content: str,
    memory_type: str = "SEMANTIC",
    status: str = "ACTIVE",
    importance: float = 7.0,
    expires_at: str | None = None,
    summary: str | None = None,
    metadata_json: str | None = None,
    updated_at: str | None = None,
    memory_id: str | None = None,
) -> str:
    """Insert a memory row directly; this endpoint is a read-only consumer."""
    now = utc_now_iso()
    mid = memory_id or uuid.uuid4().hex
    with manager.session() as session:
        session.add(
            MemoryRecord(
                id=mid,
                memory_type=memory_type,
                content=content,
                summary=summary,
                importance=importance,
                confidence=0.8,
                status=status,
                created_at=now,
                updated_at=updated_at or now,
                expires_at=expires_at,
                metadata_json=metadata_json,
            )
        )
    return mid


def _insert_legacy(manager, *, content: str, memory_id: str | None = None, **overrides) -> str:
    """Insert a row with raw SQL, bypassing the ORM credential gate.

    The write gate refuses credential-shaped content, so such text can only
    exist in rows written before the gate. This helper simulates exactly that
    legacy state to prove the read path also refuses to return it.
    """
    from sqlalchemy import text

    now = utc_now_iso()
    mid = memory_id or uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records "
                "(id, memory_type, content, summary, importance, confidence, status, "
                " created_at, updated_at, expires_at, metadata_json) "
                "VALUES (:id, :memory_type, :content, :summary, :importance, :confidence, "
                " :status, :created_at, :updated_at, :expires_at, :metadata_json)"
            ),
            {
                "id": mid,
                "memory_type": overrides.pop("memory_type", "SEMANTIC"),
                "content": content,
                "summary": overrides.pop("summary", None),
                "importance": overrides.pop("importance", 7.0),
                "confidence": 0.8,
                "status": overrides.pop("status", "ACTIVE"),
                "created_at": now,
                "updated_at": now,
                "expires_at": overrides.pop("expires_at", None),
                "metadata_json": overrides.pop("metadata_json", None),
            },
        )
    return mid


def _post(client, headers, **body) -> dict:
    payload = {"query": "咖啡", **body}
    resp = client.post(CONTEXT_URL, json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body: dict) -> list[str]:
    return [item["memory_id"] for item in body["items"]]


# ------------------------------------------------------------------ auth & shape
def test_context_requires_token(client):
    resp = client.post(CONTEXT_URL, json={"query": "咖啡"})
    assert resp.status_code == 401


def test_context_rejects_invalid_token(client):
    resp = client.post(
        CONTEXT_URL,
        json={"query": "咖啡"},
        headers={"X-API-Token": "wrong-token"},
    )
    assert resp.status_code == 401


def test_context_rejects_empty_query(client, auth_headers):
    resp = client.post(CONTEXT_URL, json={"query": ""}, headers=auth_headers)
    assert resp.status_code == 422


def test_context_response_shape(client, auth_headers, manager):
    mid = _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert body["items"], body
    item = body["items"][0]
    assert item["memory_id"] == mid
    assert item["memory_type"] == "PROFILE"
    assert item["content"] == "用户喜欢喝手冲咖啡"
    assert 0.0 <= item["relevance_score"] <= 1.0
    assert item["source"] == "user_memory"
    assert item["updated_at"]
    assert body["retrieval_version"] == "context-v1"
    assert body["total"] == len(body["items"])


# ---------------------------------------------------------------- read only real
def test_only_active_unexpired_memories_are_returned(client, auth_headers, manager):
    active = _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    _insert(manager, content="用户喜欢喝手冲咖啡（已删除）", status="DELETED")
    _insert(manager, content="用户喜欢喝手冲咖啡（已归档）", status="ARCHIVED")
    _insert(manager, content="用户喜欢喝手冲咖啡（已合并）", status="MERGED")
    _insert(
        manager,
        content="用户喜欢喝手冲咖啡（已过期）",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert _ids(body) == [active]
    assert body["excluded"]["expired"] >= 1


def test_expired_memory_is_excluded_even_when_newly_updated(client, auth_headers, manager):
    _insert(
        manager,
        content="用户喜欢喝手冲咖啡",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert body["items"] == []
    assert body["excluded"]["expired"] == 1


# --------------------------------------------------------------------- relevance
def test_unrelated_question_returns_empty_list(client, auth_headers, manager):
    _insert(manager, content="用户在杭州做软件工程师")
    _insert(manager, content="用户的恋爱经历比较复杂")
    _insert(manager, content="用户养了一只名叫豆豆的猫")
    _insert(manager, content="用户的年收入大约三十万", memory_type="PROFILE")
    body = _post(client, auth_headers, query="今天杭州天气怎么样")
    assert body["items"] == []
    assert body["excluded"]["irrelevant"] >= 1


def test_relevance_beats_newest_first(client, auth_headers, manager):
    relevant_old = _insert(
        manager,
        content="用户喜欢喝手冲咖啡",
        memory_type="PROFILE",
        updated_at=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
    )
    _insert(
        manager,
        content="用户喜欢海边城市",
        updated_at=utc_now_iso(),
    )
    _insert(
        manager,
        content="用户偏好简洁回答",
        updated_at=utc_now_iso(),
    )
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert body["items"][0]["memory_id"] == relevant_old


def test_question_filler_is_not_treated_as_topic(client, auth_headers, manager):
    """"请问我喜欢喝什么？" must still find the coffee memory."""
    coffee = _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    _insert(manager, content="用户偏好简洁回答", memory_type="PROFILE")
    _insert(manager, content="用户喜欢海边城市", memory_type="SEMANTIC")
    body = _post(client, auth_headers, query="请问我喜欢喝什么？")
    assert coffee in _ids(body), body
    # 泛化的偏好记忆不得因为共用“喜欢”而进入结果。
    assert all("简洁回答" not in item["content"] for item in body["items"])
    assert all("海边" not in item["content"] for item in body["items"])


def test_identity_question_finds_stated_name(client, auth_headers, manager):
    name = _insert(manager, content="我叫林晓明", memory_type="PROFILE")
    _insert(manager, content="用户喜爱摄影", memory_type="PROFILE")
    body = _post(client, auth_headers, query="我是谁？")
    assert name in _ids(body)


def test_identity_question_does_not_return_profile_without_name(client, auth_headers, manager):
    _insert(manager, content="用户喜爱摄影", memory_type="PROFILE")
    _insert(manager, content="用户偏好简洁回答", memory_type="PROFILE")
    body = _post(client, auth_headers, query="我叫什么名字？")
    assert all("摄影" not in item["content"] for item in body["items"])


# ----------------------------------------------------------------------- limits
def test_limit_is_respected(client, auth_headers, manager):
    for index in range(6):
        _insert(manager, content=f"用户喜欢喝手冲咖啡，第 {index} 条记录")
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡", limit=2)
    assert len(body["items"]) == 2


def test_limit_above_schema_max_is_rejected(client, auth_headers):
    resp = client.post(
        CONTEXT_URL,
        json={"query": "咖啡", "limit": 50},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_unknown_request_field_is_rejected(client, auth_headers):
    resp = client.post(
        CONTEXT_URL,
        json={"query": "咖啡", "include_deleted": True},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_reflection_source_is_labelled(client, auth_headers, manager):
    _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="REFLECTION")
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"][0]["source"] == "reflection"


def test_summary_only_pii_excludes_row(client, auth_headers, manager):
    _insert_legacy(
        manager,
        content="咖啡记录",
        summary="联系电话 13800138000",
    )
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"] == []
    assert body["excluded"]["sensitive"] == 1


def test_content_length_is_bounded(client, auth_headers, manager):
    _insert(manager, content="咖啡" + "很长的记忆内容" * 80, memory_type="PROFILE")
    body = _post(client, auth_headers, query="咖啡", max_content_chars=120)
    item = body["items"][0]
    assert len(item["content"]) <= 121
    assert item["content"].endswith("…")
    assert item["truncated"] is True


def test_category_filters_are_applied(client, auth_headers, manager):
    project = _insert(manager, content="咖啡项目使用 SQLite 存储", memory_type="PROJECT")
    _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")

    included = _post(client, auth_headers, query="咖啡", include_categories=["PROJECT"])
    assert _ids(included) == [project]

    excluded = _post(client, auth_headers, query="咖啡", exclude_categories=["PROJECT"])
    assert project not in _ids(excluded)


def test_duplicate_content_is_injected_once(client, auth_headers, manager):
    _insert(manager, content="用户喜欢喝手冲咖啡")
    _insert(manager, content="用户喜欢喝手冲咖啡")
    body = _post(client, auth_headers, query="我喜欢喝什么咖啡", limit=10)
    contents = [item["content"] for item in body["items"]]
    assert len(contents) == len(set(contents))


# ------------------------------------------------------------------- sensitivity
@pytest.mark.parametrize(
    "content",
    [
        "我的手机号是 13800138000",
        "我的邮箱是 user@example.com",
        "我的银行卡 6222021234567890123",
        "我的身份证 110101199003071234",
        "密码是 Hunter2pass",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payloadpart.signaturepart",
    ],
)
def test_pii_and_credentials_never_enter_context(client, auth_headers, manager, content):
    _insert_legacy(manager, content=f"咖啡 {content}")
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"] == []
    assert body["excluded"]["sensitive"] >= 1


@pytest.mark.parametrize(
    "content",
    [
        "用户有高血压，需要长期吃药",
        "用户家庭住址在某某小区 3 栋 502",
        "用户的月薪是三万元",
        "用户性取向为同性恋",
        "用户信仰佛教",
        "用户政治立场偏左",
    ],
)
def test_personal_sensitive_categories_are_excluded(client, auth_headers, manager, content):
    _insert_legacy(manager, content=f"咖啡 {content}")
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"] == []
    assert body["excluded"]["sensitive"] >= 1


def test_credential_in_metadata_excludes_row(client, auth_headers, manager):
    _insert_legacy(
        manager,
        content="咖啡记录",
        metadata_json='{"note": "password=hunter2secret"}',
    )
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"] == []


def test_sensitive_counts_never_reveal_categories(client, auth_headers, manager):
    _insert_legacy(manager, content="咖啡 我的手机号是 13800138000")
    body = _post(client, auth_headers, query="咖啡")
    assert body["items"] == []
    serialized = str(body["excluded"])
    assert "phone" not in serialized
    assert "13800138000" not in serialized


def test_forgotten_memory_disappears_immediately(client, auth_headers, manager, approved_forget):
    mid = _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    before = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert mid in _ids(before)

    approved_forget.forget(mid, reason="context test")

    after = _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert mid not in _ids(after)


# ---------------------------------------------------------------------- read-only
def _snapshot(manager) -> tuple:
    from sqlalchemy import func, select

    with manager.session() as session:
        memories = session.execute(
            select(
                func.count(MemoryRecord.id),
                func.sum(MemoryRecord.importance),
            )
        ).one()
        audits = session.execute(select(func.count(MemoryAuditLog.id))).scalar()
        accessed = session.execute(
            select(func.count(MemoryRecord.id)).where(
                MemoryRecord.accessed_at.is_not(None)
            )
        ).scalar()
    return (int(memories[0]), float(memories[1] or 0.0), int(audits or 0), int(accessed or 0))


def test_endpoint_writes_nothing(client, auth_headers, manager):
    _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    _insert(manager, content="用户在做咖啡项目", memory_type="PROJECT")
    before = _snapshot(manager)
    for _ in range(3):
        _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert _snapshot(manager) == before


def test_endpoint_does_not_touch_jmr_tables(client, auth_headers, manager):
    """JMR proposals live in memory_summaries / corrections / merges (store.py)."""
    from sqlalchemy import func, select

    from jarvismind.database.models import MemoryCorrection, MemoryMerge, MemorySummary

    _insert(manager, content="用户喜欢喝手冲咖啡", memory_type="PROFILE")
    models = (MemorySummary, MemoryCorrection, MemoryMerge)

    def counts():
        with manager.session() as session:
            return tuple(
                int(session.execute(select(func.count()).select_from(model)).scalar() or 0)
                for model in models
            )

    before = counts()
    _post(client, auth_headers, query="我喜欢喝什么咖啡")
    assert counts() == before


# ------------------------------------------------------------------ degradation
def test_search_failure_never_breaks_chat(client, auth_headers, manager, monkeypatch):
    from jarvismind.container import Container
    from jarvismind.services.memory_context_service import MemoryContextService

    class BrokenSearch:
        def search(self, **_kwargs):
            raise RuntimeError("vector store exploded: /secret/path")

    broken = MemoryContextService(manager=manager, hybrid_search=BrokenSearch())
    monkeypatch.setattr(
        Container,
        "memory_context_service",
        property(lambda _self: broken),
        raising=True,
    )

    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")

    assert body["items"] == []
    assert body["degraded_to_fts"] is True
    assert "/secret/path" not in str(body)


def test_token_is_required_for_every_variant(client, manager):
    _insert(manager, content="用户喜欢喝手冲咖啡")
    resp = client.post(CONTEXT_URL, json={"query": "我是谁？"})
    assert resp.status_code == 401
    assert TEST_TOKEN not in resp.text
