"""Full-text search tests, including Chinese segmentation."""
from sqlalchemy import text


def _create(client, headers, payload):
    resp = client.post("/api/v1/memories", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _seed(client, headers):
    return [
        _create(
            client,
            headers,
            {
                "memory_type": "SEMANTIC",
                "content": "机器学习与深度学习的实践笔记",
                "summary": "ML notes",
            },
        ),
        _create(
            client,
            headers,
            {
                "memory_type": "PROJECT",
                "content": "the quick brown fox jumps over the lazy dog",
                "summary": "English pangram",
            },
        ),
        _create(
            client,
            headers,
            {
                "memory_type": "EPISODIC",
                "content": "上周的用户访谈记录",
                "summary": "interview",
            },
        ),
    ]


def test_english_search(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get("/api/v1/memories/search?q=quick", headers=auth_headers).json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["memory_type"] == "PROJECT"
    assert "quick brown fox" in body["data"][0]["content"]


def test_chinese_search_four_characters(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get(
        "/api/v1/memories/search?q=机器学习", headers=auth_headers
    ).json()
    assert body["meta"]["total"] == 1
    assert "机器学习与深度学习" in body["data"][0]["content"]


def test_chinese_search_two_characters(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get("/api/v1/memories/search?q=深度", headers=auth_headers).json()
    assert body["meta"]["total"] == 1
    assert "深度学习" in body["data"][0]["content"]


def test_chinese_search_single_character(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get("/api/v1/memories/search?q=访", headers=auth_headers).json()
    assert body["meta"]["total"] == 1
    assert "用户访谈记录" in body["data"][0]["content"]


def test_chinese_search_no_match(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get("/api/v1/memories/search?q=量子计算机", headers=auth_headers).json()
    assert body["meta"]["total"] == 0
    assert body["data"] == []


def test_search_empty_query_rejected(client, auth_headers):
    assert client.get("/api/v1/memories/search?q=", headers=auth_headers).status_code == 422


def test_search_punctuation_only_rejected(client, auth_headers):
    assert client.get("/api/v1/memories/search?q=---", headers=auth_headers).status_code == 422


def test_search_fts_operators_are_not_injected(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get(
        "/api/v1/memories/search?q=" + "%22%20OR%201%3D1%20--",
        headers=auth_headers,
    ).json()
    assert body["meta"]["total"] == 0


def test_search_filter_by_memory_type(client, auth_headers):
    _seed(client, auth_headers)
    body = client.get(
        "/api/v1/memories/search?q=the&memory_type=PROJECT", headers=auth_headers
    ).json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["memory_type"] == "PROJECT"


def test_search_filter_by_status(client, auth_headers, approved_forget):
    records = _seed(client, auth_headers)
    # JW-01: forgetting requires the approved forget-request flow.
    approved_forget.forget(records[1]["id"])
    active = client.get(
        "/api/v1/memories/search?q=quick&status=ACTIVE", headers=auth_headers
    ).json()
    assert active["meta"]["total"] == 0
    deleted = client.get(
        "/api/v1/memories/search?q=quick&status=DELETED", headers=auth_headers
    ).json()
    assert deleted["meta"]["total"] == 1


def test_search_pagination(client, auth_headers):
    for index in range(4):
        _create(
            client,
            auth_headers,
            {
                "memory_type": "SEMANTIC",
                "content": f"shared keyword document number {index}",
            },
        )
    page1 = client.get(
        "/api/v1/memories/search?q=shared&page=1&page_size=2", headers=auth_headers
    ).json()
    page2 = client.get(
        "/api/v1/memories/search?q=shared&page=2&page_size=2", headers=auth_headers
    ).json()
    assert page1["meta"]["total"] == 4
    assert page1["meta"]["total_pages"] == 2
    assert len(page1["data"]) == 2
    assert len(page2["data"]) == 2
    assert {item["memory_id"] for item in page1["data"]}.isdisjoint(
        {item["memory_id"] for item in page2["data"]}
    )


def test_search_requires_token(client):
    assert client.get("/api/v1/memories/search?q=test").status_code == 401


def test_search_unavailable_returns_503(client, auth_headers, manager):
    _seed(client, auth_headers)
    with manager.engine.begin() as connection:
        connection.execute(text("DROP TABLE memory_records_fts_zh"))
    resp = client.get("/api/v1/memories/search?q=quick", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "search_unavailable"
