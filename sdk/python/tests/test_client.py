"""Unit tests for the Python SDK against a local stub server (standard library only)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvismind_sdk import (
    AuthenticationError,
    JarvisMindClient,
    NetworkError,
    PermissionDeniedError,
    RateLimitedError,
)


class _StubHandler(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        _StubHandler.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "space": self.headers.get("X-JarvisMind-Space"),
                "body": json.loads(raw) if raw else None,
            }
        )
        status, payload, extra = _StubHandler.routes.get(
            (self.command, self.path.split("?")[0]),
            (404, {"error": {"code": "not_found", "message": "no route"}}, {}),
        )
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def stub():
    _StubHandler.routes = {}
    _StubHandler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_requests_carry_bearer_token_and_space_header(stub):
    _StubHandler.routes[("POST", "/api/v1/agent/recall")] = (200, {"data": {"items": [], "total": 0}}, {})
    client = JarvisMindClient(stub, "jmt_example_secret", space_id="work-space")
    assert client.recall("绿茶", limit=3) == {"items": [], "total": 0}
    sent = _StubHandler.seen[-1]
    assert sent["authorization"] == "Bearer jmt_example_secret"
    assert sent["space"] == "work-space"
    assert sent["body"] == {"query": "绿茶", "limit": 3}


def test_token_never_appears_in_repr(stub):
    client = JarvisMindClient(stub, "jmt_example_secret")
    assert "jmt_example_secret" not in repr(client)


def test_remember_drops_unset_optional_fields(stub):
    _StubHandler.routes[("POST", "/api/v1/agent/remember")] = (200, {"data": {"status": "SAVED"}}, {})
    client = JarvisMindClient(stub, "jmt_example_secret")
    assert client.remember("我喜欢喝绿茶", "session-a")["status"] == "SAVED"
    assert _StubHandler.seen[-1]["body"] == {"text": "我喜欢喝绿茶", "conversation_id": "session-a"}


def test_forget_never_sends_a_confirmation_token(stub):
    _StubHandler.routes[("POST", "/api/v1/agent/memories/mem_1/forget-requests")] = (
        201,
        {"data": {"id": "fr_1", "status": "PENDING", "reused_existing": False}},
        {},
    )
    client = JarvisMindClient(stub, "jmt_example_secret")
    created = client.forget("mem_1", reason="用户要求删除")
    assert created["status"] == "PENDING"
    assert "confirmation_token" not in created
    assert _StubHandler.seen[-1]["body"] == {"reason": "用户要求删除"}


def test_update_is_refused_for_agents_with_a_typed_error(stub):
    _StubHandler.routes[("POST", "/api/v1/agent/memories/mem_1/modification-requests")] = (
        403,
        {"error": {"code": "non_human_actor_forbidden", "message": "agents cannot request corrections"}},
        {},
    )
    client = JarvisMindClient(stub, "jmt_example_secret")
    with pytest.raises(PermissionDeniedError) as raised:
        client.update("mem_1", {"content": "x"})
    assert raised.value.code == "non_human_actor_forbidden"
    assert raised.value.status == 403


def test_rate_limit_exposes_retry_after(stub):
    _StubHandler.routes[("GET", "/api/v1/agent/me")] = (
        429,
        {"error": {"code": "rate_limited", "message": "slow down"}},
        {"Retry-After": "7"},
    )
    client = JarvisMindClient(stub, "jmt_example_secret")
    with pytest.raises(RateLimitedError) as raised:
        client.me()
    assert raised.value.retry_after == 7


def test_unauthorized_maps_to_authentication_error(stub):
    _StubHandler.routes[("GET", "/api/v1/agent/me")] = (
        401,
        {"error": {"code": "unauthorized", "message": "token revoked"}},
        {},
    )
    with pytest.raises(AuthenticationError):
        JarvisMindClient(stub, "jmt_example_secret").me()


def test_unreachable_server_raises_network_error():
    client = JarvisMindClient("http://127.0.0.1:9", "jmt_example_secret", timeout=2)
    with pytest.raises(NetworkError):
        client.me()


def test_memory_ids_are_path_quoted(stub):
    _StubHandler.routes[("GET", "/api/v1/agent/memories/a%2Fb")] = (200, {"data": {"id": "a/b"}}, {})
    client = JarvisMindClient(stub, "jmt_example_secret")
    assert client.get_memory("a/b") == {"id": "a/b"}


def test_base_url_must_be_http():
    with pytest.raises(ValueError):
        JarvisMindClient("ftp://example.invalid", "jmt_example_secret")
