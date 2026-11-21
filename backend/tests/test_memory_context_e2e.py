"""JME-MEMORY-CONTEXT-01 · end-to-end: real write path, then real retrieval.

The reviewer's criterion for this task. Existing context tests seed rows with
the ORM, which leaves ``search_text`` empty, so hybrid search never returns
them and the merge/score path was never executed. That hid a 500
(``item.memory_id`` on a tuple). These tests go through the REAL service:

    MemoryService.create(...)  →  FTS index has search_text
    POST /api/v1/memories/context  →  200 with the memory

Nothing here calls memory_context internals directly; only the HTTP surface.
"""
from __future__ import annotations

from conftest import TEST_TOKEN

from jarvismind.schemas.memory import MemoryCreate, MemoryType

CONTEXT_URL = "/api/v1/memories/context"
HYBRID_URL = "/api/v1/memories/hybrid-search"


def _create(container, content: str, memory_type: MemoryType = MemoryType.PROFILE):
    """Write one formal memory through the real service (indexes search_text)."""
    return container.memory_service.create(
        MemoryCreate(memory_type=memory_type, content=content)
    )


def _post(client, headers, **body) -> dict:
    payload = {"query": "咖啡", **body}
    resp = client.post(CONTEXT_URL, json=payload, headers=headers)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    return resp.json()


# ------------------------------------------------------------------ A-1 regression
def test_real_write_then_context_returns_the_memory(client, auth_headers, app):
    """The exact reported failure: create 201, then query must be 200 with it."""
    container = app.state.container
    created = _create(container, "用户喜欢喝手冲咖啡")
    assert created.id

    body = _post(client, auth_headers, query="我喜欢喝什么咖啡")

    assert [item["memory_id"] for item in body["items"]] == [created.id]
    assert body["items"][0]["content"] == "用户喜欢喝手冲咖啡"
    assert body["items"][0]["relevance_score"] > 0


def test_real_write_is_visible_to_fts_before_context(client, auth_headers, app):
    """Guard the premise: the write path really indexed the row."""
    container = app.state.container
    created = _create(container, "用户喜欢喝手冲咖啡")

    resp = client.get(
        HYBRID_URL,
        params={"q": "手冲咖啡", "status": "ACTIVE"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    ids = [item["memory_id"] for item in resp.json()["data"]]
    assert created.id in ids


def test_context_merge_path_handles_multiple_real_memories(client, auth_headers, app):
    """Multiple candidates exercise the merge/score loop that used to crash."""
    container = app.state.container
    coffee = _create(container, "用户喜欢喝手冲咖啡")
    beans = _create(container, "用户常买浅烘咖啡豆")
    _create(container, "用户喜欢海边城市")

    body = _post(client, auth_headers, query="我喜欢喝什么咖啡", limit=10)

    ids = [item["memory_id"] for item in body["items"]]
    assert coffee.id in ids
    assert beans.id in ids


def test_real_write_then_identity_question(client, auth_headers, app):
    container = app.state.container
    created = _create(container, "我叫梅凯龙")

    body = _post(client, auth_headers, query="我是谁？")

    assert created.id in [item["memory_id"] for item in body["items"]]


def test_real_write_then_irrelevant_question_stays_empty(client, auth_headers, app):
    container = app.state.container
    _create(container, "用户喜欢喝手冲咖啡")
    _create(container, "用户养了一只名叫豆豆的猫")

    body = _post(client, auth_headers, query="今天杭州天气怎么样")

    assert body["items"] == []


def test_real_write_then_forget_removes_it_immediately(
    client, auth_headers, app, approved_forget
):
    container = app.state.container
    created = _create(container, "用户喜欢喝手冲咖啡")
    assert created.id in [
        item["memory_id"]
        for item in _post(client, auth_headers, query="我喜欢喝什么咖啡")["items"]
    ]

    approved_forget.forget(created.id, reason="e2e context test")

    assert created.id not in [
        item["memory_id"]
        for item in _post(client, auth_headers, query="我喜欢喝什么咖啡")["items"]
    ]


def test_real_write_response_never_contains_the_token(client, auth_headers, app):
    container = app.state.container
    _create(container, "用户喜欢喝手冲咖啡")
    resp = client.post(
        CONTEXT_URL,
        json={"query": "我喜欢喝什么咖啡"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert TEST_TOKEN not in resp.text

# ------------------------------------------------------------------ B-1 (audit)
def test_single_character_topic_matches(client, auth_headers, app):
    """Audit B-1: 茶/酒/车 are one-character topics and used to score 0.0."""
    container = app.state.container
    tea = _create(container, "用户喜欢喝茶", MemoryType.SEMANTIC)
    wine = _create(container, "用户喜欢喝白酒", MemoryType.SEMANTIC)
    car = _create(container, "用户有一辆车", MemoryType.SEMANTIC)

    assert tea.id in [i["memory_id"] for i in _post(client, auth_headers, query="我喜欢喝什么茶")["items"]]
    assert wine.id in [i["memory_id"] for i in _post(client, auth_headers, query="我喝不喝酒")["items"]]
    assert car.id in [i["memory_id"] for i in _post(client, auth_headers, query="我开什么车")["items"]]


def test_two_character_topic_bridges_a_particle(client, auth_headers, app):
    """Audit B-1: query 养狗 must match memory 养了一只狗."""
    container = app.state.container
    dog = _create(container, "用户养了一只狗", MemoryType.SEMANTIC)

    assert dog.id in [
        i["memory_id"] for i in _post(client, auth_headers, query="我养狗吗")["items"]
    ]


def test_possessive_noise_does_not_hide_the_topic(client, auth_headers, app):
    """Audit B-1: 我家猫叫什么 must reduce to the topic 猫."""
    container = app.state.container
    cat = _create(container, "用户养了一只猫", MemoryType.SEMANTIC)

    assert cat.id in [
        i["memory_id"] for i in _post(client, auth_headers, query="我家猫叫什么")["items"]
    ]


def test_single_character_fix_does_not_leak_unrelated_memories(
    client, auth_headers, app
):
    """The broader matching must not turn an unrelated question into hits."""
    container = app.state.container
    _create(container, "用户喜欢喝茶")
    _create(container, "用户上个月和女友分手了")
    _create(container, "用户的月薪是三万元")

    body = _post(client, auth_headers, query="今天杭州天气怎么样")

    assert body["items"] == []


def test_single_character_needs_a_meaningful_frame(client, auth_headers, app):
    """车 inside 卸车 is a logistics task, not the user's car (audit B-1)."""
    container = app.state.container
    unrelated = _create(
        container, "用户近期关注无头件、一键卸车、驿站等改善方向", MemoryType.SEMANTIC
    )
    owned = _create(container, "用户有一辆车", MemoryType.SEMANTIC)

    ids = [
        item["memory_id"]
        for item in _post(client, auth_headers, query="我开什么车")["items"]
    ]
    assert owned.id in ids
    assert unrelated.id not in ids
