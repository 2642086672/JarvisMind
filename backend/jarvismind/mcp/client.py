"""Minimal stdlib HTTP client for the JarvisMind agent facade.

The client talks only to ``/api/v1/agent/*``.  It never imports FastAPI,
SQLAlchemy or any service module, and every failure becomes an
``MCPUpstreamError`` whose message is safe to show to a model (no token, no
response body echo).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .config import MCPConfig
from .redaction import redact

JsonDict = Dict[str, Any]


class MCPUpstreamError(RuntimeError):
    """A transport, HTTP or response-shape failure against the local service."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
    ) -> None:
        safe = redact(str(message))
        super().__init__(safe)
        self.message = safe
        self.code = code
        self.status = status


class AgentApiClient:
    """Bridge client.  One instance per MCP server process."""

    def __init__(self, config: MCPConfig) -> None:
        self._config = config

    def __repr__(self) -> str:  # never expose the token
        return (
            "AgentApiClient("
            f"base_url={self._config.base_url!r}, "
            f"space={self._config.space!r}, "
            f"timeout={self._config.timeout!r})"
        )

    # -- public API ---------------------------------------------------------

    def context(
        self,
        query: str,
        limit: int,
        max_content_chars: Optional[int] = None,
    ) -> JsonDict:
        body: JsonDict = {"query": query, "limit": limit}
        if max_content_chars is not None:
            body["max_content_chars"] = max_content_chars
        payload = self._request("POST", "/api/v1/agent/context", body=body)
        return self._data(payload, dict, what="context")

    def remember(self, text: str, conversation_id: str, mode: str = "direct") -> JsonDict:
        body = {"text": text, "conversation_id": conversation_id, "mode": mode}
        payload = self._request("POST", "/api/v1/agent/remember", body=body)
        return self._data(payload, dict, what="remember")

    def search(self, query: str, limit: int) -> Tuple[List[Any], JsonDict]:
        payload = self._request(
            "GET",
            "/api/v1/agent/search",
            query=(("q", query), ("limit", limit)),
        )
        rows = self._data(payload, list, what="search")
        meta = payload.get("meta")
        return rows, meta if isinstance(meta, dict) else {}

    def list_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        status: str = "ACTIVE",
        page: int = 1,
        page_size: int = 20,
    ) -> List[Any]:
        params: List[Tuple[str, Any]] = [("status", status), ("page", page), ("page_size", page_size)]
        if memory_type:
            params.insert(0, ("memory_type", memory_type))
        payload = self._request("GET", "/api/v1/agent/memories", query=params)
        return self._data(payload, list, what="memories")

    def forget(self, memory_id: str, reason: Optional[str] = None) -> JsonDict:
        quoted = urllib_parse.quote(memory_id, safe="")
        path = f"/api/v1/agent/memories/{quoted}/forget-requests"
        body: JsonDict = {} if reason is None else {"reason": reason}
        payload = self._request("POST", path, body=body)
        return self._data(payload, dict, what="forget request")

    # -- transport ----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "jarvismind-mcp",
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        if self._config.space:
            headers["X-JarvisMind-Space"] = self._config.space
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Sequence[Tuple[str, Any]]] = None,
        body: Optional[JsonDict] = None,
    ) -> JsonDict:
        config = self._config
        if not config.token:
            raise MCPUpstreamError(
                config.token_error or "No agent token configured."
            )

        url = config.base_url + path
        if query:
            url += "?" + urllib_parse.urlencode(list(query))

        headers = self._headers()
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib_request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(request, timeout=config.timeout) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                raw = response.read()
        except urllib_error.HTTPError as exc:
            raw_error = b""
            try:
                raw_error = exc.read()
            except Exception:  # the body may already be closed
                pass
            raise self._http_error(int(exc.code), raw_error) from None
        except urllib_error.URLError as exc:
            raise self._transport_error(exc.reason) from None
        except TimeoutError:
            raise self._timeout_error() from None
        except OSError as exc:
            raise MCPUpstreamError(
                f"Cannot reach JarvisMind at {config.base_url} "
                f"({exc.__class__.__name__}). Is `python -m jarvismind serve` running?"
            ) from None

        return self._decode(status, raw)

    def _timeout_error(self) -> MCPUpstreamError:
        return MCPUpstreamError(
            f"Cannot reach JarvisMind at {self._config.base_url}: connection timed out "
            f"after {self._config.timeout:g}s. Is `python -m jarvismind serve` running?"
        )

    def _transport_error(self, reason: Any) -> MCPUpstreamError:
        text = "" if reason is None else str(reason)
        lowered = text.lower()
        if isinstance(reason, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
            return self._timeout_error()
        detail = text or (reason.__class__.__name__ if reason is not None else "connection failed")
        return MCPUpstreamError(
            f"Cannot reach JarvisMind at {self._config.base_url} "
            f"({detail}). Is `python -m jarvismind serve` running?"
        )

    def _http_error(self, status: int, raw: bytes) -> MCPUpstreamError:
        code = f"http_{status}"
        message = f"JarvisMind returned HTTP {status}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            envelope = payload.get("error")
            if isinstance(envelope, dict):
                raw_code = envelope.get("code")
                raw_message = envelope.get("message")
                if isinstance(raw_code, str) and raw_code:
                    code = raw_code
                if isinstance(raw_message, str) and raw_message:
                    message = raw_message
        return MCPUpstreamError(
            f"JarvisMind API error {status} ({code}): {message}",
            code=code,
            status=status,
        )

    @staticmethod
    def _decode(status: int, raw: bytes) -> JsonDict:
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MCPUpstreamError(
                f"JarvisMind returned a non-JSON response (HTTP {status})."
            ) from None
        if not isinstance(payload, dict):
            raise MCPUpstreamError(
                f"JarvisMind returned an unexpected response shape (HTTP {status})."
            )
        return payload

    @staticmethod
    def _data(payload: JsonDict, expected: type, *, what: str) -> Any:
        if "data" not in payload:
            raise MCPUpstreamError(
                f"JarvisMind {what} response is missing the 'data' field."
            )
        data = payload["data"]
        if not isinstance(data, expected):
            raise MCPUpstreamError(
                f"JarvisMind {what} response has an unexpected 'data' shape."
            )
        return data