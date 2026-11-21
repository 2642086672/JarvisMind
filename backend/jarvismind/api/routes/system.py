"""System information endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_container
from ...container import Container
from ...schemas.system import SystemInfoResponse

router = APIRouter(tags=["system"])


@router.get("/system/info", response_model=SystemInfoResponse)
def system_info(container: Container = Depends(get_container)) -> SystemInfoResponse:
    return container.system_service.info()
