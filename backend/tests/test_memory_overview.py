"""JME-MEMORY-OVERVIEW-01 · Memory Overview service and API tests.

The overview is READ-ONLY. These tests prove that:

* it reads real ACTIVE ``memory_records`` and never invents content;
* every rendered excerpt is traceable to a live memory id;
* DELETED / expired / credential / PII / personal-sensitive rows are excluded;
* conflicts are surfaced without picking a winner;
* refreshing after a write reflects the change, and forgetting removes it;
* the API requires the shared token and never mutates anything.

All fixtures use the shared in-memory test database from ``conftest.py``.
"""
from __future__ import annotations

import json

import pytest

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.models import MemoryRecord
from jarvismind.services.memory_overview import (
    CATEGORY_ORDER,
    MemoryOverviewService,
    category_keys_for,
    sensitivity_reasons,
)

OVERVIEW_URL = "/api/v1/memories/overview"


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
    memory_id: str | None = None,
) -> str:
    """Insert a memory row directly for read-path tests.

    The overview is a read-only consumer, so its tests seed rows directly in
    the test database instead of driving the approval pipeline. Writes here
    never touch production data.

    ``importance`` defaults to 7.0 so the row also qualifies for "recent
    focus", which additionally requires ``RECENT_MIN_IMPORTANCE``.
    """
    import uuid

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
                updated_at=now,
                expires_at=expires_at,
                metadata_json=metadata_json,
            )
        )
        session.commit()
    return mid


def _insert_legacy(manager, *, content: str, **overrides) -> str:
    """Insert a row with raw SQL, bypassing the ORM write gate.

    The write path already refuses credential-shaped content, so such text can
    only exist in rows written before that gate. This helper simulates exactly
    that legacy state to prove the READ path also excludes it.
    """
    import uuid

    from sqlalchemy import text

    now = utc_now_iso()
    mid = overrides.pop("memory_id", None) or uuid.uuid4().hex
    values = {
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
    }
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records "
                "(id, memory_type, content, summary, importance, confidence, status, "
                " created_at, updated_at, expires_at, metadata_json) "
                "VALUES (:id, :memory_type, :content, :summary, :importance, :confidence, "
                " :status, :created_at, :updated_at, :expires_at, :metadata_json)"
            ),
            values,
        )
        session.commit()
    return mid


def _by_key(payload, key: str):
    return next(item for item in payload["categories"] if item["key"] == key)


# ------------------------------------------------------------------ 1. reading
def test_reads_real_active_memories(manager):
    mid = _insert(manager, content="用户喜欢喝手冲咖啡")
    payload = MemoryOverviewService(manager).build()

    assert payload["eligible_count"] == 1
    interests = _by_key(payload, "interests")
    assert interests["available"] is True
    assert [item["memory_id"] for item in interests["items"]] == [mid]
    # Extractive: the excerpt is the stored text, not a paraphrase.
    assert "手冲咖啡" in interests["items"][0]["excerpt"]


def test_every_item_is_traceable_to_a_live_active_memory(manager):
    ids = {
        _insert(manager, content="用户在物流行业负责质管工作"),
        _insert(manager, content="用户在做一个叫 JarvisMind 的项目", memory_type="PROJECT"),
        _insert(manager, content="用户喜欢跑步"),
    }
    payload = MemoryOverviewService(manager).build()
    rendered = {
        item["memory_id"] for category in payload["categories"] for item in category["items"]
    }
    assert rendered <= ids
    with manager.session() as session:
        for memory_id in rendered:
            row = session.get(MemoryRecord, memory_id)
            assert row is not None and row.status == "ACTIVE"


# --------------------------------------------------------------- 2. categories
def test_multi_category_mapping(manager):
    _insert(manager, content="用户姓名为测试用户", memory_type="PROFILE")
    _insert(manager, content="用户在快运行业负责理赔", memory_type="SEMANTIC")
    _insert(manager, content="用户喜欢摄影", memory_type="SEMANTIC")
    _insert(manager, content="用户在做记忆项目", memory_type="PROJECT")
    _insert(manager, content="2026-11-14 用户导出全部记忆", memory_type="EPISODIC")
    _insert(manager, content="用户的朋友叫小王", memory_type="SEMANTIC")

    payload = MemoryOverviewService(manager).build()
    available = {item["key"] for item in payload["categories"] if item["available"]}
    # The six task-book categories are all populated; "recent" additionally
    # needs a fresh timestamp plus above-threshold importance, which the
    # helper supplies by default.
    assert available == set(CATEGORY_ORDER)


def test_category_membership_is_deterministic():
    assert "work" in category_keys_for("SEMANTIC", "用户在物流行业工作")
    assert "interests" in category_keys_for("SEMANTIC", "用户喜欢海边")
    assert "relationships" in category_keys_for("SEMANTIC", "用户的朋友住在杭州")
    assert "projects" in category_keys_for("PROJECT", "无关键词的内容")
    assert category_keys_for("REFERENCE", "无关键词的内容") == []


