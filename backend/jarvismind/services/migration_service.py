"""JME-5.8 JSON export/import with validation and duplicate handling.

SQLite stays authoritative: import writes everything inside one transaction
and rolls back on any error, so a bad file can never leave the database half
migrated. Vector points are (re)built after the commit and are rebuildable.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..core.dedup import is_duplicate
from ..core.errors import ValidationError
from ..core.lifecycle_policy import (
    AUTH_NOT_VERIFIED,
    AUTHENTICATION_LEVEL,
    SERVER_ACTOR_LABEL,
    identity_fields,
)
from ..core.security_gate import (
    RULESET_VERSION,
    UnsafeMetadataError,
    iter_metadata_strings,
    safe_hit_metadata,
    scan_credentials,
)
from ..core.search import segment_for_index
from ..core.timeutils import utc_now_iso
from ..database.connection import DatabaseManager
from ..database.models import (
    MemoryArchiveRecord,
    MemoryCorrection,
    MemoryLifecycleReview,
    MemoryMerge,
    MemoryRecord,
    MemorySource,
    MemorySummary,
)
from ..repositories.lifecycle_repository import LifecycleRepository
from ..repositories.memory_repository import MemoryRepository
from ..repositories.security_event_repository import SecurityEventRepository
from ..schemas.memory import MemoryStatus, MemoryType
from .memory_service import MemoryService

logger = logging.getLogger("jme.migration")

EXPORT_FORMAT = "jme-export"
SUPPORTED_SCHEMA_VERSION = 5

#: JW-02: document sections refused outright on import.
#:
#: The dividing line is "pending work item" vs "assertion that a human acted":
#:
#: * ``summaries`` / ``corrections`` / ``merges`` are artifacts with a PENDING
#:   state. They are imported, but FORCED to PENDING (see
#:   ``FORCE_PENDING_ON_IMPORT``), so a document can restore the work queue but
#:   cannot claim any of it was already approved.
#: * ``archive_records`` / ``lifecycle_reviews`` have no PENDING state: each row
#:   asserts that a lifecycle event already happened (this memory was archived,
#:   this expiry was reviewed). There is no safe normalisation for those, so they
#:   are refused and reported.
IMPORT_FORBIDDEN_SECTIONS: tuple = (
    "archive_records",
    "lifecycle_reviews",
)

#: Sections imported with their status forced back to PENDING.
FORCE_PENDING_ON_IMPORT: tuple = (
    "summaries",
    "corrections",
    "merges",
)


def _loads(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _dumps(value: Any) -> Optional[str]:
    if None in (value,):
        return None
    return json.dumps(value, ensure_ascii=False)


class MigrationService:
    def __init__(
        self,
        manager: DatabaseManager,
        *,
        memory_service: Optional[MemoryService] = None,
    ) -> None:
        self.manager = manager
        self.memory_service = memory_service or MemoryService(manager)

    def _exact_tokens(self) -> Tuple[str, ...]:
        settings = self.manager.settings
        return tuple(
            token
            for token in (settings.api_token, settings.llm_api_key)
            if token
        )

    #: Fields that JW-02 drops before importing a pending-state artifact.
    #:
    #: They are identity assertions ("who confirmed this") that the import path
    #: never preserves, and they hold the SERVER label, whose literal text
    #: contains "api-token". Scanning them first would make the credential gate
    #: reject a perfectly clean summary that this code then discards the field
    #: from anyway - the row would be skipped for a value it never stores.
    DROPPED_BEFORE_SCAN: tuple = (
        "confirmed_by",
        "rejected_by",
        "resolved_by",
        "confirmed_at",
        "rejected_at",
        "resolved_at",
    )

    def scan_import_row(self, row: Dict[str, Any]) -> list:
        """003-B1: scan EVERY string in an imported row, including nested
        metadata and the serialized form. Returns hits only (never values).

        Unscannable metadata shapes are treated as a hit (fail-closed) so the
        row is skipped rather than written unchecked.

        JW-02: identity-assertion fields are removed BEFORE the scan, because the
        import path never persists them (see ``DROPPED_BEFORE_SCAN``). The scan
        therefore only rejects a row for content it would actually store.
        """
        scanned = {
            key: value
            for key, value in row.items()
            if key not in self.DROPPED_BEFORE_SCAN
        }
        hits = []
        exact = self._exact_tokens()
        serialized = _dumps(scanned)
        hits.extend(scan_credentials(serialized, exact_tokens=exact))
        metadata = scanned.get("metadata")
        if metadata is not None:
            try:
                for text in iter_metadata_strings(metadata):
                    hits.extend(scan_credentials(text, exact_tokens=exact))
            except UnsafeMetadataError:
                # Represent the refusal as a synthetic, value-free marker hit.
                from ..core.sensitive import SensitiveHit

                hits.append(SensitiveHit("unsafe_metadata", 0, 0, ""))
        return hits

    # ------------------------------------------------------------------ export
    def export_document(
        self,
        *,
        memory_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        base = self.memory_service.export_memories(
            memory_type=memory_type, status=status
        )
        with self.manager.session() as session:
            lifecycle = LifecycleRepository(session)
            summaries, _ = lifecycle.list_summaries(page=1, page_size=1_000_000)
            corrections, _ = lifecycle.list_corrections(page=1, page_size=1_000_000)
            merges, _ = lifecycle.list_merges(page=1, page_size=1_000_000)
            archives, _ = lifecycle.list_archive_records(page=1, page_size=1_000_000)
            reviews, _ = lifecycle.list_reviews(page=1, page_size=1_000_000)

        def summary_doc(row: MemorySummary) -> Dict[str, Any]:
            return {
                "id": row.id,
                "title": row.title,
                "content": row.content,
                "summary_type": row.summary_type,
                "memory_type": row.memory_type,
                "memory_ids": _loads(row.memory_ids_json) or [],
                "source_ids": _loads(row.source_ids_json) or [],
                "status": row.status,
                "confidence": row.confidence,
                "engine": row.engine,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "confirmed_at": row.confirmed_at,
                "confirmed_by": row.confirmed_by,
                "rejected_at": row.rejected_at,
                "rejected_by": row.rejected_by,
                "reject_reason": row.reject_reason,
                "metadata": _loads(row.metadata_json),
            }

        def correction_doc(row: MemoryCorrection) -> Dict[str, Any]:
            return {
                "id": row.id,
                "memory_id_a": row.memory_id_a,
                "memory_id_b": row.memory_id_b,
                "detected_at": row.detected_at,
                "status": row.status,
                "chosen_memory_id": row.chosen_memory_id,
                "corrected_content": row.corrected_content,
                "reason": row.reason,
                "source": row.source,
                "created_at": row.created_at,
                "resolved_at": row.resolved_at,
                "resolved_by": row.resolved_by,
                "before_state": _loads(row.before_state_json),
                "after_state": _loads(row.after_state_json),
                "metadata": _loads(row.metadata_json),
            }

        def merge_doc(row: MemoryMerge) -> Dict[str, Any]:
            return {
                "id": row.id,
                "status": row.status,
                "target_memory_id": row.target_memory_id,
                "merged_memory_ids": _loads(row.merged_memory_ids_json) or [],
                "new_content": row.new_content,
                "new_summary": row.new_summary,
                "reason": row.reason,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "resolved_at": row.resolved_at,
                "resolved_by": row.resolved_by,
                "metadata": _loads(row.metadata_json),
            }

        memories = []
        for record in base["memories"]:
            memories.append(dict(record))

        document = {
            "format": EXPORT_FORMAT,
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "exported_at": utc_now_iso(),
            "counts": {
                "memories": len(memories),
                "sources": len(base["sources"]),
                "summaries": len(summaries),
                "corrections": len(corrections),
                "merges": len(merges),
                "archive_records": len(archives),
                "lifecycle_reviews": len(reviews),
            },
            "memories": memories,
            "sources": base["sources"],
            "summaries": [summary_doc(row) for row in summaries],
            "corrections": [correction_doc(row) for row in corrections],
            "merges": [merge_doc(row) for row in merges],
            "archive_records": [
                {
                    "id": row.id,
                    "memory_id": row.memory_id,
                    "reason": row.reason,
                    "source": row.source,
                    "previous_status": row.previous_status,
                    "archived_at": row.archived_at,
                    "restored_at": row.restored_at,
                    "restored_by": row.restored_by,
                    "metadata": _loads(row.metadata_json),
                }
                for row in archives
            ],
            "lifecycle_reviews": [
                {
                    "id": row.id,
                    "memory_id": row.memory_id,
                    "review_type": row.review_type,
                    "action": row.action,
                    "previous_expires_at": row.previous_expires_at,
                    "new_expires_at": row.new_expires_at,
                    "note": row.note,
                    "actor": row.actor,
                    "created_at": row.created_at,
                }
                for row in reviews
            ],
        }
        return document

    # ------------------------------------------------------------------ import
    def _validate(self, document: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(document, dict):
            return ["document must be a JSON object"]
        memories = document.get("memories")
        if not isinstance(memories, list):
            errors.append("'memories' must be a list")
            return errors
        version = document.get("schema_version")
        if version is not None and (not isinstance(version, int) or version < 1):
            errors.append("'schema_version' must be a positive integer")
        for index, item in enumerate(memories):
            if not isinstance(item, dict):
                errors.append(f"memories[{index}] must be an object")
                continue
            if not isinstance(item.get("id"), str) or not item.get("id"):
                errors.append(f"memories[{index}].id is required")
            if item.get("memory_type") not in {t.value for t in MemoryType}:
                errors.append(f"memories[{index}].memory_type is invalid")
            if not isinstance(item.get("content"), str) or not item.get("content"):
                errors.append(f"memories[{index}].content is required")
            status = item.get("status", "ACTIVE")
            if status not in {s.value for s in MemoryStatus}:
                errors.append(f"memories[{index}].status is invalid")
        return errors

    def preview_import(self, document: Any) -> Dict[str, Any]:
        errors = self._validate(document)
        preview = {
            "valid": not errors,
            "errors": errors,
            "format": document.get("format") if isinstance(document, dict) else None,
            "schema_version": (
                document.get("schema_version") if isinstance(document, dict) else None
            ),
            "counts": {},
            "duplicates_by_id": [],
            "duplicates_by_content": [],
            "would_import": 0,
            "would_skip": 0,
        }
        if errors:
            return preview
        memories = document.get("memories") or []
        sources = document.get("sources") or []
        with self.manager.session() as session:
            existing = {
                row.id: row.content for row in session.query(MemoryRecord).all()
            }
            existing_sources = {
                row.id for row in session.query(MemorySource.id).all()
            }
        duplicate_ids: List[str] = []
        duplicate_content: List[str] = []
        would_import = 0
        for item in memories:
            if item["id"] in existing:
                duplicate_ids.append(item["id"])
                continue
            content = item.get("content") or ""
            if any(is_duplicate(content, old) for old in existing.values()):
                duplicate_content.append(item["id"])
                continue
            would_import += 1
        preview["counts"] = {
            "memories": len(memories),
            "sources": len(sources),
            "summaries": len(document.get("summaries") or []),
            "corrections": len(document.get("corrections") or []),
            "merges": len(document.get("merges") or []),
            "archive_records": len(document.get("archive_records") or []),
            "lifecycle_reviews": len(document.get("lifecycle_reviews") or []),
        }
        preview["duplicates_by_id"] = duplicate_ids
        preview["duplicates_by_content"] = duplicate_content
        preview["would_import"] = would_import
        preview["would_skip"] = len(duplicate_ids) + len(duplicate_content)
        return preview

    def import_document(
        self,
        document: Any,
        *,
        actor: str = "user",
        skip_duplicates: bool = True,
    ) -> Dict[str, Any]:
        """P1-1: authorise at the SERVICE ENTRY (no open session yet).

        JW-02: ``actor`` is a CLIENT CLAIM. It is never written to an identity
        column; ``SERVER_ACTOR_LABEL`` is, and the claim is stored beside the
        verification result. The switch check happens at the route layer so the
        refusal produces the documented 403 before any parsing work.
        """
        from ..governance.integration import authorized_operation

        with authorized_operation(
            manager=self.manager,
            operation="MEMORY_CREATE",
            object_ref="IMPORT",
            trigger="MIGRATION_SERVICE",
        ):
            return self._import_document_impl(
                document, actor=actor, skip_duplicates=skip_duplicates
            )

    def _import_document_impl(
        self,
        document: Any,
        *,
        actor: str,
        skip_duplicates: bool,
    ) -> Dict[str, Any]:
        errors = self._validate(document)
        if errors:
            raise ValidationError(
                "Import document is invalid", detail={"errors": errors}
            )
        memories = document.get("memories") or []
        sources = document.get("sources") or []
        started = utc_now_iso()
        result: Dict[str, Any] = {
            "imported_memories": 0,
            "imported_sources": 0,
            "imported_summaries": 0,
            "imported_corrections": 0,
            "imported_merges": 0,
            "imported_archive_records": 0,
            "imported_lifecycle_reviews": 0,
            "skipped_duplicates": 0,
            "skipped_credentials": 0,
            "skipped_orphans": 0,
            # JW-02: statuses the document asked for that were normalised.
            "status_forced_to_active": 0,
            "status_forced_to_pending": 0,
            # JW-02: document sections refused because they assert a completed
            # lifecycle event instead of describing pending work.
            "rejected_sections": [],
            "vector_indexed": 0,
            "actor": SERVER_ACTOR_LABEL,
            "client_actor_claim": actor or "",
            "authentication_level": AUTHENTICATION_LEVEL,
            "authorization_verification_result": AUTH_NOT_VERIFIED,
            "started_at": started,
            "finished_at": None,
        }

        # JW-02: refuse the sections that assert a completed lifecycle event.
        # They have no PENDING state, so there is no safe way to import them:
        # a document could otherwise declare "this memory was archived" or
        # "this expiry was reviewed" with no approval and no audit chain.
        # Pending-state sections (summaries/corrections/merges) ARE imported but
        # normalised to PENDING below, so a document can restore the work queue
        # without being able to claim any of it was approved.
        for section in IMPORT_FORBIDDEN_SECTIONS:
            if document.get(section):
                result["rejected_sections"].append(section)
        if result["rejected_sections"]:
            logger.warning(
                "Import refused completed-lifecycle sections: %s",
                ", ".join(result["rejected_sections"]),
            )

        # ---- One transaction for every SQLite write. Any error rolls back. ----
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            lifecycle = LifecycleRepository(session)
            existing_ids = {row.id for row in session.query(MemoryRecord.id).all()}
            existing_contents = [
                row.content or "" for row in session.query(MemoryRecord.content).all()
            ]
            existing_sources = {row.id for row in session.query(MemorySource.id).all()}
            existing_summary_ids = {
                row.id for row in session.query(MemorySummary.id).all()
            }
            existing_correction_ids = {
                row.id for row in session.query(MemoryCorrection.id).all()
            }
            existing_merge_ids = {
                row.id for row in session.query(MemoryMerge.id).all()
            }
            existing_archive_ids = {
                row.id for row in session.query(MemoryArchiveRecord.id).all()
            }
            existing_review_ids = {
                row.id for row in session.query(MemoryLifecycleReview.id).all()
            }

            for src in sources:
                source_id = src.get("id")
                if not source_id or source_id in existing_sources:
                    continue
                now = utc_now_iso()
                session.add(
                    MemorySource(
                        id=source_id,
                        source_type=src.get("source_type") or "IMPORT",
                        source_name=src.get("source_name") or source_id,
                        uri=src.get("uri"),
                        created_at=src.get("created_at") or now,
                        updated_at=now,
                    )
                )
                existing_sources.add(source_id)
                result["imported_sources"] += 1
            # Flush sources before inserting memories so the SQLite foreign key
            # on memory_records.source_id is satisfied regardless of flush order.
            session.flush()

            for item in memories:
                memory_id = item["id"]
                if memory_id in existing_ids:
                    result["skipped_duplicates"] += 1
                    continue
                content = item.get("content") or ""
                # 003-B1: scan the WHOLE row - content, summary, nested
                # metadata, serialized form and exact live secrets. Import is a
                # formal-memory write path; a hit skips the row (never writes
                # it) and records only de-identified BLOCK metadata.
                credential_hits = self.scan_import_row(item)
                if credential_hits:
                    metadata = safe_hit_metadata(credential_hits)
                    SecurityEventRepository(session).record_block_event(
                        conversation_id="import",
                        message_id="",
                        turn_id=f"import:{memory_id}",
                        category=metadata["categories"][0],
                        hit_length=metadata["max_hit_length"],
                        ruleset_version=RULESET_VERSION,
                    )
                    result["skipped_credentials"] += 1
                    continue
                if skip_duplicates and any(
                    is_duplicate(content, old) for old in existing_contents
                ):
                    result["skipped_duplicates"] += 1
                    continue
                now = utc_now_iso()
                created = item.get("created_at") or now
                updated = item.get("updated_at") or created
                source_id = item.get("source_id")
                if source_id and source_id not in existing_sources:
                    source_id = None
                    result["skipped_orphans"] += 1
                # JW-02: an import may only produce live memories. DELETED /
                # MERGED / ARCHIVED are lifecycle outcomes reachable solely
                # through the audited flows (forget approval, merge confirmation,
                # archive), so a document cannot assert them.
                forced_status = False
                imported_status = item.get("status", MemoryStatus.ACTIVE.value)
                if imported_status != MemoryStatus.ACTIVE.value:
                    forced_status = True
                    imported_status = MemoryStatus.ACTIVE.value
                record = MemoryRecord(
                    id=memory_id,
                    memory_type=item["memory_type"],
                    content=content,
                    summary=item.get("summary"),
                    importance=float(item.get("importance", 5.0)),
                    confidence=float(item.get("confidence", 0.5)),
                    status=imported_status,
                    source_id=source_id,
                    created_at=created,
                    updated_at=updated,
                    accessed_at=item.get("accessed_at"),
                    expires_at=item.get("expires_at"),
                    metadata_json=_dumps(item.get("metadata")),
                    search_text=segment_for_index(
                        f"{content} {item.get('summary') or ''}"
                    ),
                    vector_status="PENDING",
                    # Lifecycle bookkeeping is DERIVED state, never accepted from
                    # a document: a forged forgotten_at / merged_into would make a
                    # live memory look as if the user had already forgotten it.
                    forgotten_at=None,
                    forget_reason=None,
                    merged_into=None,
                )
                memory_repo.create(record)
                existing_ids.add(memory_id)
                existing_contents.append(content)
                memory_repo.add_audit(
                    audit_id=uuid.uuid4().hex,
                    memory_id=memory_id,
                    action="IMPORT",
                    # JW-02: the actor column carries the SERVER label; the
                    # client's string is only a claim and lives beside the
                    # verification result.
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "schema_version": document.get("schema_version"),
                        "status": record.status,
                        "status_forced_by_import_policy": forced_status,
                        **identity_fields(actor),
                    },
                )
                result["imported_memories"] += 1
                if forced_status:
                    result["status_forced_to_active"] += 1

            # ---- Pending-state lifecycle artifacts (JW-02) ----------------
            # Imported so a full export -> import round trip still restores the
            # work queue, but EVERY status is forced to PENDING: a document may
            # describe what still needs a decision, never claim a decision was
            # already made. accepted_at / *_by fields are dropped for the same
            # reason - they are identity assertions, not data.
            for row in document.get("summaries") or []:
                if self.scan_import_row(row):
                    result["skipped_credentials"] += 1
                    continue
                row_id = row.get("id")
                ids = row.get("memory_ids") or []
                if not row_id or row_id in existing_summary_ids or not ids:
                    result["skipped_orphans"] += 1
                    continue
                if any(item not in existing_ids for item in ids):
                    result["skipped_orphans"] += 1
                    continue
                now = utc_now_iso()
                if row.get("status") not in (None, "PENDING"):
                    result["status_forced_to_pending"] += 1
                lifecycle.create_summary(
                    MemorySummary(
                        id=row_id,
                        title=row.get("title") or "导入总结",
                        content=row.get("content") or "",
                        summary_type=row.get("summary_type") or "COLLECTION",
                        memory_type=row.get("memory_type"),
                        memory_ids_json=_dumps(ids),
                        source_ids_json=_dumps(row.get("source_ids") or []),
                        # Forced: never import a decided state.
                        status="PENDING",
                        confidence=float(row.get("confidence", 0.5)),
                        engine=row.get("engine") or "local",
                        created_at=row.get("created_at") or now,
                        updated_at=now,
                        confirmed_at=None,
                        confirmed_by=None,
                        rejected_at=None,
                        rejected_by=None,
                        reject_reason=None,
                        metadata_json=_dumps(row.get("metadata")),
                    )
                )
                existing_summary_ids.add(row_id)
                result["imported_summaries"] += 1

            for row in document.get("corrections") or []:
                if self.scan_import_row(row):
                    result["skipped_credentials"] += 1
                    continue
                row_id = row.get("id")
                a = row.get("memory_id_a")
                b = row.get("memory_id_b")
                if not row_id or row_id in existing_correction_ids or not a or not b:
                    result["skipped_orphans"] += 1
                    continue
                if a not in existing_ids or b not in existing_ids:
                    result["skipped_orphans"] += 1
                    continue
                now = utc_now_iso()
                if row.get("status") not in (None, "PENDING"):
                    result["status_forced_to_pending"] += 1
                lifecycle.create_correction(
                    MemoryCorrection(
                        id=row_id,
                        memory_id_a=a,
                        memory_id_b=b,
                        detected_at=row.get("detected_at") or now,
                        status="PENDING",
                        chosen_memory_id=None,
                        corrected_content=None,
                        reason=row.get("reason"),
                        source=row.get("source") or "import",
                        created_at=row.get("created_at") or now,
                        resolved_at=None,
                        resolved_by=None,
                        before_state_json=_dumps(row.get("before_state")),
                        after_state_json=None,
                        metadata_json=_dumps(row.get("metadata")),
                    )
                )
                existing_correction_ids.add(row_id)
                result["imported_corrections"] += 1

            for row in document.get("merges") or []:
                if self.scan_import_row(row):
                    result["skipped_credentials"] += 1
                    continue
                row_id = row.get("id")
                ids = row.get("merged_memory_ids") or []
                if not row_id or row_id in existing_merge_ids or not ids:
                    result["skipped_orphans"] += 1
                    continue
                if any(item not in existing_ids for item in ids):
                    result["skipped_orphans"] += 1
                    continue
                now = utc_now_iso()
                if row.get("status") not in (None, "PENDING"):
                    result["status_forced_to_pending"] += 1
                lifecycle.create_merge(
                    MemoryMerge(
                        id=row_id,
                        status="PENDING",
                        # A merge preview never has a target: the product does
                        # not exist until a user confirms it here.
                        target_memory_id=None,
                        merged_memory_ids_json=_dumps(ids),
                        new_content=row.get("new_content"),
                        new_summary=row.get("new_summary"),
                        reason=row.get("reason"),
                        created_at=row.get("created_at") or now,
                        updated_at=now,
                        resolved_at=None,
                        resolved_by=None,
                        metadata_json=_dumps(row.get("metadata")),
                    )
                )
                existing_merge_ids.add(row_id)
                result["imported_merges"] += 1

        # ---- Vector index rebuild (best effort, after the commit). ----
        if result["imported_memories"]:
            # Re-index every active record still marked PENDING: that covers
            # both newly imported rows and any pre-existing unindexed ones.
            from sqlalchemy import select

            with self.manager.session() as session:
                rows = list(
                    session.execute(
                        select(MemoryRecord).where(
                            MemoryRecord.status == "ACTIVE",
                            MemoryRecord.vector_status == "PENDING",
                        )
                    ).scalars().all()
                )
            for record in rows:
                try:
                    self.memory_service._index_record_safely(
                        record.id,
                        record.content,
                        record.summary,
                        record.memory_type,
                        record.status,
                    )
                    result["vector_indexed"] += 1
                except Exception as exc:
                    logger.warning(
                        "Post-import vector indexing failed for %s: %s",
                        record.id,
                        exc,
                    )

        result["finished_at"] = utc_now_iso()
        logger.info(
            "Imported %d memories (%d duplicates skipped), %d sources, "
            "%d lifecycle records",
            result["imported_memories"],
            result["skipped_duplicates"],
            result["imported_sources"],
            result["imported_summaries"]
            + result["imported_corrections"]
            + result["imported_merges"],
        )
        return result
