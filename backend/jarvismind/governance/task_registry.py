"""P1-D unified task governance for JMR / JPA (and later JKG) background work.

Two append-only tables from ``schema.py``: ``p1_tasks`` holds current state,
``p1_task_events`` is the immutable execution record. No second scheduler
daemon and no Agent Runtime are introduced; this is a state machine and a
ledger that any caller (including a future scheduler) can drive.

State machine::

    CREATED -> RUNNING -> PAUSED -> RUNNING
    RUNNING -> SUCCEEDED | FAILED | CANCELLED
    PAUSED  -> RUNNING | CANCELLED
    FAILED  -> CREATED   (recover: state only, NEVER replays side effects)
    SUCCEEDED / CANCELLED are terminal

Idempotency: repeating a pause/resume/cancel/create against an already-satisfied
state returns ``noop=True`` and appends NO event. A failed task records its
reason; recovery is an explicit, side-effect-free state reset.
Complexity: O(1) per transition, O(events) per history query.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..core.timeutils import utc_now_iso
from .errors import TaskStateError

CREATED = "CREATED"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ALLOWED_TRANSITIONS: Dict[str, tuple] = {
    CREATED: (RUNNING, CANCELLED),
    RUNNING: (PAUSED, SUCCEEDED, FAILED, CANCELLED),
    PAUSED: (RUNNING, CANCELLED),
    FAILED: (CREATED,),
    SUCCEEDED: (),
    CANCELLED: (),
}


@dataclass(frozen=True)
class TransitionResult:
    task_id: str
    from_status: str
    to_status: str
    noop: bool
    event_id: str = ""


class TaskRegistry:
    def __init__(self, manager) -> None:
        self.manager = manager

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    def _task_row(self, session, task_id: str):
        return session.execute(
            text(
                "SELECT task_id, task_type, status, owner, created_at, updated_at, "
                "pause_reason, cancel_reason, failure_reason, metadata_json "
                "FROM p1_tasks WHERE task_id=:t"
            ),
            {"t": task_id},
        ).mappings().first()

    @staticmethod
    def _as_dict(row) -> Dict[str, Any]:
        result = dict(row)
        raw = result.get("metadata_json")
        if raw:
            try:
                result["metadata"] = json.loads(raw)
            except (ValueError, TypeError):
                result["metadata"] = {}
        else:
            result["metadata"] = {}
        return result

    def create(
        self,
        task_id: str,
        task_type: str,
        *,
        owner: str = "",
        run_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not task_id or not task_type:
            raise TaskStateError("task_id and task_type are required")
        now = utc_now_iso()
        with self.manager.session() as session:
            existing = self._task_row(session, task_id)
            if existing is not None:
                # Idempotent: never duplicate a task, never append a second
                # CREATE event for the same task_id.
                return {**self._as_dict(existing), "noop": True}
            session.execute(
                text(
                    "INSERT INTO p1_tasks("
                    "task_id, task_type, status, owner, created_at, updated_at, "
                    "pause_reason, cancel_reason, failure_reason, metadata_json"
                    ") VALUES (:id, :type, :status, :owner, :now, :now, NULL, NULL, NULL, :meta)"
                ),
                {
                    "id": task_id,
                    "type": task_type,
                    "status": CREATED,
                    "owner": owner or "",
                    "now": now,
                    "meta": json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                },
            )
            self._append_event(
                session,
                task_id,
                "CREATE",
                from_status="",
                to_status=CREATED,
                run_id=run_id,
                detail={"task_type": task_type, "owner": owner or ""},
            )
            row = self._task_row(session, task_id)
        return {**self._as_dict(row), "noop": False}

    def _append_event(
        self,
        session,
        task_id: str,
        event_type: str,
        *,
        from_status: str,
        to_status: str,
        run_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> str:
        event_id = self._new_id()
        session.execute(
            text(
                "INSERT INTO p1_task_events("
                "event_id, task_id, run_id, event_type, from_status, to_status, "
                "detail_json, created_at"
                ") VALUES (:eid, :tid, :rid, :etype, :from, :to, :detail, :now)"
            ),
            {
                "eid": event_id,
                "tid": task_id,
                "rid": run_id or "",
                "etype": event_type,
                "from": from_status,
                "to": to_status,
                "detail": json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                "now": utc_now_iso(),
            },
        )
        return event_id

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.manager.session() as session:
            row = self._task_row(session, task_id)
        return self._as_dict(row) if row is not None else None

    def update_metadata(
        self,
        task_id: str,
        patch: Dict[str, Any],
        *,
        event_type: Optional[str] = None,
        run_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.manager.session() as session:
            row = self._task_row(session, task_id)
            if row is None:
                raise TaskStateError(f"task {task_id} not found")
            try:
                current = json.loads(row["metadata_json"] or "{}")
            except (ValueError, TypeError):
                current = {}
            merged = {**current, **dict(patch or {})}
            session.execute(
                text(
                    "UPDATE p1_tasks SET metadata_json=:meta, updated_at=:now "
                    "WHERE task_id=:t"
                ),
                {
                    "meta": json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    "now": now,
                    "t": task_id,
                },
            )
            if event_type:
                self._append_event(
                    session,
                    task_id,
                    event_type,
                    from_status=row["status"],
                    to_status=row["status"],
                    run_id=run_id,
                    detail=detail,
                )
            row = self._task_row(session, task_id)
        return self._as_dict(row)

    def list(self, *, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self.manager.session() as session:
            if status:
                rows = session.execute(
                    text(
                        "SELECT * FROM p1_tasks WHERE status=:s "
                        "ORDER BY created_at ASC LIMIT :n"
                    ),
                    {"s": status, "n": int(limit)},
                ).mappings().all()
            else:
                rows = session.execute(
                    text("SELECT * FROM p1_tasks ORDER BY created_at ASC LIMIT :n"),
                    {"n": int(limit)},
                ).mappings().all()
        return [self._as_dict(row) for row in rows]

    def status(self, task_id: str) -> Dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise TaskStateError(f"task {task_id} not found")
        events = self.events(task_id, limit=1000)
        return {
            "task_id": task_id,
            "status": task["status"],
            "updated_at": task["updated_at"],
            "event_count": len(events),
            "last_event": events[-1]["event_type"] if events else None,
            "failure_reason": task.get("failure_reason") or "",
        }

    def events(self, task_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT event_id, task_id, run_id, event_type, from_status, "
                    "to_status, detail_json, created_at FROM p1_task_events "
                    "WHERE task_id=:t ORDER BY created_at ASC, event_id ASC LIMIT :n"
                ),
                {"t": task_id, "n": int(limit)},
            ).mappings().all()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except (ValueError, TypeError):
                item.pop("detail_json", None)
                item["detail"] = {}
            out.append(item)
        return out

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        run_id: str = "",
        reason: str = "",
        event_type: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        idempotent_target: bool = True,
    ) -> TransitionResult:
        now = utc_now_iso()
        with self.manager.session() as session:
            row = self._task_row(session, task_id)
            if row is None:
                raise TaskStateError(f"task {task_id} not found")
            current = row["status"]
            if current == to_status:
                if idempotent_target:
                    return TransitionResult(task_id, current, current, True)
                raise TaskStateError(f"task already in {to_status}")
            if to_status not in ALLOWED_TRANSITIONS.get(current, ()):
                raise TaskStateError(
                    f"cannot move {current} -> {to_status}",
                    detail={"code": "invalid_task_transition", "current": current},
                )
            pause_reason = reason if to_status == PAUSED else None
            cancel_reason = reason if to_status == CANCELLED else None
            failure_reason = reason if to_status == FAILED else None
            session.execute(
                text(
                    "UPDATE p1_tasks SET status=:s, updated_at=:now, "
                    "pause_reason=:pr, cancel_reason=:cr, failure_reason=:fr "
                    "WHERE task_id=:t"
                ),
                {
                    "s": to_status,
                    "now": now,
                    "pr": pause_reason,
                    "cr": cancel_reason,
                    "fr": failure_reason,
                    "t": task_id,
                },
            )
            eid = self._append_event(
                session,
                task_id,
                event_type or to_status,
                from_status=current,
                to_status=to_status,
                run_id=run_id,
                detail={"reason": reason, **(detail or {})},
            )
        return TransitionResult(task_id, current, to_status, False, eid)

    def start(self, task_id: str, *, run_id: str = "") -> TransitionResult:
        return self.transition(task_id, RUNNING, run_id=run_id, event_type="START")

    def pause(self, task_id: str, *, reason: str = "", run_id: str = "") -> TransitionResult:
        return self.transition(task_id, PAUSED, run_id=run_id, reason=reason, event_type="PAUSE")

    def resume(self, task_id: str, *, run_id: str = "") -> TransitionResult:
        return self.transition(task_id, RUNNING, run_id=run_id, event_type="RESUME")

    def cancel(self, task_id: str, *, reason: str = "", run_id: str = "") -> TransitionResult:
        return self.transition(task_id, CANCELLED, run_id=run_id, reason=reason, event_type="CANCEL")

    def succeed(self, task_id: str, *, run_id: str = "") -> TransitionResult:
        return self.transition(task_id, SUCCEEDED, run_id=run_id, event_type="SUCCEED")

    def fail(self, task_id: str, *, reason: str, run_id: str = "") -> TransitionResult:
        if not reason:
            raise TaskStateError("a failure reason is required")
        return self.transition(
            task_id, FAILED, run_id=run_id, reason=reason, event_type="FAIL"
        )

    def recover(self, task_id: str, *, run_id: str = "") -> TransitionResult:
        """Safe recovery: state only. Never re-runs the failed operation."""
        return self.transition(
            task_id,
            CREATED,
            run_id=run_id,
            event_type="RECOVER",
            detail={"replay_side_effects": False},
        )

    def runtime_state(self) -> Dict[str, Any]:
        with self.manager.session() as session:
            counts = {
                row["status"]: row["n"]
                for row in session.execute(
                    text("SELECT status, COUNT(*) AS n FROM p1_tasks GROUP BY status")
                ).mappings().all()
            }
            event_count = int(
                session.execute(text("SELECT COUNT(*) FROM p1_task_events")).scalar() or 0
            )
        return {"tasks_by_status": counts, "event_count": event_count}
