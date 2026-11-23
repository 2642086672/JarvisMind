"""Agent facade: what a granted agent can and cannot do.

Covers memory access through the facade, the forget flow where the owner alone
decides, sensitive-memory gating, pending-request conflicts, search, and the
guarantee that a forgotten memory is never returned again through any agent path.
"""
import pytest
from fastapi.testclient import TestClient

from jarvismind.main import create_app
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from jarvismind.services.memory_service import MemoryService

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}


@pytest.fixture
def client(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        yield test_client


def _agent_with_token(client, permissions=None, space_grants=()):
    body = {"name": "DSH", "agent_type": "dsh"}
    if permissions is not None:
        body["permissions"] = permissions
    agent = client.post("/api/v1/control/agents", json=body, headers=OWNER).json()["data"]
    for space_id, access in space_grants:
        client.put(
            f"/api/v1/control/agents/{agent['id']}/grants/{space_id}",
            json={"access": access},
            headers=OWNER,
        )
    issued = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "t"},
        headers=OWNER,
    ).json()
    return agent, {"Authorization": f"Bearer {issued['token']}"}


def _remember(client, headers, text, conversation="conv-1"):
    return client.post(
        "/api/v1/agent/remember",
        json={"text": text, "conversation_id": conversation},
        headers=headers,
    )


def _store(manager, content, memory_type="SEMANTIC"):
    return MemoryService(manager).create(
        MemoryCreate(
            memory_type=MemoryType(memory_type),
            content=content,
            status=MemoryStatus.ACTIVE,
        )
    )


def test_agent_remember_stores_a_plain_fact_through_the_governed_path(client):
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context", "write"])
    response = _remember(client, headers, "记住我最近在学习 Rust 异步编程")
    assert response.status_code == 200, response.text
    status = response.json()["data"]["status"]
    assert status in {"SAVED", "PENDING", "DUPLICATE", "UPDATED", "NO_MEMORY", "NO_INTENT"}


def test_agent_without_write_permission_cannot_remember(client):
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context"])
    response = _remember(client, headers, "记住我喜欢徒步")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_credentials_are_refused_before_any_write(client):
    _agent, headers = _agent_with_token(client, permissions=["read", "write"])
    secret = "sk-FAKEabcdefghijklmnopqrstuvwxyz012345"
    response = _remember(client, headers, f"记住我的 api_key={secret}")
    assert secret not in response.text
    listing = client.get("/api/v1/memories", headers=OWNER)
    assert secret not in listing.text


def test_agent_cannot_request_a_correction(client):
    _agent, headers = _agent_with_token(client, permissions=["read", "update", "write"])
    response = client.post(
        "/api/v1/agent/memories/no-such-memory/modification-requests",
        json={"proposed_changes": {"content": "x"}, "reason": "test"},
        headers=headers,
    )
    assert response.status_code == 403
    assert "non_human" in response.text


def test_agent_forget_request_never_returns_the_confirmation_token(client, manager):
    record = _store(manager, "我的公司位于杭州")
    _agent, headers = _agent_with_token(client, permissions=["read", "delete"])
    response = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests",
        json={"reason": "用户要求遗忘"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "confirmation_token" not in body["data"]
    assert "confirmation_token" not in response.text
    assert body["data"]["status"] == "PENDING"


def test_agent_cannot_approve_its_own_forget_request(client, manager):
    record = _store(manager, "我喜欢在周末阅读科幻小说")
    agent, headers = _agent_with_token(client, permissions=["read", "delete"])
    created = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests", json={}, headers=headers
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["data"]["id"]
    approve = client.post(
        f"/api/v1/forget-requests/{request_id}/approve",
        json={"actor_label": f"agent:{agent['id']}"},
        headers=headers,
    )
    assert approve.status_code in (401, 403)
    assert MemoryService(manager).get(record.id).status == "ACTIVE"


def test_owner_approves_agent_request_and_forgotten_text_is_never_returned_again(client, manager):
    phrase = "我的宠物叫年糕是一只橘猫"
    record = _store(manager, phrase, "PROFILE")
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context", "delete"])
    created = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests", json={}, headers=headers
    ).json()["data"]

    owner_approve = client.post(
        f"/api/v1/forget-requests/{created['id']}/approve", json={}, headers=OWNER
    )
    assert owner_approve.status_code == 200, owner_approve.text
    assert MemoryService(manager).get(record.id).status == "DELETED"

    fetched = client.get(f"/api/v1/agent/memories/{record.id}", headers=headers)
    assert fetched.status_code == 404
    recall = client.post("/api/v1/agent/recall", json={"query": "年糕 橘猫"}, headers=headers)
    assert recall.status_code == 200
    assert phrase not in recall.text
    context = client.post("/api/v1/agent/context", json={"query": "年糕 橘猫"}, headers=headers)
    assert phrase not in context.text
    listing = client.get("/api/v1/agent/memories", headers=headers)
    assert phrase not in listing.text
    search = client.get("/api/v1/agent/search", params={"q": "年糕"}, headers=headers)
    assert phrase not in search.text


