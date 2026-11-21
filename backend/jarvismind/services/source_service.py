"""Business logic for memory sources."""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

from ..core.errors import NotFoundError, ValidationError
from ..core.timeutils import utc_now_iso
from ..database.connection import DatabaseManager
from ..database.models import MemorySource
from ..repositories.source_repository import SourceRepository
from ..schemas.source import SourceCreate, SourceUpdate

ACTOR_SYSTEM = "system"


class SourceService:
    def __init__(self, manager: DatabaseManager) -> None:
        self.manager = manager

    def create(
        self, payload: SourceCreate, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemorySource:
        now = utc_now_iso()
        source = MemorySource(
            id=uuid.uuid4().hex,
            source_type=payload.source_type.value,
            source_name=payload.source_name,
            uri=payload.uri,
            created_at=now,
            updated_at=now,
        )
        with self.manager.session() as session:
            SourceRepository(session).create(source)
        return source

    def get(self, source_id: str) -> MemorySource:
        with self.manager.session() as session:
            source = SourceRepository(session).get_by_id(source_id)
            if source is None:
                raise NotFoundError(f"Memory source {source_id} not found")
        return source

    def list(
        self,
        *,
        source_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        sort_order: str = "desc",
    ) -> Tuple[List[MemorySource], int]:
        with self.manager.session() as session:
            return SourceRepository(session).list(
                source_type=source_type,
                page=page,
                page_size=page_size,
                sort_order=sort_order,
            )

    def update(
        self, source_id: str, payload: SourceUpdate, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemorySource:
        with self.manager.session() as session:
            repo = SourceRepository(session)
            source = repo.get_by_id(source_id)
            if source is None:
                raise NotFoundError(f"Memory source {source_id} not found")

            values: Dict[str, object] = {}
            changed = payload.model_fields_set
            if "source_type" in changed and payload.source_type is not None:
                values["source_type"] = payload.source_type.value
            if "source_name" in changed and payload.source_name is not None:
                values["source_name"] = payload.source_name
            if "uri" in changed:
                values["uri"] = payload.uri
            if not values:
                raise ValidationError("No updatable fields were provided")

            repo.update(source, values)
        return source

    def delete(self, source_id: str, actor: Optional[str] = ACTOR_SYSTEM) -> Dict[str, object]:
        with self.manager.session() as session:
            repo = SourceRepository(session)
            source = repo.get_by_id(source_id)
            if source is None:
                raise NotFoundError(f"Memory source {source_id} not found")
            affected = repo.count_memories(source_id)
            repo.delete(source)
        return {"id": source_id, "unlinked_memories": affected}
