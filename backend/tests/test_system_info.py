"""System information endpoint tests."""
from conftest import TEST_TOKEN


def test_requires_token(client):
    assert client.get("/api/v1/system/info").status_code == 401


def test_info_ok(client, auth_headers):
    resp = client.get("/api/v1/system/info", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "JarvisMind"
    assert body["database_type"] == "sqlite"
    assert body["status"] == "running"
    assert body["python_version"]
    assert body["data_dir"]
    assert set(body["memory_types"]) == {
        "PROFILE",
        "SEMANTIC",
        "EPISODIC",
        "PROJECT",
        "WORKING",
        "REFLECTION",
    }
    assert set(body["statuses"]) == {"ACTIVE", "ARCHIVED", "DELETED", "MERGED"}


def test_bearer_token_accepted(client):
    resp = client.get(
        "/api/v1/system/info", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
    )
    assert resp.status_code == 200
