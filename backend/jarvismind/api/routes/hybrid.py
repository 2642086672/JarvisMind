"""API endpoints for Hybrid Search and Vector Index operations (JME-4)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...schemas.common import DataEnvelope
from ...schemas.hybrid import (
    ConsistencyReportResponse,
    HybridSearchResponse,
    IndexRebuildRequest,
    IndexRebuildResponse,
    VectorIndexStatusResponse,
)
from ...schemas.memory import MemoryStatus, MemoryType
from ..deps import get_container

router = APIRouter(tags=["hybrid-search"])


@router.get("/hybrid-search", response_model=HybridSearchResponse)
def hybrid_search(
    q: str = Query(..., min_length=1, max_length=500),
    memory_type: Optional[MemoryType] = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> HybridSearchResponse:
    results, meta = container.hybrid_search_service.search(
        query=q,
        memory_type=memory_type.value if memory_type else None,
        status=status.value if status else None,
        page=page,
        page_size=page_size,
    )
    return HybridSearchResponse(data=results, meta=meta)


@router.get("/index/status", response_model=DataEnvelope[VectorIndexStatusResponse])
def get_index_status(
    container: Container = Depends(get_container),
) -> DataEnvelope[VectorIndexStatusResponse]:
    status = container.hybrid_search_service.get_index_status()
    return DataEnvelope(data=status)


@router.post("/index/rebuild", response_model=DataEnvelope[IndexRebuildResponse])
def rebuild_index(
    payload: Optional[IndexRebuildRequest] = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[IndexRebuildResponse]:
    force = payload.force if payload else False
    result = container.hybrid_search_service.rebuild_index(force=force)
    return DataEnvelope(data=result)


@router.get("/index/consistency", response_model=DataEnvelope[ConsistencyReportResponse])
def check_consistency(
    container: Container = Depends(get_container),
) -> DataEnvelope[ConsistencyReportResponse]:
    report = container.hybrid_search_service.check_consistency()
    return DataEnvelope(data=report)


@router.post("/index/reconcile", response_model=DataEnvelope[dict])
def reconcile_index(
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    """Repair vector_status for non-active records with stale index state."""
    result = container.hybrid_search_service.reconcile_vector_state()
    return DataEnvelope(data=result)
