"""Memory source CRUD endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...schemas.common import DataEnvelope, ListEnvelope
from ...schemas.memory import SortOrder, build_page_meta
from ...schemas.source import (
    SourceCreate,
    SourceListQuery,
    SourceListResponse,
    SourceResponse,
    SourceType,
    SourceUpdate,
    source_to_response,
)
from ..deps import get_container

router = APIRouter(tags=["memory-sources"])


@router.get("", response_model=SourceListResponse)
def list_sources(
    source_type: Optional[SourceType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_order: SortOrder = SortOrder.DESC,
    container: Container = Depends(get_container),
) -> SourceListResponse:
    query = SourceListQuery(
        source_type=source_type,
        page=page,
        page_size=page_size,
        sort_order=sort_order,
    )
    sources, total = container.source_service.list(
        source_type=query.source_type.value if query.source_type else None,
        page=query.page,
        page_size=query.page_size,
        sort_order=query.sort_order.value,
    )
    return SourceListResponse(
        data=[source_to_response(source) for source in sources],
        meta=build_page_meta(query.page, query.page_size, total),
    )


@router.post("", response_model=DataEnvelope[SourceResponse], status_code=201)
def create_source(
    payload: SourceCreate, container: Container = Depends(get_container)
) -> DataEnvelope[SourceResponse]:
    source = container.source_service.create(payload)
    return DataEnvelope(data=source_to_response(source))


@router.get("/{source_id}", response_model=DataEnvelope[SourceResponse])
def get_source(
    source_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[SourceResponse]:
    source = container.source_service.get(source_id)
    return DataEnvelope(data=source_to_response(source))


@router.patch("/{source_id}", response_model=DataEnvelope[SourceResponse])
def update_source(
    source_id: str,
    payload: SourceUpdate,
    container: Container = Depends(get_container),
) -> DataEnvelope[SourceResponse]:
    source = container.source_service.update(source_id, payload)
    return DataEnvelope(data=source_to_response(source))


@router.delete("/{source_id}", response_model=DataEnvelope[dict])
def delete_source(
    source_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[dict]:
    result = container.source_service.delete(source_id)
    return DataEnvelope(data=result)
