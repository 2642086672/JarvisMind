"""Audit log query tests."""


def _create(client, headers, content="original content"):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": content},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_audit_trail_records_lifecycle(client, auth_headers, approved_forget):
    """JW-01: forgetting adds request/approve/execute entries instead of one
    unaudited SOFT_DELETE record.

    The audit trail is now strictly richer: it names the request, the approval
    and the execution, so "who decided to forget this" is answerable.
    """
    created = _create(client, auth_headers)
    memory_id = created["id"]
    client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"content": "updated content"},
        headers=auth_headers,
    )
    approved_forget.forget(memory_id)

    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs", headers=auth_headers
    ).json()
    actions = [entry["action"] for entry in body["data"]]
    # Newest first (DESC default).
    assert actions[0] == "FORGET_EXECUTED"
    assert "FORGET_APPROVED" in actions
    assert "FORGET_REQUESTED" in actions
    assert "UPDATE" in actions
    assert "CREATE" in actions
    # The legacy unaudited path must no longer be reachable.
    assert "SOFT_DELETE" not in actions
    assert body["meta"]["total"] == len(actions)


def test_audit_entries_have_expected_fields(client, auth_headers):
    created = _create(client, auth_headers)
    entry = client.get(
        f"/api/v1/memories/{created['id']}/audit-logs", headers=auth_headers
    ).json()["data"][0]
    assert set(entry) == {"action", "actor", "detail", "created_at"}
    assert entry["action"] == "CREATE"
    assert entry["actor"] == "system"
    assert entry["detail"]["memory_type"] == "SEMANTIC"


def test_audit_log_sort_ascending(client, auth_headers):
    created = _create(client, auth_headers)
    memory_id = created["id"]
    client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"importance": 9.0},
        headers=auth_headers,
    )
    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc",
        headers=auth_headers,
    ).json()
    assert [entry["action"] for entry in body["data"]] == ["CREATE", "UPDATE"]


def test_audit_log_pagination(client, auth_headers):
    created = _create(client, auth_headers)
    memory_id = created["id"]
    for index in range(4):
        client.patch(
            f"/api/v1/memories/{memory_id}",
            json={"importance": float(index)},
            headers=auth_headers,
        )
    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?page=2&page_size=2",
        headers=auth_headers,
    ).json()
    assert body["meta"]["total"] == 5
    assert body["meta"]["total_pages"] == 3
    assert len(body["data"]) == 2


def test_audit_log_unknown_memory_returns_404(client, auth_headers):
    resp = client.get("/api/v1/memories/nope/audit-logs", headers=auth_headers)
    assert resp.status_code == 404


def test_audit_log_requires_token(client):
    assert client.get("/api/v1/memories/x/audit-logs").status_code == 401


def test_audit_log_has_no_sensitive_data(client, auth_headers):
    created = _create(client, auth_headers)
    text = client.get(
        f"/api/v1/memories/{created['id']}/audit-logs", headers=auth_headers
    ).text.lower()
    for forbidden in ("token", "select ", "sqlite", "traceback", "password"):
        assert forbidden not in text
