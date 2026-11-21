"""API token authentication tests."""
from conftest import TEST_TOKEN


def test_missing_token_rejected(client):
    resp = client.get("/api/v1/memories")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_wrong_token_rejected(client):
    resp = client.get("/api/v1/memories", headers={"X-API-Token": "not-the-token"})
    assert resp.status_code == 401


def test_token_not_echoed_in_error(client):
    resp = client.get("/api/v1/memories", headers={"X-API-Token": "not-the-token"})
    assert "not-the-token" not in resp.text


def test_bearer_token_accepted(client):
    resp = client.get(
        "/api/v1/memories", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    assert resp.status_code == 200


def test_wrong_bearer_scheme_rejected(client):
    resp = client.get(
        "/api/v1/memories", headers={"Authorization": f"Basic {TEST_TOKEN}"}
    )
    assert resp.status_code == 401


def test_write_requires_token(client, sample_memory_payload):
    resp = client.post("/api/v1/memories", json=sample_memory_payload)
    assert resp.status_code == 401
