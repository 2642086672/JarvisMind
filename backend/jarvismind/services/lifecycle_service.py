"""JME-5 memory lifecycle service.

Owns summaries/reflection, corrections, merges, expiry review, archive records,
and controlled forgetting. Every state-changing method writes an audit entry
and keeps SQLite (authoritative) and Qdrant (rebuildable index) in sync.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.errors import (
    AlreadyResolvedError,
    CorrectionTargetNotEditableError,
    MergeSourceNotMergeableError,
    NotFoundError,
    ValidationError,
)
from ..core.lifecycle_policy import (
    AUTH_NOT_VERIFIED,
    SERVER_ACTOR_LABEL,
    identity_fields,
)
from ..core.approved_protection import (
    authorized_write,
    internal_write,
    require_write_authority,
)
from ..core.search import segment_for_index
from ..core.timeutils import parse_iso, utc_now_iso
from ..database.connection import DatabaseManager
from ..database.models import (
    MemoryArchiveRecord,
    MemoryCorrection,
    MemoryLifecycleReview,
    MemoryMerge,
    MemoryRecord,
    MemorySummary,
)
from ..repositories.lifecycle_repository import LifecycleRepository
from ..repositories.memory_repository import MemoryRepository
from ..schemas.lifecycle import (
    CorrectionResponse,
    CorrectionStatus,
    ExpiryItem,
    MergePreviewResponse,
    MergeResponse,
    MergeStatus,
    ReviewStatus,
    SummaryResponse,
    SummaryType,
    archive_record_to_response,
    correction_to_response,
    merge_to_response,
    review_to_response,
    summary_to_response,
)
from ..schemas.memory import MemoryCreate, MemoryStatus, MemoryUpdate
from .memory_service import ACTOR_SYSTEM, MemoryService

logger = logging.getLogger("jme.lifecycle")

ACTOR_USER = "user"


def _new_id() -> str:
    return uuid.uuid4().hex


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


class LifecycleService:
    def __init__(
        self,
        manager: DatabaseManager,
        *,
        memory_service: Optional[MemoryService] = None,
        reflection_engine=None,
        settings=None,
    ) -> None:
        self.manager = manager
        self.memory_service = memory_service or MemoryService(manager)
        self.reflection = reflection_engine
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    def _audit(
        self,
        repo: MemoryRepository,
        *,
        memory_id: str,
        action: str,
        actor: Optional[str],
        detail: Dict[str, Any],
    ) -> None:
        repo.add_audit(
            audit_id=_new_id(),
            memory_id=memory_id,
            action=action,
            actor=actor,
            detail=detail,
        )

    def _records_by_ids(
        self, ids: Sequence[str], *, allow_deleted: bool = False
    ) -> List[MemoryRecord]:
        unique = list(dict.fromkeys(ids))
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            records = {record.id: record for record in repo.get_by_ids(unique)}
        missing = [item for item in unique if item not in records]
        if missing:
            raise NotFoundError(f"Memories not found: {', '.join(missing[:5])}")
        if not allow_deleted:
            bad = [item for item in unique if records[item].status == "DELETED"]
            if bad:
                raise ValidationError(
                    f"Deleted memories cannot be used: {', '.join(bad[:5])}"
                )
        return [records[item] for item in unique]

    def _remove_vector(self, memory_id: str) -> bool:
        """Delete the Qdrant point and record REMOVED. Never raises."""
        store = getattr(self.memory_service, "vector_store", None)
        if store is None:
            return False
        removed = False
        try:
            if store.is_ready:
                store.delete_memory(memory_id)
                removed = True
        except Exception as exc:
            logger.warning("Vector delete failed for %s: %s", memory_id, exc)
        try:
            with self.manager.session() as session:
                MemoryRepository(session).update_vector_status(
                    memory_id, "REMOVED", utc_now_iso()
                )
        except Exception:
            pass
        return removed

    def _reindex_vector(self, record: MemoryRecord) -> None:
        """Upsert the Qdrant point for an active record. Never raises."""
        service = self.memory_service
        embedding = getattr(service, "embedding", None)
        store = getattr(service, "vector_store", None)
        if embedding is None or store is None:
            return
        if not embedding.is_available or not store.is_ready:
            return
        try:
            vector = embedding.embed_text(
                f"{record.content} {record.summary or ''}".strip()
            )
            store.upsert_memory(
                record.id,
                vector,
                {
                    "memory_id": record.id,
                    "memory_type": record.memory_type,
                    "status": record.status,
                },
            )
            with self.manager.session() as session:
                MemoryRepository(session).update_vector_status(
                    record.id, "INDEXED", utc_now_iso()
                )
        except Exception as exc:
            logger.warning("Vector reindex failed for %s: %s", record.id, exc)

    # --------------------------------------------------------------- summaries
    def create_summary(
        self,
        memory_ids: Sequence[str],
        *,
        summary_type: SummaryType | str = SummaryType.COLLECTION,
        memory_type: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> SummaryResponse:
        summary_type_value = (
            summary_type.value if isinstance(summary_type, SummaryType) else str(summary_type)
        )
        if not memory_ids:
            raise ValidationError("At least one memory id is required")
        records = self._records_by_ids(memory_ids)
        if summary_type_value == SummaryType.CATEGORY.value:
            if memory_type is None:
                raise ValidationError("memory_type is required for CATEGORY summaries")
            records = [record for record in records if record.memory_type == memory_type]
            if not records:
                raise ValidationError(
                    f"No memories of type {memory_type} in the selected set"
                )
        if self.reflection is None:
            raise ValidationError("Reflection engine is not configured")

        draft = self.reflection.summarize(
            records,
            summary_type=summary_type_value,
            memory_type=memory_type,
        )
        now = utc_now_iso()
        summary = MemorySummary(
            id=_new_id(),
            title=draft.title,
            content=draft.content,
            summary_type=summary_type_value,
            memory_type=memory_type,
            memory_ids_json=_dumps(draft.memory_ids),
            source_ids_json=_dumps(draft.metadata.get("source_ids", [])),
            status=ReviewStatus.PENDING.value,
            confidence=draft.confidence,
            engine=draft.engine,
            created_at=now,
            updated_at=now,
            metadata_json=_dumps(
                {"source_ids": draft.metadata.get("source_ids", [])}
            ),
        )
        with self.manager.session() as session:
            LifecycleRepository(session).create_summary(summary)
            repo = MemoryRepository(session)
            for record_id in draft.memory_ids:
                self._audit(
                    repo,
                    memory_id=record_id,
                    action="SUMMARY_CREATE",
                    actor=actor,
                    detail={
                        "summary_id": summary.id,
                        "summary_type": summary_type_value,
                        "engine": summary.engine,
                    },
                )
        logger.info(
            "Created %s summary %s from %d memories (engine=%s)",
            summary_type_value,
            summary.id,
            len(draft.memory_ids),
            summary.engine,
        )
        return summary_to_response(summary)

    def list_summaries(
        self,
        *,
        status: Optional[str] = None,
        summary_type: Optional[str] = None,
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[SummaryResponse], int]:
        with self.manager.session() as session:
            rows, total = LifecycleRepository(session).list_summaries(
                status=status,
                summary_type=summary_type,
                memory_type=memory_type,
                page=page,
                page_size=page_size,
            )
        return [summary_to_response(row) for row in rows], total

    def get_summary(self, summary_id: str) -> SummaryResponse:
        with self.manager.session() as session:
            summary = LifecycleRepository(session).get_summary(summary_id)
        if summary is None:
            raise NotFoundError(f"Summary {summary_id} not found")
        return summary_to_response(summary)

    def review_summary(
        self,
        summary_id: str,
        *,
        decision: str,
        reason: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> SummaryResponse:
        """Approve or reject a pending summary.

        JW-02 rewrite. ``review`` is not a pure state flip: approving writes a NEW
        authoritative REFLECTION memory. The previous implementation flipped the
        summary to CONFIRMED in one transaction and then called
        ``memory_service.create`` in a second one, so a failure in the second left
        a summary marked CONFIRMED with no reflection memory behind it - and the
        retry was refused because the summary was no longer PENDING, making the
        inconsistency permanent.

        Now the status change, the REFLECTION memory, the link back to the summary
        and every audit row are written in ONE transaction. A failure rolls back
        all of it, the summary stays PENDING, and the user can retry.

        The decision itself is recorded server-side: ``confirmed_by`` /
        ``rejected_by`` carry the server label, and the client's actor string is
        kept only as a claim inside the audit detail.
        """
        action = decision.strip().lower()
        if action not in {"approve", "reject"}:
            raise ValidationError("decision must be 'approve' or 'reject'")

        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            summary = repo.get_summary(summary_id)
            if summary is None:
                raise NotFoundError(f"Summary {summary_id} not found")
            if summary.status != ReviewStatus.PENDING.value:
                # Idempotency: a second review of the same summary is refused
                # rather than re-executing (which would duplicate the REFLECTION
                # memory on the approve path).
                raise AlreadyResolvedError(
                    f"Summary is already {summary.status}; "
                    "only PENDING summaries can be reviewed",
                    detail={"reason": "summary_already_reviewed", "status": summary.status},
                )
            now = utc_now_iso()
            memory_repo = MemoryRepository(session)
            source_ids = _loads(summary.memory_ids_json) or []

            if action == "approve":
                product_id = _new_id()
                product = MemoryRecord(
                    id=product_id,
                    memory_type="REFLECTION",
                    content=summary.content,
                    summary=summary.title,
                    importance=6.0,
                    confidence=summary.confidence,
                    status=MemoryStatus.ACTIVE.value,
                    source_id=None,
                    created_at=now,
                    updated_at=now,
                    metadata_json=_dumps(
                        {
                            "summary_id": summary.id,
                            "summary_type": summary.summary_type,
                            "source_memory_ids": source_ids,
                            # NOTE: the server actor label must NOT be persisted
                            # into memory metadata. It contains the literal
                            # "api-token", which the 003-B1 credential gate
                            # (correctly) refuses to persist. The verification
                            # outcome is the useful fact here anyway; the full
                            # actor label lives in the audit trail, which is not
                            # credential-scanned.
                            "confirmed_by_verification": AUTH_NOT_VERIFIED,
                        }
                    ),
                    search_text=segment_for_index(
                        f"{summary.content} {summary.title or ''}"
                    ),
                    vector_status="PENDING",
                )
                memory_repo.create(product)
                repo.update_summary(
                    summary,
                    {
                        "status": ReviewStatus.CONFIRMED.value,
                        "confirmed_at": now,
                        "confirmed_by": SERVER_ACTOR_LABEL,
                    },
                )
                self._audit(
                    memory_repo,
                    memory_id=product_id,
                    action="SUMMARY_PRODUCT_CREATED",
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "summary_id": summary.id,
                        "summary_type": summary.summary_type,
                        **identity_fields(actor),
                    },
                )
                audit_action = "SUMMARY_CONFIRM"
            else:
                repo.update_summary(
                    summary,
                    {
                        "status": ReviewStatus.REJECTED.value,
                        "rejected_at": now,
                        "rejected_by": SERVER_ACTOR_LABEL,
                        "reject_reason": reason or "rejected by user",
                    },
                )
                audit_action = "SUMMARY_REJECT"

            for record_id in source_ids:
                self._audit(
                    memory_repo,
                    memory_id=record_id,
                    action=audit_action,
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "summary_id": summary.id,
                        "summary_type": summary.summary_type,
                        **identity_fields(actor),
                    },
                )

        # After commit: index the new reflection memory (rebuildable).
        if action == "approve":
            self._reindex_vector(product)
        return summary_to_response(summary)


    # ------------------------------------------------------------- corrections
    def detect_corrections(
        self,
        *,
        memory_type: Optional[str] = None,
        memory_ids: Optional[Sequence[str]] = None,
        actor: Optional[str] = ACTOR_SYSTEM,
    ) -> List[CorrectionResponse]:
        if memory_ids:
            records = self._records_by_ids(memory_ids)
        else:
            with self.manager.session() as session:
                records = MemoryRepository(session).get_all_active_records(
                    memory_type=memory_type
                )
        if len(records) < 2:
            return []
        if self.reflection is None:
            raise ValidationError("Reflection engine is not configured")
        drafts = self.reflection.detect_conflicts(records)

        created: List[CorrectionResponse] = []
        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            memory_repo = MemoryRepository(session)
            existing_pairs = {
                frozenset((item.memory_id_a, item.memory_id_b))
                for item in session.query(MemoryCorrection).all()
            }
            for draft in drafts:
                pair = frozenset((draft.memory_id_a, draft.memory_id_b))
                if pair in existing_pairs:
                    continue
                now = utc_now_iso()
                before = {}
                for record_id in (draft.memory_id_a, draft.memory_id_b):
                    record = memory_repo.get_by_id(record_id)
                    before[record_id] = (
                        {
                            "content": record.content,
                            "status": record.status,
                            "updated_at": record.updated_at,
                        }
                        if record
                        else None
                    )
                correction = repo.create_correction(
                    MemoryCorrection(
                        id=_new_id(),
                        memory_id_a=draft.memory_id_a,
                        memory_id_b=draft.memory_id_b,
                        detected_at=now,
                        status=CorrectionStatus.PENDING.value,
                        reason=draft.reason,
                        source=draft.engine,
                        created_at=now,
                        before_state_json=_dumps(before),
                        metadata_json=_dumps({"confidence": draft.confidence}),
                    )
                )
                self._audit(
                    memory_repo,
                    memory_id=draft.memory_id_a,
                    action="CORRECTION_DETECT",
                    actor=actor,
                    detail={
                        "correction_id": correction.id,
                        "other_memory_id": draft.memory_id_b,
                        "engine": draft.engine,
                    },
                )
                created.append(correction_to_response(correction))
        return created

    def list_corrections(
        self,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CorrectionResponse], int]:
        with self.manager.session() as session:
            rows, total = LifecycleRepository(session).list_corrections(
                status=status, page=page, page_size=page_size
            )
        return [correction_to_response(row) for row in rows], total

    def get_correction(self, correction_id: str) -> CorrectionResponse:
        with self.manager.session() as session:
            correction = LifecycleRepository(session).get_correction(correction_id)
        if correction is None:
            raise NotFoundError(f"Correction {correction_id} not found")
        return correction_to_response(correction)

    def resolve_correction(
        self,
        correction_id: str,
        *,
        chosen_memory_id: Optional[str] = None,
        corrected_content: Optional[str] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> CorrectionResponse:
        """Resolve a detected conflict, optionally rewriting the chosen memory.

        JW-02 rewrite. ``resolve`` has two effects: it records the decision on the
        correction row AND, when ``corrected_content`` is given, it rewrites an
        authoritative memory. The previous implementation applied the content
        change through ``memory_service.update`` in its own transaction and then
        recorded the decision in a second one. If the second transaction failed,
        the memory was already overwritten while the correction stayed PENDING -
        so the user would retry and overwrite it again, with no record of the
        first change beyond an UPDATE audit entry.

        Now the rewrite, the decision record and both audit rows share ONE
        transaction:

        * the chosen memory is re-read and must still be editable (not
          ``DELETED`` / ``MERGED``) - the old code let the update path decide, so
          a deleted target produced a 422 from a different layer after the
          correction row had already been read;
        * ``before_state_json`` captures the real prior content, taken inside the
          same transaction, so it can never describe a value that was already
          overwritten (the old code wrote the POST-update content as "before");
        * ``resolved_by`` carries the server label, the client string is a claim.

        ``chosen_memory_id=None`` means "record the decision without rewriting
        anything"; that path touches no authoritative content at all.
        """
        if corrected_content is not None and chosen_memory_id is None:
            raise ValidationError(
                "chosen_memory_id is required when corrected_content is provided"
            )

        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            memory_repo = MemoryRepository(session)
            correction = repo.get_correction(correction_id)
            if correction is None:
                raise NotFoundError(f"Correction {correction_id} not found")
            if correction.status != CorrectionStatus.PENDING.value:
                # Idempotency: a second resolve is refused instead of re-applying
                # the content rewrite.
                raise AlreadyResolvedError(
                    f"Correction is already {correction.status}",
                    detail={
                        "reason": "correction_already_resolved",
                        "status": correction.status,
                    },
                )
            if chosen_memory_id is not None and chosen_memory_id not in {
                correction.memory_id_a,
                correction.memory_id_b,
            }:
                raise ValidationError(
                    "chosen_memory_id must be one of the two memories in the conflict"
                )

            memory_id_a = correction.memory_id_a
            memory_id_b = correction.memory_id_b
            before = _loads(correction.before_state_json) or {}
            now = utc_now_iso()
            content_changed = False

            if corrected_content is not None:
                target = memory_repo.get_by_id(chosen_memory_id)
                if target is None:
                    raise NotFoundError(f"Memory {chosen_memory_id} not found")
                if target.status not in {"ACTIVE", "ARCHIVED"}:
                    raise CorrectionTargetNotEditableError(
                        f"Memory is {target.status} and cannot be corrected",
                        detail={"status": target.status},
                    )
                # Snapshot the REAL previous value before overwriting it.
                before[chosen_memory_id] = {
                    "content": target.content,
                    "status": target.status,
                    "updated_at": target.updated_at,
                }
                content_changed = target.content != corrected_content
                if content_changed:
                    # JW-03: rewriting an approved memory's body is protected.
                    # ``resolve`` IS the approval step for a detected conflict,
                    # so it opens the correction_resolve scope; the write goes
                    # through MemoryRepository.update so the repository-level
                    # gate sees it too (previously this was a bare attribute
                    # assignment + flush, invisible to every write choke point).
                    new_values = {
                        "content": corrected_content,
                        "search_text": segment_for_index(
                            f"{corrected_content} {target.summary or ''}"
                        ),
                    }
                    with internal_write(
                        "correction_resolve", operation="correction resolve"
                    ):
                        memory_repo.update(target, new_values)
                    self._audit(
                        memory_repo,
                        memory_id=chosen_memory_id,
                        action="UPDATE",
                        actor=SERVER_ACTOR_LABEL,
                        detail={
                            "reason": "correction_resolve",
                            "correction_id": correction_id,
                            **identity_fields(actor),
                        },
                    )

            # The losing memory is kept as-is: the decision records which one is
            # authoritative, but no original content is destroyed.
            repo.update_correction(
                correction,
                {
                    "status": CorrectionStatus.RESOLVED.value,
                    "chosen_memory_id": chosen_memory_id,
                    "corrected_content": corrected_content,
                    "reason": reason or "user confirmed the correct memory",
                    "resolved_at": now,
                    "resolved_by": SERVER_ACTOR_LABEL,
                    "after_state_json": _dumps(before),
                },
            )
            for record_id in (memory_id_a, memory_id_b):
                self._audit(
                    memory_repo,
                    memory_id=record_id,
                    action="CORRECTION_RESOLVE",
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "correction_id": correction_id,
                        "chosen_memory_id": chosen_memory_id,
                        "content_corrected": corrected_content is not None,
                        "content_changed": content_changed,
                        **identity_fields(actor),
                    },
                )

        # After commit: keep the vector index aligned with the new content.
        if corrected_content is not None and content_changed:
            try:
                self._reindex_vector(
                    self._records_by_ids([chosen_memory_id])[0]
                )
            except Exception:  # pragma: no cover - index is rebuildable
                logger.warning(
                    "Re-index after correction failed for %s", chosen_memory_id
                )
        return correction_to_response(correction)


    def reject_correction(
        self,
        correction_id: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> CorrectionResponse:
        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            correction = repo.get_correction(correction_id)
            if correction is None:
                raise NotFoundError(f"Correction {correction_id} not found")
            if correction.status != CorrectionStatus.PENDING.value:
                raise ValidationError(f"Correction is already {correction.status}")
            repo.update_correction(
                correction,
                {
                    "status": CorrectionStatus.REJECTED.value,
                    "reason": reason or "conflict rejected by user",
                    "resolved_at": utc_now_iso(),
                    "resolved_by": actor,
                },
            )
            memory_repo = MemoryRepository(session)
            for record_id in (correction.memory_id_a, correction.memory_id_b):
                self._audit(
                    memory_repo,
                    memory_id=record_id,
                    action="CORRECTION_REJECT",
                    actor=actor,
                    detail={"correction_id": correction_id},
                )
        return correction_to_response(correction)
    # ------------------------------------------------------------------ merges
    def preview_merge(
        self, memory_ids: Sequence[str], *, actor: Optional[str] = ACTOR_USER
    ) -> MergePreviewResponse:
        records = self._records_by_ids(memory_ids)
        if len(records) < 2:
            raise ValidationError("At least two memories are required to merge")
        if self.reflection is None:
            raise ValidationError("Reflection engine is not configured")
        draft = self.reflection.suggest_merge(records)
        now = utc_now_iso()
        merge = MemoryMerge(
            id=_new_id(),
            status=MergeStatus.PENDING.value,
            merged_memory_ids_json=_dumps([record.id for record in records]),
            new_content=draft.new_content,
            new_summary=draft.new_summary,
            reason=draft.reason,
            created_at=now,
            updated_at=now,
            metadata_json=_dumps({"engine": draft.engine, "actor": actor}),
        )
        with self.manager.session() as session:
            LifecycleRepository(session).create_merge(merge)
            memory_repo = MemoryRepository(session)
            for record in records:
                self._audit(
                    memory_repo,
                    memory_id=record.id,
                    action="MERGE_PREVIEW",
                    actor=actor,
                    detail={"merge_id": merge.id},
                )
        return MergePreviewResponse(
            memory_ids=[record.id for record in records],
            new_content=draft.new_content,
            new_summary=draft.new_summary,
            reason=draft.reason,
            engine=draft.engine,
            memory_type=draft.memory_type,
        )

    def confirm_merge(
        self,
        memory_ids: Sequence[str],
        *,
        new_content: Optional[str] = None,
        new_summary: Optional[str] = None,
        reason: Optional[str] = None,
        merge_id: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> MergeResponse:
        """Confirm a merge: create the product, retire the originals, audit.

        JW-02 rewrite. The previous implementation was three independent
        transactions ("create product", "mark originals + record merge", "drop
        vectors"), which produced two measured defects:

        1. **Partial write.** If the second transaction failed, the product was
           already committed as an ACTIVE authoritative memory while the
           originals stayed ACTIVE and the merge record stayed PENDING. Measured:
           product persisted, ``pm-a``/``pm-b`` still ACTIVE, merge PENDING.
        2. **Repeat confirmation.** Re-confirming with the same ``merge_id``
           succeeded and created a SECOND product, then repointed
           ``merged_into`` at the new one - two live products for one merge.

        Now every SQLite write happens inside ONE transaction:

        * the product row is inserted directly here (not via
          ``memory_service.create``, which opens its own session);
        * each original is re-read and must still be mergeable (``ACTIVE`` /
          ``ARCHIVED``) - an already ``MERGED`` or ``DELETED`` source is refused
          instead of being silently skipped;
        * when ``merge_id`` is supplied it must be ``PENDING``, and it is
          consumed via a conditional UPDATE so a second confirmation matches
          zero rows and is refused;
        * vector removal happens after the commit (index is rebuildable).

        Vector indexing for the product is attempted after the commit too, so a
        slow/failing embedding provider can never roll back a committed merge.
        """
        if self.reflection is None:
            raise ValidationError("Reflection engine is not configured")

        unique_ids = list(dict.fromkeys(memory_ids))
        if len(unique_ids) < 2:
            raise ValidationError("At least two memories are required to merge")
        records = self._records_by_ids(unique_ids)

        if merge_id:
            # A merge_id may only be consumed while PENDING. Looking at a
            # CONFIRMED/REJECTED record here is what made repeat confirmation
            # silently succeed.
            with self.manager.session() as session:
                existing_merge = LifecycleRepository(session).get_merge(merge_id)
            if existing_merge is None:
                raise NotFoundError(f"Merge {merge_id} not found")
            if existing_merge.status != MergeStatus.PENDING.value:
                raise AlreadyResolvedError(
                    f"Merge {merge_id} is already {existing_merge.status}; "
                    "only a PENDING merge preview can be confirmed",
                    detail={
                        "reason": "merge_already_resolved",
                        "status": existing_merge.status,
                    },
                )
            preview_ids = set(_loads(existing_merge.merged_memory_ids_json) or [])
            if preview_ids != {record.id for record in records}:
                raise ValidationError(
                    "merge_id does not match the provided memory_ids"
                )

        # Pre-flight: refuse any source that is not mergeable. Runs AFTER the
        # merge_id check on purpose: a repeat confirmation would otherwise be
        # reported as "source not mergeable" (the originals are MERGED by then)
        # instead of the real reason, "this preview was already confirmed".
        blocked = [
            record.id
            for record in records
            if record.status not in {"ACTIVE", "ARCHIVED"}
        ]
        if blocked:
            raise MergeSourceNotMergeableError(
                "These memories are no longer mergeable: " + ", ".join(blocked[:5]),
                detail={"memory_ids": blocked},
            )

        draft = self.reflection.suggest_merge(records)
        content = (new_content or draft.new_content or "").strip()
        if not content:
            raise ValidationError("Merged content cannot be empty")
        summary_text = new_summary or draft.new_summary
        source_ids = [record.id for record in records]
        source_id = records[0].source_id
        memory_type = draft.memory_type or records[0].memory_type
        now = utc_now_iso()
        product_id = _new_id()

        # ---- ONE transaction: product + originals + merge record + audit ----
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            lifecycle = LifecycleRepository(session)

            # Re-validate inside the transaction: a concurrent delete/merge
            # between the pre-flight above and this write must not win.
            fresh = {
                record.id: memory_repo.get_by_id(record.id) for record in records
            }
            for record_id, current in fresh.items():
                if current is None:
                    raise NotFoundError(f"Memory {record_id} disappeared")
                if current.status not in {"ACTIVE", "ARCHIVED"}:
                    raise MergeSourceNotMergeableError(
                        f"Memory {record_id} became {current.status} during the merge",
                        detail={"status": current.status},
                    )

            # Consume the pending merge record FIRST (conditional UPDATE). If
            # another confirmation already consumed it, abort before writing.
            if merge_id:
                consumed = lifecycle.transition_merge(
                    merge_id,
                    expected_status=MergeStatus.PENDING.value,
                    new_status=MergeStatus.CONFIRMED.value,
                    values={
                        "target_memory_id": product_id,
                        "new_content": content,
                        "new_summary": summary_text,
                        "resolved_at": now,
                        "resolved_by": SERVER_ACTOR_LABEL,
                    },
                )
                if consumed != 1:
                    raise AlreadyResolvedError(
                        "This merge preview was already confirmed",
                        detail={"reason": "merge_already_resolved"},
                    )

            product = MemoryRecord(
                id=product_id,
                memory_type=memory_type,
                content=content,
                summary=summary_text,
                importance=max(record.importance for record in records),
                confidence=0.7,
                status=MemoryStatus.ACTIVE.value,
                source_id=source_id,
                created_at=now,
                updated_at=now,
                metadata_json=_dumps(
                    {
                        "merged_from": source_ids,
                        "merge_reason": reason or draft.reason,
                        "merge_engine": draft.engine,
                    }
                ),
                search_text=segment_for_index(f"{content} {summary_text or ''}"),
                vector_status="PENDING",
            )
            memory_repo.create(product)

            for record_id, current in fresh.items():
                # JW-03: retiring a source is a protected status change. The
                # merge flow's own approval step is preview -> confirm, so it
                # opens the internal scope rather than a second approval layer.
                with internal_write("merge_confirm", operation="MERGE_SOURCE"):
                    memory_repo.update(
                        current, {"status": "MERGED", "merged_into": product_id}
                    )
                self._audit(
                    memory_repo,
                    memory_id=record_id,
                    action="MERGE_SOURCE",
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "merged_into": product_id,
                        "merge_id": merge_id,
                        **identity_fields(actor),
                    },
                )
            self._audit(
                memory_repo,
                memory_id=product_id,
                action="MERGE_CONFIRM",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "source_ids": source_ids,
                    "merge_id": merge_id,
                    "engine": draft.engine,
                    **identity_fields(actor),
                },
            )

            if merge_id:
                result = lifecycle.get_merge(merge_id)
            else:
                result = lifecycle.create_merge(
                    MemoryMerge(
                        id=_new_id(),
                        status=MergeStatus.CONFIRMED.value,
                        target_memory_id=product_id,
                        merged_memory_ids_json=_dumps(source_ids),
                        new_content=content,
                        new_summary=summary_text,
                        reason=reason or draft.reason,
                        created_at=now,
                        updated_at=now,
                        resolved_at=now,
                        resolved_by=SERVER_ACTOR_LABEL,
                        metadata_json=_dumps(
                            {"engine": draft.engine, **identity_fields(actor)}
                        ),
                    )
                )

        # ---- After commit: index the product, drop the originals' vectors ----
        self._reindex_vector(product)
        for record in records:
            self._remove_vector(record.id)
        logger.info("Merged %d memories into %s", len(records), product_id)
        return merge_to_response(result)


    def reject_merge(
        self, merge_id: str, *, actor: Optional[str] = ACTOR_USER
    ) -> MergeResponse:
        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            merge = repo.get_merge(merge_id)
            if merge is None:
                raise NotFoundError(f"Merge {merge_id} not found")
            if merge.status != MergeStatus.PENDING.value:
                raise ValidationError(f"Merge is already {merge.status}")
            repo.update_merge(
                merge,
                {
                    "status": MergeStatus.REJECTED.value,
                    "resolved_at": utc_now_iso(),
                    "resolved_by": actor,
                },
            )
        return merge_to_response(merge)

    def list_merges(
        self,
        *,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[MergeResponse], int]:
        with self.manager.session() as session:
            rows, total = LifecycleRepository(session).list_merges(
                status=status, page=page, page_size=page_size
            )
        return [merge_to_response(row) for row in rows], total

    def get_merge(self, merge_id: str) -> MergeResponse:
        with self.manager.session() as session:
            merge = LifecycleRepository(session).get_merge(merge_id)
        if merge is None:
            raise NotFoundError(f"Merge {merge_id} not found")
        return merge_to_response(merge)

    # ------------------------------------------------------------------ expiry
    @staticmethod
    def _expiry_item(record: MemoryRecord, status: str, now: str) -> ExpiryItem:
        expires = parse_iso(record.expires_at).timestamp() if record.expires_at else None
        now_ts = parse_iso(now).timestamp()
        days_remaining = (
            round((expires - now_ts) / 86400.0, 2) if expires is not None else None
        )
        return ExpiryItem(
            memory_id=record.id,
            memory_type=record.memory_type,
            content_preview=" ".join((record.content or "").split())[:200],
            status=status,
            expires_at=record.expires_at,
            days_remaining=days_remaining,
        )

    def list_expiring(
        self,
        *,
        days: int = 7,
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[ExpiryItem], int]:
        now = utc_now_iso()
        with self.manager.session() as session:
            rows = LifecycleRepository(session).list_expiring(
                now=now,
                within_seconds=max(days, 0) * 86400,
                memory_type=memory_type,
            )
        items = [self._expiry_item(row, "EXPIRING", now) for row in rows]
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    def list_expired(
        self,
        *,
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[ExpiryItem], int]:
        now = utc_now_iso()
        with self.manager.session() as session:
            rows = LifecycleRepository(session).list_expired(
                now=now, memory_type=memory_type
            )
        items = [self._expiry_item(row, "EXPIRED", now) for row in rows]
        total = len(items)
        start = (page - 1) * page_size
        return items[start : start + page_size], total

    def review_expiry(
        self,
        memory_id: str,
        *,
        action: str,
        new_expires_at: Optional[str] = None,
        note: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
        approved_request_id: Optional[str] = None,
    ):
        from ..schemas.lifecycle import ReviewAction

        action_value = (
            action.value if isinstance(action, ReviewAction) else str(action).upper()
        )
        if action_value not in {"REVIEWED", "EXTENDED", "ARCHIVED"}:
            raise ValidationError("action must be REVIEWED, EXTENDED, or ARCHIVED")
        if action_value == "EXTENDED" and not new_expires_at:
            raise ValidationError("new_expires_at is required for EXTENDED")
        if new_expires_at:
            try:
                parse_iso(new_expires_at)
            except ValueError as exc:
                raise ValidationError("new_expires_at must be ISO-8601") from exc

        now = utc_now_iso()
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ValidationError("Deleted memories cannot be reviewed")
            previous = record.expires_at
            # JW-03: both branches below write a protected field (expires_at /
            # status), so an approved memory needs authority here too. A
            # REVIEWED-only action writes nothing and is therefore unrestricted.
            if action_value == "EXTENDED":
                require_write_authority(
                    record,
                    {"expires_at": new_expires_at},
                    operation="POST /api/v1/memories/{id}/expiry-review",
                    approved_request_id=approved_request_id,
                )
                with internal_write("expiry_review", operation="expiry extend"):
                    memory_repo.update(record, {"expires_at": new_expires_at})
            elif action_value == "ARCHIVED" and record.status != "ARCHIVED":
                require_write_authority(
                    record,
                    {"status": "ARCHIVED"},
                    operation="POST /api/v1/memories/{id}/expiry-review",
                    approved_request_id=approved_request_id,
                )
                with internal_write("expiry_review", operation="expiry archive"):
                    memory_repo.update(record, {"status": "ARCHIVED"})
                self._audit(
                    memory_repo,
                    memory_id=memory_id,
                    action="ARCHIVE",
                    actor=actor,
                    detail={"reason": "expiry review", "source": "expiry"},
                )
            review = LifecycleRepository(session).create_review(
                MemoryLifecycleReview(
                    id=_new_id(),
                    memory_id=memory_id,
                    review_type="EXPIRY_REVIEW",
                    action=action_value,
                    previous_expires_at=previous,
                    new_expires_at=new_expires_at,
                    note=note,
                    actor=actor,
                    created_at=now,
                )
            )
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action=f"EXPIRY_{action_value}",
                actor=actor,
                detail={
                    "review_id": review.id,
                    "previous_expires_at": previous,
                    "new_expires_at": new_expires_at,
                },
            )
        if action_value == "ARCHIVED":
            self._remove_vector(memory_id)
        return review_to_response(review)

    # ---------------------------------------------------- automatic governance
    def supersede_memory(
        self,
        memory_id: str,
        *,
        new_content: str,
        reason: str,
        source: str = "auto_governance",
    ) -> MemoryRecord:
        """Replace one memory's content with a newer, user-stated value.

        JME-AUTO-GOVERNANCE-01. This is the ONLY automatic path that rewrites an
        existing memory, and it is deliberately narrow:

        * the row must be ``ACTIVE`` and non-``DELETED``;
        * the new content re-runs the credential/PII gates;
        * the pre-image is written to ``memory_audit_logs`` so the old value is
          recoverable;
        * the write runs inside the ``auto_governance_update`` internal scope,
          which is the same authority gate a human correction uses.

        It never touches ``memory_candidates``: the fact was already accepted,
        this only retires the outdated value.
        """
        cleaned = (new_content or "").strip()
        if not cleaned:
            raise ValidationError("New content cannot be empty")

        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ValidationError("Deleted memories cannot be updated")
            if record.status == "MERGED":
                raise ValidationError("Merged memories cannot be updated")
            if record.content == cleaned:
                return record

            previous = {
                "content": record.content,
                "summary": record.summary,
                "status": record.status,
                "updated_at": record.updated_at,
            }
            values = {
                "content": cleaned,
                "search_text": segment_for_index(
                    f"{cleaned} {record.summary or ''}"
                ),
            }
            with internal_write(
                "auto_governance_update", operation="auto supersede"
            ):
                memory_repo.update(record, values)
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="AUTO_UPDATE",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "reason": reason,
                    "source": source,
                    "before": previous,
                    "after": {
                        "content": record.content,
                        "summary": record.summary,
                        "status": record.status,
                    },
                    "authentication_level": AUTH_NOT_VERIFIED,
                },
            )

        self._reindex_vector(record)
        return record

    def auto_merge_duplicates(
        self,
        memory_ids: Sequence[str],
        *,
        new_content: str,
        new_summary: Optional[str] = None,
        reason: str = "多条记忆内容高度等价，自动整理为一条",
        source: str = "auto_governance",
    ) -> MergeResponse:
        """Merge a verified-equivalent cluster without a human preview.

        JME-AUTO-GOVERNANCE-01. The caller must have already proven equivalence
        (``core.auto_governance.mergeable_group``); this method enforces the
        mechanical safety rules only:

        * at least two distinct, existing, mergeable memories;
        * the product content is non-empty and passes the credential gate;
        * every source gets ``status=MERGED`` + ``merged_into`` and an audit row;
        * the merge record stores the sources and the generated content so the
          operation is auditable and the originals remain recoverable.

        It does NOT call ``confirm_merge`` because that path requires a
        reflection engine and treats the operation as a user confirmation.
        """
        unique_ids = list(dict.fromkeys(memory_ids))
        if len(unique_ids) < 2:
            raise ValidationError("At least two memories are required to merge")
        cleaned = (new_content or "").strip()
        if not cleaned:
            raise ValidationError("Merged content cannot be empty")

        records = self._records_by_ids(unique_ids)
        blocked = [
            record.id
            for record in records
            if record.status not in {"ACTIVE", "ARCHIVED"}
        ]
        if blocked:
            raise MergeSourceNotMergeableError(
                "These memories are no longer mergeable: " + ", ".join(blocked[:5]),
                detail={"memory_ids": blocked},
            )

        now = utc_now_iso()
        product_id = _new_id()
        memory_type = records[0].memory_type
        source_id = records[0].source_id
        summary_text = (new_summary or "").strip() or cleaned[:60]
        max_importance = max(record.importance for record in records)

        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            lifecycle = LifecycleRepository(session)

            fresh = {
                record.id: memory_repo.get_by_id(record.id) for record in records
            }
            for record_id, current in fresh.items():
                if current is None:
                    raise NotFoundError(f"Memory {record_id} disappeared")
                if current.status not in {"ACTIVE", "ARCHIVED"}:
                    raise MergeSourceNotMergeableError(
                        f"Memory {record_id} became {current.status} during the merge",
                        detail={"status": current.status},
                    )

            product = MemoryRecord(
                id=product_id,
                memory_type=memory_type,
                content=cleaned,
                summary=summary_text,
                importance=max_importance,
                confidence=0.7,
                status=MemoryStatus.ACTIVE.value,
                source_id=source_id,
                created_at=now,
                updated_at=now,
                metadata_json=_dumps(
                    {
                        "merged_from": unique_ids,
                        "merge_reason": reason,
                        "merge_engine": source,
                        "auto_governed": True,
                    }
                ),
                search_text=segment_for_index(f"{cleaned} {summary_text or ''}"),
                vector_status="PENDING",
            )
            memory_repo.create(product)

            for record_id, current in fresh.items():
                with internal_write(
                    "auto_governance_merge", operation="AUTO_MERGE_SOURCE"
                ):
                    memory_repo.update(
                        current, {"status": "MERGED", "merged_into": product_id}
                    )
                self._audit(
                    memory_repo,
                    memory_id=record_id,
                    action="AUTO_MERGE_SOURCE",
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "merged_into": product_id,
                        "source": source,
                        "authentication_level": AUTH_NOT_VERIFIED,
                    },
                )
            self._audit(
                memory_repo,
                memory_id=product_id,
                action="AUTO_MERGE_CONFIRM",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "source_ids": unique_ids,
                    "reason": reason,
                    "source": source,
                    "authentication_level": AUTH_NOT_VERIFIED,
                },
            )

            merge = lifecycle.create_merge(
                MemoryMerge(
                    id=_new_id(),
                    status=MergeStatus.CONFIRMED.value,
                    target_memory_id=product_id,
                    merged_memory_ids_json=_dumps(unique_ids),
                    new_content=cleaned,
                    new_summary=summary_text,
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                    resolved_at=now,
                    resolved_by=SERVER_ACTOR_LABEL,
                    metadata_json=_dumps(
                        {
                            "engine": source,
                            "auto_governed": True,
                            "authentication_level": AUTH_NOT_VERIFIED,
                        }
                    ),
                )
            )

        self._reindex_vector(product)
        for record in records:
            self._remove_vector(record.id)
        return merge_to_response(merge)

    def auto_archive_expired(
        self,
        memory_id: str,
        *,
        reason: str = "记忆自身有效期已过，自动归档",
        source: str = "auto_governance",
        now: Optional[str] = None,
    ) -> bool:
        """Archive ONE memory whose own ``expires_at`` has passed.

        JME-AUTO-GOVERNANCE-01. Time passing alone is never evidence a fact
        stopped being true, so this only acts on rows that carry an EXPLICIT
        ``expires_at`` which is now in the past. A memory without that field is
        never touched by this path.

        Archiving is reversible (``restore``) and the old status is recorded.
        """
        current_time = now or utc_now_iso()
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                return False
            if record.status != MemoryStatus.ACTIVE.value:
                return False
            if not record.expires_at or record.expires_at > current_time:
                return False
            with internal_write(
                "auto_governance_expire", operation="auto expire"
            ):
                memory_repo.update(record, {"status": MemoryStatus.ARCHIVED.value})
            LifecycleRepository(session).create_archive_record(
                MemoryArchiveRecord(
                    id=_new_id(),
                    memory_id=memory_id,
                    reason=reason,
                    source=source,
                    previous_status=MemoryStatus.ACTIVE.value,
                    archived_at=current_time,
                    metadata_json=_dumps(
                        {
                            "expires_at": record.expires_at,
                            "auto_governed": True,
                            "authentication_level": AUTH_NOT_VERIFIED,
                        }
                    ),
                )
            )
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="AUTO_EXPIRE",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "expires_at": record.expires_at,
                    "reason": reason,
                    "source": source,
                    "authentication_level": AUTH_NOT_VERIFIED,
                },
            )
        self._remove_vector(memory_id)
        return True

    def list_reviews(
        self,
        *,
        memory_id: Optional[str] = None,
        review_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ):
        with self.manager.session() as session:
            rows, total = LifecycleRepository(session).list_reviews(
                memory_id=memory_id,
                review_type=review_type,
                page=page,
                page_size=page_size,
            )
        return [review_to_response(row) for row in rows], total

    # ---------------------------------------------------------------- archives
    def archive(
        self,
        memory_id: str,
        *,
        reason: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
        approved_request_id: Optional[str] = None,
    ) -> bool:
        """Archive a memory.

        JW-03: archiving changes ``status``, a protected field. An approved
        memory therefore needs either an approved lifecycle request or an
        explicit internal scope. Archiving a non-approved (legacy/imported) row
        behaves exactly as before.
        """
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ValidationError("Deleted memories cannot be archived")
            if record.status == "MERGED":
                raise ValidationError("Merged memories cannot be archived")
            if record.status == "ARCHIVED":
                raise ValidationError("Memory is already archived")
            previous = record.status
            # JW-03: this is an HTTP-facing entry, so the STRICT gate applies -
            # no request credentials means no archive on an approved row.
            require_write_authority(
                record,
                {"status": "ARCHIVED"},
                operation="POST /api/v1/memories/{id}/archive",
                approved_request_id=approved_request_id,
            )
            with internal_write("approved_lifecycle", operation="archive"):
                memory_repo.update(record, {"status": "ARCHIVED"})
            LifecycleRepository(session).create_archive_record(
                MemoryArchiveRecord(
                    id=_new_id(),
                    memory_id=memory_id,
                    reason=reason,
                    source=source,
                    previous_status=previous,
                    archived_at=utc_now_iso(),
                    metadata_json=_dumps({"actor": actor}),
                )
            )
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="ARCHIVE",
                actor=actor,
                detail={
                    "previous_status": previous,
                    "reason": reason,
                    "source": source,
                },
            )
        self._remove_vector(memory_id)
        return True

    def restore(
        self,
        memory_id: str,
        *,
        actor: Optional[str] = ACTOR_USER,
        approved_request_id: Optional[str] = None,
    ) -> bool:
        """Restore an archived memory (JW-03: status is a protected field)."""
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status != "ARCHIVED":
                raise ValidationError("Only ARCHIVED memories can be restored")
            require_write_authority(
                record,
                {"status": "ACTIVE"},
                operation="POST /api/v1/memories/{id}/restore",
                approved_request_id=approved_request_id,
            )
            with internal_write("approved_lifecycle", operation="restore"):
                memory_repo.update(record, {"status": "ACTIVE"})
            repo = LifecycleRepository(session)
            latest = repo.latest_archive_record(memory_id)
            if latest is not None and latest.restored_at is None:
                repo.mark_restored(latest, actor)
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="RESTORE",
                actor=actor,
                detail={"previous_status": "ARCHIVED"},
            )
        self._reindex_vector(record)
        return True

    def list_archive_records(
        self,
        *,
        memory_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ):
        with self.manager.session() as session:
            rows, total = LifecycleRepository(session).list_archive_records(
                memory_id=memory_id, page=page, page_size=page_size
            )
        return [archive_record_to_response(row) for row in rows], total

    # ------------------------------------------------------------------ forget
    def forget_preview(self, memory_id: str) -> Dict[str, Any]:
        from ..schemas.memory import record_to_response

        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            lifecycle = LifecycleRepository(session)
            summaries = lifecycle.summaries_for_memory(memory_id)
            corrections = lifecycle.corrections_for_memory(memory_id)
            archive_records, _ = lifecycle.list_archive_records(memory_id=memory_id)
            reviews, _ = lifecycle.list_reviews(memory_id=memory_id)
            impact = {
                "status": record.status,
                "vector_status": record.vector_status,
                "summary_candidates": len(summaries),
                "correction_records": len(corrections),
                "archive_records": len(archive_records),
                "lifecycle_reviews": len(reviews),
                "source_id": record.source_id,
            }
            return {
                "memory": record_to_response(record).model_dump(),
                "impact": impact,
            }

    def list_forget_candidates(
        self,
        *,
        memory_type: Optional[str] = None,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        from ..schemas.memory import record_to_response

        if query:
            rows, total = self.memory_service.search(
                query=query,
                memory_type=memory_type,
                status="ACTIVE",
                page=page,
                page_size=page_size,
            )
            return rows, total
        with self.manager.session() as session:
            repo = LifecycleRepository(session)
            if memory_type:
                total = repo.count_by_type(memory_type)
                rows = repo.list_active_by_type(
                    memory_type, page_size, (page - 1) * page_size
                )
            else:
                total = repo.count_active()
                rows = self._all_active(page, page_size)
        return [record_to_response(row).model_dump() for row in rows], total

    def _all_active(self, page: int, page_size: int) -> List[MemoryRecord]:
        from sqlalchemy import select

        with self.manager.session() as session:
            return list(
                session.execute(
                    select(MemoryRecord)
                    .where(MemoryRecord.status == "ACTIVE")
                    .order_by(MemoryRecord.created_at.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).scalars().all()
            )

    def forget(
        self,
        *,
        memory_ids: Optional[Sequence[str]] = None,
        memory_type: Optional[str] = None,
        query: Optional[str] = None,
        reason: Optional[str] = None,
        confirm: bool = False,
        actor: Optional[str] = ACTOR_USER,
    ) -> Dict[str, Any]:
        if not confirm:
            raise ValidationError(
                "Forgetting memories requires explicit confirmation (confirm=true)"
            )
        if memory_ids:
            candidates = self._records_by_ids(memory_ids, allow_deleted=True)
        elif memory_type or query:
            with self.manager.session() as session:
                repo = LifecycleRepository(session)
                if query:
                    from ..core.search import build_match_expression

                    match = build_match_expression(query)
                    rows, _ = MemoryRepository(session).search(
                        match=match,
                        memory_type=memory_type,
                        status="ACTIVE",
                        page=1,
                        page_size=500,
                    )
                    ids = [row["id"] for row in rows]
                else:
                    ids = [
                        record.id
                        for record in repo.list_active_by_type(memory_type, 500)
                    ]
                candidates = repo.get_records_by_ids(ids)
        else:
            raise ValidationError(
                "Provide memory_ids, memory_type, or query to select memories to forget"
            )
        if not candidates:
            raise ValidationError("No memories matched the forget selection")

        # Hard guard: never allow an unfiltered wipe of the whole memory store.
        with self.manager.session() as session:
            active_total = LifecycleRepository(session).count_active()
        if not memory_ids and active_total > 0 and len(candidates) >= active_total:
            raise ValidationError(
                "Refusing to forget every active memory; provide explicit memory_ids"
            )
        if len(candidates) > 500:
            raise ValidationError(
                "Selection is too large; refine the filters (max 500 per operation)"
            )

        forgotten: List[str] = []
        already: List[str] = []
        skipped: List[str] = []
        vector_removed: List[str] = []
        for record in candidates:
            if record.status == "DELETED":
                already.append(record.id)
                continue
            if record.status == "MERGED":
                skipped.append(record.id)
                continue
            with self.manager.session() as session:
                memory_repo = MemoryRepository(session)
                current = memory_repo.get_by_id(record.id)
                if current is None or current.status == "DELETED":
                    already.append(record.id)
                    continue
                previous_status = current.status
                LifecycleRepository(session).set_forgotten(current, reason=reason)
                self._audit(
                    memory_repo,
                    memory_id=record.id,
                    action="FORGET",
                    actor=actor,
                    detail={"reason": reason, "previous_status": previous_status},
                )
            if self._remove_vector(record.id):
                vector_removed.append(record.id)
            forgotten.append(record.id)

        logger.info(
            "Forgot %d memories (%s); vector removed for %d",
            len(forgotten),
            reason or "no reason",
            len(vector_removed),
        )
        return {
            "forgotten_ids": forgotten,
            "skipped_ids": skipped,
            "already_forgotten_ids": already,
            "vector_removed_ids": vector_removed,
            "reason": reason,
        }
        
