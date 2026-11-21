"""Persistence for JME-5 lifecycle tables.

Every query against summaries, corrections, merges, archive records, and
lifecycle reviews lives here so services never write raw SQL for them.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..core.timeutils import parse_iso, utc_now_iso
from ..database.models import (
    MemoryArchiveRecord,
    MemoryCorrection,
    MemoryLifecycleReview,
    MemoryMerge,
    MemoryRecord,
    MemorySummary,
)


def _loads(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


class LifecycleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --------------------------------------------------------------- summaries
    def create_summary(self, summary: MemorySummary) -> MemorySummary:
        self.session.add(summary)
        self.session.flush()
        return summary

    def get_summary(self, summary_id: str) -> Optional[MemorySummary]:
        return self.session.get(MemorySummary, summary_id)

    def list_summaries(
        self,
        *,
        status: Optional[str] = None,
        summary_type: Optional[str] = None,
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemorySummary], int]:
        conditions = []
        if status is not None:
            conditions.append(MemorySummary.status == status)
        if summary_type is not None:
            conditions.append(MemorySummary.summary_type == summary_type)
        if memory_type is not None:
            conditions.append(MemorySummary.memory_type == memory_type)
        total = int(
            self.session.execute(
                select(func.count()).select_from(MemorySummary).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemorySummary)
                .where(*conditions)
                .order_by(MemorySummary.created_at.desc(), MemorySummary.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def summaries_for_memory(self, memory_id: str) -> List[MemorySummary]:
        rows = self.session.execute(
            select(MemorySummary).order_by(MemorySummary.created_at.desc())
        ).scalars().all()
        return [
            row
            for row in rows
            if memory_id in (_loads(row.memory_ids_json) or [])
        ]

    def update_summary(
        self, summary: MemorySummary, values: Dict[str, Any]
    ) -> MemorySummary:
        for key, value in values.items():
            setattr(summary, key, value)
        summary.updated_at = utc_now_iso()
        self.session.flush()
        return summary

    # ------------------------------------------------------------- corrections
    def create_correction(self, correction: MemoryCorrection) -> MemoryCorrection:
        self.session.add(correction)
        self.session.flush()
        return correction

    def get_correction(self, correction_id: str) -> Optional[MemoryCorrection]:
        return self.session.get(MemoryCorrection, correction_id)

    def list_corrections(
        self,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemoryCorrection], int]:
        conditions = []
        if status is not None:
            conditions.append(MemoryCorrection.status == status)
        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryCorrection).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemoryCorrection)
                .where(*conditions)
                .order_by(MemoryCorrection.detected_at.desc(), MemoryCorrection.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def corrections_for_memory(self, memory_id: str) -> List[MemoryCorrection]:
        rows = self.session.execute(
            select(MemoryCorrection).order_by(MemoryCorrection.detected_at.desc())
        ).scalars().all()
        return [
            row
            for row in rows
            if row.memory_id_a == memory_id or row.memory_id_b == memory_id
        ]

    def update_correction(
        self, correction: MemoryCorrection, values: Dict[str, Any]
    ) -> MemoryCorrection:
        for key, value in values.items():
            setattr(correction, key, value)
        self.session.flush()
        return correction

    # ------------------------------------------------------------------ merges
    def create_merge(self, merge: MemoryMerge) -> MemoryMerge:
        self.session.add(merge)
        self.session.flush()
        return merge

    def get_merge(self, merge_id: str) -> Optional[MemoryMerge]:
        return self.session.get(MemoryMerge, merge_id)

    def list_merges(
        self,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemoryMerge], int]:
        conditions = []
        if status is not None:
            conditions.append(MemoryMerge.status == status)
        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryMerge).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemoryMerge)
                .where(*conditions)
                .order_by(MemoryMerge.created_at.desc(), MemoryMerge.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def update_merge(
        self, merge: MemoryMerge, values: Dict[str, Any]
    ) -> MemoryMerge:
        for key, value in values.items():
            setattr(merge, key, value)
        merge.updated_at = utc_now_iso()
        self.session.flush()
        return merge

    def transition_merge(
        self,
        merge_id: str,
        *,
        expected_status: str,
        new_status: str,
        values: Optional[Dict[str, Any]] = None,
    ) -> int:
        """JW-02: conditional merge status transition. Returns rowcount.

        ``expected_status`` is enforced in the WHERE clause, so confirming the
        same preview twice cannot both succeed: the second UPDATE matches zero
        rows. Before JW-02 the code read the record, saw CONFIRMED, and then
        wrote anyway - which is how one preview produced two merge products.
        """
        from sqlalchemy import text

        assignments = ["status = :new_status", "updated_at = :now"]
        params: Dict[str, Any] = {
            "id": merge_id,
            "expected": expected_status,
            "new_status": new_status,
            "now": utc_now_iso(),
        }
        for column, value in (values or {}).items():
            # Column names are code-controlled constants, never request input.
            assignments.append(f"{column} = :{column}")
            params[column] = value
        result = self.session.execute(
            text(
                f"UPDATE memory_merges SET {', '.join(assignments)} "
                "WHERE id = :id AND status = :expected"
            ),
            params,
        )
        self.session.flush()
        return int(result.rowcount or 0)

    # ---------------------------------------------------------------- archives
    def create_archive_record(
        self, record: MemoryArchiveRecord
    ) -> MemoryArchiveRecord:
        self.session.add(record)
        self.session.flush()
        return record

    def list_archive_records(
        self,
        *,
        memory_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemoryArchiveRecord], int]:
        conditions = []
        if memory_id is not None:
            conditions.append(MemoryArchiveRecord.memory_id == memory_id)
        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryArchiveRecord).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemoryArchiveRecord)
                .where(*conditions)
                .order_by(
                    MemoryArchiveRecord.archived_at.desc(),
                    MemoryArchiveRecord.id.asc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def latest_archive_record(
        self, memory_id: str
    ) -> Optional[MemoryArchiveRecord]:
        row = (
            self.session.execute(
                select(MemoryArchiveRecord)
                .where(MemoryArchiveRecord.memory_id == memory_id)
                .order_by(MemoryArchiveRecord.archived_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return row

    def mark_restored(
        self, record: MemoryArchiveRecord, restored_by: Optional[str]
    ) -> MemoryArchiveRecord:
        record.restored_at = utc_now_iso()
        record.restored_by = restored_by
        self.session.flush()
        return record

    # -------------------------------------------------------- lifecycle reviews
    def create_review(self, review: MemoryLifecycleReview) -> MemoryLifecycleReview:
        self.session.add(review)
        self.session.flush()
        return review

    def list_reviews(
        self,
        *,
        memory_id: Optional[str] = None,
        review_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemoryLifecycleReview], int]:
        conditions = []
        if memory_id is not None:
            conditions.append(MemoryLifecycleReview.memory_id == memory_id)
        if review_type is not None:
            conditions.append(MemoryLifecycleReview.review_type == review_type)
        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryLifecycleReview).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemoryLifecycleReview)
                .where(*conditions)
                .order_by(
                    MemoryLifecycleReview.created_at.desc(),
                    MemoryLifecycleReview.id.asc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    # ------------------------------------------------------------------ expiry
    def list_expiring(
        self, *, now: str, within_seconds: int, memory_type: Optional[str] = None
    ) -> List[MemoryRecord]:
        """Active memories whose expires_at falls between now and now+window."""
        rows = self.session.execute(
            select(MemoryRecord).where(
                MemoryRecord.status == "ACTIVE",
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at > now,
            )
        ).scalars().all()
        deadline = parse_iso(now).timestamp() + within_seconds
        result = []
        for row in rows:
            if memory_type is not None and row.memory_type != memory_type:
                continue
            try:
                expires = parse_iso(row.expires_at or "").timestamp()
            except ValueError:
                continue
            if expires <= deadline:
                result.append(row)
        result.sort(key=lambda r: r.expires_at or "")
        return result

    def list_expired(
        self, *, now: str, memory_type: Optional[str] = None
    ) -> List[MemoryRecord]:
        rows = self.session.execute(
            select(MemoryRecord).where(
                MemoryRecord.status == "ACTIVE",
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= now,
            )
        ).scalars().all()
        if memory_type is not None:
            rows = [row for row in rows if row.memory_type == memory_type]
        rows.sort(key=lambda r: r.expires_at or "")
        return list(rows)

    # ------------------------------------------------------------------ forget
    def get_records_by_ids(self, ids: Sequence[str]) -> List[MemoryRecord]:
        if not ids:
            return []
        return list(
            self.session.execute(
                select(MemoryRecord).where(MemoryRecord.id.in_(list(ids)))
            ).scalars().all()
        )

    def count_active(self) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(MemoryRecord)
                .where(MemoryRecord.status == "ACTIVE")
            ).scalar()
            or 0
        )

    def count_by_type(self, memory_type: str) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(MemoryRecord)
                .where(
                    MemoryRecord.status == "ACTIVE",
                    MemoryRecord.memory_type == memory_type,
                )
            ).scalar()
            or 0
        )

    def list_active_by_type(
        self, memory_type: str, limit: int, offset: int = 0
    ) -> List[MemoryRecord]:
        return list(
            self.session.execute(
                select(MemoryRecord)
                .where(
                    MemoryRecord.status == "ACTIVE",
                    MemoryRecord.memory_type == memory_type,
                )
                .order_by(MemoryRecord.created_at.asc())
                .offset(offset)
                .limit(limit)
            ).scalars().all()
        )

    def set_forgotten(
        self,
        record: MemoryRecord,
        *,
        reason: Optional[str],
        merged_into: Optional[str] = None,
    ) -> MemoryRecord:
        record.status = "DELETED"
        record.forgotten_at = utc_now_iso()
        record.forget_reason = reason
        if merged_into is not None:
            record.merged_into = merged_into
        record.updated_at = utc_now_iso()
        self.session.flush()
        return record
