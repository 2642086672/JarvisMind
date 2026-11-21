"""Read-only Memory Overview endpoints (JME-MEMORY-OVERVIEW-01).

Registered under ``/api/v1/memories/overview`` BEFORE the CRUD ``/{memory_id}``
route so the path cannot be captured as a memory id. The router-level token
dependency in ``app.main`` applies, same as every other JME surface.

Two GETs, both read-only with respect to memory data:

* ``GET /overview``          — deterministic, fast, free. Never calls a model.
* ``GET /overview?enrich=1`` — the same overview plus model-written summaries
  when ``JARVISMIND_LLM_*`` is configured. Opt-in so a page refresh can never
  silently spend money.

Neither endpoint writes memories, candidates, proposals or audit rows.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...schemas.overview import MemoryOverviewResponse
from ..deps import get_container

router = APIRouter(tags=["memory-overview"])


@router.get("/overview", response_model=MemoryOverviewResponse)
def get_memory_overview(
    enrich: bool = Query(
        default=False,
        description=(
            "When true, ask the configured model to rewrite the summaries if no "
            "valid cached result exists. Requires JARVISMIND_LLM_BASE_URL / "
            "JARVISMIND_LLM_MODEL; falls back silently."
        ),
    ),
    refresh: bool = Query(
        default=False,
        description=(
            "When true, force a new model call even if the cached summaries "
            "still match the current memory set. Implies enrich."
        ),
    ),
    container: Container = Depends(get_container),
) -> MemoryOverviewResponse:
    """Return the current personal memory overview.

    Sensitive, credential-shaped, expired and deleted memories are excluded by
    the service. Conflicts are reported, never auto-resolved.

    Cost model: a plain GET never calls a model. ``?enrich=1`` only calls one
    when there is no cached result for the current memory set. ``?refresh=1``
    forces a fresh call.
    """
    payload = container.memory_overview_service.build(
        enrich=enrich or refresh,
        refresh=refresh,
    )
    return MemoryOverviewResponse(**payload)
