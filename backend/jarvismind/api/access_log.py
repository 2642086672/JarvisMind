"""Access-log middleware for ``/api/`` traffic.

Only request metadata is recorded: method, route template, status, latency,
principal kind, agent id, token id (never the token), space id, error code and a
keyed digest of the client address. Bodies, query strings, headers and raw IP
addresses are never written. A logging failure never changes the response.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("jarvismind.access")

PRUNE_EVERY = 200


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp, *, target: Any) -> None:
        self.app = app
        self.target = target
        self._writes = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api/"):
            await self.app(scope, receive, send)
            return
        status_holder = {"code": 500}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await run_in_threadpool(self._record, scope, status_holder["code"], latency_ms)

    @staticmethod
    def route_template(scope: Scope) -> str:
        """The route pattern (for example ``/api/v1/agent/memories/{memory_id}``).

        FastAPI records the matched route in its effective route context. The raw
        request path is never used, because it would leak identifiers into logs.
        """
        context = (scope.get("fastapi") or {}).get("effective_route_context")
        template = getattr(context, "path", None)
        if isinstance(template, str) and template:
            return template
        template = getattr(scope.get("route"), "path", None)
        if isinstance(template, str) and template:
            return template
        return "unmatched"

    def _record(self, scope: Scope, status_code: int, latency_ms: int) -> None:
        try:
            control = getattr(self.target.state, "control", None)
            if control is None:
                return
            state = scope.get("state") or {}
            principal = state.get("principal")
            client = scope.get("client")
            host = client[0] if client else ""
            control.record_access(
                principal_kind=principal.kind if principal is not None else "anonymous",
                agent_id=getattr(principal, "agent_id", None),
                token_id=getattr(principal, "token_id", None),
                space_id=state.get("space_id"),
                method=str(scope.get("method", "GET")),
                route=self.route_template(scope),
                status_code=status_code,
                latency_ms=latency_ms,
                error_code=state.get("error_code"),
                client_digest=control.client_digest(host),
            )
            self._writes += 1
            if self._writes % PRUNE_EVERY == 0:
                control.prune_access_logs()
        except Exception:  # noqa: BLE001 - logging must never break a response
            logger.warning("access log write failed")
