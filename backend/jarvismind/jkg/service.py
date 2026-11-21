"""JKG write service and JME-lifecycle reconciliation.

Boundary rules (Authority Spec §12 / §13):

* JME code must not import JKG. This module is therefore a downstream reader:
  it reads source memories through ``ReadOnlyProjection.for_module("JKG")`` and
  only writes the three proposed jkg_* tables.
* No code here writes ``memory_records`` / ``memory_candidates``.
* An edge is never created without at least one valid JME memory source.
* Relation validity is never trusted from a stored flag alone: every read also
  checks the current memory status, so an unsynced forget cannot leave a
  forgotten fact visible.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from ..core.errors import ValidationError
from ..core.timeutils import utc_now_iso
from ..governance.projection import ReadOnlyProjection
from .errors import (
    JkgInvalidEntityError,
    JkgInvalidSourceError,
    JkgSchemaNotReadyError,
    JkgSelfEdgeError,
    JkgSourceRequiredError,
)
from .models import JkgEdge, JkgEdgeSource, JkgNode
from .normalization import (
    VALID_SOURCE_STATUSES,
    canonicalize_entity_name,
    normalize_entity_name,
    serialize_metadata,
    validate_endpoint_types,
    validate_entity_type,
    validate_relation_type,
)
from .repository import JkgRepository
from .schema import missing_jkg_tables
from .serialization import edge_payload, node_payload


def _new_id() -> str:
    return uuid.uuid4().hex


def _auto_reason_for_status(status: Optional[str]) -> str:
    if status is None:
        return "MEMORY_MISSING"
    return f"MEMORY_{status or 'UNKNOWN'}"


def _is_auto_reason(reason: Optional[str]) -> bool:
    return bool(reason) and str(reason).startswith("MEMORY_")


#: Invalidation reason of a source whose memory body was rewritten. It does not
#: start with ``MEMORY_``, so only a caller naming it in ``reactivate_reasons``
#: can clear it again.
CONTENT_STALE_REASON = "CONTENT_UPDATED_STALE"


class JkgService:
    """Write operations for entities, edges, and edge sources."""

    def __init__(self, manager, *, projection=None) -> None:
        self.manager = manager
        self.projection = projection or ReadOnlyProjection(manager).for_module("JKG")

    # ------------------------------------------------------------- preconditions
    def _require_schema(self) -> None:
        with self.manager.session() as session:
            missing = missing_jkg_tables(session)
        if missing:
            raise JkgSchemaNotReadyError(
                "The JKG schema has not been approved and installed",
                detail={"code": "jkg_schema_not_ready", "missing": missing},
            )

    def _memory_status(self, memory_id: str) -> Optional[str]:
        row = self.projection.read_memory(memory_id)
        if row is None:
            return None
        return str(row.get("status") or "")

    def _memory_statuses(self, memory_ids: Iterable[str]) -> Dict[str, Optional[str]]:
        statuses: Dict[str, Optional[str]] = {}
        for memory_id in memory_ids:
            if memory_id not in statuses:
                statuses[memory_id] = self._memory_status(memory_id)
        return statuses

    def _collect_memory_ids(self, memory_ids: Any) -> List[str]:
        if memory_ids is None or isinstance(memory_ids, (str, bytes)):
            raise JkgSourceRequiredError(
                detail={"reason": "memory_ids must be a non-empty sequence"}
            )
        if not isinstance(memory_ids, Iterable):
            raise JkgSourceRequiredError(
                detail={"reason": "memory_ids must be a non-empty sequence"}
            )
        raw_items = list(memory_ids)
        if isinstance(memory_ids, (set, frozenset)):
            raw_items.sort()
        ordered: List[str] = []
        seen = set()
        for raw in raw_items:
            if not isinstance(raw, str) or not raw.strip():
                raise JkgInvalidSourceError(
                    detail={"reason": "memory_id must be a non-empty string"}
                )
            memory_id = raw.strip()
            if memory_id not in seen:
                seen.add(memory_id)
                ordered.append(memory_id)
        if not ordered:
            raise JkgSourceRequiredError()
        return ordered

    def _validate_sources(self, memory_ids: Any) -> Dict[str, str]:
        """Validate every source and return ``{memory_id: status}``.

        The whole operation is refused if any id is missing or in DELETED /
        MERGED state; silently dropping a bad source would create a fact with
        less provenance than the caller claimed.
        """
        ordered = self._collect_memory_ids(memory_ids)
        statuses: Dict[str, str] = {}
        missing: List[str] = []
        ineligible: List[Dict[str, str]] = []
        for memory_id in ordered:
            row = self.projection.read_memory(memory_id)
            if row is None:
                missing.append(memory_id)
                continue
            status = str(row.get("status") or "")
            if status not in VALID_SOURCE_STATUSES:
                ineligible.append({"memory_id": memory_id, "status": status})
                continue
            statuses[memory_id] = status
        if missing or ineligible:
            raise JkgInvalidSourceError(
                "One or more source memories are missing or not eligible",
                detail={
                    "missing_memory_ids": missing,
                    "ineligible_sources": ineligible,
                    "valid_statuses": sorted(VALID_SOURCE_STATUSES),
                },
            )
        return statuses

    # ------------------------------------------------------------------ entities
    def upsert_node(
        self,
        *,
        entity_type: Any,
        name: str,
        source_memory_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create an entity, or return the existing one for the same key.

        Dedupe key: ``(entity_type, normalized_key)``. The same name may exist
        under different entity types. The first establishing source memory is
        kept; a later call never rewrites an existing entity's label.

        The payload carries ``created``: ``True`` only when this call inserted
        the node.
        """
        self._require_schema()
        canonical = canonicalize_entity_name(name)
        normalized = normalize_entity_name(canonical)
        entity = validate_entity_type(entity_type)
        metadata_json = serialize_metadata(metadata)
        source_statuses = self._validate_sources([source_memory_id])
        source_status = source_statuses[source_memory_id]
        now = utc_now_iso()

        try:
            with self.manager.session() as session:
                repo = JkgRepository(session)
                existing = repo.find_node(entity, normalized)
                if existing is not None:
                    payload = node_payload(
                        existing,
                        source_memory_status=self._memory_status(
                            existing.source_memory_id
                        ),
                    )
                    payload["created"] = False
                    return payload
                node = JkgNode(
                    node_id=_new_id(),
                    entity_type=entity,
                    name=canonical,
                    canonical_name=canonical,
                    normalized_key=normalized,
                    source_memory_id=source_memory_id,
                    status="ACTIVE",
                    generation=1,
                    metadata_json=metadata_json,
                    created_at=now,
                    updated_at=now,
                )
                repo.create_node(node)
                payload = node_payload(node, source_memory_status=source_status)
                payload["created"] = True
                return payload
        except IntegrityError:
            # A concurrent writer created the same (type, key). Re-read it.
            with self.manager.session() as session:
                existing = JkgRepository(session).find_node(entity, normalized)
                if existing is None:
                    raise
                payload = node_payload(
                    existing,
                    source_memory_status=self._memory_status(
                        existing.source_memory_id
                    ),
                )
                payload["created"] = False
                return payload

    # -------------------------------------------------------------------- edges
    @staticmethod
    def _validate_confidence(confidence: Any) -> float:
        if isinstance(confidence, bool):
            raise ValidationError("confidence must be a number in [0, 1]")
        try:
            value = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValidationError("confidence must be a number in [0, 1]") from exc
        if not 0.0 <= value <= 1.0:
            raise ValidationError("confidence must be a number in [0, 1]")
        return value

    def create_edge(
        self,
        *,
        subject_id: str,
        predicate: Any,
        object_id: str,
        source_memory_ids: Any,
        confidence: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        reactivate_reasons: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """Create a relation, or attach new sources to the existing one.

        The edge unique key is ``(subject_id, predicate, object_id)``. A second
        call with the same triple returns the same edge and adds sources rather
        than creating a duplicate.

        ``reactivate_reasons`` lists extra invalidation reasons that this
        re-attestation may clear, on top of the automatic ``MEMORY_`` ones. The
        default is empty, so existing callers keep their behaviour.
        """
        self._require_schema()
        relation = validate_relation_type(predicate)
        score = self._validate_confidence(confidence)
        metadata_json = serialize_metadata(metadata)
        reactivate = frozenset(str(reason) for reason in reactivate_reasons)
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise JkgInvalidEntityError(
                detail={"reason": "subject_id must be a non-empty string"}
            )
        if not isinstance(object_id, str) or not object_id.strip():
            raise JkgInvalidEntityError(
                detail={"reason": "object_id must be a non-empty string"}
            )
        subject_id = subject_id.strip()
        object_id = object_id.strip()
        if subject_id == object_id:
            raise JkgSelfEdgeError(
                detail={"node_id": subject_id, "predicate": relation}
            )
        source_ids = self._collect_memory_ids(source_memory_ids)
        self._validate_sources(source_ids)
        now = utc_now_iso()

        try:
            return self._upsert_edge_once(
                subject_id=subject_id,
                relation=relation,
                object_id=object_id,
                source_ids=source_ids,
                confidence=score,
                metadata_json=metadata_json,
                now=now,
                reactivate_reasons=reactivate,
            )
        except IntegrityError:
            # Concurrent edge/source insert: retry once against the committed
            # rows. The second pass finds the existing edge and only adds
            # missing sources.
            return self._upsert_edge_once(
                subject_id=subject_id,
                relation=relation,
                object_id=object_id,
                source_ids=source_ids,
                confidence=score,
                metadata_json=metadata_json,
                now=utc_now_iso(),
                reactivate_reasons=reactivate,
            )

    def _upsert_edge_once(
        self,
        *,
        subject_id: str,
        relation: str,
        object_id: str,
        source_ids: List[str],
        confidence: float,
        metadata_json: Optional[str],
        now: str,
        reactivate_reasons: frozenset = frozenset(),
    ) -> Dict[str, Any]:
        with self.manager.session() as session:
            repo = JkgRepository(session)
            subject = repo.get_node(subject_id)
            target = repo.get_node(object_id)
            missing = [
                node_id
                for node_id, node in (
                    (subject_id, subject),
                    (object_id, target),
                )
                if node is None
            ]
            if missing:
                raise JkgInvalidEntityError(
                    "One or more edge endpoints do not exist",
                    detail={"missing_node_ids": missing},
                )
            validate_endpoint_types(
                relation, subject.entity_type, target.entity_type
            )

            edge = repo.find_edge(subject_id, relation, object_id)
            created_edge = edge is None
            if edge is None:
                edge = JkgEdge(
                    edge_id=_new_id(),
                    predicate=relation,
                    subject_id=subject_id,
                    object_id=object_id,
                    status="ACTIVE",
                    generation=1,
                    confidence=confidence,
                    metadata_json=metadata_json,
                    recorded_at=now,
                    updated_at=now,
                )
                repo.create_edge(edge)

            added: List[str] = []
            reactivated: List[str] = []
            for memory_id in source_ids:
                existing_source = repo.get_source(edge.edge_id, memory_id)
                if existing_source is None:
                    repo.create_source(
                        JkgEdgeSource(
                            source_id=_new_id(),
                            edge_id=edge.edge_id,
                            memory_id=memory_id,
                            created_at=now,
                            invalidated_at=None,
                            invalidation_reason=None,
                        )
                    )
                    added.append(memory_id)
                elif existing_source.invalidated_at is not None:
                    # Re-attesting clears automatic lifecycle invalidation and the
                    # content-stale reasons the caller names. A manual, merged or
                    # deleted source is never revived by this path.
                    reason = existing_source.invalidation_reason
                    if _is_auto_reason(reason) or reason in reactivate_reasons:
                        existing_source.invalidated_at = None
                        existing_source.invalidation_reason = None
                        reactivated.append(memory_id)

            if added or reactivated or edge.status != "ACTIVE":
                edge.updated_at = now
            if edge.status != "ACTIVE":
                edge.status = "ACTIVE"

            sources = repo.list_sources(edge.edge_id)
            statuses = self._memory_statuses(
                source.memory_id for source in sources
            )
            payload = edge_payload(
                edge, sources, statuses, include_sources=True
            )
            payload["created"] = created_edge
            payload["sources_added"] = added
            payload["sources_reactivated"] = reactivated
            return payload

    def add_edge_sources(
        self, *, edge_id: str, memory_ids: Any
    ) -> Dict[str, Any]:
        """Attach additional JME memories to an existing edge."""
        self._require_schema()
        if not isinstance(edge_id, str) or not edge_id.strip():
            raise ValidationError("edge_id must be a non-empty string")
        edge_id = edge_id.strip()
        source_ids = self._collect_memory_ids(memory_ids)
        self._validate_sources(source_ids)
        now = utc_now_iso()

        with self.manager.session() as session:
            repo = JkgRepository(session)
            edge = repo.get_edge(edge_id)
            if edge is None:
                raise JkgInvalidEntityError(
                    "The edge does not exist",
                    detail={"missing_edge_id": edge_id},
                )
            added: List[str] = []
            reactivated: List[str] = []
            for memory_id in source_ids:
                existing = repo.get_source(edge_id, memory_id)
                if existing is None:
                    repo.create_source(
                        JkgEdgeSource(
                            source_id=_new_id(),
                            edge_id=edge_id,
                            memory_id=memory_id,
                            created_at=now,
                            invalidated_at=None,
                            invalidation_reason=None,
                        )
                    )
                    added.append(memory_id)
                elif existing.invalidated_at is not None and _is_auto_reason(
                    existing.invalidation_reason
                ):
                    existing.invalidated_at = None
                    existing.invalidation_reason = None
                    reactivated.append(memory_id)
            if added or reactivated or edge.status != "ACTIVE":
                edge.updated_at = now
            if edge.status != "ACTIVE":
                edge.status = "ACTIVE"
            sources = repo.list_sources(edge_id)
            statuses = self._memory_statuses(
                source.memory_id for source in sources
            )
            payload = edge_payload(edge, sources, statuses, include_sources=True)
            payload["sources_added"] = added
            payload["sources_reactivated"] = reactivated
            return payload

    # --------------------------------------------------------------- lifecycle
    def sync_memory(self, memory_id: str) -> Dict[str, Any]:
        """Reconcile edge sources when a JME memory changes lifecycle state.

        Called by JKG-owned consumers (a future scheduled/event worker), never
        by JME itself. ``DELETED`` / ``MERGED`` / missing memories invalidate
        their source rows; a later valid status clears only automatic
        invalidation. Edges with zero valid sources become ``INVALID``.
        """
        self._require_schema()
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValidationError("memory_id must be a non-empty string")
        memory_id = memory_id.strip()
        status = self._memory_status(memory_id)
        now = utc_now_iso()

        with self.manager.session() as session:
            repo = JkgRepository(session)
            sources = repo.list_sources_for_memory(memory_id)
            affected_edges = sorted({source.edge_id for source in sources})
            invalidated = 0
            reactivated = 0
            for source in sources:
                if status in VALID_SOURCE_STATUSES:
                    if source.invalidated_at is not None and _is_auto_reason(
                        source.invalidation_reason
                    ):
                        source.invalidated_at = None
                        source.invalidation_reason = None
                        reactivated += 1
                elif source.invalidated_at is None:
                    source.invalidated_at = now
                    source.invalidation_reason = _auto_reason_for_status(status)
                    invalidated += 1

            edges_invalidated: List[str] = []
            edges_reactivated: List[str] = []
            for edge_id in affected_edges:
                edge = repo.get_edge(edge_id)
                if edge is None:
                    continue
                edge_sources = repo.list_sources(edge_id)
                statuses = self._memory_statuses(
                    source.memory_id for source in edge_sources
                )
                valid_count = sum(
                    1
                    for source in edge_sources
                    if source.invalidated_at is None
                    and statuses.get(source.memory_id) in VALID_SOURCE_STATUSES
                )
                new_status = "ACTIVE" if valid_count > 0 else "INVALID"
                if edge.status != new_status:
                    previous = edge.status
                    edge.status = new_status
                    edge.updated_at = now
                    if new_status == "INVALID":
                        edges_invalidated.append(edge_id)
                    elif previous == "INVALID":
                        edges_reactivated.append(edge_id)

            return {
                "memory_id": memory_id,
                "memory_status": status,
                "sources_invalidated": invalidated,
                "sources_reactivated": reactivated,
                "edges_invalidated": edges_invalidated,
                "edges_reactivated": edges_reactivated,
                "affected_edges": affected_edges,
            }

    def reconcile(self, *, limit: int = 1000) -> Dict[str, Any]:
        """Reconcile every distinct source memory, bounded by ``limit``.

        This is a maintenance entry point for a future JKG worker and for the
        migration verification checklist; it is not called from startup.
        """
        self._require_schema()
        if limit < 1 or limit > 5000:
            raise ValidationError("limit must be between 1 and 5000")
        with self.manager.session() as session:
            memory_ids = JkgRepository(session).distinct_source_memory_ids(limit)
        totals = {
            "memories_checked": len(memory_ids),
            "sources_invalidated": 0,
            "sources_reactivated": 0,
            "edges_invalidated": [],
            "edges_reactivated": [],
        }
        for memory_id in memory_ids:
            result = self.sync_memory(memory_id)
            totals["sources_invalidated"] += result["sources_invalidated"]
            totals["sources_reactivated"] += result["sources_reactivated"]
            totals["edges_invalidated"].extend(result["edges_invalidated"])
            totals["edges_reactivated"].extend(result["edges_reactivated"])
        return totals


__all__ = ["JkgService"]
