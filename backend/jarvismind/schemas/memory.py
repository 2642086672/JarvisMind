"""Request and response schemas for the memory API."""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from ..core.security_gate import validate_client_value
from ..core.timeutils import parse_iso
from ..database.models import MemoryRecord
from .common import ListEnvelope, PageMeta

MAX_CONTENT_LENGTH = 100_000
MAX_SUMMARY_LENGTH = 5_000


class MemoryType(str, Enum):
    PROFILE = "PROFILE"
    SEMANTIC = "SEMANTIC"
    EPISODIC = "EPISODIC"
    PROJECT = "PROJECT"
    WORKING = "WORKING"
    REFLECTION = "REFLECTION"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"
    # Assigned only by the merge operation; merged originals are retained.
    MERGED = "MERGED"


SUPPORTED_MEMORY_TYPES: List[str] = [item.value for item in MemoryType]
SUPPORTED_STATUSES: List[str] = [item.value for item in MemoryStatus]


class SortBy(str, Enum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    IMPORTANCE = "importance"
    MEMORY_TYPE = "memory_type"
    STATUS = "status"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


def _validate_optional_iso(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        parse_iso(value)
    except ValueError as exc:
        raise ValueError("must be an ISO-8601 timestamp") from exc
    return value


class MemoryCreate(BaseModel):
    model_config = {"extra": "forbid"}

    memory_type: MemoryType
    content: str = Field(..., min_length=1, max_length=MAX_CONTENT_LENGTH)
    summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    importance: float = Field(default=5.0, ge=0.0, le=10.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_id: Optional[str] = Field(default=None, max_length=128)
    expires_at: Optional[str] = Field(default=None)
    metadata: Optional[Dict[str, Any]] = Field(default=None)

    @field_validator("expires_at")
    @classmethod
    def _check_expires_at(cls, value: Optional[str]) -> Optional[str]:
        return _validate_optional_iso(value)

    @field_validator("metadata", "source_id", "expires_at")
    @classmethod
    def _reject_credential_in_client_fields(cls, value, info: ValidationInfo):
        # 003-B1 P0-1: structured metadata is walked recursively before the
        # request reaches any service. content/summary stay with the unified
        # service + ORM gate so the credential_blocked error code is preserved.
        validate_client_value(value, field=info.field_name)
        return value


class MemoryUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    content: Optional[str] = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    summary: Optional[str] = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    importance: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    status: Optional[MemoryStatus] = None
    source_id: Optional[str] = Field(default=None, max_length=128)
    expires_at: Optional[str] = Field(default=None)
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("expires_at")
    @classmethod
    def _check_expires_at(cls, value: Optional[str]) -> Optional[str]:
        return _validate_optional_iso(value)

    @field_validator("metadata", "source_id", "expires_at")
    @classmethod
    def _reject_credential_in_client_fields(cls, value, info: ValidationInfo):
        validate_client_value(value, field=info.field_name)
        return value


class MemoryListQuery(BaseModel):
    model_config = {"extra": "ignore"}

    memory_type: Optional[MemoryType] = None
    status: Optional[MemoryStatus] = MemoryStatus.ACTIVE
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort_by: SortBy = SortBy.CREATED_AT
    sort_order: SortOrder = SortOrder.DESC


class MemoryResponse(BaseModel):
    id: str
    memory_type: MemoryType
    content: str
    summary: Optional[str] = None
    importance: float
    confidence: float
    status: MemoryStatus
    source_id: Optional[str] = None
    created_at: str
    updated_at: str
    accessed_at: Optional[str] = None
    expires_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    # JME-5 lifecycle bookkeeping
    forgotten_at: Optional[str] = None
    forget_reason: Optional[str] = None
    merged_into: Optional[str] = None


class MemoryListResponse(ListEnvelope[MemoryResponse]):
    pass


class MemorySearchQuery(BaseModel):
    model_config = {"extra": "ignore"}

    q: str = Field(..., min_length=1, max_length=500)
    memory_type: Optional[MemoryType] = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class SearchResult(BaseModel):
    memory_id: str
    content: str
    summary: Optional[str] = None
    memory_type: MemoryType
    importance: float
    confidence: float
    created_at: str
    rank: Optional[float] = None


class SearchResponse(ListEnvelope[SearchResult]):
    pass


class AuditLogQuery(BaseModel):
    model_config = {"extra": "ignore"}

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort_order: SortOrder = SortOrder.DESC


class AuditLogEntry(BaseModel):
    action: str
    actor: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    created_at: str


class AuditLogResponse(ListEnvelope[AuditLogEntry]):
    pass


def record_to_response(record: MemoryRecord) -> MemoryResponse:
    """Convert an ORM record into the public response shape."""
    metadata: Optional[Dict[str, Any]] = None
    if record.metadata_json:
        try:
            parsed = json.loads(record.metadata_json)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    return MemoryResponse(
        id=record.id,
        memory_type=MemoryType(record.memory_type),
        content=record.content,
        summary=record.summary,
        importance=record.importance,
        confidence=record.confidence,
        status=MemoryStatus(record.status),
        source_id=record.source_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        accessed_at=record.accessed_at,
        expires_at=record.expires_at,
        metadata=metadata,
        forgotten_at=record.forgotten_at,
        forget_reason=record.forget_reason,
        merged_into=record.merged_into,
    )


def build_page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return PageMeta(page=page, page_size=page_size, total=total, total_pages=total_pages)