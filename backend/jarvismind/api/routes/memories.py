"""Memory CRUD endpoints.

All routes require the API token (enforced by the router-level dependency in
``app.main``). Every response is a validated pydantic model.

JW-00: ``POST`` and ``PATCH`` are direct-write paths that bypass the candidate
approval chain, so they are gated behind ``JARVISMIND_DIRECT_WRITE_ENABLED`` (default
OFF).

JW-01: ``DELETE`` is no longer a direct write. It now only acts as a transport
for an ALREADY-APPROVED path: the caller must present ``forget_request_id`` and
the one-time ``confirmation_token`` issued when that request was created, and the
request must target the same memory. Without both, the call is refused with 403
``forget_approval_required``. This closes the bypass JW-00 deliberately left
open; the iOS client now uses ``POST /forget-requests`` +
``POST /forget-requests/{id}/approve`` instead.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query

from ...container import Container
from ...core.errors import DirectWriteDisabledError, PatchBridgeRemovedError
from ...schemas.common import DataEnvelope, ListEnvelope
from ...schemas.memory import (
    AuditLogQuery,
    AuditLogResponse,
    AuditLogEntry,
    MemoryCreate,
    MemoryListQuery,
    MemoryResponse,
    MemorySearchQuery,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    SearchResult,
    SearchResponse,
    SortBy,
    SortOrder,
    build_page_meta,
    record_to_response,
)
from ..deps import get_container

router = APIRouter(tags=["memories"])


def _require_direct_write(container: Container, *, endpoint: str) -> None:
    """JW-00 gate for direct-write endpoints.

    Raises 403 when ``JARVISMIND_DIRECT_WRITE_ENABLED`` is not strictly true. The
    check reads the resolved setting (never a request field), so no query
    parameter, header or body value can bypass it. Absent/malformed env values
    resolve to ``False`` via ``_env_bool``'s safe default.
    """
    if bool(getattr(container.settings, "direct_write_enabled", False)):
        return
    raise DirectWriteDisabledError(
        detail={
            "endpoint": endpoint,
            "hint": "set JARVISMIND_DIRECT_WRITE_ENABLED=1 to enable this endpoint",
        },
    )


@router.get("", response_model=ListEnvelope[MemoryResponse])
def list_memories(
    memory_type: Optional[MemoryType] = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: SortBy = SortBy.CREATED_AT,
    sort_order: SortOrder = SortOrder.DESC,
    container: Container = Depends(get_container),
) -> ListEnvelope[MemoryResponse]:
    query = MemoryListQuery(
        memory_type=memory_type,
        status=status,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    records, total = container.memory_service.list(
        memory_type=query.memory_type.value if query.memory_type else None,
        status=query.status.value if query.status else None,
        page=query.page,
        page_size=query.page_size,
        sort_by=query.sort_by.value,
        sort_order=query.sort_order.value,
    )
    return ListEnvelope(
        data=[record_to_response(record) for record in records],
        meta=build_page_meta(query.page, query.page_size, total),
    )


@router.get("/search", response_model=SearchResponse)
def search_memories(
    q: str = Query(..., min_length=1, max_length=500),
    memory_type: Optional[MemoryType] = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> SearchResponse:
    query = MemorySearchQuery(
        q=q,
        memory_type=memory_type,
        status=status,
        page=page,
        page_size=page_size,
    )
    rows, total = container.memory_service.search(
        query=query.q,
        memory_type=query.memory_type.value if query.memory_type else None,
        status=query.status.value if query.status else None,
        page=query.page,
        page_size=query.page_size,
    )
    results = [
        SearchResult(
            memory_id=row["id"],
            content=row["content"],
            summary=row["summary"],
            memory_type=MemoryType(row["memory_type"]),
            importance=row["importance"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            rank=row["rank"],
        )
        for row in rows
    ]
    return SearchResponse(
        data=results,
        meta=build_page_meta(query.page, query.page_size, total),
    )


@router.post("", response_model=DataEnvelope[MemoryResponse], status_code=201)
def create_memory(
    payload: MemoryCreate, container: Container = Depends(get_container)
) -> DataEnvelope[MemoryResponse]:
    _require_direct_write(container, endpoint="POST /api/v1/memories")
    record = container.memory_service.create(payload)
    return DataEnvelope(data=record_to_response(record))


@router.get("/{memory_id}", response_model=DataEnvelope[MemoryResponse])
def get_memory(
    memory_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[MemoryResponse]:
    record = container.memory_service.get(memory_id)
    return DataEnvelope(data=record_to_response(record))


@router.get("/{memory_id}/audit-logs", response_model=AuditLogResponse)
def memory_audit_logs(
    memory_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_order: SortOrder = SortOrder.DESC,
    container: Container = Depends(get_container),
) -> AuditLogResponse:
    query = AuditLogQuery(page=page, page_size=page_size, sort_order=sort_order)
    entries, total = container.memory_service.audit_logs(
        memory_id,
        page=query.page,
        page_size=query.page_size,
        sort_order=query.sort_order.value,
    )
    return AuditLogResponse(
        data=[
            AuditLogEntry(
                action=entry["action"],
                actor=entry["actor"],
                detail=entry["detail"],
                created_at=entry["created_at"],
            )
            for entry in entries
        ],
        meta=build_page_meta(query.page, query.page_size, total),
    )


@router.patch("/{memory_id}", response_model=DataEnvelope[MemoryResponse])
def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    container: Container = Depends(get_container),
    x_modification_request_id: Optional[str] = Header(default=None),
    x_modification_token: Optional[str] = Header(default=None),
) -> DataEnvelope[MemoryResponse]:
    """Update a memory.

    JW-03-R1 (rectification): the PATCH bridge is REMOVED.

    The first JW-03 release let a caller authorise a PATCH by presenting
    ``X-Modification-Request-Id`` + ``X-Modification-Token``. That was a weak
    grant: the approved request froze one set of ``proposed_changes``, but the
    PATCH body supplied the values that were actually written. An approver could
    therefore be shown "fix this typo" and receive an arbitrary rewrite - the
    approval and the effect were not the same thing.

    Now there is exactly one path for changing an approved memory's protected
    fields:

        POST /memories/{id}/modification-requests   -> freeze proposed_changes
        POST /modification-requests/{id}/approve    -> apply exactly those values

    This endpoint still updates UNPROTECTED rows (legacy / imported,
    ``source_candidate_id IS NULL``) exactly as before, and still updates an
    approved row's non-protected fields (e.g. nothing currently) through the
    normal service path.

    The two legacy headers are still DECLARED, but only so a stale client is
    told plainly that the bridge is gone instead of silently having its headers
    ignored. Sending them is an error, not a shortcut.
    """
    _require_direct_write(container, endpoint="PATCH /api/v1/memories/{id}")

    if x_modification_request_id or x_modification_token:
        raise PatchBridgeRemovedError(
            "The PATCH modification bridge was removed in JW-03-R1. To change an "
            "approved memory, create a modification request, then approve it: "
            "POST /api/v1/memories/{id}/modification-requests followed by "
            "POST /api/v1/modification-requests/{request_id}/approve.",
            detail={
                "code": "patch_bridge_removed",
                "remedy": [
                    "POST /api/v1/memories/{id}/modification-requests",
                    "POST /api/v1/modification-requests/{request_id}/approve",
                ],
            },
        )

    record = container.memory_service.update(memory_id, payload)
    return DataEnvelope(data=record_to_response(record))


@router.delete("/{memory_id}", response_model=DataEnvelope[MemoryResponse])
def delete_memory(
    memory_id: str,
    forget_request_id: Optional[str] = Query(default=None, max_length=128),
    x_forget_confirmation_token: Optional[str] = Header(default=None),
    x_actor_label: Optional[str] = Header(default=None),
    container: Container = Depends(get_container),
) -> DataEnvelope[MemoryResponse]:
    """Legacy delete transport. Refused without an approved forget request.

    JW-01: the credentials travel in a header, never in the URL, so the one-time
    token cannot leak into access logs, proxies or shell history. The memory is
    only soft-deleted through ``ForgetApprovalService.approve``, which is the
    single code path that is allowed to change the status to DELETED.
    """
    container.forget_service.approve_via_legacy_endpoint(
        memory_id=memory_id,
        forget_request_id=forget_request_id,
        confirmation_token=x_forget_confirmation_token,
        actor_label=x_actor_label,
    )
    record = container.memory_service.get(memory_id)
    return DataEnvelope(data=record_to_response(record))
