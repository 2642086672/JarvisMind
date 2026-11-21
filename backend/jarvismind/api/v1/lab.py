"""Owner-only Context Lab: retrieval debugging for the console.

The lab shows the owner the retrieval result and the exact context block an agent
would receive. The owner credential may read every memory, so the preview can
contain sensitive text. This router is registered behind the owner credential
only; agents cannot reach it.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ...container import Container
from ..deps import get_container
from .agent import context_block

router = APIRouter(tags=["console"])


class LabContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)
    max_content_chars: int = Field(default=600, ge=80, le=2000)


@router.post("/lab/context")
def lab_context(
    payload: LabContextIn,
    container: Container = Depends(get_container),
) -> Dict[str, Any]:
    result = container.memory_context_service.retrieve(
        query=payload.query,
        limit=payload.limit,
        max_content_chars=payload.max_content_chars,
    )
    items = list(result.items)
    return {
        "data": {
            "context": context_block(items),
            "items": [item.model_dump(mode="json") for item in items],
            "total": result.total,
            "retrieval_version": result.retrieval_version,
            "degraded_to_fts": result.degraded_to_fts,
            "excluded": result.excluded.model_dump(mode="json"),
        }
    }
