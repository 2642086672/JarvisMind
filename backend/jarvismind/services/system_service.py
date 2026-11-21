"""Read-only system and health information."""
from __future__ import annotations

import platform
import time

from ..config import SERVICE_NAME, Settings
from ..database.connection import DatabaseManager
from ..database.migrations import MigrationRunner
from ..schemas.memory import SUPPORTED_MEMORY_TYPES, SUPPORTED_STATUSES
from ..schemas.system import HealthResponse, SystemInfoResponse


class SystemService:
    def __init__(
        self, manager: DatabaseManager, settings: Settings, started_at: float
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.started_at = started_at

    def health(self) -> HealthResponse:
        connected = self.manager.verify_connection()
        schema_version = MigrationRunner(self.manager).current_version()
        return HealthResponse(
            status="ok" if connected else "degraded",
            version=self.settings.version,
            database_connected=connected,
            schema_version=schema_version,
        )

    def info(self) -> SystemInfoResponse:
        return SystemInfoResponse(
            name=SERVICE_NAME,
            version=self.settings.version,
            python_version=platform.python_version(),
            database_type="sqlite",
            data_dir=str(self.settings.data_dir),
            database_file=str(self.settings.database_path),
            memory_types=list(SUPPORTED_MEMORY_TYPES),
            statuses=list(SUPPORTED_STATUSES),
            status="running",
            uptime_seconds=round(max(time.monotonic() - self.started_at, 0.0), 3),
        )
