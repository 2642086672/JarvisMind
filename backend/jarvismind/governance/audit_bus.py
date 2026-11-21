"""P1-B AuditBus: one audit surface, reusing JME's memory_audit_logs.

No second audit table is created. Every entry is a normal ``MemoryAuditLog``
row (via ``MemoryRepository.add_audit``), with ``actor`` set to the SERVER-VERIFIED
principal id and a fixed ``detail_json`` schema carrying the nine fields the
task book requires:

    principal / operation / object / trigger / executed_at / status /
    failure_reason / task_id / run_id

plus ``grant_id`` and ``p1_audit_version`` for traceability. Failures are
recorded in their OWN session, so a rolled-back write cannot erase the audit
trail of the denial.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..core.timeutils import utc_now_iso
from ..database.models import MemoryAuditLog
from ..repositories.memory_repository import MemoryRepository

P1_AUDIT_VERSION = 1
STATUS_ALLOWED = "ALLOWED"
STATUS_DENIED = "DENIED"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"


def _new_id() -> str:
    return uuid.uuid4().hex


class AuditBus:
    def __init__(self, manager) -> None:
        self.manager = manager

    def record(
        self,
        *,
        principal_id: str,
        principal_type: str,
        operation: str,
        object_ref: str,
        trigger: str,
        status: str,
        failure_reason: str = "",
        task_id: str = "",
        run_id: str = "",
        grant_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        session=None,
    ) -> str:
        """Write one audit row. ``session`` lets a successful write share its
        transaction; a denial always uses a fresh session of its own."""
        detail: Dict[str, Any] = {
            "p1_audit_version": P1_AUDIT_VERSION,
            "principal_id": principal_id,
            "principal_type": principal_type,
            "operation": operation,
            "object_ref": object_ref,
            "trigger": trigger,
            "executed_at": utc_now_iso(),
            "status": status,
            "failure_reason": failure_reason or "",
            "task_id": task_id or "",
            "run_id": run_id or "",
            "grant_id": grant_id or "",
        }
        if extra:
            detail["extra"] = dict(extra)
        audit_id = _new_id()
        action = f"P1:{operation}:{status}"

        def _write(s):
            MemoryRepository(s).add_audit(
                audit_id=audit_id,
                memory_id=object_ref or "GLOBAL",
                action=action,
                actor=principal_id or "UNTRUSTED",
                detail=detail,
            )

        if session is not None:
            _write(session)
        else:
            with self.manager.session() as own_session:
                _write(own_session)
        return audit_id

    def query(self, memory_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.manager.session() as session:
            rows = (
                session.query(MemoryAuditLog)
                .filter(MemoryAuditLog.memory_id == memory_id)
                .order_by(MemoryAuditLog.created_at.asc())
                .limit(limit)
                .all()
            )
        result: List[Dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row.detail_json) if row.detail_json else {}
            except (ValueError, TypeError):
                detail = {}
            result.append(
                {
                    "id": row.id,
                    "action": row.action,
                    "actor": row.actor,
                    "created_at": row.created_at,
                    "detail": detail,
                }
            )
        return result

    def p1_entries(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT id, memory_id, action, actor, detail_json, created_at "
                    "FROM memory_audit_logs WHERE action LIKE 'P1:%' "
                    "ORDER BY created_at ASC LIMIT :n"
                ),
                {"n": int(limit)},
            ).mappings().all()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
            except (ValueError, TypeError):
                detail = {}
            out.append(
                {
                    "id": row["id"],
                    "memory_id": row["memory_id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "created_at": row["created_at"],
                    "detail": detail,
                }
            )
        return out

    def count_p1(self) -> int:
        with self.manager.session() as session:
            return int(
                session.execute(
                    text("SELECT COUNT(*) FROM memory_audit_logs WHERE action LIKE 'P1:%'")
                ).scalar()
                or 0
            )
