"""Read-only MCE HTTP surface (MCE-01A).

Two endpoints, both registered under the shared token dependency in
``app.main``:

* ``GET  /api/v1/mce/status``  — diagnostic, never raises;
* ``POST /api/v1/mce/resolve`` — one bounded, read-only reconstruction.

The module deliberately imports **no** MCE core service at import time: it
speaks to ``container.mce_adapter``, which is itself lazy. Importing this
router therefore opens no database connection and installs no schema. This
mirrors the JKG router's startup-safety contract.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...container import Container
from ...schemas.mce import (
    MceResolveRequest,
    MceResolveResponse,
    MceStatusResponse,
    resolution_payload,
)
from ..deps import get_container

router = APIRouter(tags=["mce"])


@router.get("/status", response_model=MceStatusResponse)
def mce_status(container: Container = Depends(get_container)) -> MceStatusResponse:
    return MceStatusResponse.model_validate(container.mce_adapter.status())


@router.post("/resolve", response_model=MceResolveResponse)
def resolve_project_context(
    payload: MceResolveRequest,
    container: Container = Depends(get_container),
) -> MceResolveResponse:
    result = container.mce_adapter.resolve(
        project_id=payload.project_id,
        query=payload.query,
        max_tokens=payload.max_tokens,
        max_items=payload.limit,
        include_jkg=payload.include_jkg,
        include_superseded=payload.include_superseded,
    )
    return MceResolveResponse.model_validate(resolution_payload(result))
