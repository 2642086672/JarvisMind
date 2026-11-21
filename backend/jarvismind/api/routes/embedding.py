"""API endpoints for Embedding model status and info (JME-4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...container import Container
from ...schemas.common import DataEnvelope
from ...schemas.hybrid import EmbeddingStatusResponse
from ..deps import get_container

router = APIRouter(tags=["embedding"])


@router.get("/status", response_model=DataEnvelope[EmbeddingStatusResponse])
def get_embedding_status(
    container: Container = Depends(get_container),
) -> DataEnvelope[EmbeddingStatusResponse]:
    info = container.embedding_provider.get_info()
    resp = EmbeddingStatusResponse(
        provider=info.provider,
        model_name=info.model_name,
        version=info.version,
        dimension=info.dimension,
        is_available=info.is_available,
        device=info.device,
        error=info.error,
    )
    return DataEnvelope(data=resp)
