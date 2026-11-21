"""Qdrant vector store implementation for JME-4.

Manages local vector collections, point indexing, persistence, and semantic similarity search.
Uses qdrant-client in embedded file storage mode (data/qdrant) or in-memory mode (:memory:).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from ..core.errors import VectorIndexError

logger = logging.getLogger("jme.vector_store")


def to_qdrant_point_id(memory_id: str) -> str:
    """Convert an arbitrary string memory_id to a valid UUID format for Qdrant."""
    clean = str(memory_id).strip()
    try:
        return str(uuid.UUID(clean))
    except Exception:
        try:
            return str(uuid.UUID(hex=clean))
        except Exception:
            return str(uuid.uuid5(uuid.NAMESPACE_DNS, clean))


class VectorSearchResult(BaseModel):
    memory_id: str
    score: float
    payload: Dict[str, Any]


class VectorIndexStatus(BaseModel):
    collection_name: str
    storage_path: str
    total_points: int
    dimension: int
    distance: str
    is_ready: bool
    status: str
    error: Optional[str] = None


class VectorStore:
    def __init__(
        self,
        path: str = "data/qdrant",
        collection_name: str = "memories",
        dimension: int = 512,
        distance: Distance = Distance.COSINE,
    ) -> None:
        self.storage_path = str(path)
        self.collection_name = collection_name
        self.dimension = dimension
        self.distance = distance
        self._client: Optional[QdrantClient] = None
        self._is_ready = False
        self._error: Optional[str] = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            if self.storage_path == ":memory:":
                self._client = QdrantClient(":memory:")
            else:
                p = Path(self.storage_path)
                p.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(p))

            self._ensure_collection()
            self._is_ready = True
            self._error = None
            logger.info(
                "VectorStore ready (path=%s, collection=%s, dim=%d)",
                self.storage_path,
                self.collection_name,
                self.dimension,
            )
        except Exception as exc:
            self._is_ready = False
            self._error = str(exc)
            logger.error("Failed to initialize VectorStore at %s: %s", self.storage_path, exc)

    def _ensure_collection(self) -> None:
        if self._client is None:
            return
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.dimension, distance=self.distance),
            )
            logger.info("Created Qdrant collection: %s (dim=%d)", self.collection_name, self.dimension)
        else:
            info = self._client.get_collection(self.collection_name)
            existing_dim = info.config.params.vectors.size
            if existing_dim != self.dimension:
                logger.warning(
                    "Qdrant collection %s dimension mismatch: expected %d, got %d",
                    self.collection_name,
                    self.dimension,
                    existing_dim,
                )

    @property
    def is_ready(self) -> bool:
        return self._is_ready and self._client is not None

    def recreate_collection(self, dimension: Optional[int] = None) -> bool:
        """Recreate the collection with specified dimension, clearing all points."""
        if dimension is not None:
            self.dimension = dimension
        try:
            if self._client.collection_exists(self.collection_name):
                self._client.delete_collection(self.collection_name)
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.dimension, distance=self.distance),
            )
            self._is_ready = True
            self._error = None
            logger.info("Recreated Qdrant collection %s (dim=%d)", self.collection_name, self.dimension)
            return True
        except Exception as exc:
            self._error = str(exc)
            logger.error("Failed to recreate collection %s: %s", self.collection_name, exc)
            raise VectorIndexError(f"Failed to recreate collection: {exc}") from exc

    def upsert_memory(
        self,
        memory_id: str,
        vector: List[float],
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.is_ready:
            raise VectorIndexError(f"VectorStore is not ready: {self._error}")
        point_id = to_qdrant_point_id(memory_id)
        p = dict(payload or {})
        p["memory_id"] = str(memory_id)
        point = PointStruct(id=point_id, vector=vector, payload=p)
        try:
            self._client.upsert(collection_name=self.collection_name, points=[point])
            return True
        except Exception as exc:
            logger.error("Failed to upsert vector for %s: %s", memory_id, exc)
            raise VectorIndexError(f"Upsert failed: {exc}") from exc

    def upsert_batch(
        self,
        items: Sequence[Tuple[str, List[float], Dict[str, Any]]],
    ) -> int:
        if not self.is_ready:
            raise VectorIndexError(f"VectorStore is not ready: {self._error}")
        if not items:
            return 0
        points = []
        for mid, vec, payload in items:
            p = dict(payload or {})
            p["memory_id"] = str(mid)
            points.append(PointStruct(id=to_qdrant_point_id(mid), vector=vec, payload=p))
        try:
            self._client.upsert(collection_name=self.collection_name, points=points)
            return len(points)
        except Exception as exc:
            logger.error("Failed to batch upsert %d vectors: %s", len(items), exc)
            raise VectorIndexError(f"Batch upsert failed: {exc}") from exc

    def delete_memory(self, memory_id: str) -> bool:
        if not self.is_ready:
            return False
        point_id = to_qdrant_point_id(memory_id)
        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=[point_id],
            )
            return True
        except Exception as exc:
            logger.warning("Failed to delete vector for %s: %s", memory_id, exc)
            return False

    def search(
        self,
        query_vector: List[float],
        limit: int = 20,
        memory_type: Optional[str] = None,
        status: Optional[str] = "ACTIVE",
    ) -> List[VectorSearchResult]:
        if not self.is_ready:
            raise VectorIndexError(f"VectorStore is not ready: {self._error}")

        conditions = []
        if memory_type is not None:
            conditions.append(FieldCondition(key="memory_type", match=MatchValue(value=memory_type)))
        if status is not None:
            conditions.append(FieldCondition(key="status", match=MatchValue(value=status)))

        q_filter = Filter(must=conditions) if conditions else None

        try:
            query_res = self._client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=q_filter,
                limit=limit,
                with_payload=True,
            )
            results = []
            for pt in query_res.points:
                raw_payload = pt.payload or {}
                mid = str(raw_payload.get("memory_id") or pt.id)
                results.append(
                    VectorSearchResult(
                        memory_id=mid,
                        score=float(pt.score),
                        payload=raw_payload,
                    )
                )
            return results
        except Exception as exc:
            logger.error("Vector search failed: %s", exc)
            raise VectorIndexError(f"Vector search failed: {exc}") from exc

    def get_all_point_ids(self) -> Set[str]:
        """Fetch all indexed memory_ids by scrolling through points."""
        if not self.is_ready:
            return set()
        ids: Set[str] = set()
        offset = None
        while True:
            pts, next_page = self._client.scroll(
                collection_name=self.collection_name,
                offset=offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            for pt in pts:
                payload = pt.payload or {}
                mid = str(payload.get("memory_id") or pt.id)
                ids.add(mid)
            if next_page is None:
                break
            offset = next_page
        return ids

    def get_status(self) -> VectorIndexStatus:
        if not self.is_ready:
            return VectorIndexStatus(
                collection_name=self.collection_name,
                storage_path=self.storage_path,
                total_points=0,
                dimension=self.dimension,
                distance=str(self.distance.value if hasattr(self.distance, "value") else self.distance),
                is_ready=False,
                status="uninitialized",
                error=self._error,
            )
        try:
            info = self._client.get_collection(self.collection_name)
            points_count = info.points_count or 0
            dim = info.config.params.vectors.size
            return VectorIndexStatus(
                collection_name=self.collection_name,
                storage_path=self.storage_path,
                total_points=points_count,
                dimension=dim,
                distance=str(self.distance.value if hasattr(self.distance, "value") else self.distance),
                is_ready=True,
                status="ready",
                error=None,
            )
        except Exception as exc:
            return VectorIndexStatus(
                collection_name=self.collection_name,
                storage_path=self.storage_path,
                total_points=0,
                dimension=self.dimension,
                distance="unknown",
                is_ready=False,
                status="error",
                error=str(exc),
            )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._is_ready = False
