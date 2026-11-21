"""Memory source CRUD tests."""
from jarvismind.database.models import MemoryRecord


def _create_source(client, headers, payload):
    resp = client.post("/api/v1/memory-sources", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _memory_with_source(client, headers, source_id):
    return client.post(
        "/api/v1/memories",
        json={
            "memory_type": "SEMANTIC",
            "content": "memory attached to a source",
            "source_id": source_id,
        },
        headers=headers,
    )


def test_create_and_get_source(client, auth_headers):
    created = _create_source(
        client,
        auth_headers,
        {"source_type": "CHAT", "source_name": "Chat session 42", "uri": "chat://42"},
    )
    assert created["id"]
    assert created["source_type"] == "CHAT"
    assert created["uri"] == "chat://42"

    resp = client.get(
        f"/api/v1/memory-sources/{created['id']}", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["source_name"] == "Chat session 42"


def test_list_sources(client, auth_headers):
    _create_source(client, auth_headers, {"source_type": "CHAT", "source_name": "a"})
    _create_source(client, auth_headers, {"source_type": "FILE", "source_name": "b"})
    body = client.get("/api/v1/memory-sources", headers=auth_headers).json()
    assert body["meta"]["total"] == 2

    filtered = client.get(
        "/api/v1/memory-sources?source_type=FILE", headers=auth_headers
    ).json()
    assert filtered["meta"]["total"] == 1
    assert filtered["data"][0]["source_type"] == "FILE"


def test_update_source(client, auth_headers):
    created = _create_source(
        client, auth_headers, {"source_type": "CHAT", "source_name": "old"}
    )
    resp = client.patch(
        f"/api/v1/memory-sources/{created['id']}",
        json={"source_name": "new", "uri": None},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["source_name"] == "new"
    assert body["uri"] is None


def test_delete_source(client, auth_headers):
    created = _create_source(
        client, auth_headers, {"source_type": "NOTE", "source_name": "temp"}
    )
    resp = client.delete(
        f"/api/v1/memory-sources/{created['id']}", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["unlinked_memories"] == 0
    assert (
        client.get(
            f"/api/v1/memory-sources/{created['id']}", headers=auth_headers
        ).status_code
        == 404
    )


def test_invalid_source_type_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memory-sources",
        json={"source_type": "BOGUS", "source_name": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_empty_source_name_rejected(client, auth_headers):
    resp = client.post(
        "/api/v1/memory-sources",
        json={"source_type": "CHAT", "source_name": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_unknown_source_returns_404(client, auth_headers):
    assert (
        client.get("/api/v1/memory-sources/nope", headers=auth_headers).status_code
        == 404
    )


def test_memory_with_unknown_source_rejected(client, auth_headers):
    resp = _memory_with_source(client, auth_headers, "does-not-exist")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_memory_with_valid_source_accepted(client, auth_headers):
    source = _create_source(
        client, auth_headers, {"source_type": "CHAT", "source_name": "chat"}
    )
    resp = _memory_with_source(client, auth_headers, source["id"])
    assert resp.status_code == 201
    assert resp.json()["data"]["source_id"] == source["id"]


def test_deleting_source_keeps_memory(client, auth_headers, manager):
    source = _create_source(
        client, auth_headers, {"source_type": "CHAT", "source_name": "chat"}
    )
    created = _memory_with_source(client, auth_headers, source["id"]).json()["data"]

    resp = client.delete(
        f"/api/v1/memory-sources/{source['id']}", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["unlinked_memories"] == 1

    with manager.session() as session:
        record = session.get(MemoryRecord, created["id"])
        assert record is not None
        assert record.source_id is None

    fetched = client.get(
        f"/api/v1/memories/{created['id']}", headers=auth_headers
    ).json()["data"]
    assert fetched["source_id"] is None


def test_sources_require_token(client):
    assert client.get("/api/v1/memory-sources").status_code == 401
    assert (
        client.post(
            "/api/v1/memory-sources",
            json={"source_type": "CHAT", "source_name": "x"},
        ).status_code
        == 401
    )
