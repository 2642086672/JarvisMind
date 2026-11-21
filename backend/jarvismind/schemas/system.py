"""Schemas for health and system information endpoints."""
from __future__ import annotations

from typing import List

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    database_connected: bool
    schema_version: int


class SystemInfoResponse(BaseModel):
    name: str
    version: str
    python_version: str
    database_type: str
    data_dir: str
    database_file: str
    memory_types: List[str]
    statuses: List[str]
    status: str
    uptime_seconds: float
