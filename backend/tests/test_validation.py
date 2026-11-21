"""Input validation tests."""

LONG_CONTENT = "x" * 100_001


def test_unknown_memory_type_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "BOGUS", "content": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_empty_content_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_content_too_long_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": LONG_CONTENT},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_importance_out_of_range_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "x", "importance": 11.0},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_confidence_out_of_range_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "x", "confidence": 1.5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_unknown_field_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "x", "surprise": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_invalid_expires_at_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "x", "expires_at": "not-a-date"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_invalid_sort_by_rejected(client, auth_headers):
    resp = client.get(
        "/api/v1/memories?sort_by=password", headers=auth_headers
    )
    assert resp.status_code == 422


def test_invalid_sort_order_rejected(client, auth_headers):
    resp = client.get("/api/v1/memories?sort_order=sideways", headers=auth_headers)
    assert resp.status_code == 422


def test_empty_patch_rejected(client, auth_headers, sample_memory_payload):
    created = client.post(
        "/api/v1/memories", json=sample_memory_payload, headers=auth_headers
    ).json()["data"]
    resp = client.patch(
        f"/api/v1/memories/{created['id']}", json={}, headers=auth_headers
    )
    assert resp.status_code == 422


def test_error_response_does_not_leak_internals(client, auth_headers):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": ""},
        headers=auth_headers,
    )
    text = resp.text.lower()
    for forbidden in ("traceback", "sqlite3", "sqlalchemy", "file \""):
        assert forbidden not in text
