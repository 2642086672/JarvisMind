"""JW-01 forget-request endpoints.

Route layout
------------
``POST   /api/v1/memories/{memory_id}/forget-requests``  create (never deletes)
``GET    /api/v1/forget-requests``                       list
``GET    /api/v1/forget-requests/{request_id}``          detail
``POST   /api/v1/forget-requests/{request_id}/approve``  user approval -> execute
``POST   /api/v1/forget-requests/{request_id}/reject``   user refusal

Why creation and approval are separate endpoints
------------------------------------------------
The task requires that "the user asked to forget" never equals "the memory is
deleted". A single endpoint with a ``confirm`` flag cannot express that: the flag
is client-controlled, so any caller (including a model tool) can assert it. Here
the two facts have separate storage, separate audit records and a server-issued
one-time token in between, so approval is a distinct, verifiable event.

The router is registered under both prefixes (``/api/v1/memories`` and
``/api/v1``) because the task specifies the create endpoint under the memory
resource and the rest under a top-level ``forget-requests`` collection.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query

from ...container import Container
from ...schemas.common import DataEnvelope, ListEnvelope
from ...schemas.forget import (
    ForgetApprovalRequest,
    ForgetRejectionRequest,
    ForgetRequestCreate,
    ForgetRequestCreateResponse,
    ForgetRequestListResponse,
    ForgetRequestResponse,
    ForgetRequestSource,
    ForgetRequestStatus,
)
from ...schemas.memory import build_page_meta
from ..deps import get_container

memory_router = APIRouter(tags=["memory-forget"])
request_router = APIRouter(tags=["memory-forget"])


@memory_router.post(
    "/{memory_id}/forget-requests",
    response_model=DataEnvelope[ForgetRequestCreateResponse],
    status_code=201,
)
def create_forget_request(
    memory_id: str,
    payload: Optional[ForgetRequestCreate] = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[ForgetRequestCreateResponse]:
    """Register a forget request. The memory is NOT modified.

    Response carries the one-time ``confirmation_token`` exactly once. The
    client must present it (and only it) to approve or reject.
    """
    body = payload or ForgetRequestCreate()
    response, token, reused = container.forget_service.create_request(
        memory_id,
        reason=body.reason,
        requester_label=body.requester_label,
        request_source=body.request_source.value,
    )
    return DataEnvelope(
        data=ForgetRequestCreateResponse(
            **response.model_dump(),
            confirmation_token=token,
            reused_existing=reused,
        )
    )


@request_router.get("", response_model=ForgetRequestListResponse)
def list_forget_requests(
    status: Optional[ForgetRequestStatus] = None,
    memory_id: Optional[str] = Query(default=None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ForgetRequestListResponse:
    rows, total = container.forget_service.list_requests(
        status=status.value if status else None,
        memory_id=memory_id,
        page=page,
        page_size=page_size,
    )
    return ForgetRequestListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@request_router.get("/{request_id}", response_model=DataEnvelope[ForgetRequestResponse])
def get_forget_request(
    request_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[ForgetRequestResponse]:
    return DataEnvelope(data=container.forget_service.get_request(request_id))


@request_router.post(
    "/{request_id}/approve", response_model=DataEnvelope[ForgetRequestResponse]
)
def approve_forget_request(
    request_id: str,
    payload: ForgetApprovalRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[ForgetRequestResponse]:
    """The ONLY endpoint that sets a memory to DELETED.

    ``memory_forgotten`` in the response is true only when the execution actually
    committed. An approval that fails returns an error and leaves the memory
    ACTIVE, so a client can never display "forgotten" on the strength of a
    successful HTTP call alone.
    """
    if payload.confirmation_token is None:
        container.forget_service.require_agent_origin(request_id)
    response = container.forget_service.approve(
        request_id,
        confirmation_token=payload.confirmation_token,
        actor_label=payload.actor_label,
        owner_authority=payload.confirmation_token is None,
    )
    return DataEnvelope(data=response)


@request_router.post(
    "/{request_id}/reject", response_model=DataEnvelope[ForgetRequestResponse]
)
def reject_forget_request(
    request_id: str,
    payload: ForgetRejectionRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[ForgetRequestResponse]:
    """Refuse a forget request. The memory is never touched."""
    if payload.confirmation_token is None:
        container.forget_service.require_agent_origin(request_id)
    response = container.forget_service.reject(
        request_id,
        confirmation_token=payload.confirmation_token,
        reason=payload.reason,
        actor_label=payload.actor_label,
        owner_authority=payload.confirmation_token is None,
    )
    return DataEnvelope(data=response)


__all__ = ["memory_router", "request_router"]