# ------------------------------------------------------------------ 3. empty
def test_empty_database_yields_no_fabricated_content(manager):
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert payload["excluded_count"] == 0
    for category in payload["categories"]:
        assert category["items"] == []
        assert category["summary"] == ""
        assert category["available"] is False


# ------------------------------------------------------------- 4. exclusions
def test_deleted_memory_is_excluded(manager):
    _insert(manager, content="用户喜欢跑步", status="DELETED")
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert all(not c["items"] for c in payload["categories"])


def test_merged_memory_is_excluded(manager):
    _insert(manager, content="用户喜欢跑步", status="MERGED")
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0


def test_expired_memory_is_excluded(manager):
    _insert(manager, content="用户临时偏好速溶咖啡", expires_at="2020-01-01T00:00:00+00:00")
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert payload["excluded_reasons"].get("expired") == 1


def test_blank_content_is_excluded(manager):
    _insert(manager, content="   ")
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert payload["excluded_reasons"].get("empty") == 1


# ------------------------------------------------------- 5. credential filter
@pytest.mark.parametrize(
    "content",
    [
        "用户的 api_key = SYNTHKEY0123456789ABCDEF",
        "用户的 GitHub Token: ghp_" "SYNTH0123456789ABCDEFGHIJ01234",
        "密码是 SynthPassw0rd",
        "-----BEGIN " "PRIVATE KEY-----\nSYNTHBODY\n-----END " "PRIVATE KEY-----",
        "用户的 token = SynthToken123456",
    ],
)
def test_credential_shaped_content_never_reaches_the_overview(manager, content):
    _insert_legacy(manager, content=content)
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert payload["excluded_reasons"].get("credential") == 1
    # And no fragment of it is echoed anywhere in the response.
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SynthPassw0rd" not in serialized
    assert "SYNTHKEY0123456789ABCDEF" not in serialized


def test_credential_inside_metadata_is_excluded(manager):
    _insert_legacy(
        manager,
        content="用户喜欢跑步",
        metadata_json=json.dumps({"note": "api_key = SYNTHKEY0123456789ABCDEF"}),
    )
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert payload["excluded_reasons"].get("credential") == 1


# ---------------------------------------------------- 6. personal sensitivity
@pytest.mark.parametrize(
    "content",
    [
        "用户有高血压，需要每天吃药",
        "用户的家庭住址在某个小区",
        "用户的年薪是某个数字",
        "用户的宗教信仰是某个宗教",
        "用户的政治立场属于某个党派",
        "用户关联的银行卡信息需要保密",
        "用户的手机号 13800138000",
    ],
)
def test_personal_sensitive_content_is_excluded(manager, content):
    _insert(manager, content=content)
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 0
    assert set(payload["excluded_reasons"]) & {"personal_sensitive", "pii"}


def test_sensitivity_reasons_are_value_free(manager):
    reasons = sensitivity_reasons("用户有高血压", None)
    assert reasons == ["personal_sensitive"]
    # Category names only: no echo of the source text.
    assert "高血压" not in json.dumps(reasons, ensure_ascii=False)


def test_ordinary_content_is_not_over_filtered(manager):
    _insert(manager, content="用户在同一家公司工作了三年")
    _insert(manager, content="用户喜欢在周末跑步")
    payload = MemoryOverviewService(manager).build()
    assert payload["eligible_count"] == 2
    assert payload["excluded_count"] == 0


# --------------------------------------------------------------- 7. conflicts
def test_different_names_are_flagged_without_choosing(manager):
    first = _insert(manager, content="用户姓名为张三", memory_type="PROFILE")
    second = _insert(manager, content="用户姓名是李四", memory_type="PROFILE")
    payload = MemoryOverviewService(manager).build()
    profile = _by_key(payload, "profile")

    assert profile["uncertain"] is True
    assert profile["conflicts"], "expected a name conflict to be reported"
    conflict = profile["conflicts"][0]
    assert set(conflict["memory_ids"]) == {first, second}
    # Both memories are still shown: no winner is chosen.
    assert {item["memory_id"] for item in profile["items"]} == {first, second}


def test_single_name_is_not_a_conflict(manager):
    _insert(manager, content="用户姓名为张三", memory_type="PROFILE")
    _insert(manager, content="用户希望被称呼为张三先生", memory_type="PROFILE")
    payload = MemoryOverviewService(manager).build()
    assert _by_key(payload, "profile")["uncertain"] is False


def test_unrelated_shared_words_do_not_create_a_conflict(manager):
    _insert(manager, content="用户姓名为张三", memory_type="PROFILE")
    _insert(manager, content="用户希望助手称呼自己为张三先生", memory_type="PROFILE")
    _insert(manager, content="用户希望回答简洁", memory_type="PROFILE")
    payload = MemoryOverviewService(manager).build()
    assert _by_key(payload, "profile")["uncertain"] is False


