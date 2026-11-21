"""Business logic for memory records.

Services own transaction boundaries: every public method opens one session and
commits once. Database exceptions are never swallowed here; they propagate to
the API layer's exception handler.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .embedding import BaseEmbeddingProvider
    from .vector_store import VectorStore

logger = logging.getLogger("jme.memory_service")

from ..core.errors import NotFoundError, SearchUnavailableError, ValidationError
from ..core.security_gate import assert_writable
from ..core.search import build_match_expression, segment_for_index
from ..core.timeutils import utc_now_iso
from ..core.approved_protection import authorized_write
from ..database.connection import DatabaseManager
from ..database.models import MemoryRecord, MemorySource
from ..database.search_index import zh_index_available
from ..repositories.memory_repository import MemoryRepository
from ..schemas.memory import MemoryCreate, MemoryUpdate

ACTOR_SYSTEM = "system"

VALID_STATUS_TRANSITIONS = {
    "ACTIVE": {"ARCHIVED"},
    "ARCHIVED": {"ACTIVE"},
    "DELETED": set(),
    # Merged originals are terminal; only the merge operation may set it.
    "MERGED": set(),
}


def _new_id() -> str:
    return uuid.uuid4().hex


def _parse_json(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _serialize_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    if metadata is None:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata must be JSON-serializable") from exc


#: Columns captured in the before/after audit image. Kept to the fields whose
#: change is meaningful to a reviewer; derived bookkeeping is excluded because
#: it changes on every write and would only add noise.
_AUDIT_SNAPSHOT_FIELDS = (
    "content",
    "summary",
    "status",
    "importance",
    "confidence",
    "memory_type",
    "expires_at",
    "metadata_json",
    "source_id",
    "forgotten_at",
    "forget_reason",
    "merged_into",
)


def _snapshot(record: MemoryRecord) -> Dict[str, Any]:
    """Pre/post image for the audit log (JW-03).

    ``content`` is truncated to a bounded prefix: the audit table is meant to
    show *that* and *what kind of* change happened, and duplicating a long
    memory body on every edit would grow the table without adding evidence
    (the authoritative value is still in ``memory_records``).
    """
    image: Dict[str, Any] = {}
    for field in _AUDIT_SNAPSHOT_FIELDS:
        value = getattr(record, field, None)
        if field == "content" and isinstance(value, str):
            image[field] = _content_digest(value)
        else:
            image[field] = value
    return image


def _content_digest(text: Optional[str]) -> Optional[str]:
    """Bounded, comparable representation of a memory body."""
    if text is None:
        return None
    if len(text) <= 120:
        return text
    return text[:117] + "..."


def require_scope_label(
    record: MemoryRecord,
    values: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Describe WHY this write was allowed, for the audit record.

    Returns ``None`` for rows that were never protected, so legacy audit rows
    keep their historical shape and an operator can tell at a glance which
    writes actually exercised JW-03 authority.

    JW-03-R1: the ``approved_request_id`` branch is gone. A protected write can
    no longer be authorised from this path at all, so the only authority a
    surviving label can report is an internal scope.
    """
    from ..core.approved_protection import (
        active_scopes,
        is_approved,
        protected_changes,
    )

    if not is_approved(record):
        return None
    changed = protected_changes(values, record=record)
    if not changed:
        return None
    return {
        "fields": sorted(changed),
        "authority": "internal_scope",
        "scopes": list(active_scopes()),
    }


