"""Response schemas for the read-only Memory Overview surface."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MemoryOverviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(..., min_length=1)
    memory_type: str
    excerpt: str
    created_at: str = ""
    updated_at: str = ""


class MemoryOverviewConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: List[str] = Field(default_factory=list)
    memory_ids: List[str] = Field(default_factory=list)
    note: str = ""


class MemoryOverviewCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    summary: str = ""
    ai_summary: Optional[str] = None
    item_count: int = Field(default=0, ge=0)
    items: List[MemoryOverviewItem] = Field(default_factory=list)
    truncated: bool = False
    uncertain: bool = False
    conflicts: List[MemoryOverviewConflict] = Field(default_factory=list)
    available: bool = False


class MemoryOverviewResponse(BaseModel):
    """Structured overview. Every item is traceable to a real memory id."""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    source_version: str
    eligible_count: int = Field(default=0, ge=0)
    excluded_count: int = Field(default=0, ge=0)
    excluded_reasons: Dict[str, int] = Field(default_factory=dict)
    recent_days: int = Field(default=30, ge=1)
    recent_min_importance: float = Field(default=6.5, ge=0.0, le=10.0)
    categories: List[MemoryOverviewCategory] = Field(default_factory=list)
    ai_enhanced: bool = False
    ai_model: Optional[str] = None
    ai_state: str = "none"
    ai_generated_at: Optional[str] = None