def test_project_status_disagreement_is_reported(manager):
    first = _insert(manager, content="记忆整理项目已完成", memory_type="PROJECT")
    second = _insert(manager, content="记忆整理项目进行中", memory_type="PROJECT")
    payload = MemoryOverviewService(manager).build()
    projects = _by_key(payload, "projects")
    assert projects["uncertain"] is True
    assert set(projects["conflicts"][0]["memory_ids"]) == {first, second}


# ----------------------------------------------------------------- 8. refresh
def test_new_memory_appears_on_refresh(manager):
    service = MemoryOverviewService(manager)
    before = service.build()
    assert before["eligible_count"] == 0

    _insert(manager, content="用户喜欢新的咖啡品牌")
    after = service.build()
    assert after["eligible_count"] == 1
    assert after["source_version"] != before["source_version"]


def test_updated_memory_changes_the_source_version(manager):
    memory_id = _insert(manager, content="用户喜欢咖啡")
    service = MemoryOverviewService(manager)
    before = service.build()

    with manager.session() as session:
        row = session.get(MemoryRecord, memory_id)
        row.content = "用户喜欢茶"
        row.updated_at = utc_now_iso()
        session.commit()

    after = service.build()
    assert after["source_version"] != before["source_version"]
    assert "茶" in _by_key(after, "interests")["items"][0]["excerpt"]


def test_forgotten_memory_disappears(manager):
    memory_id = _insert(manager, content="用户喜欢咖啡")
    service = MemoryOverviewService(manager)
    assert service.build()["eligible_count"] == 1

    with manager.session() as session:
        row = session.get(MemoryRecord, memory_id)
        row.status = "DELETED"
        session.commit()

    after = service.build()
    assert after["eligible_count"] == 0
    assert all(not c["items"] for c in after["categories"])


# ------------------------------------------------------------ 9. degradation
def test_failing_enricher_falls_back_to_deterministic_output(manager):
    _insert(manager, content="用户喜欢跑步")

    def boom(_payload):
        raise RuntimeError("synthetic model failure")

    payload = MemoryOverviewService(manager, enricher=boom).build()
    assert payload["ai_enhanced"] is False
    assert payload["eligible_count"] == 1
    assert _by_key(payload, "interests")["available"] is True


def test_successful_enricher_marks_the_payload(manager):
    _insert(manager, content="用户喜欢跑步")

    def decorate(payload):
        payload = dict(payload)
        payload["categories"] = [
            {**category, "summary": category["summary"]} for category in payload["categories"]
        ]
        payload["ai_enhanced"] = True
        return payload

    # Enrichment is opt-in: a plain build() must never spend money on a model.
    plain = MemoryOverviewService(manager, enricher=decorate).build()
    assert plain["ai_enhanced"] is False

    enriched = MemoryOverviewService(manager, enricher=decorate).build(enrich=True)
    assert enriched["ai_enhanced"] is True


def test_enrich_flag_is_off_by_default_on_the_api(client, auth_headers, manager):
    _insert(manager, content="用户喜欢跑步")

    # A normal GET must serve the deterministic payload without requiring a model.
    response = client.get("/api/v1/memories/overview", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["ai_enhanced"] is False


def test_enrich_query_is_accepted(client, auth_headers):
    response = client.get(
        "/api/v1/memories/overview", params={"enrich": "true"}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    # No model configured in tests, so enrichment silently degrades.
    assert response.json()["ai_enhanced"] is False


# -------------------------------------------------------------------- 10. API
def test_api_requires_token(client):
    response = client.get(OVERVIEW_URL)
    assert response.status_code == 401


def test_api_returns_the_overview(client, auth_headers, manager):
    memory_id = _insert(manager, content="用户喜欢跑步")
    response = client.get(OVERVIEW_URL, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    rendered = {
        item["memory_id"]
        for category in body["categories"]
        for item in category["items"]
    }
    assert memory_id in rendered
    assert body["source_version"]
    assert body["generated_at"]


def test_api_is_read_only(client, auth_headers, manager):
    _insert(manager, content="用户喜欢跑步")
    with manager.session() as session:
        before = session.execute(
            MemoryRecord.__table__.select().order_by(MemoryRecord.id)
        ).all()

    assert client.get(OVERVIEW_URL, headers=auth_headers).status_code == 200
    assert client.get(OVERVIEW_URL, headers=auth_headers).status_code == 200

    with manager.session() as session:
        after = session.execute(
            MemoryRecord.__table__.select().order_by(MemoryRecord.id)
        ).all()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]


def test_api_rejects_write_methods(client, auth_headers):
    """The overview route is GET-only.

    PostgreSQL-style 405 is not guaranteed here because the shared CRUD router
    owns other verbs on sibling paths; what must hold is that no write method
    succeeds (200/201/204) and no memory row is created.
    """
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(OVERVIEW_URL, headers=auth_headers)
        assert response.status_code >= 400, (method, response.status_code)
