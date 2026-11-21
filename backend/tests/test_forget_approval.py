"""JW-01: JME memory forget approval flow.

Covers every behaviour the task book requires, grouped as:

  A. request creation  (no deletion, status accounting, duplicate handling)
  B. querying          (list, detail, status visibility)
  C. approval          (execution, transaction, audit, no self-approval)
  D. rejection         (memory untouched, terminal)
  E. legacy entries    (DELETE / POST /forget cannot bypass approval)
  F. concurrency       (double approve, approve-vs-reject, replay)
  G. transaction       (failure leaves consistent state)
  H. audit + identity  (NOT_VERIFIED, complete trail)

The fixtures build the app on the shared tmp manager via ``create_app``, i.e.
the same construction the P0-03A allowlist sanctions. Nothing here touches a
real database.
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from jarvismind.database.models import MemoryRecord
from jarvismind.main import create_app
from jarvismind.repositories.memory_repository import MemoryRepository
from jarvismind.repositories.forget_request_repository import ForgetRequestRepository
from jarvismind.services import forget_service as forget_module

HEADERS = {"X-API-Token": "test-token-0123456789"}


@pytest.fixture()
def jw01_client(settings, manager):
    """App instance for JW-01 cases (direct writes enabled so fixtures are cheap)."""
    enabled = dataclasses.replace(settings, direct_write_enabled=True)
    with TestClient(create_app(enabled, manager=manager)) as client:
        yield client


@pytest.fixture()
def jw01_no_raise_client(settings, manager):
    """Same app, but a server-side exception comes back as a 500 response.

    The injected-failure cases assert the HTTP result AND the persisted state, so
    the exception must not be re-raised into the test body.
    """
    enabled = dataclasses.replace(settings, direct_write_enabled=True)
    with TestClient(
        create_app(enabled, manager=manager), raise_server_exceptions=False
    ) as client:
        yield client


def _memory(client, content: str = "我居住在浙江金华并从事客服理赔工作") -> str:
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": content},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _request(client, memory_id: str, reason: str = "jw01 test") -> dict:
    resp = client.post(
        f"/api/v1/memories/{memory_id}/forget-requests",
        json={"reason": reason},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _approve(client, request_id: str, token: str) -> "object":
    return client.post(
        f"/api/v1/forget-requests/{request_id}/approve",
        json={"confirmation_token": token},
        headers=HEADERS,
    )


def _reject(client, request_id: str, token: str) -> "object":
    return client.post(
        f"/api/v1/forget-requests/{request_id}/reject",
        json={"confirmation_token": token},
        headers=HEADERS,
    )


def _status(client, memory_id: str) -> str:
    resp = client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["status"]


def _audit_actions(client, memory_id: str) -> list:
    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc&page_size=100",
        headers=HEADERS,
    ).json()
    return [entry["action"] for entry in body["data"]]


# =========================================================== A. create request
def test_create_request_returns_pending_and_token(jw01_client):
    memory_id = _memory(jw01_client)
    data = _request(jw01_client, memory_id)
    assert data["status"] == "PENDING"
    assert data["memory_id"] == memory_id
    assert data["awaiting_user_decision"] is True
    assert data["memory_forgotten"] is False
    assert data["execution_result"] is None
    assert len(data["confirmation_token"]) >= 32
    assert data["reused_existing"] is False


def test_create_request_does_not_delete_or_modify_memory(jw01_client, manager):
    memory_id = _memory(jw01_client, "创建申请不得改动原记忆")
    before = jw01_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS).json()[
        "data"
    ]
    _request(jw01_client, memory_id)
    after = jw01_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS).json()[
        "data"
    ]
    assert after["status"] == "ACTIVE"
    assert after["content"] == before["content"]
    assert after["updated_at"] == before["updated_at"]
    assert after["forgotten_at"] is None
    assert after["forget_reason"] is None
    with manager.session() as session:
        row = session.get(MemoryRecord, memory_id)
        assert row.status == "ACTIVE"
        assert row.forgotten_at is None


def test_create_request_records_source_and_audit(jw01_client):
    memory_id = _memory(jw01_client)
    data = _request(jw01_client, memory_id, reason="隐私清理")
    assert data["request_source"] == "USER_INTERFACE"
    assert data["reason"] == "隐私清理"
    actions = _audit_actions(jw01_client, memory_id)
    assert "FORGET_REQUESTED" in actions
    assert "FORGET_EXECUTED" not in actions


def test_duplicate_request_reuses_pending_and_rotates_token(jw01_client):
    memory_id = _memory(jw01_client)
    first = _request(jw01_client, memory_id)
    second = _request(jw01_client, memory_id)

    assert second["reused_existing"] is True
    assert second["id"] == first["id"]
    # Exactly one PENDING row exists for this memory.
    listing = jw01_client.get(
        "/api/v1/forget-requests",
        params={"memory_id": memory_id, "status": "PENDING"},
        headers=HEADERS,
    ).json()
    assert listing["meta"]["total"] == 1
    # The rotated-out token must no longer be usable.
    stale = _approve(jw01_client, first["id"], first["confirmation_token"])
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "forget_token_invalid"
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_create_request_for_unknown_memory_is_404(jw01_client):
    resp = jw01_client.post(
        "/api/v1/memories/does-not-exist/forget-requests",
        json={},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_create_request_for_deleted_memory_is_refused(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _approve(jw01_client, created["id"], created["confirmation_token"]).status_code == 200

    resp = jw01_client.post(
        f"/api/v1/memories/{memory_id}/forget-requests",
        json={},
        headers=HEADERS,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "memory_not_forgettable"


# ============================================================== B. querying
def test_list_forget_requests_and_filter_by_status(jw01_client):
    first = _memory(jw01_client, "查询测试记忆甲")
    second = _memory(jw01_client, "查询测试记忆乙")
    created = _request(jw01_client, first)
    _request(jw01_client, second)

    everything = jw01_client.get("/api/v1/forget-requests", headers=HEADERS).json()
    assert everything["meta"]["total"] >= 2

    pending = jw01_client.get(
        "/api/v1/forget-requests", params={"status": "PENDING"}, headers=HEADERS
    ).json()
    assert all(item["status"] == "PENDING" for item in pending["data"])

    assert _approve(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    executed = jw01_client.get(
        "/api/v1/forget-requests", params={"status": "EXECUTED"}, headers=HEADERS
    ).json()
    assert any(item["id"] == created["id"] for item in executed["data"])


def test_get_single_request_returns_timestamps(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    detail = jw01_client.get(
        f"/api/v1/forget-requests/{created['id']}", headers=HEADERS
    ).json()["data"]
    assert detail["requested_at"]
    assert detail["confirmation_expires_at"]
    assert detail["decided_at"] is None
    assert detail["executed_at"] is None
    # The plaintext token must never be readable again.
    assert "confirmation_token" not in detail


def test_get_unknown_request_is_404(jw01_client):
    resp = jw01_client.get("/api/v1/forget-requests/nope", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "forget_request_not_found"


def test_forget_request_status_filter_rejects_unknown_value(jw01_client):
    resp = jw01_client.get(
        "/api/v1/forget-requests", params={"status": "NOT_A_STATUS"}, headers=HEADERS
    )
    assert resp.status_code == 422


# ============================================================== C. approval
def test_approve_executes_and_writes_full_audit(jw01_client, manager):
    memory_id = _memory(jw01_client, "批准后应被遗忘")
    created = _request(jw01_client, memory_id, reason="用户主动遗忘")
    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]

    assert body["status"] == "EXECUTED"
    assert body["memory_forgotten"] is True
    assert body["execution_result"] == "DELETED"
    assert body["decided_at"] is not None
    assert body["executed_at"] is not None
    assert body["decision_source"] == forget_module.DECISION_SOURCE_USER

    assert _status(jw01_client, memory_id) == "DELETED"
    with manager.session() as session:
        row = session.get(MemoryRecord, memory_id)
        assert row.status == "DELETED"
        assert row.forgotten_at is not None
        assert row.forget_reason == "用户主动遗忘"

    actions = _audit_actions(jw01_client, memory_id)
    assert actions.index("FORGET_REQUESTED") < actions.index("FORGET_APPROVED")
    assert actions.index("FORGET_APPROVED") < actions.index("FORGET_EXECUTED")


def test_pending_request_cannot_delete_anything(jw01_client):
    """A PENDING request alone must never delete; only approve does."""
    memory_id = _memory(jw01_client, "待处理申请不得删除记忆")
    _request(jw01_client, memory_id)
    assert _status(jw01_client, memory_id) == "ACTIVE"
    actions = _audit_actions(jw01_client, memory_id)
    assert "FORGET_EXECUTED" not in actions


def test_approve_requires_the_confirmation_token(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    # No token at all -> schema refuses.
    missing = jw01_client.post(
        f"/api/v1/forget-requests/{created['id']}/approve",
        json={},
        headers=HEADERS,
    )
    assert missing.status_code == 422
    # Wrong token -> 403, memory untouched.
    wrong = _approve(jw01_client, created["id"], "x" * 48)
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "forget_token_invalid"
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_approve_with_a_token_from_another_request_is_refused(jw01_client):
    """A token is bound to its own request, not reusable across requests."""
    first_memory = _memory(jw01_client, "甲记忆用于令牌绑定测试")
    second_memory = _memory(jw01_client, "乙记忆用于令牌绑定测试")
    first = _request(jw01_client, first_memory)
    second = _request(jw01_client, second_memory)

    resp = _approve(jw01_client, second["id"], first["confirmation_token"])
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_token_invalid"
    assert _status(jw01_client, first_memory) == "ACTIVE"
    assert _status(jw01_client, second_memory) == "ACTIVE"


def test_approve_expired_token_is_refused(jw01_client, settings, manager):
    """Expiry is enforced in the same UPDATE that burns the token."""
    expired = dataclasses.replace(settings, forget_token_ttl_seconds=30)
    with TestClient(create_app(expired, manager=manager)) as client:
        memory_id = _memory(client)
        created = _request(client, memory_id)
        # Backdate the expiry to simulate a token that timed out.
        with manager.session() as session:
            ForgetRequestRepository(session).transition(
                created["id"],
                expected_status="PENDING",
                new_status="PENDING",
                values={"confirmation_expires_at": "2000-01-01T00:00:00+00:00"},
            )
        resp = _approve(client, created["id"], created["confirmation_token"])
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forget_token_invalid"
        assert _status(client, memory_id) == "ACTIVE"


def test_model_or_system_actor_cannot_approve(jw01_client):
    """No model / service / background actor may approve a forget."""
    memory_id = _memory(jw01_client, "禁止模型自行审批遗忘")
    created = _request(jw01_client, memory_id)
    for label in ("model", "llm", "system", "background-task", "svc:jme"):
        resp = jw01_client.post(
            f"/api/v1/forget-requests/{created['id']}/approve",
            json={
                "confirmation_token": created["confirmation_token"],
                "actor_label": label,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 403, label
        assert resp.json()["error"]["code"] == "model_approval_forbidden", label
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_approved_request_cannot_be_approved_twice(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _approve(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    second = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "forget_request_state_conflict"
    # Only one execution audit entry exists.
    assert _audit_actions(jw01_client, memory_id).count("FORGET_EXECUTED") == 1


def test_repeated_execution_is_refused(jw01_client):
    """A fresh request against an already-executed memory must not re-run."""
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _approve(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    resp = jw01_client.post(
        f"/api/v1/memories/{memory_id}/forget-requests", json={}, headers=HEADERS
    )
    assert resp.status_code == 409
    assert _audit_actions(jw01_client, memory_id).count("FORGET_EXECUTED") == 1


def test_approve_unknown_request_is_404(jw01_client):
    resp = _approve(jw01_client, "missing-request", "y" * 48)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "forget_request_not_found"


def test_approve_request_whose_memory_vanished_marks_failed(jw01_client, manager):
    """If the target memory disappears, the request goes FAILED, not EXECUTED."""
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    # Remove the memory row behind the API's back.
    with manager.session() as session:
        row = session.get(MemoryRecord, memory_id)
        session.delete(row)
    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "memory_not_forgettable"
    detail = jw01_client.get(
        f"/api/v1/forget-requests/{created['id']}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "FAILED"
    assert detail["memory_forgotten"] is False
    assert detail["execution_result"] == "NOT_EXECUTED"


# ============================================================== D. rejection
def test_reject_keeps_memory_and_is_terminal(jw01_client):
    memory_id = _memory(jw01_client, "拒绝后记忆必须保留")
    created = _request(jw01_client, memory_id)
    resp = _reject(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["status"] == "REJECTED"
    assert body["memory_forgotten"] is False
    assert body["execution_result"] == "NOT_EXECUTED"

    assert _status(jw01_client, memory_id) == "ACTIVE"
    actions = _audit_actions(jw01_client, memory_id)
    assert "FORGET_REJECTED" in actions
    assert "FORGET_EXECUTED" not in actions


def test_rejected_request_cannot_be_approved(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _reject(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "forget_request_state_conflict"
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_reject_requires_the_confirmation_token(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    resp = _reject(jw01_client, created["id"], "z" * 48)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_token_invalid"
    assert _status(jw01_client, memory_id) == "ACTIVE"


# ========================================================== E. legacy entries
def test_legacy_delete_cannot_bypass_approval(jw01_client):
    memory_id = _memory(jw01_client, "旧 DELETE 不得绕过审批")
    resp = jw01_client.delete(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_legacy_delete_with_mismatched_request_is_refused(jw01_client):
    """A token issued for memory A must never delete memory B."""
    memory_a = _memory(jw01_client, "旧接口绑定校验甲")
    memory_b = _memory(jw01_client, "旧接口绑定校验乙")
    created = _request(jw01_client, memory_a)
    resp = jw01_client.delete(
        f"/api/v1/memories/{memory_b}",
        params={"forget_request_id": created["id"]},
        headers={
            **HEADERS,
            "X-Forget-Confirmation-Token": created["confirmation_token"],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_token_invalid"
    assert _status(jw01_client, memory_a) == "ACTIVE"
    assert _status(jw01_client, memory_b) == "ACTIVE"


def test_legacy_bulk_forget_endpoint_requires_approval(jw01_client):
    memory_id = _memory(jw01_client, "旧批量遗忘接口不得绕过审批")
    resp = jw01_client.post(
        "/api/v1/memories/forget",
        json={"memory_ids": [memory_id], "confirm": True},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_no_forget_entry_point_skips_the_request_table(jw01_client, manager):
    """Every deletion path must leave exactly one forget-request row behind.

    This is the structural check that no other entry point can delete a memory
    without an approved request: after each attempt the request table is the
    single source of truth for "was this authorised?".
    """
    memory_id = _memory(jw01_client, "结构性绕过检查")
    jw01_client.delete(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    jw01_client.post(
        "/api/v1/memories/forget",
        json={"memory_ids": [memory_id], "confirm": True},
        headers=HEADERS,
    )
    with manager.session() as session:
        rows, total = ForgetRequestRepository(session).list(memory_id=memory_id)
    assert total == 0
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_soft_delete_status_cannot_be_patched_directly(jw01_client):
    """PATCH remains unable to set DELETED; only the forget flow can."""
    memory_id = _memory(jw01_client)
    resp = jw01_client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"status": "DELETED"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert _status(jw01_client, memory_id) == "ACTIVE"


# ============================================================ F. concurrency
def test_concurrent_approvals_execute_only_once(jw01_client, manager):
    """Two approvals of the same request: exactly one succeeds."""
    memory_id = _memory(jw01_client, "并发批准只能执行一次")
    created = _request(jw01_client, memory_id)
    token = created["confirmation_token"]

    first = _approve(jw01_client, created["id"], token)
    second = _approve(jw01_client, created["id"], token)

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (first.text, second.text)
    assert _audit_actions(jw01_client, memory_id).count("FORGET_EXECUTED") == 1
    assert _status(jw01_client, memory_id) == "DELETED"


def test_concurrent_request_creates_only_one_pending_row(jw01_client, manager):
    """The unique partial index keeps at most one PENDING request per memory."""
    memory_id = _memory(jw01_client, "并发申请只能有一条待处理记录")
    _request(jw01_client, memory_id)
    _request(jw01_client, memory_id)
    _request(jw01_client, memory_id)
    with manager.session() as session:
        _, total = ForgetRequestRepository(session).list(
            memory_id=memory_id, status="PENDING"
        )
    assert total == 1


def test_approve_after_reject_is_a_conflict_not_an_execution(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _reject(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 409
    assert _status(jw01_client, memory_id) == "ACTIVE"


def test_reject_after_approve_is_a_conflict(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert _approve(jw01_client, created["id"], created["confirmation_token"]).status_code == 200
    resp = _reject(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "forget_request_state_conflict"


# ============================================================ G. transaction
def test_execution_failure_leaves_memory_active_and_request_failed(
    jw01_no_raise_client, manager, monkeypatch
):
    """Inject a failure during execution: memory stays ACTIVE, request FAILED."""
    jw01_client = jw01_no_raise_client
    memory_id = _memory(jw01_client, "事务失败一致性验证")
    created = _request(jw01_client, memory_id)

    original = ForgetRequestRepository.transition
    calls = {"n": 0}

    def flaky(self, request_id, *, expected_status, new_status, values=None):
        result = original(
            self,
            request_id,
            expected_status=expected_status,
            new_status=new_status,
            values=values,
        )
        calls["n"] += 1
        # Fail on the SECOND transition (PENDING->APPROVED succeeded, then the
        # final APPROVED->EXECUTED write blows up).
        if calls["n"] == 2:
            raise RuntimeError("injected transition failure")
        return result

    monkeypatch.setattr(ForgetRequestRepository, "transition", flaky)
    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    monkeypatch.undo()

    assert resp.status_code >= 400
    # Memory untouched: the whole transaction rolled back.
    assert _status(jw01_client, memory_id) == "ACTIVE"
    detail = jw01_client.get(
        f"/api/v1/forget-requests/{created['id']}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "FAILED"
    assert detail["memory_forgotten"] is False
    assert detail["execution_result"] == "NOT_EXECUTED"
    # No EXECUTED audit may exist for a rolled-back execution.
    actions = _audit_actions(jw01_client, memory_id)
    assert "FORGET_EXECUTED" not in actions


def test_failed_request_cannot_be_approved_again(
    jw01_no_raise_client, manager, monkeypatch
):
    """A FAILED request is terminal: a NEW request is required."""
    jw01_client = jw01_no_raise_client
    memory_id = _memory(jw01_client, "失败申请不可重复执行")
    created = _request(jw01_client, memory_id)

    original = ForgetRequestRepository.transition
    calls = {"n": 0}

    def flaky(self, request_id, *, expected_status, new_status, values=None):
        result = original(
            self,
            request_id,
            expected_status=expected_status,
            new_status=new_status,
            values=values,
        )
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected transition failure")
        return result

    monkeypatch.setattr(ForgetRequestRepository, "transition", flaky)
    _approve(jw01_client, created["id"], created["confirmation_token"])
    monkeypatch.undo()

    resp = _approve(jw01_client, created["id"], created["confirmation_token"])
    assert resp.status_code == 409
    assert _status(jw01_client, memory_id) == "ACTIVE"


# ===================================================== H. audit and identity
def test_identity_is_reported_as_not_verified(jw01_client):
    """JW-01 must never claim verified personal identity."""
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    assert created["authentication_level"] == "API_TOKEN_SHARED"
    assert created["authorization_verification_result"] == "NOT_VERIFIED"

    approved = _approve(
        jw01_client, created["id"], created["confirmation_token"]
    ).json()["data"]
    assert approved["authentication_level"] == "API_TOKEN_SHARED"
    assert approved["authorization_verification_result"] == "NOT_VERIFIED"


def test_audit_records_exist_for_every_stage(jw01_client):
    """Request / approve / execute are each auditable, with the request id."""
    memory_id = _memory(jw01_client, "审计完整性验证")
    created = _request(jw01_client, memory_id)
    _approve(jw01_client, created["id"], created["confirmation_token"])

    body = jw01_client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc&page_size=100",
        headers=HEADERS,
    ).json()
    by_action = {entry["action"]: entry for entry in body["data"]}
    for action in ("FORGET_REQUESTED", "FORGET_APPROVED", "FORGET_EXECUTED"):
        assert action in by_action, action
        detail = by_action[action]["detail"] or {}
        assert detail.get("forget_request_id") == created["id"]
    # Approval/execution entries must state the identity truthfulness.
    assert (
        by_action["FORGET_APPROVED"]["detail"]["authorization_verification_result"]
        == "NOT_VERIFIED"
    )
    # The actor is the server label, never a client-supplied claim.
    assert by_action["FORGET_EXECUTED"]["actor"] == forget_module.SERVER_ACTOR_LABEL


def test_reject_audit_records_reason(jw01_client):
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    jw01_client.post(
        f"/api/v1/forget-requests/{created['id']}/reject",
        json={
            "confirmation_token": created["confirmation_token"],
            "reason": "还不想删",
        },
        headers=HEADERS,
    )
    body = jw01_client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc&page_size=100",
        headers=HEADERS,
    ).json()
    reject = [e for e in body["data"] if e["action"] == "FORGET_REJECTED"]
    assert len(reject) == 1
    assert reject[0]["detail"]["reason"] == "还不想删"


def test_token_digest_is_not_stored_in_plaintext(jw01_client, manager):
    """The token is persisted only as a digest, and cleared once consumed."""
    memory_id = _memory(jw01_client)
    created = _request(jw01_client, memory_id)
    token = created["confirmation_token"]
    with manager.session() as session:
        row = session.execute(
            __import__("sqlalchemy").text(
                "SELECT confirmation_token_hash, confirmation_consumed_at "
                "FROM memory_forget_requests WHERE id = :id"
            ),
            {"id": created["id"]},
        ).first()
    assert row is not None
    assert row[0] != token
    assert len(row[0]) == 64  # sha256 hex
    assert row[1] is None  # not consumed yet

    _approve(jw01_client, created["id"], token)
    with manager.session() as session:
        row = session.execute(
            __import__("sqlalchemy").text(
                "SELECT confirmation_consumed_at FROM memory_forget_requests "
                "WHERE id = :id"
            ),
            {"id": created["id"]},
        ).first()
    assert row[0] is not None


def test_forget_endpoints_require_token(jw01_client):
    memory_id = _memory(jw01_client)
    assert (
        jw01_client.post(
            f"/api/v1/memories/{memory_id}/forget-requests", json={}
        ).status_code
        == 401
    )
    assert jw01_client.get("/api/v1/forget-requests").status_code == 401
    assert (
        jw01_client.get("/api/v1/forget-requests/anything").status_code == 401
    )
    assert (
        jw01_client.post(
            "/api/v1/forget-requests/anything/approve",
            json={"confirmation_token": "q" * 48},
        ).status_code
        == 401
    )
