"""``mode="direct"`` on the agent remember endpoint.

The agent facade gained a second addressing mode: a caller that has already
decided a statement is worth remembering can skip the trigger-word heuristic.
These tests pin the boundary of that change — what ``direct`` may skip, and
every governance step it must still pass.
"""
import pytest
from fastapi.testclient import TestClient

from jarvismind.main import create_app
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from jarvismind.services.memory_service import MemoryService

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}
GRANTS = ["read", "search", "context", "write"]


@pytest.fixture
def client(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        yield test_client


def _agent_headers(client):
    agent = client.post(
        "/api/v1/control/agents",
        json={"name": "DSH", "agent_type": "dsh", "permissions": GRANTS},
        headers=OWNER,
    ).json()["data"]
    issued = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "t"},
        headers=OWNER,
    ).json()
    return agent, {"Authorization": f"Bearer {issued['token']}"}


def _remember(client, headers, text, *, mode=None, conversation="conv-direct"):
    payload = {"text": text, "conversation_id": conversation}
    if mode is not None:
        payload["mode"] = mode
    return client.post("/api/v1/agent/remember", json=payload, headers=headers)


def _store(manager, content, memory_type="SEMANTIC"):
    return MemoryService(manager).create(
        MemoryCreate(
            memory_type=MemoryType(memory_type),
            content=content,
            status=MemoryStatus.ACTIVE,
        )
    )


def _active_contents(manager):
    with manager.session() as session:
        from jarvismind.database.models import MemoryRecord

        return [
            row.content
            for row in session.query(MemoryRecord)
            .filter(MemoryRecord.status == "ACTIVE")
            .all()
        ]


# --- the gap this mode closes ---------------------------------------------


def test_a_plain_statement_without_a_trigger_word_is_refused_in_auto_mode(client):
    """The original behavior is unchanged: no trigger word, no memory."""
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, "用户平时在 Mac 上开发 JarvisMind。", mode="auto")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "NO_INTENT"


def test_the_same_statement_is_stored_in_direct_mode(client, manager):
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, "用户平时在 Mac 上开发 JarvisMind。", mode="direct")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "SAVED", body
    assert any("JarvisMind" in content for content in _active_contents(manager))


def test_omitting_mode_keeps_the_original_auto_behavior(client):
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, "用户平时在 Mac 上开发 JarvisMind。")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "NO_INTENT"


def test_a_companion_style_distilled_memory_is_stored_in_direct_mode(client, manager):
    """A memory the agent distilled about a shared moment.

    ``auto`` rejects this as a third-party statement; that heuristic exists to
    guess whether a raw user sentence is a memory request, and it misfires on a
    memory the agent wrote itself.
    """
    _agent, headers = _agent_headers(client)
    text = "用户说最近工作压力很大，我陪他聊了很久，约定以后先听他说完再给建议。"
    assert _remember(client, headers, text, mode="auto").json()["data"]["status"] in {
        "NO_INTENT",
        "NO_MEMORY",
    }
    response = _remember(client, headers, text, mode="direct")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "SAVED", response.text


# --- what direct mode must never skip -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "用户的 API key 是 sk-abcdef0123456789abcdef0123456789",
        "用户的密码是 hunter2hunter2",
        "用户的 token 是 jmt_c0f26d38_" + "A" * 43,
        "用户的私钥是 -----BEGIN RSA PRIVATE KEY-----MIIEow",
    ],
)
def test_direct_mode_still_blocks_credentials(client, manager, text):
    """The credential gate is not an intent heuristic and must always run."""
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, text, mode="direct")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "DENIED", body
    assert not any("sk-abcdef" in content for content in _active_contents(manager))
    assert not any("hunter2" in content for content in _active_contents(manager))
    assert not any("jmt_c0f26d38" in content for content in _active_contents(manager))


def test_direct_mode_still_holds_sensitive_personal_facts(client, manager):
    """JMR sensitivity governance is unchanged: a sensitive fact is held, not written."""
    _agent, headers = _agent_headers(client)
    text = "用户的身份证号是 110101199001011234"
    response = _remember(client, headers, text, mode="direct")
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "PROTECTED", body
    assert not any("110101199001011234" in content for content in _active_contents(manager))


def test_direct_mode_still_de_duplicates(client, manager):
    _agent, headers = _agent_headers(client)
    text = "用户平时在 Mac 上开发 JarvisMind。"
    first = _remember(client, headers, text, mode="direct").json()["data"]
    assert first["status"] == "SAVED"
    second = _remember(client, headers, text, mode="direct").json()["data"]
    assert second["status"] == "DUPLICATE", second
    assert sum(1 for content in _active_contents(manager) if "JarvisMind" in content) == 1


def test_direct_mode_still_requires_the_write_permission(client):
    agent = client.post(
        "/api/v1/control/agents",
        json={"name": "no-write", "agent_type": "dsh", "permissions": ["read", "search", "context"]},
        headers=OWNER,
    ).json()["data"]
    issued = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "t"},
        headers=OWNER,
    ).json()
    headers = {"Authorization": f"Bearer {issued['token']}"}
    response = _remember(client, headers, "用户平时在 Mac 上开发 JarvisMind。", mode="direct")
    assert response.status_code in {401, 403}, response.text


# --- input hygiene --------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", " ", "\n\t "])
def test_direct_mode_refuses_empty_input(client, text):
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, text, mode="direct")
    # An empty body is rejected by schema validation; whitespace by the service.
    assert response.status_code in {200, 422}, response.text
    if response.status_code == 200:
        assert response.json()["data"]["status"] in {"NO_MEMORY", "NO_INTENT"}


@pytest.mark.parametrize("text", ["嗯", "好", "谢谢", "。", "多谢", "好的", "收到", "thanks", "辛苦了"])
def test_direct_mode_refuses_meaningless_one_character_input(client, manager, text):
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, text, mode="direct")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] in {"NO_MEMORY", "NO_INTENT"}
    assert not any(content.strip() == text.strip() for content in _active_contents(manager))


@pytest.mark.parametrize(
    "text",
    [
        "谢谢，我叫年糕",
        "好的，以后都叫我年糕",
        "收到。我在用 Rust 写这个项目",
    ],
)
def test_a_pleasantry_with_a_real_fact_is_still_stored(client, manager, text):
    """The noise filter matches the whole message, never a substring."""
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, text, mode="direct")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "SAVED", response.text


def test_an_unknown_mode_is_rejected_by_the_schema(client):
    _agent, headers = _agent_headers(client)
    response = _remember(client, headers, "用户喜欢年糕", mode="privileged")
    assert response.status_code == 422, response.text
