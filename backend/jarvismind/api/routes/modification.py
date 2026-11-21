"""JW-03 controlled-modification endpoints.

Route layout
------------
``POST /api/v1/memories/{memory_id}/modification-requests``          create
``GET  /api/v1/memories/{memory_id}/modification-requests``          list per memory
``GET  /api/v1/modification-requests``                              list all
``GET  /api/v1/modification-requests/{request_id}``                  detail
``POST /api/v1/modification-requests/{request_id}/approve``          approve AND execute
``POST /api/v1/modification-requests/{request_id}/reject``           refuse, memory untouched

Why the token travels in the JSON body and not the URL
------------------------------------------------------
Same reasoning as JW-01's forget flow: a query string ends up in access logs,
proxy logs and shell history. A POST body does not. The token is returned
exactly once, at creation, and only its SHA-256 digest is stored.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...schemas.common import DataEnvelope
from ...schemas.modification import (
    ModificationRequestApprove,
    ModificationRequestCreate,
    ModificationRequestCreateResponse,
    ModificationRequestListResponse,
    ModificationRequestReject,
)
from ...schemas.memory import build_page_meta
from ..deps import get_container

memory_router = APIRouter(tags=["memory-modification"])
request_router = APIRouter(tags=["memory-modification"])


@memory_router.post(
    "/{memory_id}/modification-requests",
    response_model=DataEnvelope[ModificationRequestCreateResponse],
    status_code=201,
)
def create_modification_request(
    memory_id: str,
    payload: ModificationRequestCreate,
    container: Container = Depends(get_container),
) -> DataEnvelope[ModificationRequestCreateResponse]:
    """Register a modification request. The memory is NOT modified.

    Only an approved memory (``source_candidate_id`` set) can be the target: an
    unprotected row does not need this flow and is refused with a pointer to the
    plain PATCH endpoint instead of being silently routed through governance it
    does not require.
    """
    service = container.modification_service
    request_row, token, reused = service.create_request(
        memory_id,
        proposed_changes=payload.proposed_changes,
        request_kind=payload.request_kind,
        reason=payload.reason,
        requester_label=payload.requester_label,
    )
    return DataEnvelope(
        data=ModificationRequestCreateResponse(
            request=request_row,
            confirmation_token=token,
            confirmation_expires_in_seconds=service.token_ttl_seconds,
            reused_pending_request=reused,
            authentication_level=request_row["authentication_level"],
            authorization_verification_result=request_row[
                "authorization_verification_result"
            ],
        )
    )


@memory_router.get(
    "/{memory_id}/modification-requests",
    response_model=ModificationRequestListResponse,
)
def list_memory_modification_requests(
    memory_id: str,
    status: Optional[str] = Query(default=None, max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ModificationRequestListResponse:
    rows, total = container.modification_service.list_requests(
        memory_id=memory_id, status=status, page=page, page_size=page_size
    )
    return ModificationRequestListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@request_router.get("", response_model=ModificationRequestListResponse)
def list_modification_requests(
    memory_id: Optional[str] = Query(default=None, max_length=128),
    status: Optional[str] = Query(default=None, max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ModificationRequestListResponse:
    rows, total = container.modification_service.list_requests(
        memory_id=memory_id, status=status, page=page, page_size=page_size
    )
    return ModificationRequestListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@request_router.get("/{request_id}", response_model=DataEnvelope[dict])
def get_modification_request(
    request_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    return DataEnvelope(data=container.modification_service.get_request(request_id))


@request_router.post("/{request_id}/approve", response_model=DataEnvelope[dict])
def approve_modification_request(
    request_id: str,
    payload: ModificationRequestApprove,
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    """The ONLY endpoint that applies a modification. Approval == execution.

    There is deliberately no ``approved=true`` flag and no separate "execute"
    endpoint: a boolean is client-controlled, and splitting approve from execute
    would reintroduce the check/act window JW-01 closed.
    """
    row = container.modification_service.approve(
        request_id,
        confirmation_token=payload.confirmation_token,
        actor_label=payload.actor_label,
    )
    return DataEnvelope(data=row)


@request_router.post("/{request_id}/reject", response_model=DataEnvelope[dict])
def reject_modification_request(
    request_id: str,
    payload: Optional[ModificationRequestReject] = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    body = payload or ModificationRequestReject()
    row = container.modification_service.reject(
        request_id,
        reason=body.reason,
        actor_label=body.actor_label,
    )
    return DataEnvelope(data=row)


__all__ = ["memory_router", "request_router"]
