"""JW-03: protection of approved (authoritative) memories.

Task book §4 requires these proofs, each mapped to a group below:

  A. 普通接口不能绕过保护直接修改已审批记忆
  B. 未授权请求不能修改权威数据
  C. 合法审批流程可以正常修改
  D. 合法修正流程可以正常执行
  E. 合法遗忘流程可以正常执行
  F. 重复请求不会产生重复修改
  G. 并发修改不会导致数据覆盖
  H. 历史记忆数据不会因本次改造丢失
  I. JW-00 / JW-01 / JW-02 相关功能全部回归正常

Every case runs on the shared tmp manager via ``create_app``; no real database
and no network. An "approved" memory is produced by driving the REAL candidate
approval chain, never by writing ``source_candidate_id`` by hand - a hand-written
column would prove the test, not the product.
"""
from __future__ import annotations

import dataclasses
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from jarvismind.core.approved_protection import (
    IMMUTABLE_FIELDS,
    PROTECTED_FIELDS,
    ApprovedMemoryProtectedError,
    ImmutableFieldError,
    UnknownInternalScopeError,
    active_scopes,
    internal_write,
    is_approved,
    protected_changes,
)
from jarvismind.core.fingerprint import content_fingerprint
from jarvismind.database.models import MemoryCandidate, MemoryRecord
from jarvismind.main import create_app

HEADERS = {"X-API-Token": "test-token-0123456789"}


# --------------------------------------------------------------------- helpers
def _client(settings, manager, **overrides):
    return TestClient(
        create_app(dataclasses.replace(settings, **overrides), manager=manager)
    )


@pytest.fixture()
def jw03_client(settings, manager):
    """All switches ON so both the protected and the legacy paths are reachable."""
    with _client(
        settings,
        manager,
        direct_write_enabled=True,
        lifecycle_import_enabled=True,
        lifecycle_merge_enabled=True,
        lifecycle_summary_review_enabled=True,
        lifecycle_correction_resolve_enabled=True,
    ) as client:
        yield client


@pytest.fixture()
def jw03_no_raise(settings, manager):
    """Server exceptions surface as 500 instead of propagating out of TestClient."""
    with TestClient(
        create_app(
            dataclasses.replace(
                settings,
                direct_write_enabled=True,
                lifecycle_import_enabled=True,
                lifecycle_merge_enabled=True,
                lifecycle_summary_review_enabled=True,
                lifecycle_correction_resolve_enabled=True,
            ),
            manager=manager,
        ),
        raise_server_exceptions=False,
    ) as client:
        yield client


def _seed_candidate(manager, settings, *, content, summary=None, cid=None):
    """Insert an approval-ready candidate with a valid content fingerprint."""
    cid = cid or uuid.uuid4().hex
    fingerprint = content_fingerprint(
        settings,
        content=content,
        summary=summary,
        metadata_json=None,
        decision_version=1,
    )
    with manager.session() as session:
        session.add(
            MemoryCandidate(
                id=cid,
                memory_type="SEMANTIC",
                content=content,
                summary=summary,
                importance=6.0,
                confidence=0.9,
                status="PENDING",
                extraction_engine="jw03-test",
                extracted_at="2026-11-16T00:00:00+00:00",
                scan_status="SCANNED",
                content_fingerprint=fingerprint,
                decision_version=1,
                message_id="jw03-msg",
                turn_id="jw03-turn",
            )
        )
    return cid


