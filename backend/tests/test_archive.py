"""Archive and restore transition tests."""


def _create(client, headers, status="ACTIVE"):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "archivable", "status": status},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_active_to_archived(client, auth_headers):
    created = _create(client, auth_headers)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "ARCHIVED"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ARCHIVED"


def test_archived_to_active(client, auth_headers):
    created = _create(client, auth_headers, status="ARCHIVED")
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "ACTIVE"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ACTIVE"


def test_active_to_deleted_rejected(client, auth_headers):
    created = _create(client, auth_headers)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "DELETED"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_archived_to_deleted_rejected(client, auth_headers):
    created = _create(client, auth_headers, status="ARCHIVED")
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "DELETED"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_deleted_cannot_be_restored(client, auth_headers, approved_forget):
    created = _create(client, auth_headers)
    # JW-01: forgetting requires the approved forget-request flow.
    approved_forget.forget(created["id"])
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "ACTIVE"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_same_status_is_noop(client, auth_headers):
    created = _create(client, auth_headers)
    resp = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "ACTIVE"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ACTIVE"


def test_archived_hidden_from_default_list(client, auth_headers):
    created = _create(client, auth_headers)
    client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"status": "ARCHIVED"},
        headers=auth_headers,
    )
    active = client.get("/api/v1/memories", headers=auth_headers).json()
    assert all(item["id"] != created["id"] for item in active["data"])
    archived = client.get(
        "/api/v1/memories?status=ARCHIVED", headers=auth_headers
    ).json()
    assert any(item["id"] == created["id"] for item in archived["data"])
