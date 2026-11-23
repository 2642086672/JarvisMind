"""Tests for the native stdio MCP bridge.

The tests use a local stub HTTP service (no real JarvisMind process) and drive
``MCPServer`` through in-memory streams plus one real ``os.pipe()``.  They never
import ``subprocess``/``multiprocessing`` (the repository P0 gate forbids it) and
never touch a database.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import jarvismind
from jarvismind import cli
from jarvismind.mcp.config import MCPConfig
from jarvismind.mcp.redaction import redact, redact_value, strip_secret_fields
from jarvismind.mcp.server import DEFAULT_PROTOCOL_VERSION, MCPServer

STUB_HOST = "127.0.0.1"
FAKE_TOKEN = "jmt_0123abcd_" + "0123456789abcdef" * 2
OTHER_TOKEN = "jmt_0123abcd_" + "f" * 32

TOOL_NAMES = {
    "jarvismind_context",
    "jarvismind_remember",
    "jarvismind_search",
    "jarvismind_forget",
    "jarvismind_core_anchor",
}

MEMORIES_PATH = "/api/v1/agent/memories"
SEARCH_PATH = "/api/v1/agent/search"
CONTEXT_PATH = "/api/v1/agent/context"
REMEMBER_PATH = "/api/v1/agent/remember"


def _request(message_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _tool_call(name, arguments=None, message_id=1):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _request(message_id, "tools/call", params)


def _memory_row(memory_id, memory_type, content, summary=None):
    return {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "summary": summary,
        "importance": 5.0,
        "confidence": 0.8,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _context_payload():
    return {
        "data": {
            "context": (
                '<jarvismind-context trust="untrusted-data">\n'
                "- [PROFILE] Alex prefers to be called Alex\n"
                "</jarvismind-context>"
            ),
            "citations": [
                {
                    "memory_id": "mem-ctx",
                    "memory_type": "PROFILE",
                    "relevance_score": 0.91,
                }
            ],
            "retrieval_version": 7,
        }
    }


def _forget_payload():
    return {
        "data": {
            "id": "req-1",
            "memory_id": "mem-1",
            "status": "PENDING",
            "request_source": "USER_API",
            "requester_label": "agent:1",
            "reason": None,
            "requested_at": "2026-01-01T00:00:00Z",
            "awaiting_user_decision": True,
            "memory_forgotten": False,
            # The real server never returns this.  The stub includes it so the
            # bridge's defensive stripping is exercised.
            "confirmation_token": FAKE_TOKEN,
        }
    }


class StubState:
    """Mutable behaviour for the stub service plus a request journal."""

    def __init__(self):
        self.requests = []
        self.delay = 0.0
        self.overrides = {}
        self.error_response = None
        self.context_response = None
        self.remember_response = None
        self.search_rows = None
        self.memories_by_type = {}
        self.base_url = ""

    def respond(self, method, path, record):
        if self.delay:
            time.sleep(self.delay)
        if self.error_response is not None:
            return self.error_response
        override = self.overrides.get((method, path))
        if override is not None:
            return override(record) if callable(override) else override

        if method == "POST" and path == CONTEXT_PATH:
            return 200, self.context_response or _context_payload()
        if method == "POST" and path == REMEMBER_PATH:
            return 200, self.remember_response or {
                "data": {"status": "SAVED", "memory_id": "mem-new"}
            }
        if method == "GET" and path == SEARCH_PATH:
            rows = self.search_rows
            if rows is None:
                rows = [
                    _memory_row("search-1", "PROFILE", "Alex likes green tea"),
                    _memory_row("search-2", "SEMANTIC", "Tea is brewed at 80C"),
                ]
            return 200, {"data": rows, "meta": {"returned": len(rows)}}
        if method == "GET" and path == MEMORIES_PATH:
            memory_type = (record["query"].get("memory_type") or [None])[0]
            rows = self.memories_by_type.get(memory_type, [])
            return 200, {"data": rows, "meta": {"returned": len(rows)}}
        if method == "POST" and path.endswith("/forget-requests"):
            return 201, _forget_payload()
        return 404, {
            "error": {
                "code": "not_found",
                "message": "no stub route",
                "details": None,
            }
        }


def _handler_factory(state):
    class StubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # silence the stub
            pass

        def _handle(self, method):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b""
            record = {
                "method": method,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": json.loads(raw_body.decode("utf-8")) if raw_body else None,
            }
            state.requests.append(record)
            status, payload = state.respond(method, parsed.path, record)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return StubHandler


@pytest.fixture()
def stub():
    state = StubState()
    server = ThreadingHTTPServer((STUB_HOST, 0), _handler_factory(state))
    server.daemon_threads = True
    server.handle_error = lambda *args, **kwargs: None
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    state.base_url = f"http://{STUB_HOST}:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _config(stub=None, **overrides):
    values = {
        "url": stub.base_url if stub is not None else f"http://{STUB_HOST}:9",
        "token": FAKE_TOKEN,
        "timeout": 1.0,
        "environ": {},
    }
    values.update(overrides)
    return MCPConfig.from_values(**values)


def _run(config, messages):
    stdin = io.StringIO()
    for item in messages:
        if isinstance(item, str):
            stdin.write(item + "\n")
        else:
            stdin.write(json.dumps(item) + "\n")
    stdin.seek(0)
    stdout = io.StringIO()
    stderr = io.StringIO()
    server = MCPServer(config, stdin=stdin, stdout=stdout, stderr=stderr)
    assert server.run() == 0
    output = stdout.getvalue()
    responses = [json.loads(line) for line in output.splitlines() if line.strip()]
    return responses, output, stderr.getvalue()


def _call_tool(config, name, arguments=None, message_id=1):
    responses, _output, _stderr = _run(config, [_tool_call(name, arguments, message_id)])
    assert len(responses) == 1
    response = responses[0]
    assert "result" in response, response
    return response["result"]


def _tool_by_name(tools, name):
    for tool in tools:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"missing tool {name}")


def _jsonrpc_line_ok(line):
    assert isinstance(line, dict)
    assert line.get("jsonrpc") == "2.0"
    assert "id" in line
    assert ("result" in line) != ("error" in line)
    if "error" in line:
        assert isinstance(line["error"]["code"], int)
        assert isinstance(line["error"]["message"], str) and line["error"]["message"]


def _wait_for_lines(lines, count, timeout=2.0):
    deadline = time.monotonic() + timeout
    while len(lines) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(lines) >= count, f"expected {count} lines, got {lines}"


# ---------------------------------------------------------------------------
# handshake and protocol plumbing
# ---------------------------------------------------------------------------


def test_initialize_tools_list_and_resources_list(stub):
    config = _config(stub)
    responses, output, stderr = _run(
        config,
        [
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request(2, "tools/list"),
            _request(3, "resources/list"),
        ],
    )

    # The notification produced no response.
    assert len(responses) == 3
    for line in responses:
        _jsonrpc_line_ok(line)

    init = responses[0]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"] == {
        "name": "jarvismind-mcp",
        "version": jarvismind.__version__,
    }
    assert init["capabilities"] == {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
    }
    assert "untrusted" in init["instructions"].lower()
    assert "<jarvismind-context>" in init["instructions"]
    assert "<jarvismind-core-anchor>" in init["instructions"]
    assert "owner" in init["instructions"].lower()

    tools = responses[1]["result"]["tools"]
    assert len(tools) == 5
    assert {tool["name"] for tool in tools} == TOOL_NAMES
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        assert schema["additionalProperties"] is False

    context_tool = _tool_by_name(tools, "jarvismind_context")
    assert context_tool["inputSchema"]["required"] == ["query"]
    assert context_tool["inputSchema"]["properties"]["limit"]["default"] == 6
    assert context_tool["inputSchema"]["properties"]["limit"]["minimum"] == 1
    assert context_tool["inputSchema"]["properties"]["limit"]["maximum"] == 20

    remember_tool = _tool_by_name(tools, "jarvismind_remember")
    remember_schema = remember_tool["inputSchema"]
    # Either `text` or `items` is required, so neither can be listed as a single
    # required field; the tool validates the pair itself.
    assert remember_schema["required"] == []
    assert remember_schema["properties"]["mode"]["enum"] == ["auto", "direct"]
    assert remember_schema["properties"]["mode"]["default"] == "direct"
    assert remember_schema["properties"]["items"]["type"] == "array"
    assert remember_schema["properties"]["items"]["maxItems"] == 20
    assert remember_schema["properties"]["items"]["items"]["type"] == "string"

    search_tool = _tool_by_name(tools, "jarvismind_search")
    assert set(search_tool["inputSchema"]["properties"]["memory_type"]["enum"]) == {
        "PROFILE",
        "SEMANTIC",
        "EPISODIC",
        "PROJECT",
        "WORKING",
        "REFLECTION",
    }
    assert search_tool["inputSchema"]["properties"]["limit"]["default"] == 10

    forget_tool = _tool_by_name(tools, "jarvismind_forget")
    assert forget_tool["inputSchema"]["required"] == ["memory_id"]
    assert forget_tool["inputSchema"]["properties"]["reason"]["maxLength"] == 1000

    anchor_tool = _tool_by_name(tools, "jarvismind_core_anchor")
    assert anchor_tool["inputSchema"]["required"] == []
    assert anchor_tool["inputSchema"]["properties"]["limit"]["default"] == 6
    assert anchor_tool["inputSchema"]["properties"]["max_chars"]["default"] == 500

    resources = responses[2]["result"]["resources"]
    assert len(resources) == 1
    assert resources[0]["uri"] == "jarvismind://core-anchor"
    assert resources[0]["mimeType"] == "text/plain"
    assert resources[0]["name"] == "JarvisMind core anchor"

    assert output.endswith("\n")
    assert stderr == ""


def test_ping_default_protocol_and_errors(stub):
    config = _config(stub)
    responses, output, _stderr = _run(
        config,
        [
            _request(1, "ping"),
            _request(2, "initialize", {}),
            _request(3, "does/not/exist"),
            "{not valid json",
            [{"jsonrpc": "2.0", "id": 4, "method": "ping"}],
            json.dumps("just a string"),
            _tool_call("not_a_tool", {}, 5),
            _tool_call("jarvismind_context", {}, 6),
            _tool_call("jarvismind_context", {"query": "ok", "limit": "six"}, 7),
            _tool_call("jarvismind_context", {"query": "ok", "limit": True}, 8),
            _tool_call("jarvismind_forget", {"memory_id": 42}, 9),
        ],
    )

    assert responses[0]["result"] == {}
    assert responses[1]["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
    assert responses[2]["error"]["code"] == -32601
    assert responses[3]["id"] is None
    assert responses[3]["error"]["code"] == -32700
    assert responses[4]["id"] is None
    assert responses[4]["error"]["code"] == -32600
    assert responses[5]["id"] is None
    assert responses[5]["error"]["code"] == -32600
    for index in range(6, 11):
        assert responses[index]["error"]["code"] == -32602, responses[index]
    for line in responses:
        _jsonrpc_line_ok(line)
    assert len(responses) == 11
    assert len(output.splitlines()) == 11


def test_non_finite_json_id_is_rejected_with_plain_json(stub):
    config = _config(stub)
    raw = '{"jsonrpc":"2.0","id":NaN,"method":"ping"}'
    responses, output, _stderr = _run(config, [raw])

    assert len(responses) == 1
    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] == -32600
    assert "NaN" not in output
    for line in output.splitlines():
        _jsonrpc_line_ok(json.loads(line))


def test_notification_without_id_never_executes_tools(stub):
    config = _config(stub)
    notification = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "jarvismind_remember", "arguments": {"text": "side effect"}},
    }
    responses, output, _stderr = _run(config, [notification, _request(1, "ping")])
    assert len(responses) == 1
    assert responses[0]["result"] == {}
    assert stub.requests == []
    assert output.count("\n") == 1


def test_notification_with_an_id_gets_an_empty_result(stub):
    config = _config(stub)
    responses, _output, _stderr = _run(
        config, [_request(1, "notifications/initialized")]
    )
    assert len(responses) == 1
    assert responses[0]["result"] == {}
    assert stub.requests == []


# ---------------------------------------------------------------------------
# the five tools
# ---------------------------------------------------------------------------


def test_context_tool_keeps_trust_wrapper_and_citations(stub):
    config = _config(stub)
    result = _call_tool(config, "jarvismind_context", {"query": "who am I"})

    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert '<jarvismind-context trust="untrusted-data">' in text
    assert "</jarvismind-context>" in text
    citation_part = text.split("\n\n", 1)[1]
    citations = json.loads(citation_part)
    assert citations["citations"][0]["memory_id"] == "mem-ctx"
    assert citations["retrieval_version"] == 7

    request = stub.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == CONTEXT_PATH
    assert request["body"] == {"query": "who am I", "limit": 6}

    # A server block without the wrapper is re-wrapped with escaped content.
    stub.context_response = {
        "data": {
            "context": "plain <script> & text",
            "citations": [],
            "retrieval_version": 1,
        }
    }
    result = _call_tool(config, "jarvismind_context", {"query": "again", "limit": 2})
    text = result["content"][0]["text"]
    assert text.startswith('<jarvismind-context trust="untrusted-data">')
    assert "&lt;script&gt;" in text
    assert "&amp;" in text
    assert "<script>" not in text
    assert "plain" in text


def test_remember_defaults_and_status_passthrough(stub):
    config = _config(stub)

    responses, _output, _stderr = _run(
        config,
        [
            _tool_call("jarvismind_remember", {"text": "I live in Berlin"}, 1),
            _tool_call("jarvismind_remember", {"text": "second"}, 2),
        ],
    )
    assert responses[0]["result"]["isError"] is False
    first_body = stub.requests[-2]["body"]
    second_body = stub.requests[-1]["body"]
    assert first_body["text"] == "I live in Berlin"
    assert first_body["mode"] == "direct"
    assert re.fullmatch(r"mcp-[0-9a-f]{12}", first_body["conversation_id"])
    # The per-process conversation id is stable across calls.
    assert second_body["conversation_id"] == first_body["conversation_id"]

    result = _call_tool(
        config,
        "jarvismind_remember",
        {"text": "explicit", "conversation_id": "conv-9", "mode": "auto"},
    )
    assert result["isError"] is False
    assert stub.requests[-1]["body"] == {
        "text": "explicit",
        "conversation_id": "conv-9",
        "mode": "auto",
    }

    stub.remember_response = {
        "data": {"status": "DENIED", "reason": "credential_blocked"}
    }
    result = _call_tool(config, "jarvismind_remember", {"text": "secret"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "DENIED"
    assert payload["reason"] == "credential_blocked"


def test_search_forwards_query_and_filters_memory_type(stub):
    config = _config(stub)
    stub.search_rows = [
        _memory_row(f"profile-{index}", "PROFILE", f"profile {index}") for index in range(12)
    ] + [
        _memory_row(f"semantic-{index}", "SEMANTIC", f"semantic {index}")
        for index in range(5)
    ]

    result = _call_tool(
        config,
        "jarvismind_search",
        {"query": "tea", "memory_type": "PROFILE", "limit": 10},
    )
    request = stub.requests[-1]
    assert request["method"] == "GET"
    assert request["path"] == SEARCH_PATH
    assert request["query"]["q"] == ["tea"]
    assert request["query"]["limit"] == ["40"]

    payload = json.loads(result["content"][0]["text"])
    assert payload["query"] == "tea"
    assert payload["count"] == 10
    assert all(row["memory_type"] == "PROFILE" for row in payload["memories"])

    result = _call_tool(config, "jarvismind_search", {"query": "tea", "limit": 3})
    assert stub.requests[-1]["query"]["limit"] == ["3"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["count"] == 3
    assert len(payload["memories"]) == 3


def test_forget_creates_pending_request_and_strips_token(stub):
    config = _config(stub)
    responses, output, stderr = _run(
        config,
        [_tool_call("jarvismind_forget", {"memory_id": "mem-1", "reason": "duplicate"}, 1)],
    )
    result = responses[0]["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]

    request = stub.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/api/v1/agent/memories/mem-1/forget-requests"
    assert request["body"] == {"reason": "duplicate"}

    payload = json.loads(text)
    assert payload["status"] == "PENDING"
    assert payload["awaiting_user_decision"] is True
    assert payload["memory_forgotten"] is False
    assert "owner must approve" in payload["note"].lower()
    assert "confirmation_token" not in payload
    assert "plaintext_token" not in payload
    assert FAKE_TOKEN not in text
    assert FAKE_TOKEN not in output
    assert FAKE_TOKEN not in stderr

    # Only the forget-request endpoint was called: no approval/rejection call.
    assert len(stub.requests) == 1
    for recorded in stub.requests:
        lowered = recorded["path"].lower()
        assert "approve" not in lowered
        assert "reject" not in lowered
        assert "decide" not in lowered
        assert recorded["method"] == "POST"

    # reason is omitted, not sent as null, when the tool call omits it.
    _call_tool(config, "jarvismind_forget", {"memory_id": "mem-2"})
    assert stub.requests[-1]["body"] == {}


def test_core_anchor_basic_order_and_wrapper(stub):
    config = _config(stub)
    stub.memories_by_type = {
        "PROFILE": [
            _memory_row("p1", "PROFILE", "Call the user Alex"),
            _memory_row("p2", "PROFILE", "Alex lives in Berlin"),
        ],
        "SEMANTIC": [_memory_row("s1", "SEMANTIC", "Prefers green tea")],
    }
    result = _call_tool(config, "jarvismind_core_anchor", {"limit": 5})
    text = result["content"][0]["text"]
    assert text.startswith('<jarvismind-core-anchor trust="untrusted-data">')
    assert text.endswith("</jarvismind-core-anchor>")
    profile_index = text.index("Call the user Alex")
    semantic_index = text.index("Prefers green tea")
    assert profile_index < semantic_index
    assert "- [PROFILE]" in text
    assert "- [SEMANTIC]" in text

    requested_types = [
        (request["query"].get("memory_type") or [None])[0]
        for request in stub.requests
        if request["path"] == MEMORIES_PATH
    ]
    assert requested_types[:2] == ["PROFILE", "SEMANTIC"]
    # Nothing left to fill: every later type is still consulted, WORKING never is.
    assert "WORKING" not in requested_types


def test_core_anchor_profile_first_over_many_newer_working_memories(stub):
    config = _config(stub)
    profiles = [
        _memory_row("p1", "PROFILE", "PROFILE-ALPHA"),
        _memory_row("p2", "PROFILE", "PROFILE-BETA"),
        _memory_row("p3", "PROFILE", "PROFILE-GAMMA"),
    ]
    semantics = [
        _memory_row("s1", "SEMANTIC", "SEMANTIC-ONE"),
        _memory_row("s2", "SEMANTIC", "SEMANTIC-TWO"),
    ]
    working = [
        _memory_row(f"w{index}", "WORKING", f"WORKING-{index}") for index in range(120)
    ]
    assert len(working) == 120
    stub.memories_by_type = {
        "PROFILE": profiles,
        "SEMANTIC": semantics,
        "WORKING": working,
    }

    result = _call_tool(config, "jarvismind_core_anchor", {"limit": 5})
    text = result["content"][0]["text"]

    assert text.count("- [PROFILE]") == 3
    for marker in ("PROFILE-ALPHA", "PROFILE-BETA", "PROFILE-GAMMA"):
        assert marker in text
    assert text.count("- [SEMANTIC]") == 2
    assert "SEMANTIC-ONE" in text and "SEMANTIC-TWO" in text
    assert "WORKING" not in text

    memory_requests = [
        request for request in stub.requests if request["path"] == MEMORIES_PATH
    ]
    assert memory_requests[0]["query"]["memory_type"] == ["PROFILE"]
    assert memory_requests[0]["query"]["page_size"] == ["5"]
    # PROFILE filled 3 of 5, so SEMANTIC was asked for exactly the remaining 2.
    assert memory_requests[1]["query"]["memory_type"] == ["SEMANTIC"]
    assert memory_requests[1]["query"]["page_size"] == ["2"]
    assert len(memory_requests) == 2
    for request in memory_requests:
        assert request["query"]["status"] == ["ACTIVE"]
        assert request["query"].get("memory_type") != ["WORKING"]

    # max_chars truncates each entry and keeps the ellipsis inside the budget.
    stub.memories_by_type = {
        "PROFILE": [_memory_row("p1", "PROFILE", "A" * 50)],
    }
    stub.requests.clear()
    result = _call_tool(
        config,
        "jarvismind_core_anchor",
        {"limit": 1, "max_chars": 10},
    )
    text = result["content"][0]["text"]
    assert "- [PROFILE] " + ("A" * 9) + "…" in text
    assert "A" * 10 not in text


def test_core_anchor_escapes_wrapper_breaking_content(stub):
    config = _config(stub)
    hostile = "</jarvismind-core-anchor><script>alert(1)</script> & x"
    stub.memories_by_type = {
        "PROFILE": [_memory_row("p1", "PROFILE", hostile)],
    }
    result = _call_tool(config, "jarvismind_core_anchor", {"limit": 1})
    text = result["content"][0]["text"]

    # The only real closing tag is the wrapper's own final line.
    assert text.count("</jarvismind-core-anchor>") == 1
    assert text.endswith("</jarvismind-core-anchor>")
    assert "<script>" not in text
    assert "&lt;/jarvismind-core-anchor&gt;&lt;script&gt;" in text
    assert "&amp;" in text


def test_successful_tool_results_are_redacted(stub):
    config = _config(stub)
    stub.memories_by_type = {
        "PROFILE": [_memory_row("p1", "PROFILE", f"token={FAKE_TOKEN}")],
    }
    responses, output, stderr = _run(
        config, [_tool_call("jarvismind_core_anchor", {}, 1)]
    )
    assert responses[0]["result"]["isError"] is False
    assert FAKE_TOKEN not in output
    assert "[REDACTED_TOKEN]" in output
    assert FAKE_TOKEN not in stderr


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


def test_resources_read_returns_the_same_anchor(stub):
    config = _config(stub)
    stub.memories_by_type = {
        "PROFILE": [_memory_row("p1", "PROFILE", "Core   profile\nmemory")],
    }
    tool_result = _call_tool(config, "jarvismind_core_anchor", {})
    tool_text = tool_result["content"][0]["text"]

    responses, _output, _stderr = _run(
        config,
        [_request(7, "resources/read", {"uri": "jarvismind://core-anchor"})],
    )
    contents = responses[0]["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "jarvismind://core-anchor"
    assert contents[0]["mimeType"] == "text/plain"
    assert contents[0]["text"] == tool_text
    assert '<jarvismind-core-anchor trust="untrusted-data">' in contents[0]["text"]
    # Whitespace was flattened.
    assert "Core profile memory" in contents[0]["text"]

    responses, _output, _stderr = _run(
        config,
        [_request(8, "resources/read", {"uri": "jarvismind://unknown"})],
    )
    assert responses[0]["error"]["code"] == -32002


def test_resources_read_without_token_is_an_error_but_does_not_crash(stub):
    config = _config(stub, token=None, token_file=None)
    responses, _output, _stderr = _run(
        config,
        [_request(1, "resources/read", {"uri": "jarvismind://core-anchor"})],
    )
    assert responses[0]["error"]["code"] == -32603
    assert "token" in responses[0]["error"]["message"].lower()


# ---------------------------------------------------------------------------
# stdout purity and fail-open behaviour
# ---------------------------------------------------------------------------


def test_stdout_is_pure_jsonrpc_lines(stub):
    config = _config(stub)
    responses, output, _stderr = _run(
        config,
        [
            _request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request(2, "tools/list"),
            "this is not json",
            _request(3, "no/such/method"),
            _tool_call("jarvismind_context", {"query": "hi"}, 4),
        ],
    )
    lines = output.splitlines()
    assert len(lines) == 5
    assert len(responses) == 5
    for raw in lines:
        parsed = json.loads(raw)  # would raise on a log line
        _jsonrpc_line_ok(parsed)
        assert parsed["jsonrpc"] == "2.0"
    assert responses[2]["error"]["code"] == -32700
    assert responses[3]["error"]["code"] == -32601


def test_deeply_nested_json_is_a_parse_error_and_never_kills_the_session(stub):
    config = _config(stub)
    nested = "[" * 3000 + "]" * 3000
    responses, output, _stderr = _run(config, [nested, _request(1, "ping")])

    assert len(responses) == 2
    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] in {-32700, -32600}
    assert responses[1]["result"] == {}
    for raw in output.splitlines():
        _jsonrpc_line_ok(json.loads(raw))


def test_missing_token_still_initializes_and_fails_tools_open(stub):
    config = _config(stub, token=None, token_file=None)
    responses, output, stderr = _run(
        config,
        [
            _request(1, "initialize", {}),
            _tool_call("jarvismind_context", {"query": "hello"}, 2),
            _tool_call("jarvismind_core_anchor", {}, 3),
        ],
    )
    assert responses[0]["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
    for index in (1, 2):
        result = responses[index]["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"].lower()
        assert "token" in text
        assert "jarvismind_agent_token" in text or "token-file" in text

    warning_lines = [
        line for line in stderr.splitlines() if "no agent token configured" in line.lower()
    ]
    assert len(warning_lines) == 1
    assert FAKE_TOKEN not in output
    assert FAKE_TOKEN not in stderr
    assert stub.requests == []


def test_service_offline_fails_open():
    probe = ThreadingHTTPServer((STUB_HOST, 0), _handler_factory(StubState()))
    port = probe.server_address[1]
    probe.server_close()

    config = _config(url=f"http://{STUB_HOST}:{port}", timeout=0.5)
    result = _call_tool(config, "jarvismind_context", {"query": "hello"})
    assert result["isError"] is True
    text = result["content"][0]["text"].lower()
    assert "cannot reach" in text or "connection" in text


def test_timeout_fails_open(stub):
    stub.delay = 1.0
    config = _config(stub, timeout=0.2)
    started = time.monotonic()
    result = _call_tool(config, "jarvismind_context", {"query": "slow"})
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result["isError"] is True
    text = result["content"][0]["text"].lower()
    assert "timed out" in text or "timeout" in text


def test_server_error_is_readable_and_redacted(stub):
    stub.error_response = (
        500,
        {
            "error": {
                "code": "internal_error",
                "message": f"upstream exploded with {FAKE_TOKEN}",
                "details": None,
            }
        },
    )
    config = _config(stub)
    responses, output, stderr = _run(
        config, [_tool_call("jarvismind_context", {"query": "boom"}, 1)]
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "500" in text
    assert "internal_error" in text
    assert FAKE_TOKEN not in text
    assert FAKE_TOKEN not in output
    assert FAKE_TOKEN not in stderr
    assert "[REDACTED_TOKEN]" in output
    assert "[REDACTED_TOKEN]" in stderr


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "authentication_error"), (403, "permission_denied")],
)
def test_auth_errors_are_readable_and_redacted(stub, status, code):
    stub.error_response = (
        status,
        {
            "error": {
                "code": code,
                "message": f"refused for {FAKE_TOKEN}",
                "details": {"permission": "read"},
            }
        },
    )
    config = _config(stub)
    responses, output, stderr = _run(
        config, [_tool_call("jarvismind_core_anchor", {}, 1)]
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert str(status) in text
    assert code in text
    assert FAKE_TOKEN not in output
    assert FAKE_TOKEN not in stderr


# ---------------------------------------------------------------------------
# token resolution
# ---------------------------------------------------------------------------


def test_token_file_success_and_environment_precedence(tmp_path):
    token_path = tmp_path / "agent.token"
    token_path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")

    config = MCPConfig.from_values(url=f"http://{STUB_HOST}:9", token_file=str(token_path), environ={})
    assert config.token == FAKE_TOKEN
    assert config.token_error is None

    config = MCPConfig.from_values(
        url=f"http://{STUB_HOST}:9",
        token_file=str(token_path),
        environ={"JARVISMIND_AGENT_TOKEN": OTHER_TOKEN},
    )
    assert config.token == OTHER_TOKEN

    config = MCPConfig.from_values(
        url=f"http://{STUB_HOST}:9",
        token=FAKE_TOKEN,
        environ={"JARVISMIND_AGENT_TOKEN": OTHER_TOKEN},
    )
    assert config.token == FAKE_TOKEN

    config = MCPConfig.from_values(
        url=f"http://{STUB_HOST}:9",
        environ={"JARVISMIND_AGENT_TOKEN_FILE": str(token_path)},
    )
    assert config.token == FAKE_TOKEN


def test_token_file_expands_tilde(tmp_path, monkeypatch):
    token_path = tmp_path / "agent.token"
    token_path.write_text(FAKE_TOKEN, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    for value in ("~/agent.token", "~\\agent.token"):
        config = MCPConfig.from_values(
            url=f"http://{STUB_HOST}:9", token_file=value, environ={}
        )
        assert config.token == FAKE_TOKEN, value
        assert config.token_error is None


def test_token_file_missing_and_empty_are_reported(tmp_path, stub):
    missing = tmp_path / "missing.token"
    empty = tmp_path / "empty.token"
    empty.write_text("   \n", encoding="utf-8")

    missing_config = MCPConfig.from_values(
        url=f"http://{STUB_HOST}:9", token_file=str(missing), environ={}
    )
    assert missing_config.token is None
    assert "missing.token" in (missing_config.token_error or "")
    assert FAKE_TOKEN not in repr(missing_config)

    empty_config = MCPConfig.from_values(
        url=f"http://{STUB_HOST}:9", token_file=str(empty), environ={}
    )
    assert empty_config.token is None
    assert "empty" in (empty_config.token_error or "").lower()

    responses, output, _stderr = _run(
        MCPConfig(
            base_url=stub.base_url,
            token=None,
            token_error=missing_config.token_error,
            timeout=1.0,
        ),
        [_tool_call("jarvismind_context", {"query": "hello"}, 1)],
    )
    result = responses[0]["result"]
    assert result["isError"] is True
    assert "missing.token" in result["content"][0]["text"]
    assert "token" in output.lower()


def test_config_repr_never_contains_the_token():
    config = _config(token=FAKE_TOKEN, space="space-1")
    assert FAKE_TOKEN not in repr(config)
    assert "token" not in repr(config)


# ---------------------------------------------------------------------------
# space header and CLI wiring
# ---------------------------------------------------------------------------


def test_space_header_is_forwarded_on_every_request(stub):
    config = _config(stub, space="space-1")
    _call_tool(config, "jarvismind_context", {"query": "hello"})
    _call_tool(config, "jarvismind_core_anchor", {})
    assert len(stub.requests) >= 2
    for request in stub.requests:
        assert request["headers"]["x-jarvismind-space"] == "space-1"
        assert request["headers"]["authorization"] == f"Bearer {FAKE_TOKEN}"

    plain = _config(stub)
    stub.requests.clear()
    _call_tool(plain, "jarvismind_context", {"query": "hello"})
    assert "x-jarvismind-space" not in stub.requests[-1]["headers"]


def test_cli_parser_registers_mcp_options():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "mcp",
            "--url",
            "http://localhost:9123",
            "--token",
            "literal-token",
            "--token-file",
            "some-file",
            "--space",
            "space-9",
            "--timeout",
            "2.5",
        ]
    )
    assert args.command == "mcp"
    assert args.url == "http://localhost:9123"
    assert args.token == "literal-token"
    assert args.token_file == "some-file"
    assert args.space == "space-9"
    assert args.timeout == "2.5"


def test_cli_mcp_branch_never_loads_settings(monkeypatch):
    import jarvismind.mcp.server as mcp_server

    captured = {}

    def fake_run_stdio(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mcp_server, "run_stdio", fake_run_stdio)
    exit_code = cli.main(
        [
            "mcp",
            "--url",
            "http://localhost:9123",
            "--token",
            FAKE_TOKEN,
            "--space",
            "space-9",
            "--timeout",
            "2.5",
        ]
    )
    assert exit_code == 0
    config = captured["config"]
    assert config.base_url == "http://localhost:9123"
    assert config.token == FAKE_TOKEN
    assert config.space == "space-9"
    assert config.timeout == 2.5
    assert FAKE_TOKEN not in repr(config)


def test_cli_mcp_rejects_invalid_configuration(monkeypatch, capsys):
    import jarvismind.mcp.server as mcp_server

    calls = []
    monkeypatch.setattr(
        mcp_server, "run_stdio", lambda *args, **kwargs: calls.append(True) or 0
    )

    assert cli.main(["mcp", "--url", "ftp://example.test", "--token", FAKE_TOKEN]) == 2
    assert cli.main(
        ["mcp", "--url", "http://localhost:9123", "--token", FAKE_TOKEN, "--timeout", "nope"]
    ) == 2
    assert cli.main(
        ["mcp", "--url", "http://localhost:9123", "--token", FAKE_TOKEN, "--timeout", "-1"]
    ) == 2
    assert calls == []
    stderr = capsys.readouterr().err
    assert "URL" in stderr or "url" in stderr
    assert "timeout" in stderr.lower()
    assert FAKE_TOKEN not in stderr


# ---------------------------------------------------------------------------
# real pipe and redaction helpers
# ---------------------------------------------------------------------------


def test_real_os_pipe_serves_line_by_line(stub):
    config = _config(stub)
    in_read_fd, in_write_fd = os.pipe()
    out_read_fd, out_write_fd = os.pipe()
    stdin_stream = os.fdopen(in_read_fd, "r", encoding="utf-8", newline="\n")
    stdin_writer = os.fdopen(in_write_fd, "w", encoding="utf-8", newline="\n")
    stdout_stream = os.fdopen(out_write_fd, "w", encoding="utf-8", newline="\n")
    stdout_reader_stream = os.fdopen(out_read_fd, "r", encoding="utf-8", newline="\n")
    stderr = io.StringIO()
    lines = []

    def _read_stdout():
        for raw in stdout_reader_stream:
            lines.append(raw.rstrip("\n"))

    server = MCPServer(
        config, stdin=stdin_stream, stdout=stdout_stream, stderr=stderr
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    reader_thread = threading.Thread(target=_read_stdout, daemon=True)
    server_thread.start()
    reader_thread.start()
    try:
        stdin_writer.write(json.dumps(_request(1, "ping")) + "\n")
        stdin_writer.flush()
        _wait_for_lines(lines, 1)
        assert json.loads(lines[0])["result"] == {}

        stdin_writer.write(json.dumps(_request(2, "tools/list")) + "\n")
        stdin_writer.flush()
        _wait_for_lines(lines, 2)
        tools = json.loads(lines[1])["result"]["tools"]
        assert len(tools) == 5

        stdin_writer.write(json.dumps(_request(3, "resources/list")) + "\n")
        stdin_writer.flush()
        _wait_for_lines(lines, 3)
        assert len(json.loads(lines[2])["result"]["resources"]) == 1
    finally:
        stdin_writer.close()
        server_thread.join(timeout=2.0)
        assert not server_thread.is_alive()
        stdout_stream.close()
        reader_thread.join(timeout=2.0)
        stdin_stream.close()

    assert len(lines) == 3
    for raw in lines:
        _jsonrpc_line_ok(json.loads(raw))


def test_redaction_helpers():
    text = f"before {FAKE_TOKEN} middle {OTHER_TOKEN} after"
    cleaned = redact(text)
    assert FAKE_TOKEN not in cleaned
    assert OTHER_TOKEN not in cleaned
    assert cleaned.count("[REDACTED_TOKEN]") == 2

    payload = {
        "token": FAKE_TOKEN,
        "nested": [{"message": f"oops {OTHER_TOKEN}"}],
        "confirmation_token": "secret",
        "plaintext_token": "secret",
    }
    redacted = redact_value(payload)
    assert redacted["token"] == "[REDACTED_TOKEN]"
    assert OTHER_TOKEN not in json.dumps(redacted)
    stripped = strip_secret_fields(payload)
    assert "confirmation_token" not in stripped
    assert "plaintext_token" not in stripped
    assert stripped["token"] == FAKE_TOKEN


def test_remember_batch_stores_items_and_reports_a_held_one(stub):
    config = _config(stub)

    def remember_override(record):
        text = record["body"]["text"]
        if "敏感" in text:
            return 200, {"data": {"status": "PROTECTED", "reason": "sensitive"}}
        return 200, {"data": {"status": "SAVED", "memory_id": f"mem-{len(text)}"}}

    stub.overrides[("POST", REMEMBER_PATH)] = remember_override

    result = _call_tool(
        config,
        "jarvismind_remember",
        {
            "items": [
                "  用户喜欢绿茶  ",
                "用户有一段敏感往事",
                "",
                "用户喜欢绿茶",
            ],
            "conversation_id": "conv-batch",
        },
    )
    assert result["isError"] is False

    # Whitespace is trimmed, the blank is dropped and the repeat is collapsed:
    # a duplicate inside one batch is the caller repeating itself.
    sent = [request for request in stub.requests if request["path"] == REMEMBER_PATH]
    assert [request["body"]["text"] for request in sent] == [
        "用户喜欢绿茶",
        "用户有一段敏感往事",
    ]
    assert all(request["body"]["mode"] == "direct" for request in sent)
    assert all(request["body"]["conversation_id"] == "conv-batch" for request in sent)

    payload = json.loads(result["content"][0]["text"])
    assert payload["submitted"] == 2
    assert payload["saved"] == 1
    assert payload["protected"] == 1
    assert payload["denied"] == 0
    assert payload["failed"] == 0
    # The model must be told where a held item went, not just that it "worked".
    assert "pending-review area" in payload["note"]
    assert "owner must approve" in payload["note"]
    assert payload["results"][1]["status"] == "PROTECTED"


def test_remember_batch_cap_and_mutual_exclusion_are_errors(stub):
    config = _config(stub)

    too_many = [f"事实 {index}" for index in range(21)]
    responses, _output, _stderr = _run(
        config,
        [
            _tool_call("jarvismind_remember", {"items": too_many}, 1),
            _tool_call(
                "jarvismind_remember",
                {"text": "one", "items": ["two"]},
                2,
            ),
            _tool_call("jarvismind_remember", {}, 3),
            _tool_call("jarvismind_remember", {"items": []}, 4),
        ],
    )
    codes = [response["error"]["code"] for response in responses]
    assert codes == [-32602, -32602, -32602, -32602]
    assert "exceeds the limit of 20" in responses[0]["error"]["message"]
    assert "not both" in responses[1]["error"]["message"]
    assert "either 'text' or 'items'" in responses[2]["error"]["message"]

    # None of the rejected calls reached the service.
    assert [request for request in stub.requests if request["path"] == REMEMBER_PATH] == []


def test_remember_batch_continues_after_an_upstream_failure(stub):
    config = _config(stub)
    calls = {"count": 0}

    def flaky_override(record):
        calls["count"] += 1
        if calls["count"] == 2:
            return 500, {"error": {"code": "internal_error", "message": "boom"}}
        return 200, {"data": {"status": "SAVED", "memory_id": "mem-ok"}}

    stub.overrides[("POST", REMEMBER_PATH)] = flaky_override

    result = _call_tool(
        config,
        "jarvismind_remember",
        {"items": ["first fact", "second fact", "third fact"]},
    )
    assert result["isError"] is False

    payload = json.loads(result["content"][0]["text"])
    # A single unreachable item must not discard the results of the others.
    assert payload["saved"] == 2
    assert payload["failed"] == 1
    assert payload["submitted"] == 3
    assert payload["results"][1]["status"] == "FAILED"


def test_remember_single_text_still_works(stub):
    config = _config(stub)
    result = _call_tool(config, "jarvismind_remember", {"text": "I live in Berlin"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "SAVED"
    assert stub.requests[-1]["body"]["text"] == "I live in Berlin"