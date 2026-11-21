"""Agent-facing API (``/api/v1/agent``): what a granted agent may do.

Every route declares the permission it needs and runs in the memory space the
credential may use. Writes go through governed paths only: automatic remember,
extraction candidates and forget requests. An agent never receives a forget
confirmation token and never approves anything. Modification requests are refused
by the JMR policy for non-human actors.

Sensitive memories are withheld from an agent that lacks the ``sensitive``
permission. Forgotten and merged memories are never returned to any agent.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...container import Container
from ...control.errors import ControlConflictError, PermissionDeniedError
from ...control.permissions import CONTEXT, DELETE, GRAPH, READ, SEARCH, SENSITIVE, UPDATE, WRITE
from ...control.principal import Principal
from ...control.sensitivity import is_protected, record_is_protected
from ...core.errors import NotFoundError
from ...schemas.extraction import ExtractRequest
from ...schemas.memory import record_to_response
from ...schemas.modification import ModificationRequestCreate
from ...services.forget_service import AGENT_FACADE_ORIGIN, AGENT_REQUESTER_PREFIX
from ..auth import principal_auth, require_write_space
from ..deps import get_container

router = APIRouter(tags=["agent"])

SENSITIVE_DENIED_CODE = "sensitive_access_denied"
MAX_REQUEST_SCAN_PAGES = 20
#: Statuses readable through the facade. DELETED and MERGED rows keep their text for
#: provenance, so they are never returned to an agent.
READABLE_STATUSES = frozenset({"ACTIVE", "ARCHIVED"})


def _agent_only(permission: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(principal_auth)) -> Principal:
        if principal.is_owner or principal.agent_id is None:
            raise PermissionDeniedError(detail={"reason": "agent_credential_required"})
        if not principal.has(permission):
            raise PermissionDeniedError(detail={"permission": permission})
        return principal

    return dependency


def _route_guards(permission: str, *, write: bool = False) -> list:
    guards = [Depends(_agent_only(permission))]
    if write:
        guards.append(Depends(require_write_space))
    return guards


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _agent_label(principal: Principal) -> str:
    return f"{AGENT_REQUESTER_PREFIX}{principal.agent_id}"


def _flatten(text: str) -> str:
    return " ".join(str(text).split())


def _escape_markup(text: str) -> str:
    """Neutralise markup so memory text cannot close the context wrapper."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def context_block(items: List[Any]) -> str:
    lines = ['<jarvismind-context trust="untrusted-data">']
    for item in items:
        lines.append(f"- [{item.memory_type.value}] {_escape_markup(_flatten(item.content))}")
    lines.append("</jarvismind-context>")
    return "\n".join(lines)


def _record_is_protected(record) -> bool:
    return record_is_protected(record.content, record.summary, record.metadata_json)


def _visible_items(principal: Principal, items: List[Any]) -> List[Any]:
    if principal.has(SENSITIVE):
        return items
    return [item for item in items if not is_protected(item.content)]


def _sensitive_denied() -> PermissionDeniedError:
    return PermissionDeniedError(
        "This memory is sensitive and the credential lacks sensitive access",
        detail={"code": SENSITIVE_DENIED_CODE, "permission": SENSITIVE},
    )


class RememberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=5000)
    conversation_id: str = Field(..., min_length=1, max_length=255)
    message_id: Optional[str] = Field(default=None, max_length=128)
    turn_id: Optional[str] = Field(default=None, max_length=255)
    source_id: Optional[str] = Field(default=None, max_length=128)


class RecallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)


class ContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)
    max_content_chars: int = Field(default=600, ge=80, le=2000)


class ForgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=1000)


@router.get("/me")
def me(request: Request, principal: Principal = Depends(principal_auth)) -> dict:
    control = request.app.state.control
    accessible: List[Dict[str, Any]] = []
    for space in control.list_spaces():
        access = principal.access_for(space["id"])
        if access is not None:
            accessible.append({**space, "access": access})
    return {
        "data": {
            **principal.public_view(),
            "active_space_id": getattr(request.state, "space_id", principal.default_space_id),
            "accessible_spaces": accessible,
        }
    }


