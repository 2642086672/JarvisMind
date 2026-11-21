"""JKG HTTP surface: read-only graph data plus two administrative actions.

Design rules enforced here
--------------------------
1. **Read-only graph data; writes are two administrative actions only.** The
   graph is derived data owned by a JKG worker, so an HTTP caller inspects it.
   Every endpoint that exposes graph *data* — ``/nodes``, ``/edges``,
   ``/overview``, ``/status``, ``/sync/status`` and
   ``/extraction/preview/{memory_id}`` — is ``GET`` only. The only write methods
   registered are the lifecycle sync actions added by
   JKG-INT-04, ``POST /sync/tick`` and ``POST /sync/reconcile``, and the backfill
   task actions under ``/init`` added by JKG-INT-05. The sync actions take no
   graph content as input and create no node, edge or source: they only ask the
   derived graph to be brought back in line with the authoritative memory
   tables. The ``/init`` actions only run the backfill, which derives rows from
   memories through the ingestor. ``PUT`` / ``PATCH`` / ``DELETE`` are still absent, and no route can
   create a table.
2. **Token-gated by ``main.py``.** Every route is registered with
   ``dependencies=[Depends(token_dependency)]`` exactly like the other routers;
   this module never builds its own authentication.
3. **No memory body.** Sources are reduced to ``memory_id`` / status / validity /
   invalidation metadata. ``memory_records`` is never queried for ``content``.
4. **Always sanitised.** Every payload passes through
   ``app.jkg.governance.sanitize_*`` before serialisation, so a label or metadata
   blob that grades as sensitive is withheld at the exit even if it is present
   in the row.
5. **Honest paging.** A truncated valid-only scan leaves ``meta.total = null``.
   Filling it with ``len(items)`` would turn "we stopped looking" into "this is
   all there is".
6. **Degradation is reported, not swallowed.** Disabled / not-installed states
   are raised by ``container.jkg_query_service`` as ``JmeError`` subclasses and
   bubble into the shared handler, which emits the standard error envelope. Only
   ``GET /status`` and ``GET /sync/status`` never raise, because the client needs
   one endpoint that can answer "why is this unusable?".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...jkg.governance import sanitize_edge_payload, sanitize_node_payload
from ...jkg.normalization import VALID_SOURCE_STATUSES
from ...jkg.query import (
    DEFAULT_TRAVERSAL_EDGES,
    DEFAULT_TRAVERSAL_HOPS,
    DEFAULT_TRAVERSAL_NODES,
    MAX_PAGE_SIZE,
    MAX_TRAVERSAL_EDGES,
    MAX_TRAVERSAL_HOPS,
    MAX_TRAVERSAL_NODES,
)
from ...schemas.common import DataEnvelope
from ...schemas.jkg import (
    MAX_QUERY_LENGTH,
    JkgDirection,
    JkgEdgeItem,
    JkgEdgeListQuery,
    JkgEntityType,
    JkgListEnvelope,
    JkgNeighborItem,
    JkgNeighborQuery,
    JkgNodeDetailResponse,
    JkgNodeItem,
    JkgNodeListQuery,
    JkgNodeSourceItem,
    JkgNodeStatus,
    JkgOrphanEdgeItem,
    JkgOverviewCounts,
    JkgOverviewResponse,
    JkgPredicate,
    JkgStatusResponse,
    JkgTraversalLimits,
    JkgTraversalNodeItem,
    JkgTraversalResponse,
    JkgTraverseQuery,
    build_jkg_page_meta,
)
from ..deps import get_container
from ...governance.projection import ReadOnlyProjection
from ...jkg.adapter import JkgDisabledError, JkgSchemaNotReadyError
from ...jkg.extraction import preview_memory
from ...schemas.jkg import (
    MAX_SYNC_LIMIT,
    JkgExtractionPreviewResponse,
    JkgInitFailureItem,
    JkgInitFailuresResponse,
    JkgInitPreviewResponse,
    JkgInitTaskCreateRequest,
    JkgInitTaskListResponse,
    JkgInitTaskResponse,
    JkgInitTaskStatus,
    JkgSyncReconcileResponse,
    JkgSyncStatusResponse,
    JkgSyncTickResponse,
    JkgWorkerRunResponse,
    JkgWorkerStatusResponse,
)

router = APIRouter(tags=["jkg"])

# Upper bound for the incident-edge fan-out on ``GET /nodes/{id}``. A single
# node's degree is normally tiny; the cap keeps a hub node from turning one
# detail request into an unbounded read. ``edges_truncated`` reports when it
# bites.
_NODE_EDGE_LIMIT = MAX_PAGE_SIZE

# Window used for the integrity probe behind ``GET /overview``. Matches the
# service default and stays far below ``MAX_SCAN``.
_OVERVIEW_SCAN_LIMIT = 200


def _offset(page: int, page_size: int) -> int:
    return (page - 1) * page_size


def _node_item(payload: Dict[str, Any]) -> JkgNodeItem:
    """Sanitise a node payload and coerce it to the response model."""
    return JkgNodeItem.model_validate(sanitize_node_payload(payload))


def _edge_item(payload: Dict[str, Any]) -> JkgEdgeItem:
    """Sanitise an edge payload (including any sources) and coerce it."""
    return JkgEdgeItem.model_validate(sanitize_edge_payload(payload))


# -------------------------------------------------------------------- status
@router.get("/status", response_model=JkgStatusResponse)
def get_status(container: Container = Depends(get_container)) -> JkgStatusResponse:
    """Capability snapshot. **Always 200**, never raises.

    ``health()`` is the adapter's non-throwing surface: a missing table or a
    disabled switch is *reported* here rather than thrown, so the iOS panel has
    one stable endpoint to distinguish "switched off" and "schema not installed"
    from a real failure.
    """
    return JkgStatusResponse(**container.jkg_adapter.health())


# ------------------------------------------------------------------ overview
@router.get("/overview", response_model=JkgOverviewResponse)
def get_overview(container: Container = Depends(get_container)) -> JkgOverviewResponse:
    """Counts plus a bounded validity scan.

    ``counts`` come from ``COUNT(*)`` and are exact. ``valid_edges`` /
    ``invalid_edges`` require reading every source of every edge, so they can
    only describe the edges actually scanned; ``truncated`` says whether that
    window covered the whole table.
    """
    report = container.jkg_query_service.integrity_report(
        limit=_OVERVIEW_SCAN_LIMIT
    )
    counts = report["counts"]
    scanned_edges = int(report["scanned_edges"])
    invalid_edges = len(report["invalid_edges"])
    return JkgOverviewResponse(
        counts=JkgOverviewCounts(**counts),
        scanned_edges=scanned_edges,
        # Never negative: ``invalid_edges`` is a subset of the scanned rows.
        valid_edges=max(0, scanned_edges - invalid_edges),
        invalid_edges=invalid_edges,
        truncated=bool(report["truncated"]),
        orphan_edges=[
            JkgOrphanEdgeItem(**orphan) for orphan in report["orphan_edges"]
        ],
    )


# --------------------------------------------------------------------- nodes
@router.get("/nodes", response_model=JkgListEnvelope[JkgNodeItem])
def list_nodes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    entity_type: Optional[JkgEntityType] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=MAX_QUERY_LENGTH),
    status: Optional[JkgNodeStatus] = Query(default=None),
    container: Container = Depends(get_container),
) -> JkgListEnvelope[JkgNodeItem]:
    """Page through entities, filtered by type, label substring and/or status."""
    query = JkgNodeListQuery(
        page=page, page_size=page_size, entity_type=entity_type, q=q, status=status
    )
    result = container.jkg_query_service.list_nodes(
        entity_type=query.entity_type,
        query=query.q,
        status=query.status,
        limit=query.page_size,
        offset=_offset(query.page, query.page_size),
    )
    return JkgListEnvelope[JkgNodeItem](
        data=[_node_item(item) for item in result["items"]],
        meta=build_jkg_page_meta(
            page=query.page,
            page_size=query.page_size,
            # ``list_nodes`` counts exactly, so a null here would be a bug, not
            # a truncation; pass it through either way rather than guessing.
            total=result.get("total"),
        ),
    )


@router.get("/nodes/{node_id}", response_model=DataEnvelope[JkgNodeDetailResponse])
def get_node(
    node_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[JkgNodeDetailResponse]:
    """One entity with both incident edge directions and its source memory.

    Edges are fetched with ``valid_only=False`` on purpose: the detail view must
    show an invalidated relation *as* an invalidated one (each carries
    ``is_valid``), not hide it. A missing node surfaces the service's
    ``NotFoundError`` → 404 envelope.
    """
    service = container.jkg_query_service
    node_payload = service.get_node(node_id)
    # Compare on the persisted id, not the raw path segment: ``get_node`` trims
    # the argument before lookup, so a padded id would otherwise match nothing.
    canonical_id = str(node_payload["node_id"])

    edges = service.list_edges(
        node_id=canonical_id,
        direction="both",
        valid_only=False,
        limit=_NODE_EDGE_LIMIT,
        offset=0,
    )
    outgoing: List[JkgEdgeItem] = []
    incoming: List[JkgEdgeItem] = []
    for item in edges["items"]:
        if item["subject_id"] == canonical_id:
            outgoing.append(_edge_item(item))
        elif item["object_id"] == canonical_id:
            incoming.append(_edge_item(item))

    known_total: Optional[int] = edges.get("total")
    fetched = len(edges["items"])
    # ``valid_only=False`` gives an exact total, but a service change could make
    # it null; treat "more rows exist than we fetched" as the truncation signal
    # in both cases and never claim a full picture we did not read.
    truncated = known_total is None and fetched >= _NODE_EDGE_LIMIT
    if known_total is not None and fetched < known_total:
        truncated = True

    status = node_payload.get("source_memory_status")
    source = JkgNodeSourceItem(
        memory_id=str(node_payload["source_memory_id"]),
        memory_status=status,
        is_valid=status in VALID_SOURCE_STATUSES,
    )

    return DataEnvelope(
        data=JkgNodeDetailResponse(
            node=_node_item(node_payload),
            outgoing_edges=outgoing,
            incoming_edges=incoming,
            sources=[source],
            edge_total=known_total,
            edges_truncated=truncated,
        )
    )


@router.get(
    "/nodes/{node_id}/neighbors", response_model=JkgListEnvelope[JkgNeighborItem]
)
def list_neighbors(
    node_id: str,
    direction: JkgDirection = Query("both"),
    predicate: Optional[JkgPredicate] = Query(default=None),
    valid_only: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    container: Container = Depends(get_container),
) -> JkgListEnvelope[JkgNeighborItem]:
    """One-hop neighbours only.

    ``truncated`` / ``scanned`` / ``candidate_total`` are passed through
    verbatim: a valid-only traversal is bounded, and the caller must be able to
    see that the window — not the graph — ended.
    """
    query = JkgNeighborQuery(
        direction=direction,
        predicate=predicate,
        valid_only=valid_only,
        page=page,
        page_size=page_size,
    )
    result = container.jkg_query_service.neighbors(
        node_id=node_id,
        direction=query.direction,
        predicate=query.predicate,
        valid_only=query.valid_only,
        limit=query.page_size,
        offset=_offset(query.page, query.page_size),
    )
    items = [
        JkgNeighborItem(
            node=_node_item(entry["node"]),
            edge=_edge_item(entry["edge"]),
            direction=entry["direction"],
        )
        for entry in result["items"]
    ]
    return JkgListEnvelope[JkgNeighborItem](
        data=items,
        meta=build_jkg_page_meta(
            page=query.page,
            page_size=query.page_size,
            total=result.get("total"),
            truncated=bool(result.get("truncated", False)),
            scanned=result.get("scanned"),
            candidate_total=result.get("candidate_total"),
        ),
    )


@router.get(
    "/nodes/{node_id}/traverse", response_model=DataEnvelope[JkgTraversalResponse]
)
def traverse_node(
    node_id: str,
    hops: int = Query(DEFAULT_TRAVERSAL_HOPS, ge=1, le=MAX_TRAVERSAL_HOPS),
    direction: JkgDirection = Query("both"),
    predicate: Optional[JkgPredicate] = Query(default=None),
    valid_only: bool = Query(True),
    max_nodes: int = Query(DEFAULT_TRAVERSAL_NODES, ge=1, le=MAX_TRAVERSAL_NODES),
    max_edges: int = Query(DEFAULT_TRAVERSAL_EDGES, ge=1, le=MAX_TRAVERSAL_EDGES),
    container: Container = Depends(get_container),
) -> DataEnvelope[JkgTraversalResponse]:
    """Bounded breadth-first walk (JKG-INT-02). Read-only.

    ``truncated`` says a hard cap (``max_nodes`` / ``max_edges`` / scan budget)
    stopped the walk; it is never silent. Nodes keep their source validity flags
    and are not hidden. Edges carry no source detail; use ``/edges/{edge_id}``.
    """
    query = JkgTraverseQuery(
        hops=hops,
        direction=direction,
        predicate=predicate,
        valid_only=valid_only,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    result = container.jkg_query_service.traverse_nodes(
        node_id,
        hops=query.hops,
        direction=query.direction,
        predicate=query.predicate,
        valid_only=query.valid_only,
        max_nodes=query.max_nodes,
        max_edges=query.max_edges,
    )
    return DataEnvelope(
        data=JkgTraversalResponse(
            root_id=result["root_id"],
            hops=result["hops"],
            direction=result["direction"],
            nodes=[
                JkgTraversalNodeItem(node=_node_item(entry["node"]), hop=entry["hop"])
                for entry in result["nodes"]
            ],
            edges=[_edge_item(item) for item in result["edges"]],
            truncated=bool(result["truncated"]),
            visited_nodes=result["visited_nodes"],
            visited_edges=result["visited_edges"],
            limits=JkgTraversalLimits(**result["limits"]),
        )
    )


# --------------------------------------------------------------------- edges
@router.get("/edges", response_model=JkgListEnvelope[JkgEdgeItem])
def list_edges(
    node_id: Optional[str] = Query(default=None, max_length=128),
    predicate: Optional[JkgPredicate] = Query(default=None),
    direction: JkgDirection = Query("both"),
    valid_only: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    container: Container = Depends(get_container),
) -> JkgListEnvelope[JkgEdgeItem]:
    """Page through relations.

    ``valid_only`` defaults to **true**: a relation with no live source is
    derived from a memory the user forgot, so it does not belong in a default
    listing. Asking for ``valid_only=false`` returns them with ``is_valid``
    marked, which is what the panel's diagnostics view needs.
    """
    query = JkgEdgeListQuery(
        node_id=node_id,
        predicate=predicate,
        direction=direction,
        valid_only=valid_only,
        page=page,
        page_size=page_size,
    )
    result = container.jkg_query_service.list_edges(
        node_id=query.node_id,
        predicate=query.predicate,
        direction=query.direction,
        valid_only=query.valid_only,
        limit=query.page_size,
        offset=_offset(query.page, query.page_size),
    )
    truncated = bool(result.get("truncated", False))
    return JkgListEnvelope[JkgEdgeItem](
        data=[_edge_item(item) for item in result["items"]],
        meta=build_jkg_page_meta(
            page=query.page,
            page_size=query.page_size,
            # ``total`` is None exactly when the valid-only scan was capped —
            # keep it null instead of substituting len(items).
            total=result.get("total"),
            truncated=truncated,
            scanned=result.get("scanned"),
            candidate_total=result.get("candidate_total"),
        ),
    )


@router.get("/edges/{edge_id}", response_model=DataEnvelope[JkgEdgeItem])
def get_edge(
    edge_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[JkgEdgeItem]:
    """One relation with **every** source, including invalidated ones.

    ``invalidation_reason`` is part of the payload so an operator can explain why
    a relation lost its provenance. Sources still carry no memory content.
    """
    payload = container.jkg_query_service.get_edge(edge_id, valid_only=False)
    # ``_edge_item`` already validates ``sources`` into ``JkgSourceItem`` via
    # ``sanitize_edge_payload``; the model's default empty list covers the
    # include_sources=False shape, so no second copy is needed.
    return DataEnvelope(data=_edge_item(payload))


__all__ = ["router"]


# ===========================================================================
# Lifecycle sync (JKG-INT-04)
# ===========================================================================
# The three routes below are the ONLY HTTP surface that may touch ``jkg_*``
# tables. They are administrative actions over derived data, not a data-writing
# API for memories: nothing here creates a memory, and every write it performs is
# an invalidation/validation of derivations whose authority lives in JME.
#
# ``GET /sync/status`` stays 200 in every state, matching ``/status``: the panel
# needs one endpoint that can always answer "why can't I tick?".


@router.get("/sync/status", response_model=JkgSyncStatusResponse)
def get_sync_status(
    container: Container = Depends(get_container),
) -> JkgSyncStatusResponse:
    """Lifecycle-sync snapshot. **Always 200**, read-only.

    Reads the ``jkg.sync.*`` cursor and the last tick record; creates nothing.
    ``usable`` is the answer to "can a tick do anything right now", so a client
    does not have to combine ``state`` with its own guess about the schema.
    """
    return JkgSyncStatusResponse(**container.jkg_lifecycle.status())


@router.post("/sync/tick", response_model=JkgSyncTickResponse)
def post_sync_tick(
    limit: int = Query(200, ge=1, le=MAX_SYNC_LIMIT),
    dry_run: bool = Query(False),
    container: Container = Depends(get_container),
) -> JkgSyncTickResponse:
    """Run exactly one lifecycle pass (pull-based; no daemon is started).

    ``dry_run=true`` reads and computes but writes nothing at all — no cursor, no
    graph. The response always states ``dry_run`` and ``wrote`` so the caller can
    tell a preview from a real run without inferring it from the counters.

    A per-memory failure is isolated: it appears in ``errors`` and sets
    ``ok=false``, and the batch continues.
    """
    result = container.jkg_lifecycle.tick(limit=limit, dry_run=dry_run)
    return JkgSyncTickResponse(**result)


@router.post("/sync/reconcile", response_model=JkgSyncReconcileResponse)
def post_sync_reconcile(
    limit: int = Query(200, ge=1, le=MAX_SYNC_LIMIT),
    after_memory_id: str = Query("", max_length=MAX_QUERY_LENGTH),
    container: Container = Depends(get_container),
) -> JkgSyncReconcileResponse:
    """Run one keyset-cursored reconcile page over the distinct source memories.

    Call it again with ``next_after_memory_id`` while ``has_more`` is true. Unlike
    ``JkgService.reconcile`` this version cannot be aborted by one bad memory, so
    a caller can loop it safely.
    """
    result = container.jkg_lifecycle.reconcile(
        limit=limit, after_memory_id=after_memory_id
    )
    return JkgSyncReconcileResponse(**result)


# ===========================================================================
# Worker (P0-1 / P0-2 / P0-3)
# ===========================================================================
# Two routes, deliberately asymmetric:
#
# * ``GET /worker/status`` is **always 200**, in every state, like ``/status``
#   and ``/sync/status``. It is the one endpoint a scheduler can poll without
#   first knowing whether the feature is switched on.
# * ``POST /worker/run`` executes one pass. It returns 200 even when it declines
#   to run (``skipped`` + ``skip_reason``): a lease refusal is not a client error,
#   and turning it into 409/503 would make every ordinary "somebody else is
#   running" collide with a real outage in any dashboard counting 5xx.
#
# Neither route degrades to 503 on disabled/schema_not_ready. That is the same
# choice ``/sync/status`` made and for the same reason: derived-data maintenance
# is not a data endpoint. A 503 would tell a scheduler the service is broken when
# the truthful answer is "the switch is off".


@router.get("/worker/status", response_model=JkgWorkerStatusResponse)
def get_worker_status(
    container: Container = Depends(get_container),
) -> JkgWorkerStatusResponse:
    """Worker snapshot. **Always 200**, read-only, writes nothing.

    Reads the worker's own ``jkg.worker.*`` records plus the lease. It creates
    no table and no row: a status probe on a fresh database must leave the
    database exactly as it found it.
    """
    return JkgWorkerStatusResponse(**container.jkg_worker.status())


@router.post("/worker/run", response_model=JkgWorkerRunResponse)
def post_worker_run(
    limit: Optional[int] = Query(None, ge=1, le=MAX_SYNC_LIMIT),
    dry_run: bool = Query(False),
    container: Container = Depends(get_container),
) -> JkgWorkerRunResponse:
    """Run exactly one worker pass (pull-based; no daemon is started).

    ``limit`` defaults to the configured ``jkg_worker_batch_limit`` when omitted,
    so an operator who never passes it still gets the install's own batch size
    instead of whatever this route happens to hard-code.

    ``dry_run=true`` takes no lease and writes nothing at all (not the cursor,
    not the graph, not the worker bookkeeping), so it can never contend with a
    real pass.

    Every outcome is 200: a skip is a normal result, and a failure is reported
    as ``ok=false`` plus an ``errors`` entry — never as a 5xx, because the whole
    point of a never-raising worker is that its caller does not have to catch.
    """
    result = container.jkg_worker.run_once(limit=limit, dry_run=dry_run)
    return JkgWorkerRunResponse(**result)


@router.get(
    "/extraction/preview/{memory_id}", response_model=JkgExtractionPreviewResponse
)
def get_extraction_preview(
    memory_id: str,
    container: Container = Depends(get_container),
) -> JkgExtractionPreviewResponse:
    """Rule-based extraction preview for one memory (C3: never persisted).

    Reads the memory through the read-only projection, runs the pure rule set and
    returns the candidates. Nothing is written: no ``jkg_*`` row, no candidate
    table, no cursor. A missing memory is 404 (it is not "an empty preview"), and
    a graph that is switched off or not installed is 503, because in those states
    the preview could not be acted on even if it were correct.
    """
    status = container.jkg_adapter.status()
    if status.get("state") != "ready":
        raise (
            JkgDisabledError()
            if status.get("state") == "disabled"
            else JkgSchemaNotReadyError(
                detail={"missing": status.get("missing_tables", [])}
            )
        )
    projection = ReadOnlyProjection(container.manager).for_module("JKG")
    result = preview_memory(projection, memory_id)
    payload = result.to_dict()
    payload["persisted"] = False
    return JkgExtractionPreviewResponse(**payload)


# ===========================================================================
# Historical initialization (JKG-INT-05)
# ===========================================================================
# Admin surface of the one-shot backfill. Every state change goes through
# ``container.jkg_initializer``; these routes never touch ``jkg_*`` tables.
# Task creation is refused while the graph is off, mirroring the preview above:
# the service only checks the P1 registry at create time.


def _require_init_ready(container: Container) -> None:
    status = container.jkg_adapter.status()
    if status.get("state") != "ready":
        raise (
            JkgDisabledError()
            if status.get("state") == "disabled"
            else JkgSchemaNotReadyError(
                detail={"missing": status.get("missing_tables", [])}
            )
        )


@router.get("/init/preview", response_model=JkgInitPreviewResponse)
def get_init_preview(
    container: Container = Depends(get_container),
) -> JkgInitPreviewResponse:
    """What a backfill would scan right now. Writes nothing.

    ``p1_available`` says whether the task registry is installed; when it is
    false, create and run are unavailable but the preview still answers.
    """
    return JkgInitPreviewResponse(**container.jkg_initializer.preview())


@router.post(
    "/init/tasks", response_model=JkgInitTaskResponse, status_code=201
)
def create_init_task(
    body: JkgInitTaskCreateRequest,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    """Create a PENDING backfill task. Nothing is scanned until it is run."""
    _require_init_ready(container)
    task = container.jkg_initializer.create_task(
        batch_size=body.batch_size, mode=body.mode
    )
    return JkgInitTaskResponse(**task)


@router.get("/init/tasks", response_model=JkgInitTaskListResponse)
def list_init_tasks(
    status: Optional[JkgInitTaskStatus] = Query(default=None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    container: Container = Depends(get_container),
) -> JkgInitTaskListResponse:
    """Newest first. ``total`` counts every task that matches the filter."""
    result = container.jkg_initializer.list_tasks(
        status=status, limit=limit, offset=offset
    )
    return JkgInitTaskListResponse(
        data=[JkgInitTaskResponse(**item) for item in result["data"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/init/tasks/{task_id}", response_model=JkgInitTaskResponse)
def get_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    return JkgInitTaskResponse(**container.jkg_initializer.get_task(task_id))


@router.post("/init/tasks/{task_id}/run", response_model=JkgInitTaskResponse)
def run_init_task(
    task_id: str,
    max_batches: int = Query(1, ge=1, le=20),
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    """Advance the task by at most ``max_batches`` batches, then return its state.

    A run stops early at a pause or cancel, and at completion; it returns
    ``RUNNING`` otherwise, so the caller calls again until a terminal state.
    """
    return JkgInitTaskResponse(
        **container.jkg_initializer.run_task(task_id, max_batches=max_batches)
    )


@router.post("/init/tasks/{task_id}/pause", response_model=JkgInitTaskResponse)
def pause_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    return JkgInitTaskResponse(**container.jkg_initializer.pause_task(task_id))


@router.post("/init/tasks/{task_id}/resume", response_model=JkgInitTaskResponse)
def resume_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    return JkgInitTaskResponse(**container.jkg_initializer.resume_task(task_id))


@router.post("/init/tasks/{task_id}/cancel", response_model=JkgInitTaskResponse)
def cancel_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    return JkgInitTaskResponse(**container.jkg_initializer.cancel_task(task_id))


@router.post("/init/tasks/{task_id}/retry", response_model=JkgInitTaskResponse)
def retry_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    """Re-open a FAILED task in place; it keeps its cursor and counters."""
    return JkgInitTaskResponse(**container.jkg_initializer.retry_task(task_id))


@router.post(
    "/init/tasks/{task_id}/retry-failed",
    response_model=JkgInitTaskResponse,
    status_code=201,
)
def retry_failed_init_task(
    task_id: str,
    container: Container = Depends(get_container),
) -> JkgInitTaskResponse:
    """Spawn a new retry task for the memories that failed in a completed task."""
    return JkgInitTaskResponse(
        **container.jkg_initializer.retry_failed_task(task_id)
    )


@router.get(
    "/init/tasks/{task_id}/failures", response_model=JkgInitFailuresResponse
)
def list_init_failures(
    task_id: str,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    container: Container = Depends(get_container),
) -> JkgInitFailuresResponse:
    """Failures recorded for a task. ``failed_total`` is the true count.

    The service keeps at most 100 failure rows; a task with more failures reports
    the full count in ``failed_total`` and only the kept rows in ``failures``.
    """
    result = container.jkg_initializer.failures(task_id)
    page = list(result["failures"])[offset : offset + limit]
    return JkgInitFailuresResponse(
        task_id=result["task_id"],
        failed_total=result["failed_total"],
        failures=[JkgInitFailureItem(**item) for item in page],
        limit=limit,
        offset=offset,
    )
