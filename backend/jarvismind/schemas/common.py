"""Shared response envelopes and error shapes."""
from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageMeta(BaseModel):
    page: int = Field(..., ge=1, description="Current 1-based page number")
    page_size: int = Field(..., ge=1, le=100)
    total: int = Field(..., ge=0)
    total_pages: int = Field(..., ge=0)


class DataEnvelope(BaseModel, Generic[T]):
    data: T


class ListEnvelope(BaseModel, Generic[T]):
    data: List[T]
    meta: PageMeta


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
