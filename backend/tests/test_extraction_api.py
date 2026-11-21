"""HTTP-level tests for the extraction and candidate endpoints."""


def test_extract_requires_token(client):
    resp = client.post(
        "/api/v1/memories/extract",
        json={"conversation": "我从事物流快运行业。", "conversation_id": "c1"},
    )
    assert resp.status_code == 401


def test_extract_endpoint_returns_candidates(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "我从事物流快运行业，负责省区客服质管和理赔工作。",
            "conversation_id": "conv-http-1",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["conversation_id"] == "conv-http-1"
    assert body["engine"] == "local"
    assert body["counts"]["PENDING"] >= 1


def test_candidates_endpoint_and_route_order(client, auth_headers):
    # /candidates must not be captured by /{memory_id}
    client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "JarvisMind 项目计划明年支持 NAS 部署。",
            "conversation_id": "c-route",
        },
        headers=auth_headers,
    )
    resp = client.get(
        "/api/v1/memories/candidates?conversation_id=c-route", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["meta"]["total"] == 1


def test_candidate_approve_via_http(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "今天要把周报做完，顺便处理一下升级工单。",
            "conversation_id": "c-approve",
        },
        headers=auth_headers,
    )
    candidate_id = resp.json()["candidates"][0]["id"]

    approved = client.post(
        f"/api/v1/memories/candidates/{candidate_id}/approve",
        json={"actor": "tester"},
        headers=auth_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "APPROVED"

    listed = client.get("/api/v1/memories", headers=auth_headers).json()
    assert listed["meta"]["total"] == 1


def test_candidate_reject_via_http(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "今天要把周报做完，顺便处理一下升级工单。",
            "conversation_id": "c-reject",
        },
        headers=auth_headers,
    )
    candidate_id = resp.json()["candidates"][0]["id"]

    rejected = client.post(
        f"/api/v1/memories/candidates/{candidate_id}/reject",
        json={"reason": "短期任务", "actor": "tester"},
        headers=auth_headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "REJECTED"
    assert rejected.json()["data"]["reject_reason"] == "短期任务"


def test_extraction_status_endpoint(client, auth_headers):
    resp = client.get("/api/v1/memories/extraction/status", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "local"
    assert body["provider"] is None
    assert "budget" in body


def test_extraction_status_requires_token(client):
    assert client.get("/api/v1/memories/extraction/status").status_code == 401


def test_extract_rejects_unknown_source(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "我从事物流快运行业。",
            "conversation_id": "c-bad-source",
            "source_id": "nope",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_extract_requires_conversation_id(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/extract",
        json={"conversation": "我从事物流快运行业。"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_search_succeeds_with_valid_token(client, auth_headers):
    """003-B: search finds the memory only AFTER an explicit approval."""
    extracted = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "JarvisMind 项目计划明年支持 NAS 部署。",
            "conversation_id": "c-search",
        },
        headers=auth_headers,
    )
    assert extracted.json()["counts"]["APPROVED"] == 0
    candidate_id = extracted.json()["candidates"][0]["id"]
    assert client.post(
        f"/api/v1/memories/candidates/{candidate_id}/approve",
        json={},
        headers=auth_headers,
    ).status_code == 200
    resp = client.get("/api/v1/memories/search?q=NAS", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["meta"]["total"] >= 1


def test_search_rejects_missing_token(client):
    assert client.get("/api/v1/memories/search?q=NAS").status_code == 401


def test_search_rejects_wrong_token(client):
    resp = client.get(
        "/api/v1/memories/search?q=NAS", headers={"X-API-Token": "wrong-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_candidates_rejects_wrong_token(client):
    assert (
        client.get(
            "/api/v1/memories/candidates", headers={"X-API-Token": "wrong-token"}
        ).status_code
        == 401
    )


def test_approve_requires_token(client):
    assert (
        client.post("/api/v1/memories/candidates/whatever/approve", json={}).status_code
        == 401
    )


def test_extract_rejects_wrong_token(client):
    resp = client.post(
        "/api/v1/memories/extract",
        json={"conversation": "我从事物流快运行业。", "conversation_id": "c-x"},
        headers={"X-API-Token": "wrong-token"},
    )
    assert resp.status_code == 401
    assert "wrong-token" not in resp.text