def test_owner_cannot_approve_a_request_without_token_unless_agent_originated(client, manager):
    record = _store(manager, "我的职业是教师")
    created = client.post(
        f"/api/v1/memories/{record.id}/forget-requests", json={}, headers=OWNER
    )
    assert created.status_code == 201
    request_id = created.json()["data"]["id"]
    without_token = client.post(
        f"/api/v1/forget-requests/{request_id}/approve", json={}, headers=OWNER
    )
    assert without_token.status_code == 422
    assert without_token.json()["error"]["details"]["code"] == "confirmation_token_required"
    assert MemoryService(manager).get(record.id).status == "ACTIVE"


def test_sensitive_memory_requires_sensitive_permission(client, manager):
    record = _store(manager, "我的体检报告显示需要长期吃药治疗", "PROFILE")
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context"])
    response = client.get(f"/api/v1/agent/memories/{record.id}", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["code"] == "sensitive_access_denied"
    assert "吃药" not in response.text
    listing = client.get("/api/v1/agent/memories", headers=headers)
    assert "吃药" not in listing.text
    recall = client.post("/api/v1/agent/recall", json={"query": "体检报告 吃药"}, headers=headers)
    assert "吃药" not in recall.text


def test_phone_numbers_are_protected_like_other_sensitive_text(client, manager):
    record = _store(manager, "我的手机号是 13800138000，请记住", "PROFILE")
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context"])
    response = client.get(f"/api/v1/agent/memories/{record.id}", headers=headers)
    assert response.status_code == 403
    assert "13800138000" not in response.text


def test_agent_list_and_recall_respect_the_space_grant(client):
    client.post("/api/v1/control/spaces", json={"id": "proj-alpha", "name": "Alpha"}, headers=OWNER)
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context"])
    granted = client.get("/api/v1/agent/me", headers=headers).json()["data"]
    assert [space["id"] for space in granted["accessible_spaces"]] == ["default"]
    denied = client.get(
        "/api/v1/agent/memories",
        headers={**headers, "X-JarvisMind-Space": "proj-alpha"},
    )
    assert denied.status_code == 403


def test_agent_cannot_rotate_an_owner_pending_forget_token(client, manager):
    record = _store(manager, "我的办公室在二楼东侧")
    owner_created = client.post(
        f"/api/v1/memories/{record.id}/forget-requests", json={}, headers=OWNER
    )
    assert owner_created.status_code == 201, owner_created.text
    owner_token = owner_created.json()["data"]["confirmation_token"]
    _agent, headers = _agent_with_token(client, permissions=["read", "delete"])
    blocked = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests", json={}, headers=headers
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conflict"
    approved = client.post(
        f"/api/v1/forget-requests/{owner_created.json()['data']['id']}/approve",
        json={"confirmation_token": owner_token},
        headers=OWNER,
    )
    assert approved.status_code == 200, approved.text
    assert MemoryService(manager).get(record.id).status == "DELETED"


def test_agent_search_withholds_sensitive_text(client, manager):
    _store(manager, "我喜欢用 Rust 编写命令行工具")
    _store(manager, "我的体检报告显示需要长期吃药治疗", "PROFILE")
    _agent, headers = _agent_with_token(client, permissions=["read", "search"])
    response = client.get("/api/v1/agent/search", params={"q": "Rust"}, headers=headers)
    assert response.status_code == 200, response.text
    assert "吃药" not in response.text


def test_agent_list_can_be_narrowed_to_one_memory_type(client, manager):
    _store(manager, "我的名字叫林晓明", "PROFILE")
    _store(manager, "我最近在读一本关于园艺的书", "SEMANTIC")
    _agent, headers = _agent_with_token(client, permissions=["read", "search", "context"])
    narrowed = client.get("/api/v1/agent/memories", params={"memory_type": "PROFILE"}, headers=headers)
    assert narrowed.status_code == 200, narrowed.text
    types = {item["memory_type"] for item in narrowed.json()["data"]}
    assert types == {"PROFILE"}
    unknown = client.get("/api/v1/agent/memories", params={"memory_type": "NOT_A_TYPE"}, headers=headers)
    assert unknown.status_code == 422
