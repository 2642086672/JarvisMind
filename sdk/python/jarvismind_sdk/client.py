"""HTTP client for the JarvisMind agent API (``/api/v1/agent``).

Standard library only. Single-object methods return the ``data`` value of the
response. Paged list methods return the whole envelope, ``{"data": [...], "meta": {...}}``,
so the caller can read paging information.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from .errors import JarvisMindError, NetworkError, error_for_status

SPACE_HEADER = "X-JarvisMind-Space"
DEFAULT_TIMEOUT_SECONDS = 30.0
USER_AGENT = "jarvismind-python-sdk/1.0.0"


class JarvisMindClient:
    """One agent credential against one JarvisMind server.

    ``space_id`` selects a memory space the agent has been granted. When omitted,
    the server uses the agent's default space.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        space_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if not token:
            raise ValueError("an agent token is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._space_id = space_id
        self._timeout = timeout

    def __repr__(self) -> str:  # the token is never part of the representation
        return f"JarvisMindClient(base_url={self._base_url!r}, space_id={self._space_id!r})"

    # ------------------------------------------------------------------ transport
    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self._base_url + path
        params = {key: value for key, value in (query or {}).items() if value is not None}
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._space_id:
            headers[SPACE_HEADER] = self._space_id
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise error_for_status(error.code, error.read(), error.headers.get("Retry-After")) from None
        except (urllib.error.URLError, OSError) as error:
            raise NetworkError(f"cannot reach JarvisMind at {self._base_url} ({type(error).__name__})") from None
        return json.loads(raw.decode("utf-8")) if raw else {}

    @staticmethod
    def _memory_path(memory_id: str, suffix: str = "") -> str:
        return f"/api/v1/agent/memories/{urllib.parse.quote(memory_id, safe='')}{suffix}"

    # ------------------------------------------------------------------- identity
    def me(self) -> Dict[str, Any]:
        """The agent's identity, permissions and the spaces it may use."""
        return self._request("GET", "/api/v1/agent/me")["data"]

    # --------------------------------------------------------------------- writes
    def remember(
        self,
        text: str,
        conversation_id: str,
        *,
        message_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        source_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit one explicit memory through the governed path.

        ``mode`` is optional and omitted by default, so an existing call sends
        exactly the bytes it always did. Omit it (or pass ``"auto"``) to keep
        the server's original behavior: the text must look like a memory
        request, or the call returns ``NO_INTENT``. Pass ``"direct"`` when you
        have already decided the statement is worth remembering; it skips only
        that intent filter. The credential gate, the sensitivity hold and
        de-duplication still apply in both modes.

        ``status`` says what happened: ``SAVED`` or ``UPDATED`` when stored;
        ``CONFLICT`` when it conflicts with an existing memory; ``PROTECTED`` when it
        is sensitive and held; ``DUPLICATE``, ``NO_MEMORY`` or ``NO_INTENT`` when not stored; ``FAILED`` when the write did not complete;
        ``DENIED`` when it contains a credential. Credentials are never stored.
        """
        body = {
            "text": text,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "turn_id": turn_id,
            "source_id": source_id,
            "mode": mode,
        }
        return self._request("POST", "/api/v1/agent/remember", body=_drop_none(body))["data"]

    def extract(
        self,
        conversation: str,
        conversation_id: str,
        *,
        context: Optional[str] = None,
        message_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract memory candidates from a transcript. Candidates still pass through governance."""
        body = {
            "conversation": conversation,
            "conversation_id": conversation_id,
            "context": context,
            "message_id": message_id,
            "turn_id": turn_id,
            "source_id": source_id,
        }
        return self._request("POST", "/api/v1/agent/extract", body=_drop_none(body))["data"]

    def forget(self, memory_id: str, *, reason: Optional[str] = None) -> Dict[str, Any]:
        """Ask the owner to forget a memory.

        The memory is not changed by this call. The owner approves or rejects the request
        in the console. The response never contains a confirmation token.
        ``reused_existing`` is true when a request for this agent was already pending.
        """
        body = _drop_none({"reason": reason})
        return self._request("POST", self._memory_path(memory_id, "/forget-requests"), body=body)["data"]

    def update(
        self,
        memory_id: str,
        proposed_changes: Mapping[str, Any],
        *,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Request a correction. Agents are non-human actors under JMR policy.

        The server refuses this with ``PermissionDeniedError`` (code ``non_human_actor_forbidden``).
        A correction is made by the owner in the console. The method exists so the
        contract is explicit and testable.
        """
        body = _drop_none({"proposed_changes": dict(proposed_changes), "reason": reason})
        return self._request("POST", self._memory_path(memory_id, "/modification-requests"), body=body)["data"]

    # ---------------------------------------------------------------------- reads
    def recall(self, query: str, *, limit: int = 8) -> Dict[str, Any]:
        """Ranked memories for a query, without context formatting."""
        return self._request("POST", "/api/v1/agent/recall", body={"query": query, "limit": limit})["data"]

    def context(self, query: str, *, limit: int = 8, max_content_chars: int = 600) -> Dict[str, Any]:
        """A context block ready for a system prompt, plus citations.

        The block is wrapped in ``<jarvismind-context trust="untrusted-data">``. Its content
        is data to read, not instructions to follow.
        """
        body = {"query": query, "limit": limit, "max_content_chars": max_content_chars}
        return self._request("POST", "/api/v1/agent/context", body=body)["data"]

    def search(self, query: str, *, limit: int = 10) -> Dict[str, Any]:
        """Keyword search over the agent's readable memories. Returns the envelope."""
        return self._request("GET", "/api/v1/agent/search", query={"q": query, "limit": limit})

    def list_memories(self, *, status: str = "ACTIVE", page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Readable memories, newest first. Returns the envelope with ``meta.has_more``."""
        query = {"status": status, "page": page, "page_size": page_size}
        return self._request("GET", "/api/v1/agent/memories", query=query)

    def get_memory(self, memory_id: str) -> Dict[str, Any]:
        """One memory. Forgotten and merged memories are not returned."""
        return self._request("GET", self._memory_path(memory_id))["data"]

    def my_requests(self, *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Governance requests this agent created, with their current status."""
        return self._request(
            "GET", "/api/v1/agent/governance/requests", query={"page": page, "page_size": page_size}
        )

    def events(self, *, limit: int = 50) -> Dict[str, Any]:
        """This agent's own recent API access records (metadata only)."""
        return self._request("GET", "/api/v1/agent/events", query={"limit": limit})

    def graph_status(self) -> Dict[str, Any]:
        """Whether the knowledge graph is enabled and usable for this space."""
        return self._request("GET", "/api/v1/agent/graph/status")["data"]

    def graph_nodes(self, *, q: Optional[str] = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Graph entities whose source memory is still valid. Returns the envelope."""
        query = {"q": q, "page": page, "page_size": page_size}
        return self._request("GET", "/api/v1/agent/graph/nodes", query=query)


def _drop_none(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


__all__ = ["JarvisMindClient", "JarvisMindError"]