def _approved_memory(client, manager, settings, *, content, summary=None):
    """Create a memory through the real approval chain; return (memory_id, cid)."""
    cid = _seed_candidate(manager, settings, content=content, summary=summary)
    resp = client.post(
        f"/api/v1/memories/candidates/{cid}/approve", json={}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    with manager.session() as session:
        record = (
            session.query(MemoryRecord)
            .filter(MemoryRecord.source_candidate_id == cid)
            .one()
        )
        return record.id, cid


def _record(manager, memory_id):
    with manager.session() as session:
        return session.get(MemoryRecord, memory_id)


def _patch(client, memory_id, body, headers=None):
    return client.patch(
        f"/api/v1/memories/{memory_id}", json=body, headers=headers or HEADERS
    )


def _create_request(client, memory_id, changes, *, kind="UPDATE", reason=None):
    body = {"proposed_changes": changes, "request_kind": kind}
    if reason:
        body["reason"] = reason
    return client.post(
        f"/api/v1/memories/{memory_id}/modification-requests",
        json=body,
        headers=HEADERS,
    )


def _create_lifecycle_request(client, memory_id, kind, *, changes=None):
    """Create an ARCHIVE / RESTORE request.

    These kinds carry no ``proposed_changes`` - the kind IS the instruction, and
    the service rejects any field other than ``status`` / ``expires_at`` for
    them. Sending an empty object would trip the schema's min-length rule, so
    the body omits the key entirely.
    """
    body = {"request_kind": kind}
    if changes:
        body["proposed_changes"] = changes
    return client.post(
        f"/api/v1/memories/{memory_id}/modification-requests",
        json=body,
        headers=HEADERS,
    )


def _approved_update_request(client, memory_id, changes, *, reason=None):
    """Create an UPDATE request and return (request_id, token)."""
    resp = _create_request(client, memory_id, changes, reason=reason)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    return data["request"]["id"], data["confirmation_token"]


# ============================================================ A. 保护判定基础
def test_approved_predicate_requires_source_candidate_id():
    """The protection basis is exactly the server-issued approval proof."""

    class Row:
        def __init__(self, cid):
            self.source_candidate_id = cid

    assert is_approved(Row("abc")) is True
    assert is_approved(Row(None)) is False
    assert is_approved(Row("")) is False
    assert is_approved(Row("   ")) is False


def test_protected_field_set_covers_every_writer_column():
    """Every column the lifecycle writers touch is classified, explicitly."""
    # Columns written by memory_service / lifecycle_service / forget_service.
    written = {
        "content",
        "summary",
        "status",
        "expires_at",
        "metadata_json",
        "importance",
        "confidence",
        "forgotten_at",
        "forget_reason",
        "merged_into",
    }
    assert written <= PROTECTED_FIELDS
    # Derived bookkeeping must NOT be protected: the server owns it and blocking
    # it would break search_text sync and vector bookkeeping.
    assert not ({"search_text", "vector_status", "accessed_at"} & PROTECTED_FIELDS)
    # Provenance is immutable, not merely protected.
    assert {"source_candidate_id", "id", "created_at"} <= IMMUTABLE_FIELDS


def test_protected_changes_ignores_a_reasserted_value():
    """Re-sending the current status is a no-op, not a protected change."""

    class Row:
        status = "ACTIVE"
        content = "same"

    assert protected_changes({"status": "ACTIVE"}, record=Row()) == set()
    assert protected_changes({"status": "ARCHIVED"}, record=Row()) == {"status"}


def test_immutable_field_is_refused_even_inside_a_scope(manager):
    """A scope may authorise protected fields; it never authorises provenance."""
    from jarvismind.core.approved_protection import require_write_authority

    record = _record(manager, "nonexistent")
    assert record is None

    class Row:
        id = "m1"
        source_candidate_id = "cand-1"
        content = "x"

        def __repr__(self):
            return "<Row>"

    with internal_write("forget_approve"):
        with pytest.raises(ImmutableFieldError):
            require_write_authority(
                Row(), {"source_candidate_id": "other"}, operation="test"
            )


def test_unknown_internal_scope_reason_is_refused():
    """A typo must not silently grant authority."""
    with pytest.raises(UnknownInternalScopeError):
        with internal_write("totally_made_up_reason"):
            pass


def test_internal_scope_is_drained_on_exception():
    """A failing flow must not leave authority behind for the next request."""
    assert active_scopes() == ()
    with pytest.raises(RuntimeError):
        with internal_write("merge_confirm"):
            assert active_scopes() == ("merge_confirm",)
            raise RuntimeError("boom")
    assert active_scopes() == ()


# ================================================ B. 普通接口不能绕过保护（PATCH）
def test_patch_content_on_approved_memory_is_refused(jw03_client, manager, settings):
    """The concrete JW-03 defect: PATCH used to rewrite an approved body."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="用户偏好：回答要简短直接。"
    )
    resp = _patch(jw03_client, memory_id, {"content": "被未授权改写"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    # The stored value is untouched.
    assert _record(manager, memory_id).content == "用户偏好：回答要简短直接。"


def test_patch_status_on_approved_memory_is_refused(jw03_client, manager, settings):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="状态保护测试内容一"
    )
    resp = _patch(jw03_client, memory_id, {"status": "ARCHIVED"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    assert _record(manager, memory_id).status == "ACTIVE"


def test_patch_metadata_cannot_forge_approval_provenance(
    jw03_client, manager, settings
):
    """metadata_json carries candidate_id; a PATCH must not be able to rewrite it."""
    memory_id, cid = _approved_memory(
        jw03_client, manager, settings, content="溯源保护测试内容一"
    )
    before = _record(manager, memory_id).metadata_json
    resp = _patch(
        jw03_client,
        memory_id,
        {"metadata": {"candidate_id": "forged", "extracted": True}},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    assert _record(manager, memory_id).metadata_json == before
    assert cid in before


def test_patch_summary_importance_confidence_are_all_protected(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="多字段保护测试内容", summary="原标题"
    )
    for body in (
        {"summary": "被改的标题"},
        {"importance": 1.0},
        {"confidence": 0.1},
        {"expires_at": "2030-01-01T00:00:00+00:00"},
    ):
        resp = _patch(jw03_client, memory_id, body)
        assert resp.status_code == 403, (body, resp.text)
        assert resp.json()["error"]["code"] == "approved_memory_protected"


def test_patch_refusal_names_the_fields_and_the_remedy(
    jw03_client, manager, settings
):
    """A refusal must be actionable, not a bare 403."""
    memory_id, cid = _approved_memory(
        jw03_client, manager, settings, content="拒绝信息完整性测试"
    )
    body = _patch(jw03_client, memory_id, {"content": "x"}).json()
    details = body["error"]["details"]
    assert "content" in details["fields"]
    assert details["memory_id"] == memory_id
    assert details["source_candidate_id"] == cid
    assert "modification-requests" in details["remedy"]


# ==================================== C. 未授权请求不能修改权威数据（多入口）
def test_archive_without_request_is_refused_on_approved_memory(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="归档保护测试内容一"
    )
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/archive", json={}, headers=HEADERS
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    assert _record(manager, memory_id).status == "ACTIVE"


def test_restore_without_request_is_refused_on_approved_memory(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="恢复保护测试内容一"
    )
    # Put it into ARCHIVED through the approved flow first. ARCHIVE requests
    # carry no proposed_changes: the kind itself states the transition.
    resp = _create_lifecycle_request(jw03_client, memory_id, "ARCHIVE")
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    approved = jw03_client.post(
        f"/api/v1/modification-requests/{data['request']['id']}/approve",
        json={"confirmation_token": data["confirmation_token"]},
        headers=HEADERS,
    )
    assert approved.status_code == 200, approved.text
    assert _record(manager, memory_id).status == "ARCHIVED"

    # Now the unauthenticated restore must be refused.
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/restore", headers=HEADERS
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    assert _record(manager, memory_id).status == "ARCHIVED"


def test_expiry_extend_without_request_is_refused_on_approved_memory(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="过期延长保护测试内容"
    )
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/expiry-review",
        json={"action": "EXTENDED", "new_expires_at": "2031-01-01T00:00:00+00:00"},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "approved_memory_protected"
    assert _record(manager, memory_id).expires_at is None


def test_expiry_archived_without_request_is_refused_on_approved_memory(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="过期归档保护测试内容"
    )
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/expiry-review",
        json={"action": "ARCHIVED"},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert _record(manager, memory_id).status == "ACTIVE"


def test_expiry_reviewed_only_is_allowed_because_it_writes_nothing(
    jw03_client, manager, settings
):
    """REVIEWED records a decision but changes no protected field."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="仅记录复核不写保护字段"
    )
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/expiry-review",
        json={"action": "REVIEWED", "note": "看过，保持现状"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert _record(manager, memory_id).status == "ACTIVE"


def test_delete_without_forget_approval_is_still_refused(
    jw03_client, manager, settings
):
    """JW-01 must stay intact after JW-03 touched the same write path."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="JW-01 回归：删除仍需审批"
    )
    resp = jw03_client.delete(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    assert _record(manager, memory_id).status == "ACTIVE"


# ================================================== D. 合法审批流程可以正常修改
def test_approved_request_updates_content(jw03_client, manager, settings):
    memory_id, cid = _approved_memory(
        jw03_client, manager, settings, content="原始内容：回答要简短。"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "新内容：回答简短直接，不要绕弯。"}
    )
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "EXECUTED"
    record = _record(manager, memory_id)
    assert record.content == "新内容：回答简短直接，不要绕弯。"
    # Provenance survives the rewrite.
    assert record.source_candidate_id == cid


def test_approved_request_can_update_metadata_without_losing_provenance(
    jw03_client, manager, settings
):
    memory_id, cid = _approved_memory(
        jw03_client, manager, settings, content="metadata 合法修改测试"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"metadata": {"tag": "reviewed", "candidate_id": cid}}
    )
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    record = _record(manager, memory_id)
    assert "reviewed" in record.metadata_json
    assert record.source_candidate_id == cid


def test_patch_bridge_headers_are_removed(jw03_client, manager, settings):
    """JW-03-R1: the PATCH bridge no longer authorises anything.

    JW-03 originally let a caller authorise a PATCH by presenting
    ``X-Modification-Request-Id`` + ``X-Modification-Token``. That grant was
    weak: the request froze one set of values, but the PATCH body supplied the
    values actually written. A stale client must now be told the bridge is gone,
    not silently ignored.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="PATCH 桥接已删除测试原始内容"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "冻结值：不应通过 PATCH 写入"}
    )
    resp = jw03_client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"content": "PATCH 桥接尝试写入的内容"},
        headers={
            **HEADERS,
            "X-Modification-Request-Id": req_id,
            "X-Modification-Token": token,
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "patch_bridge_removed"
    details = resp.json()["error"]["details"]
    assert "modification-requests" in " ".join(details["remedy"])
    # Nothing was written by the refused PATCH.
    assert _record(manager, memory_id).content == "PATCH 桥接已删除测试原始内容"


def test_patch_refuses_a_single_legacy_header_too(jw03_client, manager, settings):
    """A partial credential set must also be refused, not partially honoured."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="单头拒绝测试内容"
    )
    req_id, _ = _approved_update_request(
        jw03_client, memory_id, {"content": "不应生效"}
    )
    resp = jw03_client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"content": "不应生效"},
        headers={**HEADERS, "X-Modification-Request-Id": req_id},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "patch_bridge_removed"
    assert _record(manager, memory_id).content == "单头拒绝测试内容"


def test_legacy_bridge_does_not_consume_the_request(jw03_client, manager, settings):
    """A refused bridge call must not burn the token or execute the request.

    The request stays PENDING and its token stays usable, so the client can
    still approve it properly - the removal must not become a denial of service
    against legitimate flow.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="桥接不消耗令牌测试原始"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "应通过 approve 写入"}
    )
    jw03_client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"content": "被拒绝的桥接写入"},
        headers={
            **HEADERS,
            "X-Modification-Request-Id": req_id,
            "X-Modification-Token": token,
        },
    )
    detail = jw03_client.get(
        f"/api/v1/modification-requests/{req_id}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "PENDING", "the refused bridge must not consume it"

    approved = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert approved.status_code == 200, approved.text
    assert _record(manager, memory_id).content == "应通过 approve 写入"


def test_request_for_another_memory_cannot_authorise_this_one(
    jw03_client, manager, settings
):
    """Authority is scoped to one memory; a request cannot be retargeted.

    JW-03-R1: the PATCH transport that used to carry this proof is gone, so the
    same property is proved on the surviving lifecycle transport, which still
    takes a request id + token as headers.
    """
    first_id, _ = _approved_memory(
        jw03_client, manager, settings, content="目标记忆甲的内容"
    )
    second_id, _ = _approved_memory(
        jw03_client, manager, settings, content="目标记忆乙的内容"
    )
    # An ARCHIVE request created for memory 甲.
    created = _create_lifecycle_request(jw03_client, first_id, "ARCHIVE")
    assert created.status_code == 201, created.text
    payload = created.json()["data"]

    # Presenting it against memory 乙 must be refused.
    resp = jw03_client.post(
        f"/api/v1/memories/{second_id}/archive",
        json={},
        headers={
            **HEADERS,
            "X-Modification-Request-Id": payload["request"]["id"],
            "X-Modification-Token": payload["confirmation_token"],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "modification_not_allowed"
    assert resp.json()["error"]["details"]["code"] == (
        "modification_request_memory_mismatch"
    )
    # Neither memory changed.
    assert _record(manager, second_id).status == "ACTIVE"
    assert _record(manager, first_id).status == "ACTIVE"


def test_archive_and_restore_through_approved_requests(
    jw03_client, manager, settings
):
    """Full lifecycle round trip: ARCHIVE request then RESTORE request."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="生命周期往返测试内容"
    )
    created = _create_lifecycle_request(jw03_client, memory_id, "ARCHIVE")
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{payload['request']['id']}/approve",
        json={"confirmation_token": payload["confirmation_token"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert _record(manager, memory_id).status == "ARCHIVED"

    created = _create_lifecycle_request(jw03_client, memory_id, "RESTORE")
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{payload['request']['id']}/approve",
        json={"confirmation_token": payload["confirmation_token"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert _record(manager, memory_id).status == "ACTIVE"


def test_request_kind_cannot_be_reused_for_a_different_operation(
    jw03_client, manager, settings
):
    """An ARCHIVE request cannot authorise a restore."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="请求类型绑定测试内容"
    )
    created = _create_lifecycle_request(jw03_client, memory_id, "ARCHIVE")
    payload = created.json()["data"]
    resp = jw03_client.post(
        f"/api/v1/memories/{memory_id}/restore",
        headers={
            **HEADERS,
            "X-Modification-Request-Id": payload["request"]["id"],
            "X-Modification-Token": payload["confirmation_token"],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "modification_not_allowed"


# ==================================================== E. 重复请求不会重复修改
def test_duplicate_request_reuses_the_pending_row_and_rotates_the_token(
    jw03_client, manager, settings
):
    """Identical resubmission reuses the row; the previous token stops working.

    JW-03-R1 changed what "duplicate" means: only an IDENTICAL payload reuses the
    pending request. Submitting different content is a conflict (see
    ``test_approved_content_cannot_be_swapped_after_creation``), because the
    approved values must not change underneath an approver.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="重复申请复用测试内容"
    )
    first = _create_request(jw03_client, memory_id, {"content": "同一版内容"})
    second = _create_request(jw03_client, memory_id, {"content": "同一版内容"})
    assert first.status_code == 201 and second.status_code == 201
    first_id = first.json()["data"]["request"]["id"]
    second_id = second.json()["data"]["request"]["id"]
    assert first_id == second_id, "a second submission must reuse the pending row"
    assert second.json()["data"]["reused_pending_request"] is True

    # The first token must no longer work.
    stale = jw03_client.post(
        f"/api/v1/modification-requests/{first_id}/approve",
        json={"confirmation_token": first.json()["data"]["confirmation_token"]},
        headers=HEADERS,
    )
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "modification_token_invalid"

    # The rotated token does, and it writes the frozen value.
    fresh = jw03_client.post(
        f"/api/v1/modification-requests/{first_id}/approve",
        json={"confirmation_token": second.json()["data"]["confirmation_token"]},
        headers=HEADERS,
    )
    assert fresh.status_code == 200, fresh.text
    assert _record(manager, memory_id).content == "同一版内容"


def test_second_approval_is_refused_not_reapplied(jw03_client, manager, settings):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="重复批准拒绝测试内容"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "只应生效一次"}
    )
    first = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text
    second = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "modification_state_conflict"
    assert _record(manager, memory_id).content == "只应生效一次"


def test_rejected_request_cannot_be_approved_later(jw03_client, manager, settings):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="拒绝后不可批准测试"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "不该生效"}
    )
    rejected = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/reject",
        json={"reason": "不需要"},
        headers=HEADERS,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["status"] == "REJECTED"
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert resp.status_code == 409
    assert _record(manager, memory_id).content == "拒绝后不可批准测试"


# ================================================== F. 并发修改不会造成覆盖
def test_concurrent_approvals_produce_exactly_one_write(
    jw03_client, manager, settings
):
    """Two simultaneous approvals: exactly one write, the other is refused.

    The refusal may be reported as 409 (the request was already consumed) or 403
    (the token was already burnt) depending on which guard the loser hits first -
    both are refusals, and which one wins is not part of the contract. What IS
    contractual is that the memory is written exactly once and the content is the
    approved value.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="并发批准测试原始内容"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "并发唯一生效的内容"}
    )
    url = f"/api/v1/modification-requests/{req_id}/approve"

    def _fire(_):
        return jw03_client.post(
            url, json={"confirmation_token": token}, headers=HEADERS
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(_fire, range(2)))
    assert codes.count(200) == 1, codes
    assert all(code in {403, 409} for code in codes if code != 200), codes
    assert _record(manager, memory_id).content == "并发唯一生效的内容"


def test_concurrent_bridge_patches_all_fail_and_write_nothing(
    jw03_client, manager, settings
):
    """JW-03-R1: concurrent legacy-bridge PATCHes are refused, every one of them.

    The previous case proved "only one of two bridge writes wins". After the
    rectification the correct expectation is stronger and simpler: NONE of them
    may write, and the request must still be PENDING afterwards.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="并发桥接全部拒绝测试原始"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "并发场景冻结值"}
    )
    headers = {
        **HEADERS,
        "X-Modification-Request-Id": req_id,
        "X-Modification-Token": token,
    }

    def _fire(value):
        return jw03_client.patch(
            f"/api/v1/memories/{memory_id}",
            json={"content": value},
            headers=headers,
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(_fire, ["并发值甲", "并发值乙"]))
    assert all(code == 403 for code in codes), codes
    assert _record(manager, memory_id).content == "并发桥接全部拒绝测试原始"
    detail = jw03_client.get(
        f"/api/v1/modification-requests/{req_id}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "PENDING"


# ============================================ G. 历史记忆不被本次改造影响
def test_unapproved_memory_keeps_unrestricted_patch(jw03_client, manager, settings):
    """Legacy/imported rows (source_candidate_id NULL) behave exactly as before."""
    created = jw03_client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "历史直写记忆", "summary": "旧标题"},
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["data"]["id"]
    assert _record(manager, memory_id).source_candidate_id is None

    resp = _patch(jw03_client, memory_id, {"content": "历史记忆可以正常修改"})
    assert resp.status_code == 200, resp.text
    assert _record(manager, memory_id).content == "历史记忆可以正常修改"
    resp = _patch(jw03_client, memory_id, {"status": "ARCHIVED"})
    assert resp.status_code == 200, resp.text
    assert _record(manager, memory_id).status == "ARCHIVED"


def test_imported_memory_stays_unprotected(jw03_client, manager, settings):
    """Import never sets source_candidate_id, so its rows stay unprotected."""
    resp = jw03_client.post(
        "/api/v1/memories/import",
        json={
            "document": {
                "schema_version": 5,
                "memories": [
                    {"id": "hist-jw03-1", "memory_type": "SEMANTIC",
                     "content": "导入的历史记忆内容，与其它内容差异明显"}
                ],
            }
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    record = _record(manager, "hist-jw03-1")
    assert record.source_candidate_id is None
    patched = _patch(jw03_client, "hist-jw03-1", {"content": "导入记忆可正常修改"})
    assert patched.status_code == 200, patched.text


# ================================== H. 合法修正 / 遗忘 / 合并流程全部回归正常
def test_forget_flow_still_works_on_approved_memory(
    jw03_client, manager, settings
):
    """JW-01 two-step forget on a JW-03-protected memory."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="遗忘流程回归测试内容"
    )
    created = jw03_client.post(
        f"/api/v1/memories/{memory_id}/forget-requests",
        json={"reason": "不再需要"},
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    approved = jw03_client.post(
        f"/api/v1/forget-requests/{payload['id']}/approve",
        json={"confirmation_token": payload["confirmation_token"]},
        headers=HEADERS,
    )
    assert approved.status_code == 200, approved.text
    record = _record(manager, memory_id)
    assert record.status == "DELETED"
    assert record.forgotten_at is not None


def test_forget_rejection_leaves_approved_memory_intact(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="遗忘拒绝回归测试内容"
    )
    created = jw03_client.post(
        f"/api/v1/memories/{memory_id}/forget-requests",
        json={"reason": "犹豫中"},
        headers=HEADERS,
    )
    payload = created.json()["data"]
    # JW-01 requires the same one-time token for a rejection, so the request
    # cannot be closed by anyone who merely knows its id.
    rejected = jw03_client.post(
        f"/api/v1/forget-requests/{payload['id']}/reject",
        json={
            "reason": "算了",
            "confirmation_token": payload["confirmation_token"],
        },
        headers=HEADERS,
    )
    assert rejected.status_code == 200, rejected.text
    assert _record(manager, memory_id).status == "ACTIVE"


def test_merge_flow_still_retires_approved_sources(jw03_client, manager, settings):
    """Merge confirmation writes status=MERGED on approved rows via its own scope."""
    first_id, _ = _approved_memory(
        jw03_client, manager, settings, content="合并来源记忆甲的完整内容描述"
    )
    second_id, _ = _approved_memory(
        jw03_client, manager, settings, content="合并来源记忆乙的完整内容描述"
    )
    preview = jw03_client.post(
        "/api/v1/memories/merges/preview",
        json={"memory_ids": [first_id, second_id]},
        headers=HEADERS,
    )
    assert preview.status_code == 200, preview.text
    confirmed = jw03_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": [first_id, second_id]},
        headers=HEADERS,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _record(manager, first_id).status == "MERGED"
    assert _record(manager, second_id).status == "MERGED"


def test_correction_resolve_still_rewrites_the_chosen_approved_memory(
    jw03_client, manager, settings
):
    """The correction flow's rewrite goes through its own scope, not a bypass."""
    first_id, _ = _approved_memory(
        jw03_client, manager, settings, content="用户偏好简洁直接的回答风格"
    )
    second_id, _ = _approved_memory(
        jw03_client, manager, settings, content="用户偏好冗长详细的回答风格"
    )
    detected = jw03_client.post(
        "/api/v1/memories/corrections/detect",
        json={"memory_ids": [first_id, second_id]},
        headers=HEADERS,
    )
    assert detected.status_code == 200, detected.text
    rows = detected.json()["data"]
    assert rows, "expected the conflict detector to flag this pair"
    correction_id = rows[0]["id"]

    resolved = jw03_client.post(
        f"/api/v1/memories/corrections/{correction_id}/resolve",
        json={
            "chosen_memory_id": first_id,
            "corrected_content": "用户偏好简洁直接的回答风格（已确认）",
            "reason": "以第一条为准",
        },
        headers=HEADERS,
    )
    assert resolved.status_code == 200, resolved.text
    assert "已确认" in _record(manager, first_id).content


def test_summary_approve_still_creates_a_memory(jw03_client, manager, settings):
    """SW-02 regression: the reflection product is still created."""
    first_id, _ = _approved_memory(
        jw03_client, manager, settings, content="总结来源之一：在快运公司做仲裁理赔"
    )
    second_id, _ = _approved_memory(
        jw03_client, manager, settings, content="总结来源之二：养了两只猫需要喂食"
    )
    created = jw03_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first_id, second_id]},
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    summary_id = created.json()["data"]["id"]
    reviewed = jw03_client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "approve"},
        headers=HEADERS,
    )
    assert reviewed.status_code == 200, reviewed.text
    active = jw03_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert [m for m in active if m["memory_type"] == "REFLECTION"]


