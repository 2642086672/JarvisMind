"""JMR HTTP surface (JMR-02 + JMR-03 review bridge).

All endpoints are token-protected by ``main.py``. The routes are thin: they
call the real JMR service/query/bridge and never fabricate a response. A
missing P1 schema is surfaced as ``409 p1_schema_missing`` by the service.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from ...container import Container
from ...schemas.common import DataEnvelope
from ...schemas.jmr import (
    JMRControlRequest,
    JMRControlResponse,
    JMRReviewRequest,
    JMRReviewResponse,
    JMRRunRequest,
    JMRRunResponse,
    JMRStatusResponse,
    JMRTaskResponse,
    ProposalDetail,
    ProposalPage,
)
from ..deps import get_container

router = APIRouter(tags=["jmr"])


def _run_response(result) -> JMRRunResponse:
    return JMRRunResponse(**asdict(result))


# --------------------------------------------------------------------- runs
@router.post("/runs", response_model=DataEnvelope[JMRRunResponse])
def start_run(
    payload: JMRRunRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRRunResponse]:
    result = container.jmr_service.run(
        task_id=payload.task_id,
        run_id=payload.run_id,
        memory_type=payload.memory_type,
        resume=payload.resume,
    )
    return DataEnvelope(data=_run_response(result))


@router.get("/runs/{task_id}", response_model=DataEnvelope[JMRTaskResponse])
def get_run(
    task_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRTaskResponse]:
    return DataEnvelope(
        data=JMRTaskResponse(**container.jmr_service.task_status(task_id))
    )


@router.post("/runs/{task_id}/pause", response_model=DataEnvelope[JMRControlResponse])
def pause_run(
    task_id: str,
    payload: JMRControlRequest | None = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRControlResponse]:
    body = payload or JMRControlRequest()
    return DataEnvelope(
        data=JMRControlResponse(
            **container.jmr_service.pause(task_id, reason=body.reason)
        )
    )


@router.post("/runs/{task_id}/resume", response_model=DataEnvelope[JMRControlResponse])
def resume_run(
    task_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRControlResponse]:
    return DataEnvelope(
        data=JMRControlResponse(**container.jmr_service.resume(task_id))
    )


@router.post("/runs/{task_id}/cancel", response_model=DataEnvelope[JMRControlResponse])
def cancel_run(
    task_id: str,
    payload: JMRControlRequest | None = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRControlResponse]:
    body = payload or JMRControlRequest()
    return DataEnvelope(
        data=JMRControlResponse(
            **container.jmr_service.cancel(task_id, reason=body.reason)
        )
    )


# ---------------------------------------------------------------- proposals
@router.get("/proposals", response_model=ProposalPage)
def list_proposals(
    rule: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ProposalPage:
    result = container.jmr_query_service.list_proposals(
        rule=rule, status=status, page=page, page_size=page_size
    )
    return ProposalPage(
        items=result.items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("/proposals/{proposal_id}", response_model=DataEnvelope[ProposalDetail])
def get_proposal(
    proposal_id: str,
    container: Container = Depends(get_container),
) -> DataEnvelope[ProposalDetail]:
    return DataEnvelope(
        data=container.jmr_query_service.get_proposal(proposal_id)
    )


@router.get("/conflicts", response_model=ProposalPage)
def list_conflicts(
    conflict_class: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> ProposalPage:
    result = container.jmr_query_service.list_conflicts(
        conflict_class=conflict_class, page=page, page_size=page_size
    )
    return ProposalPage(
        items=result.items,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/proposals/{proposal_id}/review",
    response_model=DataEnvelope[JMRReviewResponse],
)
def review_proposal(
    proposal_id: str,
    payload: JMRReviewRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[JMRReviewResponse]:
    result = container.jmr_review_bridge.review(
        proposal_id,
        decision=payload.decision,
        actor=payload.actor,
        reason=payload.reason,
        chosen_memory_id=payload.chosen_memory_id,
        corrected_content=payload.corrected_content,
    )
    return DataEnvelope(data=JMRReviewResponse(**result))


# ---------------------------------------------------------------------- cost
@router.get("/cost", response_model=DataEnvelope[Dict[str, Any]])
def get_cost_report(
    days: int = Query(30, ge=1, le=3660),
    container: Container = Depends(get_container),
) -> DataEnvelope[Dict[str, Any]]:
    """JMR-06 cost report: ledger, reservations, rejections, degradation.

    Read-only. It never starts a model call and never changes a budget.
    """
    return DataEnvelope(data=container.jmr_cost_governance.report(days=days))


# -------------------------------------------------------------------- status
@router.get("/status", response_model=JMRStatusResponse)
def get_status(container: Container = Depends(get_container)) -> JMRStatusResponse:
    status = container.jmr_query_service.status()
    return JMRStatusResponse(
        governance_active=status.governance_active,
        p1_schema_installed=status.p1_schema_installed,
        p1_tables_present=status.p1_tables_present,
        algorithm_version=status.algorithm_version,
        llm_online=status.llm_online,
        notes=status.notes,
        expired_pending=status.expired_pending,
        breaker_tripped=status.breaker_tripped,
        automatic_execution_authorized=status.automatic_execution_authorized,
        daily_spend_cny=status.daily_spend_cny,
        monthly_spend_cny=status.monthly_spend_cny,
        daily_budget_cny=status.daily_budget_cny,
        monthly_budget_cny=status.monthly_budget_cny,
    )


# ----------------------------------------------------------------- scheduler
@router.get("/scheduler", response_model=DataEnvelope[Dict[str, Any]])
def get_scheduler_status(
    container: Container = Depends(get_container),
) -> DataEnvelope[Dict[str, Any]]:
    """JMR-05 scheduler snapshot (read-only)."""
    engine = container.jmr_scheduler
    return DataEnvelope(
        data={
            "stats": engine.stats(),
            "config": {
                "max_tasks_per_tick": engine.config.max_tasks_per_tick,
                "batch_size": engine.config.batch_size,
                "max_runtime_seconds": engine.config.max_runtime_seconds,
                "default_timeout_seconds": engine.config.default_timeout_seconds,
                "stale_running_seconds": engine.config.stale_running_seconds,
            },
        }
    )


@router.post("/scheduler/tick", response_model=DataEnvelope[Dict[str, Any]])
def run_scheduler_tick(
    container: Container = Depends(get_container),
) -> DataEnvelope[Dict[str, Any]]:
    """Run exactly one scheduler pass (pull-based; no daemon is started).

    Stale RUNNING tasks are recovered by this pass, so a process crash does not
    leave a task RUNNING forever.
    """
    result = container.jmr_scheduler.tick()
    return DataEnvelope(
        data={
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "executed": list(result.executed),
            "succeeded": list(result.succeeded),
            "failed": list(result.failed),
            "retry_scheduled": list(result.retry_scheduled),
            "stale_recovered": list(result.stale_recovered),
            "skipped": list(result.skipped),
            "materialized": list(result.materialized),
            "due_candidates": result.due_candidates,
            "truncated": result.truncated,
        }
    )
