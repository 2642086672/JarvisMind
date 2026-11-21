"""Request/response schemas for JME-5 memory lifecycle operations."""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from ..core.timeutils import parse_iso
from ..database.models import (
    MemoryArchiveRecord,
    MemoryCorrection,
    MemoryLifecycleReview,
    MemoryMerge,
    MemoryRecord,
    MemorySummary,
)
from .common import ListEnvelope, PageMeta
from .memory import MemoryResponse, MemoryStatus, MemoryType, record_to_response


class SummaryType(str, Enum):
    COLLECTION = "COLLECTION"
    CATEGORY = "CATEGORY"
    PROJECT = "PROJECT"
    PROFILE = "PROFILE"
    REFLECTION = "REFLECTION"


class ReviewStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class CorrectionStatus(str, Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class MergeStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ReviewType(str, Enum):
    EXPIRY_REVIEW = "EXPIRY_REVIEW"
    REFLECTION = "REFLECTION"
    OTHER = "OTHER"


class ReviewAction(str, Enum):
    REVIEWED = "REVIEWED"
    EXTENDED = "EXTENDED"
    ARCHIVED = "ARCHIVED"


def _parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return parse_iso(value).timestamp()
    except ValueError:
        return None


def _loads(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


class SummaryCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    memory_ids: List[str] = Field(..., min_length=1, max_length=500)
    summary_type: SummaryType = SummaryType.COLLECTION
    memory_type: Optional[MemoryType] = None
    title: Optional[str] = Field(default=None, max_length=200)


class SummaryReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: Optional[str] = Field(default=None, max_length=1000)
    actor: Optional[str] = Field(default=None, max_length=128)


class SummaryResponse(BaseModel):
    id: str
    title: str
    content: str
    summary_type: SummaryType
    memory_type: Optional[str] = None
    memory_ids: List[str]
    source_ids: List[str] = Field(default_factory=list)
    status: ReviewStatus
    confidence: float
    engine: str
    created_at: str
    updated_at: str
    confirmed_at: Optional[str] = None
    confirmed_by: Optional[str] = None
    rejected_at: Optional[str] = None
    rejected_by: Optional[str] = None
    reject_reason: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SummaryListResponse(ListEnvelope[SummaryResponse]):
    pass


class CorrectionResponse(BaseModel):
    id: str
    memory_id_a: str
    memory_id_b: str
    detected_at: str
    status: CorrectionStatus
    chosen_memory_id: Optional[str] = None
    corrected_content: Optional[str] = None
    reason: Optional[str] = None
    source: Optional[str] = None
    created_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


class CorrectionResolveRequest(BaseModel):
    model_config = {"extra": "forbid"}

    chosen_memory_id: Optional[str] = Field(default=None, max_length=128)
    corrected_content: Optional[str] = Field(default=None, max_length=100_000)
    reason: Optional[str] = Field(default=None, max_length=1000)
    actor: Optional[str] = Field(default=None, max_length=128)


class CorrectionRejectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Optional[str] = Field(default=None, max_length=1000)
    actor: Optional[str] = Field(default=None, max_length=128)


class CorrectionListResponse(ListEnvelope[CorrectionResponse]):
    pass


class MergePreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    memory_ids: List[str] = Field(..., min_length=2, max_length=50)


class MergeConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    memory_ids: List[str] = Field(..., min_length=2, max_length=50)
    new_content: Optional[str] = Field(default=None, max_length=100_000)
    new_summary: Optional[str] = Field(default=None, max_length=5_000)
    reason: Optional[str] = Field(default=None, max_length=1000)
    actor: Optional[str] = Field(default=None, max_length=128)


class MergeResponse(BaseModel):
    id: str
    status: MergeStatus
    target_memory_id: Optional[str] = None
    merged_memory_ids: List[str]
    new_content: Optional[str] = None
    new_summary: Optional[str] = None
    reason: Optional[str] = None
    created_at: str
    updated_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class MergeListResponse(ListEnvelope[MergeResponse]):
    pass


class MergePreviewResponse(BaseModel):
    memory_ids: List[str]
    new_content: str
    new_summary: Optional[str] = None
    reason: Optional[str] = None
    engine: str
    memory_type: Optional[str] = None


class ExpiryItem(BaseModel):
    memory_id: str
    memory_type: str
    content_preview: str
    status: str
    expires_at: Optional[str] = None
    days_remaining: Optional[float] = None


class ExpiryListResponse(BaseModel):
    data: List[ExpiryItem]
    meta: PageMeta


class ExpiryReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    action: ReviewAction = ReviewAction.REVIEWED
    new_expires_at: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None, max_length=1000)
    actor: Optional[str] = Field(default=None, max_length=128)

    @field_validator("new_expires_at")
    @classmethod
    def _check(cls, value: Optional[str]) -> Optional[str]:
        if value:
            try:
                parse_iso(value)
            except ValueError as exc:
                raise ValueError("must be an ISO-8601 timestamp") from exc
        return value


class ArchiveRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Optional[str] = Field(default=None, max_length=1000)
    source: Optional[str] = Field(default=None, max_length=128)
    actor: Optional[str] = Field(default=None, max_length=128)


class ArchiveRecordResponse(BaseModel):
    id: str
    memory_id: str
    reason: Optional[str] = None
    source: Optional[str] = None
    previous_status: str
    archived_at: str
    restored_at: Optional[str] = None
    restored_by: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ArchiveListResponse(ListEnvelope[ArchiveRecordResponse]):
    pass


class ForgetPreviewResponse(BaseModel):
    memory: MemoryResponse
    impact: Dict[str, Any]


class ForgetRequest(BaseModel):
    """Legacy bulk-forget body (JW-01 hardened).

    ``confirm`` is retained ONLY so old clients get a clear refusal instead of a
    422 shape error; it no longer authorises anything. The real authorization is
    ``forget_request_id`` + ``confirmation_token``, issued by the forget-request
    endpoints. ``memory_type``/``query`` selectors are still accepted for
    compatibility but are not used to widen a deletion: the approved request
    names exactly one memory.
    """

    model_config = {"extra": "forbid"}

    memory_ids: Optional[List[str]] = Field(default=None, max_length=500)
    memory_type: Optional[MemoryType] = None
    query: Optional[str] = Field(default=None, max_length=500)
    reason: Optional[str] = Field(default=None, max_length=1000)
    #: Deprecated and NOT an authorization. Kept for a clear, explicit refusal.
    confirm: bool = False
    forget_request_id: Optional[str] = Field(default=None, max_length=128)
    confirmation_token: Optional[str] = Field(default=None, max_length=256)
    actor: Optional[str] = Field(default=None, max_length=128)


class ForgetResponse(BaseModel):
    forgotten_ids: List[str]
    skipped_ids: List[str]
    already_forgotten_ids: List[str]
    vector_removed_ids: List[str]
    reason: Optional[str] = None


class LifecycleReviewResponse(BaseModel):
    id: str
    memory_id: str
    review_type: str
    action: str
    previous_expires_at: Optional[str] = None
    new_expires_at: Optional[str] = None
    note: Optional[str] = None
    actor: Optional[str] = None
    created_at: str


# ---------------------------------------------------------------- converters
def summary_to_response(record: MemorySummary) -> SummaryResponse:
    return SummaryResponse(
        id=record.id,
        title=record.title,
        content=record.content,
        summary_type=SummaryType(record.summary_type),
        memory_type=record.memory_type,
        memory_ids=_loads(record.memory_ids_json) or [],
        source_ids=_loads(record.source_ids_json) or [],
        status=ReviewStatus(record.status),
        confidence=record.confidence,
        engine=record.engine,
        created_at=record.created_at,
        updated_at=record.updated_at,
        confirmed_at=record.confirmed_at,
        confirmed_by=record.confirmed_by,
        rejected_at=record.rejected_at,
        rejected_by=record.rejected_by,
        reject_reason=record.reject_reason,
        metadata=_loads(record.metadata_json),
    )


def correction_to_response(record: MemoryCorrection) -> CorrectionResponse:
    return CorrectionResponse(
        id=record.id,
        memory_id_a=record.memory_id_a,
        memory_id_b=record.memory_id_b,
        detected_at=record.detected_at,
        status=CorrectionStatus(record.status),
        chosen_memory_id=record.chosen_memory_id,
        corrected_content=record.corrected_content,
        reason=record.reason,
        source=record.source,
        created_at=record.created_at,
        resolved_at=record.resolved_at,
        resolved_by=record.resolved_by,
        before_state=_loads(record.before_state_json),
        after_state=_loads(record.after_state_json),
        metadata=_loads(record.metadata_json),
    )


def merge_to_response(record: MemoryMerge) -> MergeResponse:
    return MergeResponse(
        id=record.id,
        status=MergeStatus(record.status),
        target_memory_id=record.target_memory_id,
        merged_memory_ids=_loads(record.merged_memory_ids_json) or [],
        new_content=record.new_content,
        new_summary=record.new_summary,
        reason=record.reason,
        created_at=record.created_at,
        updated_at=record.updated_at,
        resolved_at=record.resolved_at,
        resolved_by=record.resolved_by,
        metadata=_loads(record.metadata_json),
    )


def archive_record_to_response(record: MemoryArchiveRecord) -> ArchiveRecordResponse:
    return ArchiveRecordResponse(
        id=record.id,
        memory_id=record.memory_id,
        reason=record.reason,
        source=record.source,
        previous_status=record.previous_status,
        archived_at=record.archived_at,
        restored_at=record.restored_at,
        restored_by=record.restored_by,
        metadata=_loads(record.metadata_json),
    )


def review_to_response(record: MemoryLifecycleReview) -> LifecycleReviewResponse:
    return LifecycleReviewResponse(
        id=record.id,
        memory_id=record.memory_id,
        review_type=record.review_type,
        action=record.action,
        previous_expires_at=record.previous_expires_at,
        new_expires_at=record.new_expires_at,
        note=record.note,
        actor=record.actor,
        created_at=record.created_at,
    )


def build_page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages)
