"""Filtering, pagination, and sorting tests."""


def _seed(client, headers, items):
    ids = []
    for item in items:
        resp = client.post("/api/v1/memories", json=item, headers=headers)
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["data"]["id"])
    return ids


def test_filter_by_memory_type(client, auth_headers):
    _seed(
        client,
        auth_headers,
        [
            {"memory_type": "SEMANTIC", "content": "s1"},
            {"memory_type": "EPISODIC", "content": "e1"},
            {"memory_type": "SEMANTIC", "content": "s2"},
        ],
    )
    resp = client.get("/api/v1/memories?memory_type=SEMANTIC", headers=auth_headers)
    body = resp.json()
    assert body["meta"]["total"] == 2
    assert all(item["memory_type"] == "SEMANTIC" for item in body["data"])


def test_filter_by_status(
    client, auth_headers, sample_memory_payload, approved_forget
):
    created = client.post(
        "/api/v1/memories", json=sample_memory_payload, headers=auth_headers
    ).json()["data"]
    # JW-01: forgetting requires the approved forget-request flow.
    approved_forget.forget(created["id"])

    active = client.get("/api/v1/memories?status=ACTIVE", headers=auth_headers).json()
    assert active["meta"]["total"] == 0
    deleted = client.get(
        "/api/v1/memories?status=DELETED", headers=auth_headers
    ).json()
    assert deleted["meta"]["total"] == 1


def test_pagination_pages(client, auth_headers):
    _seed(
        client,
        auth_headers,
        [{"memory_type": "SEMANTIC", "content": f"m{i}"} for i in range(5)],
    )
    page1 = client.get(
        "/api/v1/memories?page=1&page_size=2", headers=auth_headers
    ).json()
    page2 = client.get(
        "/api/v1/memories?page=2&page_size=2", headers=auth_headers
    ).json()
    page3 = client.get(
        "/api/v1/memories?page=3&page_size=2", headers=auth_headers
    ).json()

    assert page1["meta"]["total"] == 5
    assert page1["meta"]["total_pages"] == 3
    assert len(page1["data"]) == 2
    assert len(page2["data"]) == 2
    assert len(page3["data"]) == 1

    seen = {item["id"] for page in (page1, page2, page3) for item in page["data"]}
    assert len(seen) == 5


def test_page_size_upper_bound_enforced(client, auth_headers):
    resp = client.get("/api/v1/memories?page_size=1000", headers=auth_headers)
    assert resp.status_code == 422


def test_sort_by_importance_desc(client, auth_headers):
    _seed(
        client,
        auth_headers,
        [
            {"memory_type": "SEMANTIC", "content": "low", "importance": 1.0},
            {"memory_type": "SEMANTIC", "content": "high", "importance": 9.0},
            {"memory_type": "SEMANTIC", "content": "mid", "importance": 5.0},
        ],
    )
    body = client.get(
        "/api/v1/memories?sort_by=importance&sort_order=desc", headers=auth_headers
    ).json()
    values = [item["importance"] for item in body["data"]]
    assert values == [9.0, 5.0, 1.0]


def test_sort_by_importance_asc(client, auth_headers):
    _seed(
        client,
        auth_headers,
        [
            {"memory_type": "SEMANTIC", "content": "low", "importance": 1.0},
            {"memory_type": "SEMANTIC", "content": "high", "importance": 9.0},
        ],
    )
    body = client.get(
        "/api/v1/memories?sort_by=importance&sort_order=asc", headers=auth_headers
    ).json()
    assert [item["importance"] for item in body["data"]] == [1.0, 9.0]


def test_empty_result_is_valid_page(client, auth_headers):
    body = client.get("/api/v1/memories", headers=auth_headers).json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["total_pages"] == 0