# ========================================================== I. 审计与身份诚实性
def test_audit_records_before_and_after_for_a_protected_write(
    jw03_client, manager, settings
):
    """§3.4 修改前后的数据审计: the pre-image and the authority are both recorded."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="审计前后对比测试原始内容"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "审计前后对比测试新内容"}
    )
    jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    entries = jw03_client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc&page_size=100",
        headers=HEADERS,
    ).json()["data"]
    updates = [e for e in entries if e["action"] == "UPDATE"]
    assert updates, entries
    detail = updates[-1]["detail"]
    if isinstance(detail, str):
        import json as _json

        detail = _json.loads(detail)
    assert detail["before"]["content"] == "审计前后对比测试原始内容"
    assert detail["after"]["content"] == "审计前后对比测试新内容"
    assert detail["protected"]["authority"] == "approved_modification_request"
    assert detail["protected"]["request_id"] == req_id


def test_audit_never_claims_a_verified_human_identity(
    jw03_client, manager, settings
):
    """003-C is not implemented, so identity stays NOT_VERIFIED everywhere."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="身份诚实性测试内容一"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "身份诚实性测试内容二"}
    )
    jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token, "actor_label": "human:林晓明"},
        headers=HEADERS,
    )
    detail = jw03_client.get(
        f"/api/v1/modification-requests/{req_id}", headers=HEADERS
    ).json()["data"]
    assert detail["authentication_level"] == "API_TOKEN_SHARED"
    assert detail["authorization_verification_result"] == "NOT_VERIFIED"


