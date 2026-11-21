"""JEA-01 auto-memory tests on a real isolated SQLite database."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from jarvismind.services.auto_memory import AutoMemoryService, detect_memory_intent
from jarvismind.services.approval_service import ApprovalService

TS = "2026-01-01T00:00:00+00:00"


def _count(manager, table, where="", params=None):
    with manager.session() as session:
        return int(
            session.execute(
                text(f"SELECT COUNT(*) FROM {table} {where}"), params or {}
            ).scalar()
            or 0
        )


def _remember(manager, text_value, conversation_id="conv-auto"):
    return AutoMemoryService(manager).auto_remember(
        text_value=text_value,
        conversation_id=conversation_id,
        message_id="msg-1",
        turn_id="turn-1",
    )


def _seed_memory(manager, content, memory_type="SEMANTIC"):
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records("
                "id, memory_type, content, importance, confidence, status, "
                "created_at, updated_at, source_id, metadata_json, search_text"
                ") VALUES (:id, :t, :c, 5.0, 0.9, 'ACTIVE', :now, :now, NULL, NULL, :c)"
            ),
            {
                "id": "seed-1",
                "t": memory_type,
                "c": content,
                "now": TS,
            },
        )


def test_intent_parser_handles_explicit_requests():
    assert detect_memory_intent("我叫梅凯龙，记住我的名字").fact == "我叫梅凯龙"
    assert detect_memory_intent("帮我记住我喜欢喝美式咖啡").fact == "我喜欢喝美式咖啡"
    assert detect_memory_intent("以后记得我的职业是软件工程师").fact == "我的职业是软件工程师"
    assert detect_memory_intent("更新我的个人信息：我长期使用 Windows 10").fact == (
        "我长期使用 Windows 10"
    )
    assert detect_memory_intent("不要记住我的密码").is_memory is False
    assert detect_memory_intent("今天天气不错").is_memory is False


def test_name_is_auto_saved_through_approval_service(manager):
    result = _remember(manager, "我叫梅凯龙，记住我的名字")
    assert result.status == "SAVED", result.message
    assert result.memory_id
    assert result.candidate_id
    assert result.content == "我叫梅凯龙"
    with manager.session() as session:
        memory = session.execute(
            text("SELECT content, source_candidate_id FROM memory_records WHERE id=:id"),
            {"id": result.memory_id},
        ).mappings().one()
        candidate = session.execute(
            text("SELECT status, final_result FROM memory_candidates WHERE id=:id"),
            {"id": result.candidate_id},
        ).mappings().one()
    assert memory["content"] == "我叫梅凯龙"
    assert memory["source_candidate_id"] == result.candidate_id
    assert candidate["status"] == "APPROVED"
    assert candidate["final_result"] == "MANUALLY_APPROVED"
    with manager.session() as session:
        meta = session.execute(
            text("SELECT metadata_json FROM memory_records WHERE id=:id"),
            {"id": result.memory_id},
        ).scalar()
    assert json.loads(meta)["engine"] == "auto-memory"


def test_occupation_device_and_preference_auto_save(manager):
    for text_value in (
        "记住我的职业是软件工程师",
        "记住我长期使用 Windows 10 和 iPhone",
        "记住我喜欢简洁直接的沟通方式",
    ):
        result = _remember(manager, text_value)
        assert result.status == "SAVED", (text_value, result.message)
    assert _count(manager, "memory_records") == 3


def test_health_information_is_held_not_written(manager):
    result = _remember(manager, "我有糖尿病，记住这个健康信息")
    assert result.status == "PROTECTED", result.message
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


def test_phone_pii_is_held_and_redacted(manager):
    result = _remember(manager, "记住我的手机号是 13800138000")
    assert result.status == "PROTECTED"
    assert "phone_cn" in result.sensitive_categories
    assert "13800138000" not in (result.content or "[REDACTED]")
    assert _count(manager, "memory_records") == 0


def test_credential_is_denied_and_not_persisted(manager):
    result = _remember(manager, "记住我的 api_key=sk-FAKEabcdefghijklmnopqrstuvwxyz012345")
    assert result.status == "DENIED"
    assert result.code == "credential_blocked"
    assert _count(manager, "memory_records") == 0
    assert _count(manager, "memory_candidates") == 0


def test_duplicate_does_not_write_twice(manager):
    first = _remember(manager, "我叫梅凯龙，记住我的名字")
    second = _remember(manager, "我叫梅凯龙，记住我的名字")
    assert first.status == "SAVED"
    assert second.status == "DUPLICATE"
    assert _count(manager, "memory_records") == 1


def test_conflict_is_held_for_review(manager):
    """A value difference WITHOUT an explicit replacement marker stays pending.

    "工程师" vs "产品经理" are both plausible; JME must not guess which one is
    current just because the sentence is newer.
    """
    _seed_memory(manager, "我的职业是软件工程师", "PROFILE")
    result = _remember(manager, "记住我的职业是产品经理")
    assert result.status == "CONFLICT", result.message
    assert result.candidate_id
    assert _count(manager, "memory_records") == 1
    with manager.session() as session:
        row = session.execute(
            text("SELECT status, conflicts_with FROM memory_candidates WHERE id=:id"),
            {"id": result.candidate_id},
        ).mappings().one()
    assert row["status"] == "PENDING"
    assert row["conflicts_with"] == "seed-1"


def test_write_failure_is_not_reported_as_saved(manager, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(ApprovalService, "review", boom)
    result = _remember(manager, "记住我喜欢喝茶")
    assert result.status == "FAILED"
    assert result.code == "write_failed"
    assert result.memory_id is None
    assert _count(manager, "memory_records") == 0


def test_no_intent_is_not_written(manager):
    result = _remember(manager, "今天天气不错")
    assert result.status == "NO_INTENT"
    assert _count(manager, "memory_records") == 0


def test_auto_written_memory_can_be_forgotten(manager):
    from jarvismind.services.forget_service import ForgetApprovalService

    saved = _remember(manager, "我叫梅凯龙，记住我的名字")
    assert saved.status == "SAVED" and saved.memory_id
    service = ForgetApprovalService(manager)
    created, token, _ = service.create_request(saved.memory_id, reason="device test")
    result = service.approve(created.id, confirmation_token=token)
    assert result.memory_forgotten is True
    with manager.session() as session:
        row = session.execute(
            text("SELECT status FROM memory_records WHERE id=:id"),
            {"id": saved.memory_id},
        ).mappings().one()
    assert row["status"] == "DELETED"


def test_http_auto_remember_receipt(client, auth_headers):
    response = client.post(
        "/api/v1/memories/auto-remember",
        json={
            "text": "我叫梅凯龙，记住我的名字",
            "conversation_id": "conv-http",
            "message_id": "msg-http",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "SAVED"
    assert data["memory_id"]
    assert data["content"] == "我叫梅凯龙"
    assert data["sensitive_categories"] == []
