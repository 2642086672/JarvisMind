"""Create / read / update / soft-delete tests."""
from jarvismind.database.models import MemoryRecord
from jarvismind.repositories.memory_repository import MemoryRepository


def _create(client, headers, payload):
    resp = client.post("/api/v1/memories", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_create_memory(client, auth_headers, sample_memory_payload):
    body = _create(client, auth_headers, sample_memory_payload)
    assert body["id"]
    assert body["memory_type"] == "SEMANTIC"
    assert body["status"] == "ACTIVE"
    assert body["content"] == sample_memory_payload["content"]
    assert body["importance"] == 7.5
    assert body["confidence"] == 0.9
    assert body["metadata"] is None


def test_create_with_metadata_and_expiry(client, auth_headers, sample_memory_payload):
    payload = {
        **sample_memory_payload,
        "metadata": {"origin": "chat", "tags": ["preference"]},
        "expires_at": "2030-01-01T00:00:00+00:00",
        "status": "ARCHIVED",
    }
    body = _create(client, auth_headers, payload)
    assert body["metadata"] == {"origin": "chat", "tags": ["preference"]}
    assert body["expires_at"] == "2030-01-01T00:00:00+00:00"
    assert body["status"] == "ARCHIVED"


def test_get_memory(client, auth_headers, sample_memory_payload):
    created = _create(client, auth_headers, sample_memory_payload)
    resp = client.get(f"/api/v1/memories/{created['id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == created["id"]


def test_get_updates_accessed_at(client, auth_headers, sample_memory_payload):
    created = _create(client, auth_headers, sample_memory_payload)
    assert created["accessed_at"] is None
    fetched = client.get(
        f"/api/v1/memories/{created['id']}", headers=auth_headers
    ).json()["data"]
    assert fetched["accessed_at"] is not None


def test_get_unknown_returns_404(client, auth_headers):
    resp = client.get("/api/v1/memories/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_update_memory(client, auth_headers, sample_memory_payload):
    created = _create(client, auth_headers, sample_memory_payload)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"content": "Updated content", "importance": 9.0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["content"] == "Updated content"
    assert body["importance"] == 9.0
    assert body["updated_at"] >= created["updated_at"]


def test_update_can_clear_summary(client, auth_headers, sample_memory_payload):
    created = _create(client, auth_headers, sample_memory_payload)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"summary": None},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["summary"] is None


def test_update_can_replace_metadata(client, auth_headers, sample_memory_payload):
    created = _create(client, auth_headers, sample_memory_payload)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"metadata": {"a": 1}},
        headers=auth_headers,
    )
    assert resp.json()["data"]["metadata"] == {"a": 1}


def test_update_unknown_returns_404(client, auth_headers):
    resp = client.patch(
        "/api/v1/memories/nope", json={"content": "x"}, headers=auth_headers
    )
    assert resp.status_code == 404


def test_soft_delete_keeps_row_and_writes_audit(
    client, auth_headers, sample_memory_payload, manager, approved_forget
):
    """JW-01: forgetting now goes through the approved-forget flow.

    The end state asserted here is unchanged from before (row kept, status
    DELETED, audit trail present). What changed is the route: an approved forget
    request instead of a bare DELETE.
    """
    created = _create(client, auth_headers, sample_memory_payload)
    approved_forget.forget(created["id"])

    with manager.session() as session:
        row = session.get(MemoryRecord, created["id"])
        assert row is not None
        assert row.status == "DELETED"
        actions = [
            entry.action
            for entry in MemoryRepository(session).audit_trail(created["id"])
        ]
    assert "CREATE" in actions
    assert "SOFT_DELETE" not in actions  # the unaudited legacy path is gone
    assert "FORGET_REQUESTED" in actions
    assert "FORGET_APPROVED" in actions
    assert "FORGET_EXECUTED" in actions


def test_deleted_memory_hidden_by_default(
    client, auth_headers, sample_memory_payload, approved_forget
):
    created = _create(client, auth_headers, sample_memory_payload)
    approved_forget.forget(created["id"])

    active = client.get("/api/v1/memories", headers=auth_headers).json()
    assert all(item["id"] != created["id"] for item in active["data"])

    deleted = client.get(
        "/api/v1/memories?status=DELETED", headers=auth_headers
    ).json()
    assert any(item["id"] == created["id"] for item in deleted["data"])


def test_delete_unknown_returns_404(client, auth_headers):
    """A bogus memory id cannot be deleted even with a well-formed request."""
    resp = client.delete("/api/v1/memories/nope", headers=auth_headers)
    # No credentials at all -> the approval gate refuses before existence leak.
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"


def test_double_delete_rejected(
    client, auth_headers, sample_memory_payload, approved_forget
):
    """A second forget attempt on an already-forgotten memory is refused.

    Before JW-01 this returned 422 from ``memory_service.delete``. Now the
    refusal happens one layer earlier: the memory is already DELETED, so a new
    forget request is rejected with 409 ``memory_not_forgettable``.
    """
    created = _create(client, auth_headers, sample_memory_payload)
    approved_forget.forget(created["id"])

    resp = client.post(
        f"/api/v1/memories/{created['id']}/forget-requests",
        json={"reason": "again"},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "memory_not_forgettable"
    # Reusing the original approved request must also be impossible.
    replay = client.delete(
        f"/api/v1/memories/{created['id']}", headers=auth_headers
    )
    assert replay.status_code == 403


def test_deleted_memory_cannot_be_updated(
    client, auth_headers, sample_memory_payload, approved_forget
):
    created = _create(client, auth_headers, sample_memory_payload)
    approved_forget.forget(created["id"])
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"content": "nope"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_status_cannot_be_set_to_deleted_via_patch(
    client, auth_headers, sample_memory_payload
):
    created = _create(client, auth_headers, sample_memory_payload)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "DELETED"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