def test_non_human_actor_is_refused_for_approval(jw03_client, manager, settings):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="非人类调用者拒绝测试"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "不应由模型批准"}
    )
    resp = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token, "actor_label": "model"},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "non_human_actor_forbidden"


# ============================================ J. 申请单本身不能被滥用
def test_request_on_unapproved_memory_is_refused_with_a_pointer(
    jw03_client, manager, settings
):
    """An unprotected row does not need this flow; say so instead of pretending."""
    created = jw03_client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "未审批的普通记忆"},
        headers=HEADERS,
    )
    memory_id = created.json()["data"]["id"]
    resp = _create_request(jw03_client, memory_id, {"content": "x"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "modification_not_allowed"
    assert resp.json()["error"]["details"]["code"] == "memory_not_approved"


def test_request_cannot_propose_immutable_or_unknown_fields(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="字段白名单测试内容一"
    )
    for field in ("source_candidate_id", "id", "created_at"):
        resp = _create_request(jw03_client, memory_id, {field: "x"})
        assert resp.status_code in (403, 422), (field, resp.text)
    resp = _create_request(jw03_client, memory_id, {"vector_status": "INDEXED"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["code"] == "field_not_proposable"


def test_update_request_cannot_smuggle_a_status_change(
    jw03_client, manager, settings
):
    """status must go through ARCHIVE/RESTORE, not an UPDATE request."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="状态走私测试内容一"
    )
    resp = _create_request(jw03_client, memory_id, {"status": "DELETED"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["code"] == "status_requires_lifecycle_kind"


def test_request_list_and_detail_never_leak_the_token(
    jw03_client, manager, settings
):
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="令牌不外泄测试内容一"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "不应回显的令牌"}
    )
    listed = jw03_client.get(
        "/api/v1/modification-requests", headers=HEADERS
    ).json()["data"]
    assert isinstance(listed, list)
    assert listed
    for row in listed:
        assert "confirmation_token" not in row
        assert "confirmation_token_hash" not in row
        assert token not in str(row)
    detail = jw03_client.get(
        f"/api/v1/modification-requests/{req_id}", headers=HEADERS
    ).json()["data"]
    assert token not in str(detail)


def test_approved_content_cannot_be_swapped_after_creation(
    jw03_client, manager, settings
):
    """JW-03-R1 核心不变量: what gets written is what was frozen, not a later body.

    Covers the three ways a caller might try to substitute content:
      1. send extra fields in the approve body;
      2. re-submit the same memory with different proposed_changes;
      3. rewrite ``proposed_changes_json`` through the repository transition.
    """
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="冻结值不可替换测试原始"
    )
    req_id, token = _approved_update_request(
        jw03_client, memory_id, {"content": "冻结值：这就是批准的内容"}
    )

    # (1) extra fields in the approve body are rejected outright.
    tampered = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={
            "confirmation_token": token,
            "proposed_changes": {"content": "篡改：不应生效"},
            "content": "篡改：不应生效",
        },
        headers=HEADERS,
    )
    assert tampered.status_code == 422, tampered.text
    assert _record(manager, memory_id).content == "冻结值不可替换测试原始"

    # (2) a second create with DIFFERENT content is refused, not silently swapped.
    conflict = _create_request(jw03_client, memory_id, {"content": "另一个完全不同的值"})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["details"]["code"] == (
        "modification_request_conflict"
    )
    # The frozen value in the pending request is unchanged.
    pending = jw03_client.get(
        f"/api/v1/modification-requests/{req_id}", headers=HEADERS
    ).json()["data"]
    assert pending["proposed_changes"] == {"content": "冻结值：这就是批准的内容"}

    # (3) the repository refuses to rewrite the frozen column.
    from jarvismind.repositories.modification_request_repository import (
        ModificationRequestRepository,
    )

    with pytest.raises(ValueError, match="proposed_changes_json"):
        with manager.session() as session:
            ModificationRequestRepository(session).transition(
                req_id,
                expected_status="PENDING",
                new_status="PENDING",
                values={"proposed_changes_json": '{"content":"篡改"}'},
            )

    # And the legitimate approval still writes exactly the frozen value.
    ok = jw03_client.post(
        f"/api/v1/modification-requests/{req_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )
    assert ok.status_code == 200, ok.text
    assert _record(manager, memory_id).content == "冻结值：这就是批准的内容"


def test_identical_resubmission_reuses_the_pending_request(
    jw03_client, manager, settings
):
    """Same content may reuse the pending row; the token is rotated."""
    memory_id, _ = _approved_memory(
        jw03_client, manager, settings, content="相同内容重复提交测试原始"
    )
    first = _create_request(jw03_client, memory_id, {"content": "同一份内容"})
    second = _create_request(jw03_client, memory_id, {"content": "同一份内容"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["data"]["request"]["id"] == second.json()["data"]["request"]["id"]
    assert second.json()["data"]["reused_pending_request"] is True
    # The stale token stops working; the rotated one does not.
    stale = jw03_client.post(
        f"/api/v1/modification-requests/{first.json()['data']['request']['id']}/approve",
        json={"confirmation_token": first.json()["data"]["confirmation_token"]},
        headers=HEADERS,
    )
    assert stale.status_code == 403
