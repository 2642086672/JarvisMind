"""Automatic extraction and candidate-review endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request

from ...container import Container
from ...core.errors import AuthenticationError
from ...core.security import extract_token, token_matches
from ...schemas.common import DataEnvelope, ListEnvelope
from ...schemas.extraction import (
    AutoRememberRequest,
    AutoRememberResponse,
    CandidateListResponse,
    CandidateResponse,
    CandidateReviewRequest,
    CandidateStatus,
    ExtractRequest,
    ExtractResponse,
)
from ...schemas.memory import MemoryType
from ..deps import get_container

router = APIRouter(tags=["extraction"])


def require_recovery_authorization(
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    container: Container = Depends(get_container),
) -> None:
    """003-B1 P2-6: recovery is a state-changing operation. Re-check the
    shared token explicitly instead of relying only on router-level wiring.
    A token authenticates the CLIENT, not a human identity."""
    candidate = extract_token(x_api_token, authorization)
    if not token_matches(candidate, container.settings.api_token):
        raise AuthenticationError("A valid API token is required")


@router.post("/auto-remember", response_model=DataEnvelope[AutoRememberResponse])
def auto_remember(
    payload: AutoRememberRequest, container: Container = Depends(get_container)
) -> DataEnvelope[AutoRememberResponse]:
    """JEA-01 / JME-AUTO-GOVERNANCE-01: one governed memory decision.

    The service classifies the message (fact / question / hypothetical), gates
    credentials, keeps sensitive facts PENDING_AUTHORIZATION and either saves,
    updates, reports a duplicate or holds a conflict. ``SAVED``/``UPDATED`` are
    returned only after the authoritative write committed.
    """
    result = container.auto_memory_service.auto_remember(
        text_value=payload.text,
        conversation_id=payload.conversation_id,
        message_id=payload.message_id,
        turn_id=payload.turn_id,
        source_id=payload.source_id,
    )
    return DataEnvelope(data=result)


@router.post("/auto-govern/merge")
def auto_govern_merge(
    dry_run: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    """Merge verified-equivalent memories (JME-AUTO-GOVERNANCE-01).

    ``dry_run=true`` reports what WOULD merge without writing. This endpoint
    never touches PENDING candidates or JMR proposals: it only collapses
    already-written, pair-wise equivalent ACTIVE memories.
    """
    result = container.auto_memory_service.auto_merge(dry_run=dry_run)
    return DataEnvelope(data=result)


@router.post("/auto-govern/expire")
def auto_govern_expire(
    dry_run: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    """Archive memories whose OWN explicit expiry has passed.

    A memory without ``expires_at`` is never touched: time alone is not evidence
    that a fact stopped being true.
    """
    result = container.auto_memory_service.auto_expire(dry_run=dry_run)
    return DataEnvelope(data=result)


@router.get("/auto-govern/summary")
def auto_govern_summary(
    container: Container = Depends(get_container),
) -> DataEnvelope[dict]:
    """Real governance counts for the iOS memory page."""
    return DataEnvelope(data=container.auto_memory_service.governance_summary())


@router.post("/extract", response_model=ExtractResponse, status_code=201)
def extract_memories(
    payload: ExtractRequest, container: Container = Depends(get_container)
) -> ExtractResponse:
    return container.extraction_engine.extract(payload)


@router.get("/candidates", response_model=CandidateListResponse)
def list_candidates(
    status: Optional[CandidateStatus] = None,
    memory_type: Optional[MemoryType] = None,
    conversation_id: Optional[str] = Query(default=None, max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> CandidateListResponse:
    candidates, total = container.extraction_engine.list_candidates(
        status=status.value if status else None,
        memory_type=memory_type.value if memory_type else None,
        conversation_id=conversation_id,
        page=page,
        page_size=page_size,
    )
    from ...schemas.memory import build_page_meta

    return CandidateListResponse(
        data=[candidate for candidate in candidates],
        meta=build_page_meta(page, page_size, total),
    )


@router.get("/candidates/{candidate_id}", response_model=DataEnvelope[CandidateResponse])
def get_candidate(
    candidate_id: str, container: Container = Depends(get_container)
) -> DataEnvelope[CandidateResponse]:
    return DataEnvelope(data=container.extraction_engine.get_candidate(candidate_id))


@router.post("/candidates/{candidate_id}/approve", response_model=DataEnvelope[CandidateResponse])
def approve_candidate(
    candidate_id: str,
    payload: CandidateReviewRequest | None = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[CandidateResponse]:
    body = payload or CandidateReviewRequest()
    # 003-B1: ``actor`` is forwarded ONLY as an unverified CLIENT CLAIM and is
    # stored in approval_events.client_actor_claim. ``grants`` is ignored.
    # The URL fixes the decision; the service writes decided_by itself and
    # re-runs the credential gate in the same write transaction.
    candidate = container.extraction_engine.review(
        candidate_id,
        decision="approve",
        reason=body.reason,
        actor=body.actor,
    )
    return DataEnvelope(data=candidate)


@router.post("/candidates/{candidate_id}/reject", response_model=DataEnvelope[CandidateResponse])
def reject_candidate(
    candidate_id: str,
    payload: CandidateReviewRequest | None = None,
    container: Container = Depends(get_container),
) -> DataEnvelope[CandidateResponse]:
    body = payload or CandidateReviewRequest()
    candidate = container.extraction_engine.review(
        candidate_id,
        decision="reject",
        reason=body.reason,
        actor=body.actor,
    )
    return DataEnvelope(data=candidate)


@router.get("/extraction/status")
def extraction_status(container: Container = Depends(get_container)) -> dict:
    engine = container.extraction_engine
    provider = engine.provider
    return {
        "engine": "llm" if provider is not None else "local",
        "provider": provider.name if provider is not None else None,
        "model": getattr(provider, "model", None) if provider else None,
        "auto_save_threshold": container.settings.llm_auto_save_threshold,
        # D1: false by default; the threshold above no longer auto-approves.
        "auto_save_enabled": container.settings.auto_save_enabled,
        "budget": engine.budget_status(),
    }


@router.get("/block-events")
def list_block_events(
    conversation_id: Optional[str] = Query(default=None, max_length=255),
    container: Container = Depends(get_container),
) -> dict:
    """De-identified BLOCK events only. Never returns matched values."""
    from sqlalchemy import text

    from ...repositories.security_event_repository import SecurityEventRepository

    with container.manager.session() as session:
        repo = SecurityEventRepository(session)
        rows = session.execute(
            text(
                "SELECT block_event_id, conversation_id, message_id, turn_id, "
                "category, hit_length, ruleset_version FROM block_events "
                "WHERE (:cid IS NULL OR conversation_id = :cid) "
                "ORDER BY created_at DESC LIMIT 200"
            ),
            {"cid": conversation_id},
        ).all()
        total = repo.count_events("block_events")
    return {
        "data": [
            {
                "block_event_id": row[0],
                "conversation_id": row[1],
                "message_id": row[2],
                "turn_id": row[3],
                "category": row[4],
                "hit_length": row[5],
                "ruleset_version": row[6],
            }
            for row in rows
        ],
        "meta": {"total": total},
    }


@router.get("/extraction-events")
def list_extraction_events(
    conversation_id: Optional[str] = Query(default=None, max_length=255),
    container: Container = Depends(get_container),
) -> dict:
    """Non-candidate events (IGNORE/DUPLICATE/CONFLICT/...); never BLOCK."""
    from sqlalchemy import text

    with container.manager.session() as session:
        rows = session.execute(
            text(
                "SELECT event_id, conversation_id, message_id, turn_id, event_type, "
                "ignore_reason_enum FROM extraction_events "
                "WHERE (:cid IS NULL OR conversation_id = :cid) "
                "ORDER BY created_at DESC LIMIT 200"
            ),
            {"cid": conversation_id},
        ).all()
    return {
        "data": [
            {
                "event_id": row[0],
                "conversation_id": row[1],
                "message_id": row[2],
                "turn_id": row[3],
                "event_type": row[4],
                "ignore_reason_enum": row[5],
            }
            for row in rows
        ],
        "meta": {"total": len(rows)},
    }


@router.get("/extraction/stats")
def extraction_stats(container: Container = Depends(get_container)) -> dict:
    """Cumulative lifecycle counters; deliberately separate from /extract."""
    from ...services.approval_service import ApprovalService

    return {"data": ApprovalService(container.manager).stats()}


@router.post("/approval/recover", dependencies=[Depends(require_recovery_authorization)])
def recover_unknown_approvals(container: Container = Depends(get_container)) -> dict:
    """Re-read real database state and converge UNKNOWN approval attempts."""
    from ...services.approval_service import ApprovalService

    return {"data": ApprovalService(container.manager).recover_unknown()}
