"""Schemas for the read-only chat memory-context retrieval endpoint.

JME-MEMORY-CONTEXT-01. The endpoint exists because ``/hybrid-search`` is a
general search surface: it does not exclude expired rows, does not apply the
sensitive-content policy, and does not return a relevance verdict. Chat context
injection needs all three, and must never be able to write.

Field names follow the existing JME search schema (``memory_type``) rather than
inventing a second vocabulary; ``relevance_score`` and ``source`` are additive.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .memory import MemoryType

#: Bumped when the ranking/filtering contract changes in a way a client could
#: observe. Clients may log it; they must not branch on it for safety.
RETRIEVAL_VERSION = "context-v1"

MAX_QUERY_LENGTH = 500
MAX_LIMIT = 20
DEFAULT_LIMIT = 8
MAX_CONTENT_CHARS_LIMIT = 2000
DEFAULT_MAX_CONTENT_CHARS = 600


class MemoryContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    include_categories: List[MemoryType] = Field(default_factory=list)
    exclude_categories: List[MemoryType] = Field(default_factory=list)
    max_content_chars: int = Field(
        default=DEFAULT_MAX_CONTENT_CHARS, ge=80, le=MAX_CONTENT_CHARS_LIMIT
    )


class MemoryContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    summary: Optional[str] = None
    memory_type: MemoryType
    importance: float
    relevance_score: float = Field(..., ge=0.0, le=1.0)
    updated_at: str = ""
    #: Where the row comes from. ``reflection`` rows are lifecycle-reviewed
    #: summaries; they are still formal memories, never raw model output.
    source: str = "user_memory"
    #: True when the stored memory is longer than ``max_content_chars``.
    truncated: bool = False


class MemoryContextExcluded(BaseModel):
    """Value-free diagnostics: counts only, never content or categories of PII."""

    model_config = ConfigDict(extra="forbid")

    expired: int = 0
    sensitive: int = 0
    irrelevant: int = 0
    duplicate: int = 0
    filtered: int = 0


class MemoryContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MemoryContextItem] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    retrieval_version: str = RETRIEVAL_VERSION
    degraded_to_fts: bool = False
    excluded: MemoryContextExcluded = Field(default_factory=MemoryContextExcluded)
