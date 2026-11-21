"""Read-only JKG query service.

Neighbour reads are one hop. ``traverse_nodes`` (JKG-INT-02) is the only
multi-hop read: a bounded breadth-first walk capped by ``MAX_TRAVERSAL_HOPS``,
``MAX_TRAVERSAL_NODES``, ``MAX_TRAVERSAL_EDGES`` and ``MAX_TRAVERSAL_SCAN``, with
a visited set against cycles. There is no recursive CTE and no closure table.
Every listing is bounded (``MAX_PAGE_SIZE``), and valid-only scans are
additionally capped by ``MAX_SCAN`` so a corrupt graph cannot turn a query into
an unbounded table walk.

Source validity is always recomputed at read time from the JME read-only
projection. A stored ``invalidated_at`` alone is not enough: a memory may have
been forgotten without the JKG lifecycle sync having run yet.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.errors import NotFoundError, ValidationError
from ..governance.projection import ReadOnlyProjection
from .normalization import VALID_SOURCE_STATUSES
from .repository import JkgRepository
from .schema import missing_jkg_tables
from .serialization import edge_payload, node_payload

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_SCAN = 1000
_SCAN_PAGE_SIZE = 200

MAX_TRAVERSAL_HOPS = 3
MAX_TRAVERSAL_NODES = 100
MAX_TRAVERSAL_EDGES = 1000
DEFAULT_TRAVERSAL_HOPS = 1
DEFAULT_TRAVERSAL_NODES = 50
DEFAULT_TRAVERSAL_EDGES = 200
# Candidate edge rows one traversal may read across all hops combined.
MAX_TRAVERSAL_SCAN = 5000

NODE_STATUS_VALUES = ("ACTIVE", "INVALID")


def _check_int_range(name: str, value: Any, low: int, high: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValidationError(f"{name} must be between {low} and {high}")


class JkgQueryService:
    """Read-only access to entities, relations, and source provenance."""

    def __init__(self, manager, *, projection=None) -> None:
        self.manager = manager
        self.projection = projection or ReadOnlyProjection(manager).for_module("JKG")

    # ------------------------------------------------------------- preconditions
    def _require_schema(self) -> None:
        with self.manager.session() as session:
            missing = missing_jkg_tables(session)
        if missing:
            from .errors import JkgSchemaNotReadyError

            raise JkgSchemaNotReadyError(
                "The JKG schema has not been approved and installed",
                detail={"code": "jkg_schema_not_ready", "missing": missing},
            )

    @staticmethod
    def _check_page(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValidationError("limit must be an integer")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(
                f"limit must be between 1 and {MAX_PAGE_SIZE}"
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")

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

    def _edge_payloads(self, repo: JkgRepository, edges, *, include_sources: bool):
        payloads = []
        for edge in edges:
            sources = repo.list_sources(edge.edge_id)
            statuses = self._memory_statuses(
                source.memory_id for source in sources
            )
            payloads.append(
                edge_payload(
                    edge, sources, statuses, include_sources=include_sources
                )
            )
        return payloads

    # -------------------------------------------------------------------- nodes
    def get_node(self, node_id: str) -> Dict[str, Any]:
        self._require_schema()
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValidationError("node_id must be a non-empty string")
        with self.manager.session() as session:
            node = JkgRepository(session).get_node(node_id.strip())
            if node is None:
                raise NotFoundError(f"JKG node {node_id} not found")
            return node_payload(
                node,
                source_memory_status=self._memory_status(node.source_memory_id),
            )

    def list_nodes(
        self,
        *,
        entity_type: Optional[str] = None,
        query: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List nodes, labelling each with its source memory's current state.

        Every item carries ``source_memory_status`` and ``source_memory_valid``,
        computed at read time exactly as ``get_node`` does. Nodes are never
        hidden: a node whose source memory was forgotten (or no longer exists)
        is listed with ``source_memory_valid=False``; an unknown memory reports
        ``source_memory_status=None``. Paging, order and filters are unchanged.
        """
        self._require_schema()
        self._check_page(limit, offset)
        from .normalization import (
            normalize_entity_name,
            validate_entity_type,
        )

        entity = validate_entity_type(entity_type) if entity_type else None
        normalized_query = normalize_entity_name(query) if query else None
        if status is not None and status not in NODE_STATUS_VALUES:
            raise ValidationError(
                "status must be one of " + ", ".join(NODE_STATUS_VALUES)
            )
        with self.manager.session() as session:
            rows, total = JkgRepository(session).list_nodes(
                entity_type=entity,
                normalized_query=normalized_query,
                status=status,
                limit=limit,
                offset=offset,
            )
            statuses = self._memory_statuses(
                node.source_memory_id for node in rows
            )
            items = [
                node_payload(
                    node,
                    source_memory_status=statuses.get(node.source_memory_id),
                )
                for node in rows
            ]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # -------------------------------------------------------------------- edges
    def get_edge(
        self, edge_id: str, *, valid_only: bool = False
    ) -> Dict[str, Any]:
        self._require_schema()
        if not isinstance(edge_id, str) or not edge_id.strip():
            raise ValidationError("edge_id must be a non-empty string")
        with self.manager.session() as session:
            repo = JkgRepository(session)
            edge = repo.get_edge(edge_id.strip())
            if edge is None:
                raise NotFoundError(f"JKG edge {edge_id} not found")
            payload = self._edge_payloads(
                repo, [edge], include_sources=True
            )[0]
        if valid_only and not payload["is_valid"]:
            raise NotFoundError(f"JKG edge {edge_id} has no valid source")
        return payload

    def list_edges(
        self,
        *,
        node_id: Optional[str] = None,
        predicate: Optional[str] = None,
        direction: str = "both",
        valid_only: bool = True,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Dict[str, Any]:
        self._require_schema()
        self._check_page(limit, offset)
        from .normalization import validate_relation_type

        if direction not in {"outgoing", "incoming", "both"}:
            raise ValidationError(
                "direction must be 'outgoing', 'incoming', or 'both'"
            )
        relation = validate_relation_type(predicate) if predicate else None
        node = node_id.strip() if isinstance(node_id, str) and node_id.strip() else None
        if node_id is not None and node is None:
            raise ValidationError("node_id must be a non-empty string")
        if node is None and direction != "both":
            raise ValidationError("direction requires node_id")

        with self.manager.session() as session:
            repo = JkgRepository(session)
            if not valid_only:
                rows, total = repo.list_edges(
                    node_id=node,
                    predicate=relation,
                    direction=direction,
                    limit=limit,
                    offset=offset,
                )
                items = self._edge_payloads(
                    repo, rows, include_sources=False
                )
                return {
                    "items": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "scanned": len(rows),
                    "truncated": False,
                }

            # Valid-only filtering needs the source statuses, so scan a bounded
            # number of candidate edges and stop once the requested page is
            # filled. A corrupt graph cannot force an unbounded walk.
            matched: List[Dict[str, Any]] = []
            scanned = 0
            scan_offset = 0
            candidate_total: Optional[int] = None
            exhausted = False
            while scanned < MAX_SCAN and len(matched) < offset + limit:
                rows, total = repo.list_edges(
                    node_id=node,
                    predicate=relation,
                    direction=direction,
                    limit=_SCAN_PAGE_SIZE,
                    offset=scan_offset,
                )
                if candidate_total is None:
                    candidate_total = total
                if not rows:
                    exhausted = True
                    break
                scanned += len(rows)
                scan_offset += len(rows)
                matched.extend(
                    item
                    for item in self._edge_payloads(
                        repo, rows, include_sources=False
                    )
                    if item["is_valid"]
                )
                if scan_offset >= total:
                    exhausted = True
                    break

            page = matched[offset : offset + limit]
            return {
                "items": page,
                "total": len(matched) if exhausted else None,
                "candidate_total": candidate_total,
                "limit": limit,
                "offset": offset,
                "scanned": scanned,
                "truncated": not exhausted,
            }

    def neighbors(
        self,
        *,
        node_id: str,
        direction: str = "both",
        predicate: Optional[str] = None,
        valid_only: bool = True,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return direct neighbours only (one hop, never recursive)."""
        self._require_schema()
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValidationError(
                "direction must be 'outgoing', 'incoming', or 'both'"
            )
        self._check_page(limit, offset)
        with self.manager.session() as session:
            if JkgRepository(session).get_node(node_id) is None:
                raise NotFoundError(f"JKG node {node_id} not found")
        edge_page = self.list_edges(
            node_id=node_id,
            predicate=predicate,
            direction=direction,
            valid_only=valid_only,
            limit=limit,
            offset=offset,
        )

        items: List[Dict[str, Any]] = []
        with self.manager.session() as session:
            repo = JkgRepository(session)
            for edge_item in edge_page["items"]:
                if edge_item["subject_id"] == node_id:
                    neighbour_id = edge_item["object_id"]
                    edge_direction = "outgoing"
                elif edge_item["object_id"] == node_id:
                    neighbour_id = edge_item["subject_id"]
                    edge_direction = "incoming"
                else:  # pragma: no cover - the repository filter prevents this
                    continue
                neighbour = repo.get_node(neighbour_id)
                if neighbour is None:
                    continue
                items.append(
                    {
                        "node": node_payload(
                            neighbour,
                            source_memory_status=self._memory_status(
                                neighbour.source_memory_id
                            ),
                        ),
                        "edge": edge_item,
                        "direction": edge_direction,
                    }
                )
        return {
            "node_id": node_id,
            "direction": direction,
            "items": items,
            "total": edge_page.get("total"),
            "candidate_total": edge_page.get("candidate_total"),
            "limit": limit,
            "offset": offset,
            "scanned": edge_page["scanned"],
            "truncated": edge_page["truncated"],
        }

    # ---------------------------------------------------------------- traversal
    def traverse_nodes(
        self,
        node_id: str,
        *,
        hops: int = DEFAULT_TRAVERSAL_HOPS,
        direction: str = "both",
        predicate: Optional[str] = None,
        valid_only: bool = True,
        max_nodes: int = DEFAULT_TRAVERSAL_NODES,
        max_edges: int = DEFAULT_TRAVERSAL_EDGES,
    ) -> Dict[str, Any]:
        """Bounded breadth-first walk from one node. Read-only, never writes.

        Hop 0 is the root. Each node is returned once, at the hop where it was
        first reached. Every returned edge has both endpoints among the returned
        nodes. Nodes are never hidden: each carries its source memory's current
        status, exactly as ``get_node`` does. ``valid_only`` only decides whether
        edges with no live source are followed and returned.

        ``max_nodes`` and ``max_edges`` are hard gates: the walk stops at the first
        item that would exceed them and sets ``truncated``. Edges are returned
        without source detail; use ``get_edge`` for that.
        """
        self._require_schema()
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValidationError("node_id must be a non-empty string")
        root_id = node_id.strip()
        _check_int_range("hops", hops, 1, MAX_TRAVERSAL_HOPS)
        _check_int_range("max_nodes", max_nodes, 1, MAX_TRAVERSAL_NODES)
        _check_int_range("max_edges", max_edges, 1, MAX_TRAVERSAL_EDGES)
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValidationError(
                "direction must be 'outgoing', 'incoming', or 'both'"
            )
        from .normalization import validate_relation_type

        relation = validate_relation_type(predicate) if predicate else None
        with self.manager.session() as session:
            repo = JkgRepository(session)
            root = repo.get_node(root_id)
            if root is None:
                raise NotFoundError(f"JKG node {root_id} not found")
            nodes: List[Dict[str, Any]] = [
                {
                    "node": node_payload(
                        root,
                        source_memory_status=self._memory_status(
                            root.source_memory_id
                        ),
                    ),
                    "hop": 0,
                }
            ]
            edges: List[Dict[str, Any]] = []
            visited = {root.node_id}
            seen_edges = set()
            frontier: List[Tuple[str, int]] = [(root.node_id, 0)]
            scan_left = MAX_TRAVERSAL_SCAN
            truncated = False
            cursor = 0
            while cursor < len(frontier) and not truncated:
                current, hop = frontier[cursor]
                cursor += 1
                if hop >= hops:
                    continue
                offset = 0
                node_scanned = 0
                total: Optional[int] = None
                while not truncated:
                    if total is not None and offset >= total:
                        break
                    page = min(_SCAN_PAGE_SIZE, MAX_SCAN - node_scanned, scan_left)
                    if page <= 0:
                        truncated = True
                        break
                    rows, total = repo.list_edges(
                        node_id=current,
                        predicate=relation,
                        direction=direction,
                        limit=page,
                        offset=offset,
                    )
                    if not rows:
                        break
                    offset += len(rows)
                    node_scanned += len(rows)
                    scan_left -= len(rows)
                    for payload in self._edge_payloads(
                        repo, rows, include_sources=False
                    ):
                        if valid_only and not payload["is_valid"]:
                            continue
                        if payload["edge_id"] in seen_edges:
                            continue
                        if payload["subject_id"] == current:
                            other = payload["object_id"]
                        else:
                            other = payload["subject_id"]
                        is_new = other not in visited
                        neighbour = None
                        if is_new:
                            if len(nodes) >= max_nodes:
                                truncated = True
                                break
                            neighbour = repo.get_node(other)
                            if neighbour is None:
                                continue
                        if len(edges) >= max_edges:
                            truncated = True
                            break
                        seen_edges.add(payload["edge_id"])
                        edges.append(payload)
                        if neighbour is not None:
                            visited.add(other)
                            nodes.append(
                                {
                                    "node": node_payload(
                                        neighbour,
                                        source_memory_status=self._memory_status(
                                            neighbour.source_memory_id
                                        ),
                                    ),
                                    "hop": hop + 1,
                                }
                            )
                            if hop + 1 < hops:
                                frontier.append((other, hop + 1))

        return {
            "root_id": root_id,
            "hops": hops,
            "direction": direction,
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "visited_nodes": len(nodes),
            "visited_edges": len(edges),
            "limits": {
                "hops": hops,
                "max_nodes": max_nodes,
                "max_edges": max_edges,
            },
        }

    def trace_edge(self, edge_id: str) -> Dict[str, Any]:
        """Return an edge with every source, including invalidated ones."""
        return self.get_edge(edge_id, valid_only=False)

    # --------------------------------------------------------------- integrity
    def integrity_report(self, *, limit: int = 200) -> Dict[str, Any]:
        """Bounded corruption probe used by tests and migration verification."""
        self._require_schema()
        if not 1 <= limit <= MAX_SCAN:
            raise ValidationError(f"limit must be between 1 and {MAX_SCAN}")
        with self.manager.session() as session:
            repo = JkgRepository(session)
            orphans = repo.orphan_edges(limit=limit)
            orphan_payloads = [
                {
                    "edge_id": edge.edge_id,
                    "predicate": edge.predicate,
                    "subject_id": edge.subject_id,
                    "object_id": edge.object_id,
                    "source_count": 0,
                }
                for edge in orphans
            ]
            edges = repo.all_edges(limit=limit)
            payloads = self._edge_payloads(repo, edges, include_sources=True)
            invalid_edges = []
            missing_sources = []
            ineligible_sources = []
            for payload in payloads:
                if not payload["is_valid"]:
                    invalid_edges.append(
                        {
                            "edge_id": payload["edge_id"],
                            "status": payload["status"],
                            "source_count": payload["source_count"],
                            "valid_source_count": payload["valid_source_count"],
                        }
                    )
                for source in payload["sources"]:
                    if source["memory_status"] is None:
                        missing_sources.append(
                            {
                                "source_id": source["source_id"],
                                "edge_id": source["edge_id"],
                                "memory_id": source["memory_id"],
                            }
                        )
                    elif source["memory_status"] not in VALID_SOURCE_STATUSES:
                        ineligible_sources.append(source)

            return {
                "counts": {
                    "nodes": repo.count_nodes(),
                    "edges": repo.count_edges(),
                    "sources": repo.count_all_sources(),
                    "invalidated_sources": repo.count_invalidated_sources(),
                },
                "orphan_edges": orphan_payloads,
                "invalid_edges": invalid_edges,
                "missing_source_memories": missing_sources,
                "ineligible_sources": ineligible_sources,
                "scanned_edges": len(edges),
                "truncated": len(edges) >= limit,
            }


__all__ = [
    "JkgQueryService",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "MAX_SCAN",
    "MAX_TRAVERSAL_HOPS",
    "MAX_TRAVERSAL_NODES",
    "MAX_TRAVERSAL_EDGES",
    "DEFAULT_TRAVERSAL_HOPS",
    "DEFAULT_TRAVERSAL_NODES",
    "DEFAULT_TRAVERSAL_EDGES",
    "MAX_TRAVERSAL_SCAN",
    "NODE_STATUS_VALUES",
]
