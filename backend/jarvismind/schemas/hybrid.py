"""Schemas for JME-4 Hybrid Search, Embedding and Vector Index management."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .common import ListEnvelope, PageMeta
from .memory import MemoryStatus, MemoryType


class HybridSearchResultItem(BaseModel):
    memory_id: str
    content: str
    summary: Optional[str] = None
    memory_type: MemoryType
    importance: float
    confidence: float
    created_at: str
    fts_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    fusion_rank: int
    rrf_score: float
    score: float
    sources: List[str]


class HybridSearchMeta(PageMeta):
    fts_count: int = 0
    vector_count: int = 0
    degraded_to_fts: bool = False
    embedding_model: str = ""


class HybridSearchResponse(BaseModel):
    data: List[HybridSearchResultItem]
    meta: HybridSearchMeta


class EmbeddingStatusResponse(BaseModel):
    provider: str
    model_name: str
    version: str
    dimension: int
    is_available: bool
    device: str
    error: Optional[str] = None


class VectorIndexStatusResponse(BaseModel):
    collection_name: str
    storage_path: str
    total_points: int
    dimension: int
    distance: str
    is_ready: bool
    status: str
    sqlite_status_counts: Dict[str, int]
    embedding_model: str
    last_rebuilt_at: Optional[str] = None
    error: Optional[str] = None


class IndexRebuildRequest(BaseModel):
    force: bool = False


class IndexRebuildResponse(BaseModel):
    total_records: int
    indexed: int
    failed: int
    duration_ms: float
    embedding_model: str
    status: str


class ConsistencyReportResponse(BaseModel):
    is_consistent: bool
    sqlite_active_count: int
    qdrant_points_count: int
    missing_in_qdrant: List[str]
    orphans_in_qdrant: List[str]
    checked_at: str
