"""Persistence for extraction candidates."""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.models import MemoryCandidate, MemorySource


class CandidateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, candidate: MemoryCandidate) -> MemoryCandidate:
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def get_by_id(self, candidate_id: str) -> Optional[MemoryCandidate]:
        return self.session.get(MemoryCandidate, candidate_id)

    def list(
        self,
        *,
        status: Optional[str] = None,
        memory_type: Optional[str] = None,
        conversation_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MemoryCandidate], int]:
        conditions = []
        if status is not None:
            conditions.append(MemoryCandidate.status == status)
        if memory_type is not None:
            conditions.append(MemoryCandidate.memory_type == memory_type)
        if conversation_id is not None:
            conditions.append(MemoryCandidate.conversation_id == conversation_id)

        total = int(
            self.session.execute(
                select(func.count()).select_from(MemoryCandidate).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(MemoryCandidate)
                .where(*conditions)
                .order_by(MemoryCandidate.extracted_at.desc(), MemoryCandidate.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def mark_decided(
        self,
        candidate: MemoryCandidate,
        *,
        decided_at: str,
        decided_by: str,
        status: str,
        reject_reason: Optional[str] = None,
    ) -> MemoryCandidate:
        candidate.status = status
        candidate.decided_at = decided_at
        candidate.decided_by = decided_by
        if reject_reason is not None:
            candidate.reject_reason = reject_reason
        self.session.flush()
        return candidate

    def source_exists(self, source_id: str) -> bool:
        return self.session.get(MemorySource, source_id) is not None

    def all_contents(self) -> List[Tuple[str, str]]:
        rows = self.session.execute(
            select(MemoryCandidate.id, MemoryCandidate.content)
        ).all()
        return [(row.id, row.content or "") for row in rows]
