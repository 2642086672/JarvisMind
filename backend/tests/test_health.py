"""Health endpoint tests."""


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database_connected"] is True
    assert body["schema_version"] >= 1
    assert body["version"]


def test_health_needs_no_token(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_degraded_when_db_down(client, manager, monkeypatch):
    monkeypatch.setattr(manager, "verify_connection", lambda: False)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert resp.json()["database_connected"] is False
