"""JME-5 memory lifecycle endpoints.

This router is registered before the CRUD memory router so that paths such as
``/memories/summaries`` and ``/memories/{memory_id}/archive`` are not captured
by ``/memories/{memory_id}``.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Query

from ...container import Container
from ...schemas.common import DataEnvelope, ListEnvelope
from ...schemas.lifecycle import (
    ArchiveListResponse,
    ArchiveRecordResponse,
    ArchiveRequest,
    CorrectionListResponse,
    CorrectionResolveRequest,
    CorrectionResponse,
    CorrectionRejectRequest,
    CorrectionStatus,
    ExpiryListResponse,
    ExpiryReviewRequest,
    ForgetPreviewResponse,
    ForgetRequest,
    ForgetResponse,
    MergeConfirmRequest,
    MergeListResponse,
    MergePreviewRequest,
    MergePreviewResponse,
    MergeResponse,
    ReviewStatus,
    SummaryCreateRequest,
    SummaryListResponse,
    SummaryResponse,
    SummaryReviewRequest,
    SummaryType,
    build_page_meta,
)
from ...schemas.memory import MemoryType
from ...core.errors import LifecycleWriteDisabledError
from ...core.lifecycle_policy import require_human_actor
from ...services.forget_service import ForgetApprovalRequiredError
from ..deps import get_container

router = APIRouter(tags=["memory-lifecycle"])


def _require_lifecycle_switch(
    container: Container, *, attribute: str, endpoint: str
) -> None:
    """JW-02 gate for a lifecycle write entry point.

    Reads only the resolved setting - never a request field - so no query
    parameter, header or body value can open it. Absent/malformed env values fall
    back to each switch's declared default through ``_env_bool``.
    """
    if bool(getattr(container.settings, attribute, False)):
        return
    raise LifecycleWriteDisabledError(
        f"{endpoint} is disabled",
        detail={
            "endpoint": endpoint,
            "setting": attribute,
            "hint": f"set {_SWITCH_ENV[attribute]} to enable this endpoint",
        },
    )


def _guard_jmr_row(container: Container, row_id: str) -> None:
    """JMR-03: generic lifecycle endpoints must not review JMR rows."""
    container.jmr_review_bridge.assert_not_jmr(row_id)


def _resolve_modification_credentials(
    container: Container,
    *,
    memory_id: str,
    request_id: Optional[str],
    token: Optional[str],
    expected_kinds: tuple,
    endpoint: str,
) -> Optional[str]:
    """JW-03: verify (and burn) a modification request for a lifecycle write.

    Returns the request id when credentials were supplied, ``None`` when the
    caller supplied none - the service then decides whether the target actually
    needs them (unprotected rows do not).

    Both headers must arrive together. Supplying one is a client bug and is
    reported rather than silently downgraded to "no credentials", which would
    turn a typo into an unauthenticated attempt.
    """
    from ...services.modification_service import (
        ModificationNotAllowedError,
        ModificationTokenError,
    )

    if not request_id and not token:
        return None
    if not (request_id and token):
        raise ModificationTokenError(
            "Both X-Modification-Request-Id and X-Modification-Token are required",
            detail={"code": "modification_credentials_incomplete"},
        )
    verified_kind = container.modification_service.verify_for_lifecycle(
        request_id=request_id,
        memory_id=memory_id,
        confirmation_token=token,
        expected_kinds=expected_kinds,
    )
    if verified_kind is None:  # pragma: no cover - defensive
        raise ModificationNotAllowedError(
            f"{endpoint} could not be authorised",
            detail={"code": "modification_not_authorised"},
        )
    return request_id


#: Setting attribute -> environment variable, for the operator-facing hint.
_SWITCH_ENV = {
    "lifecycle_import_enabled": "JARVISMIND_LIFECYCLE_IMPORT_ENABLED",
    "lifecycle_merge_enabled": "JARVISMIND_LIFECYCLE_MERGE_ENABLED",
    "lifecycle_summary_review_enabled": "JARVISMIND_LIFECYCLE_SUMMARY_REVIEW_ENABLED",
    "lifecycle_correction_resolve_enabled": "JARVISMIND_LIFECYCLE_CORRECTION_RESOLVE_ENABLED",
}


# ------------------------------------------------------------------ summaries
@router.post("/summaries", response_model=DataEnvelope[SummaryResponse], status_code=201)
def create_summary(
    payload: SummaryCreateRequest, container: Container = Depends(get_container)
) -> DataEnvelope[SummaryResponse]:
    summary = container.lifecycle_service.create_summary(
        payload.memory_ids,
        summary_type=payload.summary_type,
        memory_type=payload.memory_type.value if payload.memory_type else None,
        actor="user",
    )
    return DataEnvelope(data=summary)


@router.get("/summaries", response_model=SummaryListResponse)
def list_summaries(
    status: Optional[ReviewStatus] = None,
    summary_type: Optional[SummaryType] = None,
    memory_type: Optional[MemoryType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> SummaryListResponse:
    rows, total = container.lifecycle_service.list_summaries(
        status=status.value if status else None,
        summary_type=summary_type.value if summary_type else None,
        memory_type=memory_type.value if memory_type else None,
        page=page,
        page_size=page_size,
    )
    return SummaryListResponse(data=rows, meta=build_page_meta(page, page_size, total))


@router.get("/summaries/{summary_id}", response_model=DataEnvelope[SummaryResponse])
def get_summary(
    summary_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[SummaryResponse]:
    return DataEnvelope(data=container.lifecycle_service.get_summary(summary_id))


@router.post("/summaries/{summary_id}/review", response_model=DataEnvelope[SummaryResponse])
def review_summary(
    summary_id: str,
    payload: SummaryReviewRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[SummaryResponse]:
    """JW-02: approving a summary CREATES an authoritative REFLECTION memory.

    So this is a lifecycle write, not just a status flip: it is switch-gated and
    refuses obviously non-human actors. The confirmation itself is an explicit
    user decision, which is why the default is ON rather than OFF - it is the
    intended confirm step of the reflection flow, and it can only act on a
    summary JME itself produced and that is still PENDING.
    """
    _require_lifecycle_switch(
        container,
        attribute="lifecycle_summary_review_enabled",
        endpoint="POST /api/v1/memories/summaries/{id}/review",
    )
    _guard_jmr_row(container, summary_id)
    require_human_actor(payload.actor, operation="summary review")
    summary = container.lifecycle_service.review_summary(
        summary_id,
        decision=payload.decision,
        reason=payload.reason,
        actor=payload.actor,
    )
    return DataEnvelope(data=summary)


# --------------------------------------------------------------- corrections
@router.post("/corrections/detect", response_model=ListEnvelope[CorrectionResponse])
def detect_corrections(
    payload: dict | None = None,
    container: Container = Depends(get_container),
) -> ListEnvelope[CorrectionResponse]:
    body = payload or {}
    memory_ids = body.get("memory_ids")
    rows = container.lifecycle_service.detect_corrections(
        memory_type=body.get("memory_type"),
        memory_ids=memory_ids if isinstance(memory_ids, list) else None,
        actor=body.get("actor") or "system",
    )
    return ListEnvelope(data=rows, meta=build_page_meta(1, len(rows) or 1, len(rows)))


@router.get("/corrections", response_model=CorrectionListResponse)
def list_corrections(
    status: Optional[CorrectionStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> CorrectionListResponse:
    rows, total = container.lifecycle_service.list_corrections(
        status=status.value if status else None,
        page=page,
        page_size=page_size,
    )
    return CorrectionListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@router.get("/corrections/{correction_id}", response_model=DataEnvelope[CorrectionResponse])
def get_correction(
    correction_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[CorrectionResponse]:
    return DataEnvelope(data=container.lifecycle_service.get_correction(correction_id))


@router.post("/corrections/{correction_id}/resolve", response_model=DataEnvelope[CorrectionResponse])
def resolve_correction(
    correction_id: str,
    payload: CorrectionResolveRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[CorrectionResponse]:
    """JW-02: resolving a conflict can rewrite an authoritative memory.

    Gate + actor policy mirror the summary review: the decision is a user action
    on a conflict JME itself detected, so the switch defaults ON, but the write is
    still authorised, audited and restricted to a PENDING correction.
    """
    _require_lifecycle_switch(
        container,
        attribute="lifecycle_correction_resolve_enabled",
        endpoint="POST /api/v1/memories/corrections/{id}/resolve",
    )
    _guard_jmr_row(container, correction_id)
    require_human_actor(payload.actor, operation="correction resolve")
    correction = container.lifecycle_service.resolve_correction(
        correction_id,
        chosen_memory_id=payload.chosen_memory_id,
        corrected_content=payload.corrected_content,
        reason=payload.reason,
        actor=payload.actor,
    )
    return DataEnvelope(data=correction)


@router.post("/corrections/{correction_id}/reject", response_model=DataEnvelope[CorrectionResponse])
def reject_correction(
    correction_id: str,
    payload: CorrectionRejectRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[CorrectionResponse]:
    _guard_jmr_row(container, correction_id)
    correction = container.lifecycle_service.reject_correction(
        correction_id, reason=payload.reason, actor=payload.actor
    )
    return DataEnvelope(data=correction)


# -------------------------------------------------------------------- merges
@router.post("/merges/preview", response_model=DataEnvelope[MergePreviewResponse])
def preview_merge(
    payload: MergePreviewRequest, container: Container = Depends(get_container)
) -> DataEnvelope[MergePreviewResponse]:
    preview = container.lifecycle_service.preview_merge(payload.memory_ids, actor="user")
    return DataEnvelope(data=preview)


@router.post("/merges/confirm", response_model=DataEnvelope[MergeResponse])
def confirm_merge(
    payload: MergeConfirmRequest,
    merge_id: Optional[str] = Query(default=None, max_length=128),
    container: Container = Depends(get_container),
) -> DataEnvelope[MergeResponse]:
    """JW-02: confirming a merge creates a product and retires originals.

    Switch-gated and actor-checked. The switch defaults ON because the operation
    is inherently bounded: it needs at least two existing memories, refuses
    already-merged/deleted sources, and - since JW-02 - can only ever produce one
    product per merge request.
    """
    _require_lifecycle_switch(
        container,
        attribute="lifecycle_merge_enabled",
        endpoint="POST /api/v1/memories/merges/confirm",
    )
    if merge_id:
        _guard_jmr_row(container, merge_id)
    require_human_actor(payload.actor, operation="merge confirm")
    merge = container.lifecycle_service.confirm_merge(
        payload.memory_ids,
        new_content=payload.new_content,
        new_summary=payload.new_summary,
        reason=payload.reason,
        merge_id=merge_id,
        actor=payload.actor,
    )
    return DataEnvelope(data=merge)


@router.post("/merges/{merge_id}/reject", response_model=DataEnvelope[MergeResponse])
def reject_merge(
    merge_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[MergeResponse]:
    _guard_jmr_row(container, merge_id)
    return DataEnvelope(data=container.lifecycle_service.reject_merge(merge_id))


@router.get("/merges", response_model=MergeListResponse)
def list_merges(
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> MergeListResponse:
    rows, total = container.lifecycle_service.list_merges(
        status=status, page=page, page_size=page_size
    )
    return MergeListResponse(data=rows, meta=build_page_meta(page, page_size, total))


@router.get("/merges/{merge_id}", response_model=DataEnvelope[MergeResponse])
def get_merge(
    merge_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[MergeResponse]:
    return DataEnvelope(data=container.lifecycle_service.get_merge(merge_id))


# -------------------------------------------------------------------- expiry
@router.get("/expiring", response_model=ExpiryListResponse)
def list_expiring(
    days: int = Query(7, ge=0, le=3650),
    memory_type: Optional[MemoryType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ExpiryListResponse:
    rows, total = container.lifecycle_service.list_expiring(
        days=days,
        memory_type=memory_type.value if memory_type else None,
        page=page,
        page_size=page_size,
    )
    return ExpiryListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@router.get("/expired", response_model=ExpiryListResponse)
def list_expired(
    memory_type: Optional[MemoryType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ExpiryListResponse:
    rows, total = container.lifecycle_service.list_expired(
        memory_type=memory_type.value if memory_type else None,
        page=page,
        page_size=page_size,
    )
    return ExpiryListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@router.post("/{memory_id}/expiry-review")
def review_expiry(
    memory_id: str,
    payload: ExpiryReviewRequest,
    x_modification_request_id: Optional[str] = Header(default=None),
    x_modification_token: Optional[str] = Header(default=None),
    container: Container = Depends(get_container),
):
    """JW-03: an approved memory needs a verified request for EXTENDED/ARCHIVED.

    ``REVIEWED`` writes no protected field, so it stays unrestricted.
    """
    approved_request_id = _resolve_modification_credentials(
        container,
        memory_id=memory_id,
        request_id=x_modification_request_id,
        token=x_modification_token,
        expected_kinds=("EXTEND", "ARCHIVE"),
        endpoint="POST /api/v1/memories/{id}/expiry-review",
    )
    review = container.lifecycle_service.review_expiry(
        memory_id,
        action=payload.action,
        new_expires_at=payload.new_expires_at,
        note=payload.note,
        actor=payload.actor,
        approved_request_id=approved_request_id,
    )
    return review


@router.get("/{memory_id}/reviews")
def list_reviews(
    memory_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
):
    rows, total = container.lifecycle_service.list_reviews(
        memory_id=memory_id, page=page, page_size=page_size
    )
    return {"data": rows, "meta": build_page_meta(page, page_size, total)}


# ------------------------------------------------------------------ archives
@router.get("/archive-records", response_model=ArchiveListResponse)
def list_archive_records(
    memory_id: Optional[str] = Query(default=None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ArchiveListResponse:
    rows, total = container.lifecycle_service.list_archive_records(
        memory_id=memory_id, page=page, page_size=page_size
    )
    return ArchiveListResponse(
        data=rows, meta=build_page_meta(page, page_size, total)
    )


@router.post("/{memory_id}/archive", response_model=DataEnvelope[bool])
def archive_memory(
    memory_id: str,
    payload: ArchiveRequest,
    x_modification_request_id: Optional[str] = Header(default=None),
    x_modification_token: Optional[str] = Header(default=None),
    container: Container = Depends(get_container),
) -> DataEnvelope[bool]:
    """JW-03: archiving an approved memory requires a verified ARCHIVE request."""
    approved_request_id = _resolve_modification_credentials(
        container,
        memory_id=memory_id,
        request_id=x_modification_request_id,
        token=x_modification_token,
        expected_kinds=("ARCHIVE",),
        endpoint="POST /api/v1/memories/{id}/archive",
    )
    ok = container.lifecycle_service.archive(
        memory_id,
        reason=payload.reason,
        source=payload.source,
        actor=payload.actor,
    )
    return DataEnvelope(data=ok)


@router.post("/{memory_id}/restore", response_model=DataEnvelope[bool])
def restore_memory(
    memory_id: str,
    actor: Optional[str] = Query(default=None, max_length=128),
    x_modification_request_id: Optional[str] = Header(default=None),
    x_modification_token: Optional[str] = Header(default=None),
    container: Container = Depends(get_container),
) -> DataEnvelope[bool]:
    """JW-03: restoring an approved memory requires a verified RESTORE request."""
    approved_request_id = _resolve_modification_credentials(
        container,
        memory_id=memory_id,
        request_id=x_modification_request_id,
        token=x_modification_token,
        expected_kinds=("RESTORE",),
        endpoint="POST /api/v1/memories/{id}/restore",
    )
    ok = container.lifecycle_service.restore(
        memory_id, actor=actor, approved_request_id=approved_request_id
    )
    return DataEnvelope(data=ok)


# ------------------------------------------------------------------- forget
@router.get("/forget-preview/{memory_id}", response_model=DataEnvelope[ForgetPreviewResponse])
def forget_preview(
    memory_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[ForgetPreviewResponse]:
    preview = container.lifecycle_service.forget_preview(memory_id)
    return DataEnvelope(
        data=ForgetPreviewResponse(
            memory=preview["memory"], impact=preview["impact"]
        )
    )


@router.get("/forget-candidates")
def forget_candidates(
    memory_type: Optional[MemoryType] = None,
    query: Optional[str] = Query(default=None, max_length=500),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
):
    rows, total = container.lifecycle_service.list_forget_candidates(
        memory_type=memory_type.value if memory_type else None,
        query=query,
        page=page,
        page_size=page_size,
    )
    return {"data": rows, "meta": build_page_meta(page, page_size, total)}


@router.post("/forget", response_model=DataEnvelope[ForgetResponse])
def forget_memories(
    payload: ForgetRequest, container: Container = Depends(get_container)
) -> DataEnvelope[ForgetResponse]:
    """JW-01: the legacy bulk forget is now an APPROVAL-GATED transport only.

    Previously ``confirm=true`` was the entire authorization: one client-supplied
    boolean caused immediate deletion of up to 500 memories, and it also accepted
    ``memory_type``/``query`` selectors, so a caller did not even need to know
    which memories existed. That is exactly the "client asserts approval" pattern
    JW-01 forbids.

    It now requires ``forget_request_id`` + ``confirmation_token`` (the one-time
    token issued by ``POST /memories/{id}/forget-requests``) for EVERY memory in
    the selection, and it is capped at one memory per call. Multi-memory bulk
    forgetting without per-memory approval is refused rather than quietly
    supported, because each memory needs its own approved request.
    """
    memory_ids = list(payload.memory_ids or [])
    forget_request_id = payload.forget_request_id
    token = payload.confirmation_token

    if not forget_request_id or not token:
        raise ForgetApprovalRequiredError(
            "Bulk/direct forgetting requires an approved forget request "
            "(forget_request_id + confirmation_token). Create one per memory with "
            "POST /api/v1/memories/{id}/forget-requests, then approve it.",
            detail={
                "code": "forget_approval_required",
                "hint": "one approved forget request per memory",
            },
        )
    if len(memory_ids) != 1:
        raise ForgetApprovalRequiredError(
            "Exactly one memory per approved forget request is allowed",
            detail={
                "code": "forget_bulk_not_permitted",
                "received": len(memory_ids),
            },
        )

    memory_id = memory_ids[0]
    container.forget_service.approve_via_legacy_endpoint(
        memory_id=memory_id,
        forget_request_id=forget_request_id,
        confirmation_token=token,
        actor_label=payload.actor,
    )
    return DataEnvelope(
        data=ForgetResponse(
            forgotten_ids=[memory_id],
            skipped_ids=[],
            already_forgotten_ids=[],
            vector_removed_ids=[],
            reason=payload.reason,
        )
    )


# ------------------------------------------------------------- import / export
@router.get("/export")
def export_memories(container: Container = Depends(get_container)):
    return container.migration_service.export_document()


@router.post("/import/preview", response_model=DataEnvelope[dict])
def preview_import(
    payload: dict, container: Container = Depends(get_container)
) -> DataEnvelope[dict]:
    document = payload.get("document", payload)
    return DataEnvelope(
        data=container.migration_service.preview_import(document)
    )


@router.post("/import", response_model=DataEnvelope[dict])
def import_memories(
    payload: dict, container: Container = Depends(get_container)
) -> DataEnvelope[dict]:
    """JW-02: administrative bulk ingestion, OFF by default.

    Why this one gets a switch while the other three lifecycle entries do not:
    it is the only entry that can create authoritative memories the system never
    saw, with caller-chosen ids, in unbounded batches. It is a migration /
    restore tool, not part of any user flow, so it must be an explicit operator
    decision rather than always-on.

    ``actor`` in the body is a CLAIM. The response reports the server label and
    the claim separately, plus ``NOT_VERIFIED`` for identity.
    """
    _require_lifecycle_switch(
        container,
        attribute="lifecycle_import_enabled",
        endpoint="POST /api/v1/memories/import",
    )
    document = payload.get("document", payload)
    actor = payload.get("actor") or "user"
    # Import is administrative, so a model/daemon label is refused rather than
    # recorded: no automated caller may inject authoritative memories.
    require_human_actor(actor, operation="memory import")
    skip_duplicates = bool(payload.get("skip_duplicates", True))
    result = container.migration_service.import_document(
        document, actor=actor, skip_duplicates=skip_duplicates
    )
    return DataEnvelope(data=result)