"""P1 unified governance HTTP surface (real application integration).

Registered at /api/v1/p1 and protected by the same token dependency as the
other JME routers. This is the entry point JMR / JKG / JPA will use:

    GET  /status            governance / cost / task state (no secret values)
    POST /memories          authoritative write through GovernedMemoryGateway
    GET  /memories          read-only projection of authorised JME memories
    POST /tasks             create a governed background task
    GET  /tasks/{id}        task state + execution records
    POST /tasks/{id}/pause|resume|cancel
    GET  /audit             P1 AuditBus entries from memory_audit_logs

The client never supplies an actor or a grant: the SERVER derives a
TrustedPrincipal from its own signing key and issues a one-shot SignedGrant for
each write. Client-supplied identity fields are ignored.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ...core.security_gate import assert_safe_identifier, assert_safe_text, assert_writable
from ..deps import get_container
from ...governance.identity import (
    CAP_MEMORY_WRITE,
    server_key_fingerprint,
)
from ...governance.integration import OP_MEMORY_CREATE, service_principal
from ...governance.key_material import key_source
from ...p1_gate import is_active
from ...schemas.memory import MemoryCreate

router = APIRouter()


def _require_governance(request: Request):
    container = get_container(request)
    gov = container.governance
    if gov is None:
        raise HTTPException(
            status_code=409,
            detail="P1 governance is not enabled (set JARVISMIND_P1_GOVERNANCE_ENABLED=1)",
        )
    return container, gov


@router.get("/status")
def governance_status(request: Request) -> Dict[str, Any]:
    container = get_container(request)
    gov = container.governance
    payload: Dict[str, Any] = {
        "governance_enabled": gov is not None,
        "gate_active": is_active(),
        "key_source": key_source(container.settings),
        # A one-way fingerprint for operations; the key itself is never returned.
        "key_fingerprint": server_key_fingerprint(container.settings),
    }
    if gov is not None:
        payload["cost"] = gov.cost_governor.status()
        payload["tasks"] = gov.task_registry.runtime_state()
        payload["projection_read_only"] = gov.projection.assert_read_only()
        payload["orm_gate"] = gov.write_governor.assert_orm_gate_active()
    return payload


@router.post("/memories")
def governed_create_memory(
    request: Request, payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    container, gov = _require_governance(request)
    principal = service_principal(container.manager)
    # Re-audit NEW-P4: task_id/run_id reach memory_audit_logs.detail_json.
    task_id = str(payload.get("task_id") or "")
    run_id = str(payload.get("run_id") or "")
    if task_id:
        assert_safe_identifier(task_id, field="task_id", strict=True)
    if run_id:
        assert_safe_identifier(run_id, field="run_id", strict=True)
    grant = gov.grant_authority.issue(
        principal=principal,
        operation=OP_MEMORY_CREATE,
        object_ref="NEW",
        grants=(CAP_MEMORY_WRITE,),
        task_id=task_id,
        run_id=run_id,
    )
    body = {k: v for k, v in payload.items() if k not in ("task_id", "run_id")}
    record = gov.gateway.create_memory(
        MemoryCreate(**body),
        principal=principal,
        grant=grant,
        trigger="P1_HTTP_GATEWAY",
        task_id=task_id,
        run_id=run_id,
    )
    return {
        "id": record.id,
        "memory_type": record.memory_type,
        "content": record.content,
        "status": record.status,
        "principal_id": principal.subject_id,
        "grant_id": grant.grant_id,
    }


@router.get("/memories")
def projection_list(
    request: Request, page: int = 1, page_size: int = 50
) -> Dict[str, Any]:
    from ...governance.projection import ReadOnlyProjection

    container = get_container(request)
    projection = ReadOnlyProjection(container.manager)
    result = projection.list_memories(page=page, page_size=page_size)
    return {
        "authority": result.authority,
        "source_table": result.source_table,
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        # MappingProxyType (and the nested lineage mapping) cannot be JSON
        # serialised by FastAPI; convert both to plain dicts at the HTTP edge.
        "items": [
            {**dict(item), "lineage": dict(item["lineage"])}
            for item in result.items
        ],
        "read_only": projection.assert_read_only(),
    }


@router.post("/tasks")
def create_task(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    container, gov = _require_governance(request)
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id is required")
    # Re-audit NEW-P1: this gateway persists task_id/metadata into p1_tasks and
    # audit. Client-controlled values must be scanned before any write.
    assert_safe_identifier(task_id, field="task_id", strict=True)
    task_type_value = str(payload.get("task_type") or "GENERIC")
    assert_safe_text(task_type_value, field="task_type")
    run_id_value = str(payload.get("run_id") or "")
    if run_id_value:
        assert_safe_identifier(run_id_value, field="run_id", strict=True)
    metadata = payload.get("metadata")
    if metadata is not None:
        assert_writable(None, metadata=metadata, field="metadata")
    principal = service_principal(container.manager)
    result = gov.task_registry.create(
        task_id,
        task_type_value,
        owner=principal.subject_id,
        run_id=run_id_value,
        metadata=payload.get("metadata"),
    )
    # Move a newly created task into RUNNING so pause/resume/cancel are valid
    # transitions through the HTTP API (CREATED only allows RUNNING/CANCELLED).
    if not result.get("noop"):
        gov.task_registry.start(task_id)
    gov.audit_bus.record(
        principal_id=principal.subject_id,
        principal_type=principal.principal_type,
        operation="TASK_CREATE",
        object_ref=task_id,
        trigger="P1_HTTP_GATEWAY",
        status="SUCCEEDED",
        task_id=task_id,
    )
    return result


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str) -> Dict[str, Any]:
    container, gov = _require_governance(request)
    task = gov.task_registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task": task,
        "state": gov.task_registry.status(task_id),
        "events": gov.task_registry.events(task_id),
    }


def _transition(request: Request, task_id: str, action: str, reason: str = ""):
    container, gov = _require_governance(request)
    # Re-audit NEW-R5-1: a non-string reason reached the scanner and produced a
    # 500. Validate the shape here so the API answers 422.
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=422, detail="reason must be a string")
    if reason:
        assert_safe_text(reason, field="reason")
    registry = gov.task_registry
    if registry.get(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    if action == "pause":
        result = registry.pause(task_id, reason=reason)
    elif action == "resume":
        result = registry.resume(task_id)
    elif action == "cancel":
        result = registry.cancel(task_id, reason=reason)
    else:
        raise HTTPException(status_code=400, detail="unknown action")
    principal = service_principal(container.manager)
    gov.audit_bus.record(
        principal_id=principal.subject_id,
        principal_type=principal.principal_type,
        operation=f"TASK_{action.upper()}",
        object_ref=task_id,
        trigger="P1_HTTP_GATEWAY",
        status="SUCCEEDED",
        task_id=task_id,
    )
    return {
        "task_id": result.task_id,
        "from_status": result.from_status,
        "to_status": result.to_status,
        "noop": result.noop,
    }


@router.post("/tasks/{task_id}/pause")
def pause_task(request: Request, task_id: str, payload: Optional[Dict] = Body(default=None)):
    return _transition(request, task_id, "pause", (payload or {}).get("reason", ""))


@router.post("/tasks/{task_id}/resume")
def resume_task(request: Request, task_id: str):
    return _transition(request, task_id, "resume")


@router.post("/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str, payload: Optional[Dict] = Body(default=None)):
    return _transition(request, task_id, "cancel", (payload or {}).get("reason", ""))


@router.get("/audit")
def audit_entries(request: Request, limit: int = 100) -> Dict[str, Any]:
    container, gov = _require_governance(request)
    return {"entries": gov.audit_bus.p1_entries(limit=limit), "count": gov.audit_bus.count_p1()}
