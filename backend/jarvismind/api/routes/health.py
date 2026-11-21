"""Health check endpoint (unauthenticated by design).

It reports the installation as a whole, so it reads the default container directly
instead of the memory space selected by authentication. It returns no memory data.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ...container import Container
from ...schemas.system import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    container: Container = request.app.state.container
    return container.system_service.health()