class MemoryService:
    def __init__(
        self,
        manager: DatabaseManager,
        embedding_provider: Optional[BaseEmbeddingProvider] = None,
        vector_store: Optional[VectorStore] = None,
    ) -> None:
        self.manager = manager
        self.embedding = embedding_provider
        self.vector_store = vector_store

    def _exact_tokens(self) -> Tuple[str, ...]:
        """Live server secrets, reused (never copied or logged) for exact BLOCK.

        003-B1 P2-1: redact() replacing a live token is not a safety decision.
        A write containing the exact configured secret must be blocked too.
        """
        settings = self.manager.settings
        return tuple(
            token
            for token in (settings.api_token, settings.llm_api_key)
            if token
        )

    def _index_record_safely(
        self,
        record_id: str,
        content: str,
        summary: Optional[str],
        memory_type: str,
        status: str,
    ) -> None:
        """Incremental vector indexing hook. Safe against errors and never rolls back SQLite."""
        if not self.embedding or not self.vector_store:
            return
        if not self.embedding.is_available or not self.vector_store.is_ready:
            return

        if status == "ACTIVE":
            try:
                text_to_embed = f"{content} {summary or ''}".strip()
                vec = self.embedding.embed_text(text_to_embed)
                payload = {
                    "memory_id": record_id,
                    "memory_type": memory_type,
                    "status": status,
                }
                self.vector_store.upsert_memory(record_id, vec, payload)
                with self.manager.session() as session:
                    MemoryRepository(session).update_vector_status(
                        record_id, "INDEXED", utc_now_iso()
                    )
            except Exception as exc:
                logger.warning("Incremental vector indexing failed for %s: %s", record_id, exc)
                try:
                    with self.manager.session() as session:
                        MemoryRepository(session).update_vector_status(
                            record_id, "FAILED", utc_now_iso()
                        )
                except Exception:
                    pass
        else:
            # ARCHIVED / MERGED / DELETED: remove from active vector search and
            # record the removal so SQLite never claims the point is still
            # INDEXED after the Qdrant point is gone (JME-5 issue A).
            try:
                self.vector_store.delete_memory(record_id)
                with self.manager.session() as session:
                    MemoryRepository(session).update_vector_status(
                        record_id, "REMOVED", utc_now_iso()
                    )
            except Exception as exc:
                logger.warning("Failed to remove vector for %s: %s", record_id, exc)

    def create(
        self, payload: MemoryCreate, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemoryRecord:
        """P1: wrap every original-memory write in unified governance."""
        # Deliberate in-function import: importing app.governance at module level
        # would create a cycle (governance/__init__ -> write_governor -> here).
        from ..governance.integration import OP_MEMORY_CREATE, authorized_operation

        with authorized_operation(
            manager=self.manager,
            operation=OP_MEMORY_CREATE,
            object_ref="NEW",
            trigger="MEMORY_SERVICE",
        ):
            return self._create_impl(payload, actor)

    def _create_impl(
        self, payload: MemoryCreate, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemoryRecord:
        # 003-B1: the gate covers content, summary, the structured metadata
        # object AND its serialized form (so split/encoded values are scanned),
        # plus exactly-matched live server secrets. The ORM before_insert gate
        # is the final backstop for any caller that skips this service.
        metadata_json = _serialize_metadata(payload.metadata)
        assert_writable(
            payload.content,
            summary=payload.summary,
            metadata=payload.metadata,
            metadata_text=metadata_json,
            exact_tokens=self._exact_tokens(),
            field="memory_records",
        )
        if payload.status.value == "MERGED":
            raise ValidationError(
                "MERGED status can only be assigned by the merge operation"
            )
        record_id = _new_id()
        now = utc_now_iso()
        record = MemoryRecord(
            id=record_id,
            memory_type=payload.memory_type.value,
            content=payload.content,
            summary=payload.summary,
            importance=payload.importance,
            confidence=payload.confidence,
            status=payload.status.value,
            source_id=payload.source_id,
            created_at=now,
            updated_at=now,
            accessed_at=None,
            expires_at=payload.expires_at,
            metadata_json=metadata_json,
        )
        record.search_text = segment_for_index(f"{record.content} {record.summary or ''}")
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            if record.source_id and not repo.source_exists(record.source_id):
                raise ValidationError(
                    f"Memory source {record.source_id} does not exist"
                )
            repo.create(record)
            repo.add_audit(
                audit_id=_new_id(),
                memory_id=record_id,
                action="CREATE",
                actor=actor,
                detail={"memory_type": record.memory_type, "status": record.status},
            )
        self._index_record_safely(
            record.id, record.content, record.summary, record.memory_type, record.status
        )
        return record

    def get(
        self, memory_id: str, *, touch: bool = True
    ) -> MemoryRecord:
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            record = repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if touch:
                repo.touch_accessed(record)
        return record

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
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            return repo.list(
                memory_type=memory_type,
                status=status,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
            )

    def update(
        self,
        memory_id: str,
        payload: MemoryUpdate,
        actor: Optional[str] = ACTOR_SYSTEM,
    ) -> MemoryRecord:
        """P1: wrap every original-memory write in unified governance.

        JW-03-R1: this method CANNOT authorise a protected write on an approved
        memory. The ``approved_modification_request_id`` escape hatch was
        removed together with the PATCH bridge it served: an approved memory's
        protected fields are only ever written by
        ``ModificationRequestService.approve``, which applies the values frozen
        into the request. Keeping a credentialed parameter here would have left
        a reachable path that bypasses that guarantee.
        """
        from ..governance.integration import OP_MEMORY_UPDATE, authorized_operation

        with authorized_operation(
            manager=self.manager,
            operation=OP_MEMORY_UPDATE,
            object_ref=memory_id,
            trigger="MEMORY_SERVICE",
        ):
            return self._update_impl(memory_id, payload, actor)

    def _update_impl(
        self,
        memory_id: str,
        payload: MemoryUpdate,
        actor: Optional[str] = ACTOR_SYSTEM,
    ) -> MemoryRecord:
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            record = repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ValidationError("Deleted memories cannot be modified")

            values: Dict[str, Any] = {}
            changed = payload.model_fields_set
            if "content" in changed:
                values["content"] = payload.content
            if "summary" in changed:
                values["summary"] = payload.summary
            if "importance" in changed:
                values["importance"] = payload.importance
            if "confidence" in changed:
                values["confidence"] = payload.confidence
            status_noop = False
            if "status" in changed and payload.status is not None:
                target = payload.status.value
                if target == "DELETED":
                    raise ValidationError(
                        "Use DELETE to remove a memory; status cannot be set to DELETED here"
                    )
                if target == "MERGED":
                    raise ValidationError(
                        "Use the merge operation to merge memories; "
                        "status cannot be set to MERGED here"
                    )
                if target == record.status:
                    # Re-asserting the current status is a harmless no-op.
                    status_noop = True
                elif target not in VALID_STATUS_TRANSITIONS.get(record.status, set()):
                    raise ValidationError(
                        f"Cannot change status from {record.status} to {target}"
                    )
                else:
                    values["status"] = target
            if "source_id" in changed:
                if payload.source_id and not repo.source_exists(payload.source_id):
                    raise ValidationError(
                        f"Memory source {payload.source_id} does not exist"
                    )
                values["source_id"] = payload.source_id
            if "expires_at" in changed:
                values["expires_at"] = payload.expires_at
            if "metadata" in changed:
                values["metadata_json"] = _serialize_metadata(payload.metadata)

            # 003-B1: scan the FINAL value set, including a replaced metadata
            # object AND its serialized form. A PATCH that only changes
            # metadata is still a formal-memory write and must be blocked.
            assert_writable(
                payload.content if "content" in changed else None,
                summary=payload.summary if "summary" in changed else None,
                metadata=payload.metadata if "metadata" in changed else None,
                metadata_text=values.get("metadata_json"),
                exact_tokens=self._exact_tokens(),
                field="memory_records",
            )

            if not values:
                if status_noop:
                    return record
                raise ValidationError("No updatable fields were provided")

            # JW-03: an approved memory (source_candidate_id set) may only have
            # JW-03-R1: an approved memory's protected fields are NOT writable
            # from here at all. There is no longer a parameter that could
            # authorise it - the only path is ModificationRequestService.approve,
            # which writes the frozen proposed_changes. This call therefore
            # always runs with NO approved request id, so an approved row whose
            # protected fields would move is refused unconditionally.
            with authorized_write(
                record,
                values,
                reason="approved_modification",
                operation="PATCH /api/v1/memories/{id}",
            ):
                # The FTS index follows search_text, so keep it in sync with the
                # fields it is derived from.
                if "content" in values or "summary" in values:
                    merged = values.get("content", record.content)
                    merged_summary = values.get("summary", record.summary)
                    values["search_text"] = segment_for_index(
                        f"{merged} {merged_summary or ''}"
                    )

                previous = _snapshot(record)
                scope_label = require_scope_label(record, values)
                repo.update(record, values)
                repo.add_audit(
                    audit_id=_new_id(),
                    memory_id=memory_id,
                    action="UPDATE",
                    actor=actor,
                    detail={
                        "fields": sorted(values.keys()),
                        # JW-03: the pre-image is recorded whenever a protected
                        # field moved, so an operator can reconstruct what the
                        # approved memory said before this write. Bookkeeping-only
                        # updates stay lean.
                        "before": previous,
                        "after": _snapshot(record),
                        "protected": scope_label,
                    },
                )
        self._index_record_safely(
            record.id, record.content, record.summary, record.memory_type, record.status
        )
        return record

    def delete(
        self, memory_id: str, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemoryRecord:
        """P1: wrap every original-memory write in unified governance."""
        from ..governance.integration import OP_MEMORY_DELETE, authorized_operation

        with authorized_operation(
            manager=self.manager,
            operation=OP_MEMORY_DELETE,
            object_ref=memory_id,
            trigger="MEMORY_SERVICE",
        ):
            return self._delete_impl(memory_id, actor)

    def _delete_impl(
        self, memory_id: str, actor: Optional[str] = ACTOR_SYSTEM
    ) -> MemoryRecord:
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            record = repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ValidationError("Memory is already deleted")

            repo.update(record, {"status": "DELETED"})
            repo.add_audit(
                audit_id=_new_id(),
                memory_id=memory_id,
                action="SOFT_DELETE",
                actor=actor,
                detail={"previous_status": record.status},
            )
        self._index_record_safely(
            record.id, record.content, record.summary, record.memory_type, "DELETED"
        )
        return record

    def search(
        self,
        *,
        query: str,
        memory_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        try:
            expression = build_match_expression(query)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not zh_index_available(self.manager):
            raise SearchUnavailableError(
                "SQLite FTS5 is not available in this build; search is disabled"
            )
        with self.manager.session() as session:
            return MemoryRepository(session).search(
                match=expression,
                memory_type=memory_type,
                status=status,
                page=page,
                page_size=page_size,
            )

    def audit_logs(
        self,
        memory_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_order: str = "desc",
    ) -> Tuple[List[Dict[str, Any]], int]:
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            record = repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            entries = list(repo.audit_trail(memory_id))

        if sort_order == "desc":
            entries.reverse()
        total = len(entries)
        start = (page - 1) * page_size
        page_entries = entries[start:start + page_size]
        results = [
            {
                "action": entry.action,
                "actor": entry.actor,
                "detail": _parse_json(entry.detail_json),
                "created_at": entry.created_at,
            }
            for entry in page_entries
        ]
        return results, total

    def export_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the JSON export document, including source relationships."""
        with self.manager.session() as session:
            records, _ = MemoryRepository(session).list(
                memory_type=memory_type,
                status=status,
                page=1,
                page_size=1_000_000,
                sort_by="created_at",
                sort_order="asc",
            )
            source_ids = {record.source_id for record in records if record.source_id}
            sources = []
            for source_id in sorted(source_ids):
                source = session.get(MemorySource, source_id)
                if source is None:
                    continue
                sources.append(
                    {
                        "id": source.id,
                        "source_type": source.source_type,
                        "source_name": source.source_name,
                        "uri": source.uri,
                        "created_at": source.created_at,
                        "updated_at": source.updated_at,
                    }
                )
            return {
                "memories": [
                    {
                        "id": record.id,
                        "memory_type": record.memory_type,
                        "content": record.content,
                        "summary": record.summary,
                        "importance": record.importance,
                        "confidence": record.confidence,
                        "status": record.status,
                        "source_id": record.source_id,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                        "accessed_at": record.accessed_at,
                        "expires_at": record.expires_at,
                        "metadata": _parse_json(record.metadata_json),
                    }
                    for record in records
                ],
                "sources": sources,
            }