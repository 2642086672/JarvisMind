"""Request and response schemas for memory sources."""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from ..database.models import MemorySource
from .common import ListEnvelope
from .memory import SortOrder

MAX_SOURCE_NAME_LENGTH = 255
MAX_URI_LENGTH = 2048


class SourceType(str, Enum):
    CHAT = "CHAT"
    FILE = "FILE"
    IMPORT = "IMPORT"
    MANUAL = "MANUAL"
    API = "API"
    NOTE = "NOTE"
    OTHER = "OTHER"


SUPPORTED_SOURCE_TYPES: List[str] = [item.value for item in SourceType]


class SourceCreate(BaseModel):
    model_config = {"extra": "forbid"}

    source_type: SourceType
    source_name: str = Field(..., min_length=1, max_length=MAX_SOURCE_NAME_LENGTH)
    uri: Optional[str] = Field(default=None, max_length=MAX_URI_LENGTH)


class SourceUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    source_type: Optional[SourceType] = None
    source_name: Optional[str] = Field(
        default=None, min_length=1, max_length=MAX_SOURCE_NAME_LENGTH
    )
    uri: Optional[str] = Field(default=None, max_length=MAX_URI_LENGTH)


class SourceListQuery(BaseModel):
    model_config = {"extra": "ignore"}

    source_type: Optional[SourceType] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort_order: SortOrder = SortOrder.DESC


class SourceResponse(BaseModel):
    id: str
    source_type: SourceType
    source_name: str
    uri: Optional[str] = None
    created_at: str
    updated_at: str


class SourceListResponse(ListEnvelope[SourceResponse]):
    pass


def source_to_response(source: MemorySource) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        source_type=SourceType(source.source_type),
        source_name=source.source_name,
        uri=source.uri,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )
