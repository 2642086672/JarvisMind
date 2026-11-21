"""Read-only chat memory-context endpoint (JME-MEMORY-CONTEXT-01).

Registered under ``/api/v1/memories/context`` BEFORE the CRUD ``/{memory_id}``
route so the path cannot be captured as a memory id. The router-level token
dependency in ``app.main`` applies, like every other JME surface.

Why not reuse ``/hybrid-search``?
--------------------------------
``/hybrid-search`` is a general search surface. It does not exclude expired
rows, does not apply the sensitive-content policy, and returns no relevance
verdict. Chat injection needs all three; extending the general endpoint with
chat-only rules would change an existing contract for every other caller. This
endpoint is additive and read-only.

Guarantees
----------
* No writes of any kind: no memory rows, candidates, audit logs, access
  timestamps, vector points or JMR proposals.
* Only ``ACTIVE``, non-expired rows.
* Credential / regex-PII / personally-sensitive memories are excluded.
* A relevance floor; an unrelated question gets an empty list rather than
  "the most recent memories".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ...container import Container
from ...schemas.context import MemoryContextRequest, MemoryContextResponse
from ..deps import get_container

router = APIRouter(tags=["memory-context"])


@router.post("/context", response_model=MemoryContextResponse)
def retrieve_memory_context(
    payload: MemoryContextRequest,
    container: Container = Depends(get_container),
) -> MemoryContextResponse:
    include = [item.value for item in payload.include_categories]
    exclude = [item.value for item in payload.exclude_categories]
    return container.memory_context_service.retrieve(
        query=payload.query,
        limit=payload.limit,
        include_categories=include,
        exclude_categories=exclude,
        max_content_chars=payload.max_content_chars,
    )
