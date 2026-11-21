"""Control plane: spaces, agents, grants, tokens, and the owner-only surface.

These tests drive the real HTTP application and check the behaviour an operator
and an agent can observe.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jarvismind.control.tokens import TOKEN_PREFIX
from jarvismind.main import create_app

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}


@pytest.fixture
def client(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        yield test_client


def _create_agent(client, **overrides):
    body = {"name": "DSH", "agent_type": "dsh", **overrides}
    response = client.post("/api/v1/control/agents", json=body, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _issue_token(client, agent_id, **overrides):
    response = client.post(
        f"/api/v1/control/agents/{agent_id}/tokens",
        json={"name": "primary", **overrides},
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _agent_headers(token, space=None):
    headers = {"Authorization": f"Bearer {token}"}
    if space is not None:
        headers["X-JarvisMind-Space"] = space
    return headers


def test_default_space_is_registered_on_first_use(client):
    response = client.get("/api/v1/control/spaces", headers=OWNER)
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "default" in ids


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "a/b", "Upper", "x", "has space", "..", "", "a" * 64, "dot.name"],
)
def test_space_id_is_validated_before_any_path_is_built(client, bad_id):
    response = client.post(
        "/api/v1/control/spaces",
        json={"id": bad_id, "name": "bad"},
        headers=OWNER,
    )
    assert response.status_code in (400, 422)


def test_space_create_provisions_isolated_database(client, settings):
    response = client.post(
        "/api/v1/control/spaces",
        json={"id": "work-space", "name": "Work", "space_type": "work"},
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    database = settings.data_dir / "spaces" / "work-space" / "jarvismind.db"
    assert database.is_file()
    assert response.json()["data"]["schema_version"] >= 6


def test_duplicate_space_id_is_a_conflict(client):
    body = {"id": "dup-space", "name": "One"}
    assert client.post("/api/v1/control/spaces", json=body, headers=OWNER).status_code == 201
    second = client.post("/api/v1/control/spaces", json=body, headers=OWNER)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_default_space_cannot_be_archived(client):
    response = client.patch(
        "/api/v1/control/spaces/default", json={"status": "archived"}, headers=OWNER
    )
    assert response.status_code == 409


def test_agent_defaults_to_least_privilege(client):
    agent = _create_agent(client)
    assert sorted(agent["permissions"]) == ["context", "read", "search"]
    assert agent["default_space_id"] == "default"
    assert agent["grants"] == [{"space_id": "default", "access": "readwrite"}]


def test_unknown_permission_is_rejected(client):
    response = client.post(
        "/api/v1/control/agents",
        json={"name": "x", "permissions": ["root"]},
        headers=OWNER,
    )
    assert response.status_code == 422


def test_token_is_shown_once_and_never_stored_in_plaintext(client, settings):
    agent = _create_agent(client)
    issued = _issue_token(client, agent["id"])
    plaintext = issued["token"]
    assert plaintext.startswith(TOKEN_PREFIX)
    assert "token_hash" not in issued["data"]

    listing = client.get("/api/v1/control/tokens", headers=OWNER).text
    assert plaintext not in listing

    control_files = [
        settings.data_dir / name
        for name in ("control.db", "control.db-wal", "control.db-shm")
        if (settings.data_dir / name).exists()
    ]
    raw_bytes = b"".join(path.read_bytes() for path in control_files)
    assert plaintext.encode("utf-8") not in raw_bytes
    with client.app.state.control.database.session() as session:
        digests = [row[0] for row in session.execute(text("SELECT token_hash FROM api_tokens"))]
    assert digests and all(len(digest) == 64 for digest in digests)


def test_agent_token_authenticates_and_reads_its_identity(client):
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    response = client.get("/api/v1/agent/me", headers=_agent_headers(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "agent"
    assert data["agent_id"] == agent["id"]
    assert "*" not in data["permissions"]


def test_revoked_token_is_refused(client):
    agent = _create_agent(client)
    issued = _issue_token(client, agent["id"])
    revoke = client.post(f"/api/v1/control/tokens/{issued['data']['id']}/revoke", headers=OWNER)
    assert revoke.status_code == 200
    response = client.get("/api/v1/agent/me", headers=_agent_headers(issued["token"]))
    assert response.status_code == 401


def test_expired_token_is_refused(client, settings):
    agent = _create_agent(client)
    issued = _issue_token(client, agent["id"], expires_in_days=1)
    with client.app.state.control.database.session() as session:
        session.execute(
            text("UPDATE api_tokens SET expires_at = :expires WHERE id = :token_id"),
            {"expires": "2000-01-01T00:00:00+00:00", "token_id": issued["data"]["id"]},
        )
    response = client.get("/api/v1/agent/me", headers=_agent_headers(issued["token"]))
    assert response.status_code == 401


def test_rotation_replaces_the_token_and_invalidates_the_old_one(client):
    agent = _create_agent(client)
    original = _issue_token(client, agent["id"])
    rotated = client.post(
        f"/api/v1/control/tokens/{original['data']['id']}/rotate", headers=OWNER
    )
    assert rotated.status_code == 201
    new_plain = rotated.json()["token"]
    assert new_plain != original["token"]
    assert client.get("/api/v1/agent/me", headers=_agent_headers(original["token"])).status_code == 401
    assert client.get("/api/v1/agent/me", headers=_agent_headers(new_plain)).status_code == 200


def test_disabled_agent_loses_access_immediately(client):
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    response = client.patch(
        f"/api/v1/control/agents/{agent['id']}", json={"status": "disabled"}, headers=OWNER
    )
    assert response.status_code == 200
    assert client.get("/api/v1/agent/me", headers=_agent_headers(token)).status_code == 401


def test_deleted_agent_revokes_its_tokens(client):
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    assert client.delete(f"/api/v1/control/agents/{agent['id']}", headers=OWNER).status_code == 200
    assert client.get("/api/v1/agent/me", headers=_agent_headers(token)).status_code == 401


def test_token_scope_can_narrow_but_never_widen(client):
    agent = _create_agent(client, permissions=["read", "search"])
    too_wide = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "wide", "scopes": ["read", "delete"]},
        headers=OWNER,
    )
    assert too_wide.status_code == 422
    narrow = _issue_token(client, agent["id"], scopes=["read"])
    assert narrow["data"]["scopes"] == ["read"]


def test_permission_ceiling_clamps_existing_token_scopes(client):
    agent = _create_agent(client, permissions=["read", "search", "context"])
    token = _issue_token(client, agent["id"])["token"]
    assert client.patch(
        f"/api/v1/control/agents/{agent['id']}",
        json={"permissions": ["read"]},
        headers=OWNER,
    ).status_code == 200
    response = client.post(
        "/api/v1/agent/context", json={"query": "anything"}, headers=_agent_headers(token)
    )
    assert response.status_code == 403


def test_owner_only_routes_refuse_agent_credentials(client):
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    headers = _agent_headers(token)
    assert client.get("/api/v1/control/agents", headers=headers).status_code == 403
    assert client.get("/api/v1/console/dashboard", headers=headers).status_code == 403
    assert client.get("/api/v1/memories", headers=headers).status_code == 403


def test_missing_or_wrong_credentials_are_unauthorized(client):
    assert client.get("/api/v1/control/agents").status_code == 401
    assert client.get("/api/v1/control/agents", headers={"X-API-Token": "wrong"}).status_code == 401


def test_agent_cannot_reach_ungranted_space_and_cannot_probe_existence(client):
    client.post("/api/v1/control/spaces", json={"id": "private-x", "name": "P"}, headers=OWNER)
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    ungranted = client.get("/api/v1/agent/me", headers=_agent_headers(token, "private-x"))
    missing = client.get("/api/v1/agent/me", headers=_agent_headers(token, "no-such-space"))
    assert ungranted.status_code == 403
    assert missing.status_code == 403
    assert ungranted.json()["error"]["code"] == missing.json()["error"]["code"]


def test_agent_with_read_grant_cannot_write(client):
    client.post("/api/v1/control/spaces", json={"id": "shared-x", "name": "S"}, headers=OWNER)
    agent = _create_agent(client, permissions=["read", "search", "context", "write"])
    client.put(
        f"/api/v1/control/agents/{agent['id']}/grants/shared-x",
        json={"access": "read"},
        headers=OWNER,
    )
    token = _issue_token(client, agent["id"])["token"]
    response = client.post(
        "/api/v1/agent/remember",
        json={"text": "我喜欢喝绿茶", "conversation_id": "c1"},
        headers=_agent_headers(token, "shared-x"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "space_access_denied"


def test_archived_space_disappears_for_agents_and_owners(client):
    client.post("/api/v1/control/spaces", json={"id": "gone-space", "name": "G"}, headers=OWNER)
    agent = _create_agent(client)
    client.put(
        f"/api/v1/control/agents/{agent['id']}/grants/gone-space",
        json={"access": "read"},
        headers=OWNER,
    )
    token = _issue_token(client, agent["id"])["token"]
    assert client.patch(
        "/api/v1/control/spaces/gone-space", json={"status": "archived"}, headers=OWNER
    ).status_code == 200
    agent_view = client.get("/api/v1/agent/me", headers=_agent_headers(token, "gone-space"))
    assert agent_view.status_code == 403
    owner_view = client.get(
        "/api/v1/memories", headers={**OWNER, "X-JarvisMind-Space": "gone-space"}
    )
    assert owner_view.status_code == 404


def test_access_log_records_metadata_only(client):
    agent = _create_agent(client)
    token = _issue_token(client, agent["id"])["token"]
    client.post(
        "/api/v1/agent/recall",
        json={"query": "secret-probe-phrase-xyz"},
        headers=_agent_headers(token),
    )
    items, total = client.app.state.control.list_access_logs(limit=50, offset=0)
    assert total >= 1
    serialized = repr(items)
    assert "secret-probe-phrase-xyz" not in serialized
    assert token not in serialized
    assert TEST_TOKEN not in serialized
    assert "testclient" not in serialized
    recorded = [item for item in items if item["route"].endswith("/recall")]
    assert recorded and recorded[0]["status_code"] == 200


def test_rate_limit_returns_retry_after(settings, manager):
    from dataclasses import replace

    limited_settings = replace(settings, api_rate_limit_owner_per_minute=3)
    with TestClient(create_app(limited_settings, manager=manager)) as limited:
        codes = [
            limited.get("/api/v1/control/agents", headers=OWNER).status_code for _ in range(5)
        ]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes[3:]
        blocked = limited.get("/api/v1/control/agents", headers=OWNER)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert blocked.json()["error"]["code"] == "rate_limited"
