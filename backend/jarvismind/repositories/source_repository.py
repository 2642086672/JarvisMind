"""Persistence for memory_sources."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.models import MemoryRecord, MemorySource


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, source: MemorySource) -> MemorySource:
        self.session.add(source)
        self.session.flush()
        return source

    def get_by_id(self, source_id: str) -> Optional[MemorySource]:
        return self.session.get(MemorySource, source_id)

    def list(
        self,
        *,
        source_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        sort_order: str = "desc",
    ) -> Tuple[List[MemorySource], int]:
        conditions = []
        if source_type is not None:
            conditions.append(MemorySource.source_type == source_type)

        total = int(
            self.session.execute(
                select(func.count()).select_from(MemorySource).where(*conditions)
            ).scalar()
            or 0
        )
        order = (
            MemorySource.created_at.asc()
            if sort_order == "asc"
            else MemorySource.created_at.desc()
        )
        rows = (
            self.session.execute(
                select(MemorySource)
                .where(*conditions)
                .order_by(order, MemorySource.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def update(
        self, source: MemorySource, values: Dict[str, Any]
    ) -> MemorySource:
        for key, value in values.items():
            setattr(source, key, value)
        source.updated_at = utc_now_iso()
        self.session.flush()
        return source

    def delete(self, source: MemorySource) -> None:
        # memory_records.source_id has ON DELETE SET NULL, so referenced
        # memories keep their rows and simply lose the link.
        self.session.delete(source)
        self.session.flush()

    def count_memories(self, source_id: str) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(MemoryRecord)
                .where(MemoryRecord.source_id == source_id)
            ).scalar()
            or 0
        )
