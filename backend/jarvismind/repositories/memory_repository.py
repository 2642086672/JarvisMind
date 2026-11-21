"""Persistence for memory records, sources, and audit logs."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.models import MemoryAuditLog, MemoryRecord, MemorySource


class MemoryRepository:
    """All SQL for memory_records lives here; services never write raw SQL."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, record: MemoryRecord) -> MemoryRecord:
        self.session.add(record)
        self.session.flush()
        return record

    def get_by_id(self, memory_id: str) -> Optional[MemoryRecord]:
        return self.session.get(MemoryRecord, memory_id)

    def source_exists(self, source_id: str) -> bool:
        return (
            self.session.get(MemorySource, source_id) is not None
        )

    def all_contents(self) -> List[Tuple[str, str]]:
        """Return (id, content) for every memory, including deleted ones."""
        rows = self.session.execute(
            text("SELECT id, content FROM memory_records")
        ).all()
        return [(row.id, row.content or "") for row in rows]

    def list(
        self,
        *,
        memory_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> Tuple[List[MemoryRecord], int]:
        conditions = []
        if memory_type is not None:
            conditions.append(MemoryRecord.memory_type == memory_type)
        if status is not None:
            conditions.append(MemoryRecord.status == status)

        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryRecord).where(*conditions)
            ).scalar()
            or 0
        )

        column = getattr(MemoryRecord, sort_by, MemoryRecord.created_at)
        order = column.asc() if sort_order == "asc" else column.desc()
        offset = (page - 1) * page_size
        rows = (
            self.session.execute(
                select(MemoryRecord)
                .where(*conditions)
                .order_by(order, MemoryRecord.id.asc())
                .offset(offset)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def search(
        self,
        *,
        match: str,
        memory_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Run an FTS5 MATCH query and return rows ranked by bm25 relevance.

        ``match`` must already be a quoted FTS5 expression built by
        ``core.search.build_match_expression``; it is passed as a bound
        parameter and never concatenated.
        """
        params: Dict[str, Any] = {
            "match": match,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        extra = ""
        if memory_type is not None:
            extra += " AND m.memory_type = :memory_type"
            params["memory_type"] = memory_type
        if status is not None:
            extra += " AND m.status = :status"
            params["status"] = status

        count_sql = (
            "SELECT COUNT(*) FROM memory_records_fts_zh "
            "JOIN memory_records m ON m.rowid = memory_records_fts_zh.rowid "
            "WHERE memory_records_fts_zh MATCH :match" + extra
        )
        total = int(self.session.execute(text(count_sql), params).scalar() or 0)

        rows_sql = (
            "SELECT m.id, m.content, m.summary, m.memory_type, m.importance, "
            "m.confidence, m.created_at, bm25(memory_records_fts_zh) AS rank "
            "FROM memory_records_fts_zh "
            "JOIN memory_records m ON m.rowid = memory_records_fts_zh.rowid "
            "WHERE memory_records_fts_zh MATCH :match" + extra + " "
            "ORDER BY rank LIMIT :limit OFFSET :offset"
        )
        rows = [
            {
                "id": row.id,
                "content": row.content,
                "summary": row.summary,
                "memory_type": row.memory_type,
                "importance": row.importance,
                "confidence": row.confidence,
                "created_at": row.created_at,
                "rank": row.rank,
            }
            for row in self.session.execute(text(rows_sql), params).all()
        ]
        return rows, total

    def update(self, record: MemoryRecord, values: Dict[str, Any]) -> MemoryRecord:
        """Apply ``values`` to ``record``.

        JW-03: this is the single choke point every authoritative write passes
        through, so the approved-memory protection is enforced HERE in addition
        to the service-layer checks. A caller that bypasses the service (a test
        helper, a future route, a maintenance script) still cannot silently
        rewrite an approved memory: it must either hold a verified approved
        request - which the service passes down - or run inside an explicit
        internal scope.
        """
        # Local import: the repository module is imported very early (models,
        # migrations), and approved_protection pulls in the error hierarchy.
        from ..core.approved_protection import require_write_authority

        require_write_authority(
            record, values, operation="MemoryRepository.update"
        )
        for key, value in values.items():
            setattr(record, key, value)
        record.updated_at = utc_now_iso()
        self.session.flush()
        return record

    def touch_accessed(self, record: MemoryRecord) -> None:
        # Server-internal bookkeeping: allowed by the P1 gates without a
        # per-request signed grant (review P1 finding: GET used to 500).
        from ..p1_gate import maintenance_write

        with maintenance_write():
            record.accessed_at = utc_now_iso()
            self.session.flush()

    def add_audit(
        self,
        *,
        audit_id: str,
        memory_id: str,
        action: str,
        actor: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.session.add(
            MemoryAuditLog(
                id=audit_id,
                memory_id=memory_id,
                action=action,
                actor=actor,
                detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
                created_at=utc_now_iso(),
            )
        )
        self.session.flush()

    def audit_trail(self, memory_id: str) -> Sequence[MemoryAuditLog]:
        return (
            self.session.execute(
                select(MemoryAuditLog)
                .where(MemoryAuditLog.memory_id == memory_id)
                .order_by(MemoryAuditLog.created_at.asc())
            )
            .scalars()
            .all()
        )

    def update_vector_status(
        self, memory_id: str, status: str, updated_at: Optional[str] = None
    ) -> None:
        from ..p1_gate import maintenance_write

        with maintenance_write():
            rec = self.get_by_id(memory_id)
            if rec:
                rec.vector_status = status
                rec.vector_updated_at = updated_at or utc_now_iso()
                self.session.flush()

    def get_records_for_indexing(
        self, limit: int = 100, status: str = "PENDING"
    ) -> List[MemoryRecord]:
        return list(
            self.session.execute(
                select(MemoryRecord)
                .where(
                    MemoryRecord.status == "ACTIVE",
                    MemoryRecord.vector_status == status,
                )
                .order_by(MemoryRecord.created_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def get_all_active_records(
        self, memory_type: Optional[str] = None
    ) -> List[MemoryRecord]:
        conditions = [MemoryRecord.status == "ACTIVE"]
        if memory_type is not None:
            conditions.append(MemoryRecord.memory_type == memory_type)
        return list(
            self.session.execute(
                select(MemoryRecord)
                .where(*conditions)
                .order_by(MemoryRecord.created_at.asc())
            )
            .scalars()
            .all()
        )

    def get_by_ids(self, ids: Sequence[str]) -> List[MemoryRecord]:
        if not ids:
            return []
        return list(
            self.session.execute(
                select(MemoryRecord).where(MemoryRecord.id.in_(list(ids)))
            )
            .scalars()
            .all()
        )

    def list_active_missing_search_text(self, limit: int = 200) -> List[MemoryRecord]:
        """ACTIVE rows the FTS index cannot see because ``search_text`` is empty.

        Read-only helper for the chat memory-context path: rows created through
        the service always get ``search_text``, so this only returns legacy or
        externally imported rows. Bounded by ``limit`` on purpose so a library
        with a broken index can never turn one chat turn into a full-table scan.
        """
        return list(
            self.session.execute(
                select(MemoryRecord)
                .where(
                    MemoryRecord.status == "ACTIVE",
                    or_(
                        MemoryRecord.search_text.is_(None),
                        func.trim(MemoryRecord.search_text) == "",
                    ),
                )
                .order_by(MemoryRecord.updated_at.desc())
                .limit(max(1, int(limit)))
            )
            .scalars()
            .all()
        )

    def count_by_vector_status(self) -> Dict[str, int]:
        rows = self.session.execute(
            text(
                "SELECT vector_status, COUNT(*) as cnt "
                "FROM memory_records "
                "WHERE status = 'ACTIVE' "
                "GROUP BY vector_status"
            )
        ).all()
        res = {"PENDING": 0, "INDEXED": 0, "FAILED": 0, "REMOVED": 0}
        for row in rows:
            res[str(row[0]).upper()] = int(row[1])
        return res

    def get_vector_meta(self, key: str) -> Optional[str]:
        row = self.session.execute(
            text("SELECT value FROM vector_index_meta WHERE key = :key"),
            {"key": key},
        ).first()
        return str(row[0]) if row else None

    def set_vector_meta(self, key: str, value: str) -> None:
        now = utc_now_iso()
        self.session.execute(
            text(
                "INSERT INTO vector_index_meta (key, value, updated_at) "
                "VALUES (:key, :value, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET value = :value, updated_at = :updated_at"
            ),
            {"key": key, "value": value, "updated_at": now},
        )
        self.session.flush()
