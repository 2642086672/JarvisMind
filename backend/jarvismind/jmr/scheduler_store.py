"""JMR-05 scheduler persistence on top of the existing P1 tables.

The scheduler needs more per-task data than ``p1_tasks`` stores: due time,
priority, retry deadline, batch id, schedule id and a logical-delete marker.
Adding columns would violate the frozen v6/P1 schema, so this store uses
namespaced keys in the existing ``system_settings`` table:

* ``jmr.scheduler.task.<task_id>``  -> one JSON object per task;
* ``jmr.scheduler.schedules``       -> JSON object of schedule definitions;
* ``jmr.scheduler.last_tick``       -> last tick summary;
* ``jmr.scheduler.schema_version``  -> scheduler metadata version.

No DDL, no new table, no history rewrite. The authoritative task state stays in
``p1_tasks`` / ``p1_task_events``; this is only the scheduler's own metadata.
A lost metadata row is recoverable: the engine can adopt a CREATED/FAILED/
PAUSED/RUNNING ``JMR_REFLECTION`` task with conservative defaults.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..core.timeutils import utc_now_iso

SCHEDULER_SCHEMA_VERSION = 1
SCHEDULER_VERSION_KEY = "jmr.scheduler.schema_version"
SCHEDULE_KEY = "jmr.scheduler.schedules"
LAST_TICK_KEY = "jmr.scheduler.last_tick"
TASK_META_PREFIX = "jmr.scheduler.task."
BREAKER_KEY = "jmr.breaker"


class SchedulerStore:
    def __init__(self, manager) -> None:
        self.manager = manager

    # ------------------------------------------------------------- primitives
    def _read_json(self, key: str, default: Any) -> Any:
        with self.manager.session() as session:
            raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"),
                {"k": key},
            ).scalar()
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    def _write_json(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        now = utc_now_iso()
        with self.manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO system_settings(key, value, updated_at) "
                    "VALUES (:k, :v, :u) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at"
                ),
                {"k": key, "v": payload, "u": now},
            )

    def ensure_version(self) -> None:
        """Write the scheduler metadata version once. No schema change."""
        current = self._read_json(SCHEDULER_VERSION_KEY, None)
        if current != SCHEDULER_SCHEMA_VERSION:
            self._write_json(SCHEDULER_VERSION_KEY, SCHEDULER_SCHEMA_VERSION)

    # --------------------------------------------------------------- task meta
    def read_task_meta(self, task_id: str) -> Optional[Dict[str, Any]]:
        raw = self._read_json(TASK_META_PREFIX + str(task_id), None)
        return raw if isinstance(raw, dict) else None

    def write_task_meta(self, meta: Dict[str, Any]) -> None:
        task_id = str(meta.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task meta requires task_id")
        payload = dict(meta)
        payload["task_id"] = task_id
        payload["updated_at"] = utc_now_iso()
        self._write_json(TASK_META_PREFIX + task_id, payload)

    def list_task_meta(self, *, limit: int = 1000) -> List[Dict[str, Any]]:
        if limit < 1:
            return []
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT key, value FROM system_settings "
                    "WHERE key LIKE :prefix ORDER BY key ASC LIMIT :n"
                ),
                {"prefix": TASK_META_PREFIX + "%", "n": int(limit)},
            ).mappings().all()
        items: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("task_id"):
                items.append(payload)
        return items

    def delete_task_meta(self, task_id: str) -> None:
        with self.manager.session() as session:
            session.execute(
                text("DELETE FROM system_settings WHERE key=:k"),
                {"k": TASK_META_PREFIX + str(task_id)},
            )

    # --------------------------------------------------------------- schedules
    def read_schedules(self) -> Dict[str, Dict[str, Any]]:
        raw = self._read_json(SCHEDULE_KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def write_schedules(self, schedules: Dict[str, Dict[str, Any]]) -> None:
        self._write_json(SCHEDULE_KEY, schedules)

    # --------------------------------------------------------------- last tick
    def read_last_tick(self) -> Optional[Dict[str, Any]]:
        raw = self._read_json(LAST_TICK_KEY, None)
        return raw if isinstance(raw, dict) else None

    def write_last_tick(self, payload: Dict[str, Any]) -> None:
        self._write_json(LAST_TICK_KEY, payload)

    # ----------------------------------------------------------------- breaker
    def read_breaker(self) -> Dict[str, Any]:
        raw = self._read_json(BREAKER_KEY, {})
        return raw if isinstance(raw, dict) else {}


__all__ = [
    "SchedulerStore",
    "SCHEDULER_SCHEMA_VERSION",
    "SCHEDULER_VERSION_KEY",
    "SCHEDULE_KEY",
    "LAST_TICK_KEY",
    "TASK_META_PREFIX",
    "BREAKER_KEY",
]
