"""Owner-only console API: dashboard, operations, governance overview, API
management, safe settings, exports and backups.

Every figure is computed from the selected memory space or from the control
plane. Anything the server does not implement is reported as ``not_available``
rather than being filled with invented numbers.
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ...container import Container
from ...control.sensitivity import is_protected, record_is_protected
from ...core.timeutils import utc_now_iso
from ...schemas.memory import MemoryType, record_to_response
from ..deps import get_container
from .control import _memory_counts

router = APIRouter(tags=["console"])

BACKUP_DIRNAME = "backups"
BACKUP_NAME_RE = re.compile(r"^jarvismind-\d{8}T\d{6}Z(?:-\d{1,4})?\.db$")
EXPORT_FORMAT = "jarvismind-export"
EXPORT_MAX_ROWS = 5000
EXPORT_GRAPH_MAX = 1000
REDACTED = "[redacted: sensitive]"
_BACKUP_LOCK = threading.Lock()


def _backup_dir(container: Container) -> Path:
    """Backups belong to the selected memory space, never to the installation as a whole."""
    return container.settings.data_dir / BACKUP_DIRNAME


def _rows(container: Container, sql: str, params: Optional[Dict[str, Any]] = None) -> list:
    with container.manager.session() as session:
        return [tuple(row) for row in session.execute(text(sql), params or {}).all()]


def _scalar(container: Container, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
    rows = _rows(container, sql, params)
    return int(rows[0][0]) if rows and rows[0][0] is not None else 0


def _day(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).date().isoformat()


def _pending_counts(container: Container) -> Dict[str, int]:
    candidates = {
        str(status): int(count)
        for status, count in _rows(
            container, "SELECT status, COUNT(*) FROM memory_candidates GROUP BY status"
        )
    }
    _rows_forget, forget_total = container.forget_service.list_requests(
        status="PENDING", memory_id=None, page=1, page_size=1
    )
    _rows_mod, modification_total = container.modification_service.list_requests(
        memory_id=None, status="PENDING", page=1, page_size=1
    )
    return {
        "candidates": candidates.get("PENDING", 0),
        "sensitive_authorization": candidates.get("PENDING_AUTHORIZATION", 0),
        "forget_requests": int(forget_total),
        "modification_requests": int(modification_total),
        "rejected_candidates": candidates.get("REJECTED", 0),
    }


def _graph_summary(container: Container) -> Dict[str, Any]:
    try:
        health = container.jkg_adapter.health()
    except Exception:  # noqa: BLE001 - status panel must never fail
        return {"status": "unavailable"}
    try:
        report = container.jkg_query_service.integrity_report(limit=500)
    except Exception:  # noqa: BLE001 - schema missing or graph disabled
        return {"status": "not_available", "health": health}
    return {
        "status": "ready",
        "health": health,
        "counts": report["counts"],
        "invalid_edges": len(report["invalid_edges"]),
        "orphan_edges": len(report["orphan_edges"]),
        "scan_truncated": bool(report.get("truncated")),
    }


def _components(request: Request, container: Container) -> List[Dict[str, Any]]:
    control = request.app.state.control
    items: List[Dict[str, Any]] = [
        {"component": "API", "status": "ok", "version": container.settings.version},
        {
            "component": "JME database",
            "status": "ok" if container.manager.verify_connection() else "error",
        },
    ]
    try:
        control.list_spaces()
        items.append({"component": "Control database", "status": "ok"})
    except Exception:  # noqa: BLE001
        items.append({"component": "Control database", "status": "error"})
    try:
        jkg = container.jkg_adapter.health()
        items.append({"component": "JKG", "status": "ok", "detail": jkg})
    except Exception:  # noqa: BLE001
        items.append({"component": "JKG", "status": "unavailable"})
    try:
        mce = container.mce_adapter.status()
        items.append({"component": "MCE", "status": "ok", "detail": mce})
    except Exception:  # noqa: BLE001
        items.append({"component": "MCE", "status": "unavailable"})
    try:
        worker = container.jkg_worker.status()
        items.append({"component": "JKG worker", "status": "ok", "detail": worker})
    except Exception:  # noqa: BLE001
        items.append({"component": "JKG worker", "status": "unavailable"})
    items.append(
        {
            "component": "Uptime",
            "status": "ok",
            "seconds": int(time.monotonic() - float(container.started_at or 0.0)),
        }
    )
    return items


def _recent_activity(container: Container, request: Request, limit: int) -> List[Dict[str, Any]]:
    audit = _rows(
        container,
        "SELECT action, actor, memory_id, created_at FROM memory_audit_logs "
        "ORDER BY created_at DESC LIMIT :n",
        {"n": limit},
    )
    access, _total = request.app.state.control.list_access_logs(limit=limit, offset=0)
    items: List[Dict[str, Any]] = [
        {
            "type": "memory",
            "action": action,
            "actor": actor,
            "memory_id": memory_id,
            "at": created_at,
        }
        for action, actor, memory_id, created_at in audit
    ]
    items.extend(
        {
            "type": "api",
            "action": f"{row['method']} {row['route']}",
            "status_code": row["status_code"],
            "principal": row["principal_kind"],
            "agent_id": row["agent_id"],
            "at": row["occurred_at"],
        }
        for row in access
    )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


def _redact(value: str, include_sensitive: bool) -> str:
    if include_sensitive:
        return value
    return REDACTED if is_protected(value) else value


class BackupVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=10, max_length=80)


@router.get("/dashboard")
def dashboard(request: Request, container: Container = Depends(get_container)) -> dict:
    today = _day()
    by_status = {
        str(status): int(count)
        for status, count in _rows(
            container, "SELECT status, COUNT(*) FROM memory_records GROUP BY status"
        )
    }
    pending = _pending_counts(container)
    pending_total = (
        pending["candidates"]
        + pending["sensitive_authorization"]
        + pending["forget_requests"]
        + pending["modification_requests"]
    )
    growth = [
        {"day": day, "count": int(count)}
        for day, count in _rows(
            container,
            "SELECT substr(created_at, 1, 10) AS d, COUNT(*) FROM memory_records "
            "WHERE substr(created_at, 1, 10) >= :since GROUP BY d ORDER BY d",
            {"since": _day(13)},
        )
    ]
    types = [
        {"type": str(kind), "count": int(count)}
        for kind, count in _rows(
            container,
            "SELECT memory_type, COUNT(*) FROM memory_records GROUP BY memory_type "
            "ORDER BY 2 DESC",
        )
    ]
    sources = [
        {"source": str(kind or "UNSPECIFIED"), "count": int(count)}
        for kind, count in _rows(
            container,
            "SELECT s.source_type, COUNT(*) FROM memory_records r "
            "LEFT JOIN memory_sources s ON s.id = r.source_id "
            "GROUP BY s.source_type ORDER BY 2 DESC",
        )
    ]
    return {
        "data": {
            "memory": {
                "total": sum(by_status.values()),
                "by_status": by_status,
                "active": by_status.get("ACTIVE", 0),
                "archived": by_status.get("ARCHIVED", 0),
                "deleted": by_status.get("DELETED", 0),
                "today_created": _scalar(
                    container,
                    "SELECT COUNT(*) FROM memory_records WHERE substr(created_at, 1, 10) = :day",
                    {"day": today},
                ),
                "today_updated": _scalar(
                    container,
                    "SELECT COUNT(*) FROM memory_records WHERE substr(updated_at, 1, 10) = :day",
                    {"day": today},
                ),
                "pending_governance": pending_total,
            },
            "pending": pending,
            "growth": growth,
            "distribution": {"types": types, "sources": sources},
            "graph": _graph_summary(container),
            "health": _components(request, container),
            "activity": _recent_activity(container, request, limit=15),
            "not_available": [
                "Worker dead-letter queue (JME has no dead-letter store)",
                "Graph growth trend (no per-day graph history table)",
            ],
        }
    }


@router.get("/activity")
def activity(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> dict:
    return {"data": _recent_activity(container, request, limit=limit)}


@router.get("/ops/components")
def ops_components(request: Request, container: Container = Depends(get_container)) -> dict:
    return {"data": _components(request, container)}


@router.get("/ops/tasks")
def ops_tasks(container: Container = Depends(get_container)) -> dict:
    tasks: Dict[str, Any] = {}
    for name, accessor in (
        ("jkg_worker", lambda: container.jkg_worker.status()),
        ("jkg_sync", lambda: container.jkg_lifecycle.status()),
    ):
        try:
            tasks[name] = accessor()
        except Exception:  # noqa: BLE001
            tasks[name] = {"status": "unavailable"}
    return {
        "data": {
            "workers": tasks,
            "queue": {"status": "not_available", "reason": "JME has no task queue table"},
            "retry_queue": {"status": "not_available"},
            "dead_letter_queue": {"status": "not_available"},
            "running_now": _scalar(container, "SELECT 0"),
        }
    }


@router.get("/ops/logs")
def ops_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_family: Optional[str] = Query(default=None, pattern="^[245]xx$"),
) -> dict:
    items, total = request.app.state.control.list_access_logs(
        limit=limit, offset=offset, status_family=status_family
    )
    return {"data": items, "meta": {"total": total, "limit": limit, "offset": offset}}


@router.get("/governance/overview")
def governance_overview(
    request: Request,
    container: Container = Depends(get_container),
) -> dict:
    settings = container.settings
    recent = _rows(
        container,
        "SELECT action, actor, memory_id, created_at FROM memory_audit_logs "
        "ORDER BY created_at DESC LIMIT 20",
    )
    return {
        "data": {
            "pending": _pending_counts(container),
            "policies": {
                "credential_gate": "always on (credentials are refused before any write)",
                "sensitive_facts": "held as PENDING_AUTHORIZATION until the owner decides",
                "auto_save_enabled": bool(settings.auto_save_enabled),
                "direct_write_enabled": bool(settings.direct_write_enabled),
                "p1_governance_enabled": bool(settings.p1_governance_enabled),
                "lifecycle": {
                    "import": bool(settings.lifecycle_import_enabled),
                    "merge": bool(settings.lifecycle_merge_enabled),
                    "summary_review": bool(settings.lifecycle_summary_review_enabled),
                    "correction_resolve": bool(settings.lifecycle_correction_resolve_enabled),
                },
                "non_human_actors": "agent labels cannot approve or request human-only actions",
            },
            "memory_types": [kind.value for kind in MemoryType],
            "recent_audit": [
                {"action": a, "actor": actor, "memory_id": mid, "at": at}
                for a, actor, mid, at in recent
            ],
        }
    }


@router.get("/api-management")
def api_management(request: Request) -> dict:
    schema = request.app.openapi()
    endpoints = []
    for path, operations in schema.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in operations.items():
            endpoints.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary") or operation.get("operationId") or "",
                    "tags": operation.get("tags", []),
                }
            )
    settings = request.app.state.settings
    return {
        "data": {
            "version": "v1",
            "endpoints": endpoints,
            "usage": request.app.state.control.access_summary(hours=24),
            "rate_limits": {
                "owner_per_minute": settings.api_rate_limit_owner_per_minute,
                "agent_per_minute": settings.api_rate_limit_agent_per_minute,
                "auth_failures_per_minute": settings.api_auth_failure_limit_per_minute,
                "window_seconds": 60,
                "scope": "per process",
            },
            "authentication": {
                "owner": "Bearer owner token (JARVISMIND_API_TOKEN or the local token file)",
                "agent": "Bearer agent token issued by the control plane (HMAC digest stored)",
                "space_header": "X-JarvisMind-Space selects a granted memory space",
            },
            "error_envelope": {"error": {"code": "string", "message": "string", "details": "any"}},
        }
    }


@router.get("/settings")
def safe_settings(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "data": {
            "general": {"version": settings.version, "service": "JarvisMind"},
            "system": {
                "host": settings.host,
                "port": settings.port,
                "env": settings.env,
                "log_level": settings.log_level,
                "cors_enabled": bool(settings.cors_origins),
                "console_served": bool(settings.console_dir),
            },
            "storage": {
                "data_directory": "configured (path not shown)",
                "database": "jarvismind.db per memory space",
                "control_database": "control.db",
            },
            "memory": {
                "embedding_provider": settings.embedding_provider,
                "embedding_model": settings.embedding_model,
                "hybrid_max_candidates": settings.hybrid_max_candidates,
                "context_candidate_limit": settings.memory_context_candidate_limit,
            },
            "security": {
                "p1_governance_enabled": settings.p1_governance_enabled,
                "direct_write_enabled": settings.direct_write_enabled,
                "rate_limits": {
                    "owner_per_minute": settings.api_rate_limit_owner_per_minute,
                    "agent_per_minute": settings.api_rate_limit_agent_per_minute,
                    "auth_failures_per_minute": settings.api_auth_failure_limit_per_minute,
                },
            },
            "integration": {
                "llm_configured": bool(settings.llm_base_url and settings.llm_model),
                "jkg_worker_enabled": settings.jkg_worker_enabled,
            },
            "secrets": "never returned by this endpoint",
        }
    }


@router.get("/exports/memories")
def export_memories(
    status: str = Query(default="ACTIVE", pattern="^(ACTIVE|ARCHIVED|DELETED)$"),
    format: Literal["json", "csv"] = Query(default="json"),
    include_sensitive: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> Response:
    collected: List[Dict[str, Any]] = []
    page = 1
    truncated = False
    while True:
        records, total = container.memory_service.list(
            status=status, page=page, page_size=100, sort_by="created_at", sort_order="asc"
        )
        for record in records:
            protected = record_is_protected(record.content, record.summary, record.metadata_json)
            data = record_to_response(record).model_dump()
            if protected and not include_sensitive:
                # A protected record is withheld as a whole: its content, summary and
                # metadata may each carry the sensitive part.
                data["content"] = REDACTED
                if data.get("summary"):
                    data["summary"] = REDACTED
                data["metadata"] = None
            data["sensitive"] = protected
            collected.append(data)
            if len(collected) >= EXPORT_MAX_ROWS:
                truncated = True
                break
        if truncated or page * 100 >= int(total) or not records:
            break
        page += 1
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=["id", "memory_type", "status", "summary", "content", "sensitive", "created_at"],
            extrasaction="ignore",
        )
        writer.writeheader()
        for item in collected:
            writer.writerow(item)
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=memories.csv"},
        )
    body = {
        "format": EXPORT_FORMAT,
        "version": 1,
        "generated_at": utc_now_iso(),
        "sensitive_included": include_sensitive,
        "count": len(collected),
        "truncated": truncated,
        "items": collected,
    }
    return Response(
        content=json.dumps(body, ensure_ascii=False, default=str),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=memories.json"},
    )


@router.get("/exports/graph")
def export_graph(
    include_sensitive: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> dict:
    health = container.jkg_adapter.health()
    if not health.get("usable"):
        # A disabled or unbuilt graph exports as an empty, explicitly labelled result,
        # not as a server error: there is simply no graph to export yet.
        return {
            "data": {
                "format": EXPORT_FORMAT,
                "usable": False,
                "graph_state": health.get("state", "unavailable"),
                "nodes": [],
                "edges": [],
                "truncated": False,
                "sensitive_included": include_sensitive,
            }
        }
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    truncated = False
    offset = 0
    while True:
        result = container.jkg_query_service.list_nodes(
            entity_type=None, query=None, status=None, limit=100, offset=offset
        )
        items = result.get("items", [])
        nodes.extend(item for item in items if item.get("source_memory_valid") is not False)
        offset += 100
        if not items or len(nodes) >= EXPORT_GRAPH_MAX:
            truncated = len(nodes) >= EXPORT_GRAPH_MAX
            break
    edge_offset = 0
    edge_truncated = False
    while True:
        edge_result = container.jkg_query_service.list_edges(
            node_id=None,
            predicate=None,
            direction="both",
            valid_only=True,
            limit=100,
            offset=edge_offset,
        )
        page_items = edge_result.get("items", [])
        edges.extend(page_items)
        edge_offset += 100
        if not page_items or len(edges) >= EXPORT_GRAPH_MAX:
            edge_truncated = len(edges) >= EXPORT_GRAPH_MAX
            break
    truncated = truncated or edge_truncated
    if not include_sensitive:
        nodes = [
            node for node in nodes
            if not is_protected(json.dumps(node, ensure_ascii=False, default=str))
        ]
        edges = [
            edge for edge in edges
            if not is_protected(json.dumps(edge, ensure_ascii=False, default=str))
        ]
    return {
        "data": {
            "format": EXPORT_FORMAT,
            "nodes": nodes,
            "edges": edges,
            "truncated": truncated,
            "sensitive_included": include_sensitive,
        }
    }


@router.get("/exports/audit")
def export_audit(
    request: Request,
    include_sensitive: bool = Query(default=False),
    limit: int = Query(default=1000, ge=1, le=EXPORT_MAX_ROWS),
    container: Container = Depends(get_container),
) -> dict:
    audit_rows = _rows(
        container,
        "SELECT id, memory_id, action, actor, detail_json, created_at FROM memory_audit_logs "
        "ORDER BY created_at DESC LIMIT :n",
        {"n": limit},
    )
    audit = []
    for row_id, memory_id, action, actor, detail_json, created_at in audit_rows:
        detail = detail_json or ""
        audit.append(
            {
                "id": row_id,
                "memory_id": memory_id,
                "action": action,
                "actor": actor,
                "detail": _redact(str(detail), include_sensitive),
                "created_at": created_at,
            }
        )
    access, total = request.app.state.control.list_access_logs(limit=min(limit, 500), offset=0)
    return {
        "data": {
            "format": EXPORT_FORMAT,
            "memory_audit": audit,
            "api_access": access,
            "api_access_total": total,
            "sensitive_included": include_sensitive,
            "notice": "API access rows contain metadata only (no bodies, queries or IPs).",
        }
    }


@router.get("/backups")
def list_backups(container: Container = Depends(get_container)) -> dict:
    directory = _backup_dir(container)
    items = []
    if directory.is_dir():
        for path in sorted(directory.glob("jarvismind-*.db")):
            if not BACKUP_NAME_RE.match(path.name):
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size_bytes": int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    return {
        "data": items,
        "restore": {
            "available_via_api": False,
            "procedure": "Stop the server, then restore offline with the operator CLI; "
            "restore is never performed by an agent or by the console.",
        },
    }


@router.post("/backups", status_code=201)
def create_backup(container: Container = Depends(get_container)) -> dict:
    directory = _backup_dir(container)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with _BACKUP_LOCK:
        # Same-second backups get a numbered suffix that BACKUP_NAME_RE accepts, so
        # every file the server writes can be listed and verified afterwards.
        target = directory / f"jarvismind-{stamp}.db"
        sequence = 1
        while target.exists():
            sequence += 1
            target = directory / f"jarvismind-{stamp}-{sequence}.db"
        with container.manager.engine.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.exec_driver_sql("VACUUM INTO ?", (str(target),))
    return {"data": {"name": target.name, "size_bytes": int(target.stat().st_size)}}


@router.post("/backups/verify")
def verify_backup(
    payload: BackupVerify,
    container: Container = Depends(get_container),
) -> dict:
    if not BACKUP_NAME_RE.match(payload.name):
        return {"data": {"name": payload.name, "ok": False, "reason": "invalid_backup_name"}}
    target = _backup_dir(container) / payload.name
    if not target.is_file():
        return {"data": {"name": payload.name, "ok": False, "reason": "not_found"}}
    alias = "backup_verify"
    with container.manager.engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.exec_driver_sql(f"ATTACH DATABASE ? AS {alias}", (str(target),))
        try:
            integrity = connection.exec_driver_sql(f"PRAGMA {alias}.integrity_check").scalar()
            records = connection.exec_driver_sql(
                f"SELECT COUNT(*) FROM {alias}.memory_records"
            ).scalar()
        finally:
            connection.exec_driver_sql(f"DETACH DATABASE {alias}")
    return {
        "data": {
            "name": payload.name,
            "ok": str(integrity).lower() == "ok",
            "integrity": str(integrity),
            "memory_records": int(records or 0),
        }
    }
