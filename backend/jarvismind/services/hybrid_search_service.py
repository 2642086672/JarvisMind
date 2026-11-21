"""Hybrid Search and Index Synchronization Service for JME-4.

Executes parallel/sequential FTS5 keyword retrieval and Qdrant semantic vector retrieval,
applies Reciprocal Rank Fusion (RRF) for deduplication and ranking, provides graceful
degradation to FTS5 when embeddings are unavailable, and manages index rebuilds and consistency.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import Settings
from ..core.errors import SearchUnavailableError, ValidationError
from ..core.search import build_match_expression, strip_query_fillers
from ..core.timeutils import utc_now_iso
from ..database.connection import DatabaseManager
from ..database.models import MemoryRecord
from ..database.search_index import zh_index_available
from ..repositories.memory_repository import MemoryRepository
from ..schemas.hybrid import (
    ConsistencyReportResponse,
    HybridSearchMeta,
    HybridSearchResponse,
    HybridSearchResultItem,
    IndexRebuildResponse,
    VectorIndexStatusResponse,
)
from ..schemas.memory import MemoryStatus, MemoryType, build_page_meta
from .embedding import BaseEmbeddingProvider
from .vector_store import VectorStore

logger = logging.getLogger("jme.hybrid_search")


class HybridSearchService:
    def __init__(
        self,
        manager: DatabaseManager,
        embedding_provider: BaseEmbeddingProvider,
        vector_store: VectorStore,
        settings: Settings,
    ) -> None:
        self.manager = manager
        self.embedding = embedding_provider
        self.vector_store = vector_store
        self.settings = settings

    def search(
        self,
        *,
        query: str,
        memory_type: Optional[str] = None,
        status: Optional[str] = "ACTIVE",
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[HybridSearchResultItem], HybridSearchMeta]:
        q_clean = query.strip()
        if not q_clean:
            raise ValidationError("Search query cannot be empty")

        k = self.settings.hybrid_rrf_k
        w_fts = self.settings.hybrid_fts_weight
        w_vec = self.settings.hybrid_vector_weight
        candidate_limit = max(page * page_size * 2, self.settings.hybrid_max_candidates)

        fts_candidates: Dict[str, Dict[str, Any]] = {}
        # 1. Execute FTS5 Keyword Search
        try:
            if zh_index_available(self.manager):
                with self.manager.session() as session:
                    repo = MemoryRepository(session)
                    expression = build_match_expression(q_clean)
                    rows, _ = repo.search(
                        match=expression,
                        memory_type=memory_type,
                        status=status,
                        page=1,
                        page_size=candidate_limit,
                    )
                    if not rows:
                        # A full question ANDs every bigram. Try the salient
                        # keywords first ("我喜欢喝什么咖啡" -> "咖啡"), then a
                        # wide OR as the last resort.
                        salient = strip_query_fillers(q_clean)
                        if salient:
                            try:
                                rows, _ = repo.search(
                                    match=build_match_expression(salient),
                                    memory_type=memory_type,
                                    status=status,
                                    page=1,
                                    page_size=candidate_limit,
                                )
                            except ValueError:
                                rows = []
                    if not rows:
                        wide_expression = build_match_expression(q_clean, operator="OR")
                        rows, _ = repo.search(
                            match=wide_expression,
                            memory_type=memory_type,
                            status=status,
                            page=1,
                            page_size=candidate_limit,
                        )
                    for rank_idx, row in enumerate(rows, start=1):
                        fts_candidates[row["id"]] = {
                            "rank": rank_idx,
                            "row": row,
                        }
        except Exception as exc:
            logger.warning("FTS5 candidate search failed or unavailable: %s", exc)

        # 2. Execute Vector Semantic Search
        vector_candidates: Dict[str, Dict[str, Any]] = {}
        degraded = False

        if not self.embedding.is_available or not self.vector_store.is_ready:
            degraded = True
        else:
            try:
                q_vec = self.embedding.embed_text(q_clean)
                vec_results = self.vector_store.search(
                    query_vector=q_vec,
                    limit=candidate_limit,
                    memory_type=memory_type,
                    status=status,
                )
                for rank_idx, res in enumerate(vec_results, start=1):
                    vector_candidates[res.memory_id] = {
                        "rank": rank_idx,
                        "score": res.score,
                        "payload": res.payload,
                    }
            except Exception as exc:
                logger.warning("Vector search failed, degrading to FTS5: %s", exc)
                degraded = True

        # 3. Fusion via Reciprocal Rank Fusion (RRF)
        all_ids = set(fts_candidates.keys()) | set(vector_candidates.keys())
        if not all_ids:
            return [], HybridSearchMeta(
                page=page,
                page_size=page_size,
                total=0,
                total_pages=0,
                fts_count=0,
                vector_count=0,
                degraded_to_fts=degraded,
                embedding_model=self.embedding.model_name,
            )

        # Max possible RRF score (rank 1 in both if active)
        if not degraded and self.embedding.is_available and self.vector_store.is_ready:
            max_rrf = (w_fts / (k + 1)) + (w_vec / (k + 1))
        else:
            max_rrf = w_fts / (k + 1) if w_fts > 0 else 1.0

        # Load authoritative SQLite records for items found only in vector search
        missing_db_ids = [mid for mid in all_ids if mid not in fts_candidates]
        db_records: Dict[str, MemoryRecord] = {}
        if missing_db_ids:
            with self.manager.session() as session:
                records = MemoryRepository(session).get_by_ids(missing_db_ids)
                for rec in records:
                    db_records[rec.id] = rec

        fused_items: List[Dict[str, Any]] = []
        for mid in all_ids:
            fts_item = fts_candidates.get(mid)
            vec_item = vector_candidates.get(mid)

            fts_rank = fts_item["rank"] if fts_item else None
            vec_rank = vec_item["rank"] if vec_item else None

            sources = []
            if fts_item:
                sources.append("fts5")
            if vec_item:
                sources.append("vector")

            rrf = 0.0
            if fts_rank is not None:
                rrf += w_fts / (k + fts_rank)
            if vec_rank is not None:
                rrf += w_vec / (k + vec_rank)

            norm_score = min(round(rrf / max_rrf, 4), 1.0) if max_rrf > 0 else 0.0

            # Extract fields authoritatively from SQLite record or FTS row
            if fts_item:
                row = fts_item["row"]
                content = row["content"]
                summary = row["summary"]
                m_type = row["memory_type"]
                importance = row["importance"]
                confidence = row["confidence"]
                created_at = row["created_at"]
            elif mid in db_records:
                rec = db_records[mid]
                # C-1: a vector-only hit carries the *payload's* status, which
                # goes stale when a memory is retired without a vector delete
                # (or before the delete lands). SQLite is the source of truth:
                # a MERGED / ARCHIVED / DELETED row must never re-enter results
                # under a different requested status (e.g. ACTIVE).
                if status is not None and rec.status != status:
                    continue
                content = rec.content
                summary = rec.summary
                m_type = rec.memory_type
                importance = rec.importance
                confidence = rec.confidence
                created_at = rec.created_at
            else:
                # Memory might have been deleted from SQLite; skip it (SQLite is source of truth)
                continue

            fused_items.append(
                {
                    "memory_id": mid,
                    "content": content,
                    "summary": summary,
                    "memory_type": MemoryType(m_type),
                    "importance": importance,
                    "confidence": confidence,
                    "created_at": created_at,
                    "fts_rank": fts_rank,
                    "vector_rank": vec_rank,
                    "rrf_score": round(rrf, 6),
                    "score": norm_score,
                    "sources": sources,
                }
            )

        # Sort: rrf_score DESC, importance DESC, created_at DESC
        fused_items.sort(
            key=lambda x: (x["rrf_score"], x["importance"], x["created_at"]),
            reverse=True,
        )

        total = len(fused_items)
        start_idx = (page - 1) * page_size
        paged_slice = fused_items[start_idx : start_idx + page_size]

        results = []
        for idx, item in enumerate(paged_slice, start=start_idx + 1):
            results.append(
                HybridSearchResultItem(
                    memory_id=item["memory_id"],
                    content=item["content"],
                    summary=item["summary"],
                    memory_type=item["memory_type"],
                    importance=item["importance"],
                    confidence=item["confidence"],
                    created_at=item["created_at"],
                    fts_rank=item["fts_rank"],
                    vector_rank=item["vector_rank"],
                    fusion_rank=idx,
                    rrf_score=item["rrf_score"],
                    score=item["score"],
                    sources=item["sources"],
                )
            )

        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        meta = HybridSearchMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            fts_count=len(fts_candidates),
            vector_count=len(vector_candidates),
            degraded_to_fts=degraded,
            embedding_model=self.embedding.model_name,
        )
        return results, meta

    def rebuild_index(self, force: bool = False) -> IndexRebuildResponse:
        """Full rebuild of the vector index from authoritative SQLite records."""
        t0 = time.monotonic()
        if not self.embedding.is_available:
            raise SearchUnavailableError(
                f"Embedding provider {self.embedding.name} is not available"
            )

        # Recreate collection to wipe stale points or adjust dimensions
        self.vector_store.recreate_collection(dimension=self.embedding.dimension)

        with self.manager.session() as session:
            repo = MemoryRepository(session)
            active_records = repo.get_all_active_records()

            total_records = len(active_records)
            indexed_count = 0
            failed_count = 0

            # Batch process in chunks of 32
            chunk_size = 32
            for i in range(0, total_records, chunk_size):
                chunk = active_records[i : i + chunk_size]
                texts = [f"{r.content} {r.summary or ''}".strip() for r in chunk]
                try:
                    vectors = self.embedding.embed_batch(texts)
                    items_to_upsert = []
                    for r, vec in zip(chunk, vectors):
                        payload = {
                            "memory_id": r.id,
                            "memory_type": r.memory_type,
                            "status": r.status,
                            "importance": r.importance,
                            "created_at": r.created_at,
                        }
                        items_to_upsert.append((r.id, vec, payload))

                    self.vector_store.upsert_batch(items_to_upsert)
                    now = utc_now_iso()
                    for r in chunk:
                        repo.update_vector_status(r.id, "INDEXED", now)
                    indexed_count += len(chunk)
                except Exception as exc:
                    logger.error("Failed to index chunk %d-%d: %s", i, i + len(chunk), exc)
                    now = utc_now_iso()
                    for r in chunk:
                        repo.update_vector_status(r.id, "FAILED", now)
                    failed_count += len(chunk)

            # Record rebuild metadata
            now = utc_now_iso()
            repo.set_vector_meta("active_embedding_model", self.embedding.model_name)
            repo.set_vector_meta("active_embedding_dimension", str(self.embedding.dimension))
            repo.set_vector_meta("last_rebuild_at", now)
            repo.set_vector_meta("rebuild_status", "completed")

        duration_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return IndexRebuildResponse(
            total_records=total_records,
            indexed=indexed_count,
            failed=failed_count,
            duration_ms=duration_ms,
            embedding_model=self.embedding.model_name,
            status="completed" if failed_count == 0 else "partial_failure",
        )

    def check_consistency(self) -> ConsistencyReportResponse:
        """Compare SQLite active memories with Qdrant vector index points."""
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            active_records = repo.get_all_active_records()
            sqlite_ids = {r.id for r in active_records}

        qdrant_ids = self.vector_store.get_all_point_ids() if self.vector_store.is_ready else set()

        missing_in_qdrant = sorted(list(sqlite_ids - qdrant_ids))
        orphans_in_qdrant = sorted(list(qdrant_ids - sqlite_ids))
        is_consistent = (len(missing_in_qdrant) == 0 and len(orphans_in_qdrant) == 0)

        return ConsistencyReportResponse(
            is_consistent=is_consistent,
            sqlite_active_count=len(sqlite_ids),
            qdrant_points_count=len(qdrant_ids),
            missing_in_qdrant=missing_in_qdrant,
            orphans_in_qdrant=orphans_in_qdrant,
            checked_at=utc_now_iso(),
        )

    def get_index_status(self) -> VectorIndexStatusResponse:
        """Combine Qdrant store status and SQLite vector metadata."""
        q_status = self.vector_store.get_status()
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            counts = repo.count_by_vector_status()
            last_rebuilt = repo.get_vector_meta("last_rebuild_at")

        return VectorIndexStatusResponse(
            collection_name=q_status.collection_name,
            storage_path=q_status.storage_path,
            total_points=q_status.total_points,
            dimension=q_status.dimension,
            distance=q_status.distance,
            is_ready=q_status.is_ready,
            status=q_status.status,
            sqlite_status_counts=counts,
            embedding_model=self.embedding.model_name,
            last_rebuilt_at=last_rebuilt,
            error=q_status.error,
        )

    def reconcile_vector_state(self) -> Dict[str, int]:
        """Make SQLite vector_status agree with the actual Qdrant index.

        Non-active records must have no vector point; any point found is
        deleted and the row is marked REMOVED. This repairs the JME-4 issue
        where archived/deleted rows still claimed to be INDEXED after their
        point had already been removed.
        """
        fixed = {"checked": 0, "marked_removed": 0, "deleted_points": 0}
        if not self.vector_store.is_ready:
            return fixed
        from sqlalchemy import select

        with self.manager.session() as session:
            rows = list(
                session.execute(
                    select(MemoryRecord).where(MemoryRecord.status != "ACTIVE")
                ).scalars().all()
            )
        qdrant_ids = self.vector_store.get_all_point_ids()
        fixed["checked"] = len(rows)
        for record in rows:
            if record.id in qdrant_ids:
                try:
                    self.vector_store.delete_memory(record.id)
                    fixed["deleted_points"] += 1
                except Exception as exc:
                    logger.warning(
                        "Reconcile delete failed for %s: %s", record.id, exc
                    )
            if record.vector_status != "REMOVED":
                with self.manager.session() as session:
                    MemoryRepository(session).update_vector_status(
                        record.id, "REMOVED", utc_now_iso()
                    )
                fixed["marked_removed"] += 1
        logger.info(
            "Vector state reconciled: %d non-active checked, %d marked REMOVED, "
            "%d stale points deleted",
            fixed["checked"],
            fixed["marked_removed"],
            fixed["deleted_points"],
        )
        return fixed
