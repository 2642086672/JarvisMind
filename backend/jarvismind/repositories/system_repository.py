"""Persistence for system settings and schema metadata."""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.models import SystemSetting


class SystemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> Optional[str]:
        setting = self.session.get(SystemSetting, key)
        return setting.value if setting else None

    def all_settings(self) -> Dict[str, str]:
        rows = self.session.execute(text("SELECT key, value FROM system_settings")).all()
        return {str(key): str(value) for key, value in rows}

    def set(self, key: str, value: str) -> None:
        self.session.execute(
            text(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES (:key, :value, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at"
            ),
            {"key": key, "value": value, "updated_at": utc_now_iso()},
        )