@router.post("/remember", dependencies=_route_guards(WRITE, write=True))
def remember(payload: RememberIn, container: Container = Depends(get_container)) -> dict:
    result = container.auto_memory_service.auto_remember(
        text_value=payload.text,
        conversation_id=payload.conversation_id,
        message_id=payload.message_id,
        turn_id=payload.turn_id,
        source_id=payload.source_id,
    )
    return {"data": _dump(result)}


@router.post("/extract", status_code=201, dependencies=_route_guards(WRITE, write=True))
def extract(payload: ExtractRequest, container: Container = Depends(get_container)) -> dict:
    return {"data": _dump(container.extraction_engine.extract(payload))}


@router.post("/recall", dependencies=_route_guards(SEARCH))
def recall(
    payload: RecallIn,
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    result = container.memory_context_service.retrieve(query=payload.query, limit=payload.limit)
    items = _visible_items(principal, list(result.items))
    return {
        "data": {
            "items": [_dump(item) for item in items],
            "total": len(items),
            "retrieval_version": result.retrieval_version,
            "degraded_to_fts": result.degraded_to_fts,
        }
    }


@router.get("/search", dependencies=_route_guards(SEARCH))
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    rows, _total = container.memory_service.search(
        query=q, memory_type=None, status="ACTIVE", page=1, page_size=limit
    )
    visible: List[Dict[str, Any]] = []
    for row in rows:
        content = str(row["content"])
        summary = str(row["summary"] or "")
        if not principal.has(SENSITIVE) and (is_protected(content) or is_protected(summary)):
            continue
        visible.append(
            {
                "memory_id": row["id"],
                "content": content,
                "summary": row["summary"],
                "memory_type": row["memory_type"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "rank": row["rank"],
            }
        )
    return {"data": visible, "meta": {"limit": limit, "returned": len(visible)}}


@router.post("/context", dependencies=_route_guards(CONTEXT))
def context(
    payload: ContextIn,
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    result = container.memory_context_service.retrieve(
        query=payload.query,
        limit=payload.limit,
        max_content_chars=payload.max_content_chars,
    )
    items = _visible_items(principal, list(result.items))
    return {
        "data": {
            "context": context_block(items),
            "citations": [
                {
                    "memory_id": item.memory_id,
                    "memory_type": item.memory_type.value,
                    "relevance_score": item.relevance_score,
                }
                for item in items
            ],
            "retrieval_version": result.retrieval_version,
        }
    }


@router.get("/memories", dependencies=_route_guards(READ))
def list_memories(
    request: Request,
    status: str = Query(default="ACTIVE", pattern="^(ACTIVE|ARCHIVED)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    records, total = container.memory_service.list(
        status=status,
        page=page,
        page_size=page_size,
        sort_by="created_at",
        sort_order="desc",
    )
    visible = [
        record
        for record in records
        if principal.has(SENSITIVE) or not _record_is_protected(record)
    ]
    return {
        "data": [_dump(record_to_response(record)) for record in visible],
        "meta": {
            "page": page,
            "page_size": page_size,
            "returned": len(visible),
            "has_more": page * page_size < int(total),
        },
    }


@router.get("/memories/{memory_id}", dependencies=_route_guards(READ))
def get_memory(
    memory_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    record = container.memory_service.get(memory_id, touch=False)
    if str(record.status) not in READABLE_STATUSES:
        raise NotFoundError("Memory not found", detail={"code": "memory_not_found"})
    if _record_is_protected(record) and not principal.has(SENSITIVE):
        raise _sensitive_denied()
    # The access is recorded only after the credential has been authorised.
    container.memory_service.get(memory_id)
    return {"data": _dump(record_to_response(record))}


@router.post(
    "/memories/{memory_id}/forget-requests",
    status_code=201,
    dependencies=_route_guards(DELETE, write=True),
)
def request_forget(
    memory_id: str,
    payload: ForgetIn,
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    """Ask the owner to forget a memory. The confirmation token is never returned.

    A pending request created by the owner is not replaced: re-creating it would
    rotate the owner's one-time token and silently break the owner's approval.
    """
    principal: Principal = request.state.principal
    record = container.memory_service.get(memory_id, touch=False)
    if str(record.status) not in READABLE_STATUSES:
        raise NotFoundError("Memory not found", detail={"code": "memory_not_found"})
    if _record_is_protected(record) and not principal.has(SENSITIVE):
        raise _sensitive_denied()
    label = _agent_label(principal)
    pending, _total = container.forget_service.list_requests(
        status="PENDING", memory_id=memory_id, page=1, page_size=20
    )
    for item in pending:
        if str(_dump(item).get("requester_label") or "") != label:
            raise ControlConflictError(
                "a forget request for this memory is already waiting for owner review",
                detail={"code": "forget_already_pending"},
            )
    response, _token, reused = container.forget_service.create_request(
        memory_id,
        reason=payload.reason,
        requester_label=label,
        request_source="USER_API",
        server_origin=AGENT_FACADE_ORIGIN,
    )
    return {"data": {**_dump(response), "reused_existing": bool(reused)}}


@router.post(
    "/memories/{memory_id}/modification-requests",
    status_code=201,
    dependencies=_route_guards(UPDATE, write=True),
)
def request_modification(
    memory_id: str,
    payload: ModificationRequestCreate,
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    """Documented refusal: the JMR policy treats agent labels as non-human.

    ``UPDATE`` stays in the permission model, but a correction needs a human
    decision, so the call reaches the JMR policy and returns 403
    ``non_human_actor_forbidden``. It never writes.
    """
    principal: Principal = request.state.principal
    request_row, _token, reused = container.modification_service.create_request(
        memory_id,
        proposed_changes=payload.proposed_changes,
        request_kind=payload.request_kind,
        reason=payload.reason,
        requester_label=_agent_label(principal),
    )
    return {"data": {"request": _dump(request_row), "reused_pending_request": bool(reused)}}


@router.get("/governance/requests", dependencies=_route_guards(READ))
def my_requests(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    label = _agent_label(principal)
    own: List[Dict[str, Any]] = []
    scanned = 0
    scan_page = 1
    while len(own) < page_size * page and scan_page <= MAX_REQUEST_SCAN_PAGES:
        rows, total = container.forget_service.list_requests(
            status=None, memory_id=None, page=scan_page, page_size=100
        )
        for row in rows:
            scanned += 1
            data = _dump(row)
            if isinstance(data, dict) and data.get("requester_label") == label:
                own.append(data)
        if scanned >= int(total) or not rows:
            break
        scan_page += 1
    start = (page - 1) * page_size
    window = own[start : start + page_size]
    return {
        "data": window,
        "meta": {"page": page, "page_size": page_size, "returned": len(window), "scanned": scanned},
    }


@router.get("/graph/status", dependencies=_route_guards(GRAPH))
def graph_status(container: Container = Depends(get_container)) -> dict:
    return {"data": container.jkg_adapter.health()}


@router.get("/graph/nodes", dependencies=_route_guards(GRAPH))
def graph_nodes(
    request: Request,
    q: Optional[str] = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> dict:
    principal: Principal = request.state.principal
    result = container.jkg_query_service.list_nodes(
        entity_type=None,
        query=q,
        status=None,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    items: List[Dict[str, Any]] = []
    for item in result.get("items", []):
        # A node whose source memory was forgotten must not reach an agent.
        if item.get("source_memory_valid") is False:
            continue
        if principal.has(SENSITIVE) or not is_protected(json.dumps(item, ensure_ascii=False, default=str)):
            items.append(item)
    return {"data": items, "meta": {"page": page, "page_size": page_size, "returned": len(items)}}


@router.get("/events")
def my_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(principal_auth),
) -> dict:
    if principal.is_owner or principal.agent_id is None:
        raise PermissionDeniedError(detail={"reason": "agent_credential_required"})
    items, total = request.app.state.control.list_access_logs(
        limit=limit,
        offset=0,
        agent_id=principal.agent_id,
    )
    return {"data": items, "meta": {"total": total, "limit": limit}}
