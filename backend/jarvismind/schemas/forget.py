"""JW-01 request/response schemas for the memory forget approval flow.

Naming and semantics
--------------------
A *forget request* is an immutable record of "the user asked to forget this
memory". Creating one never touches ``memory_records``. Only an explicit
``approve`` action (with a server-issued one-time confirmation token) may cause
the memory to be soft-deleted.

The word APPROVED never means "deleted". It means "the user said yes, execution
is authorised". Execution is a separate, atomic step that moves the request to
EXECUTED. This distinction is what lets the API answer two different questions:
"did the user approve?" and "did the memory actually get forgotten?".
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from ..core.security_gate import validate_client_value
from .common import ListEnvelope


class ForgetRequestStatus(str, Enum):
    """Persisted lifecycle of one forget request.

    PENDING   -> awaiting an explicit user decision. No deletion may happen.
    APPROVED  -> the user approved; execution is authorised but not confirmed
                 done. A request stuck here means execution did not complete and
                 MUST NOT be re-approved; an operator reconciles it.
    EXECUTED  -> the memory is DELETED and the execution is recorded. Terminal.
    REJECTED  -> the user declined. The memory is untouched. Terminal.
    FAILED    -> approval happened but execution rolled back. Terminal; a NEW
                 request must be created to try again.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ForgetRequestSource(str, Enum):
    """Where the request came from. Only user-interactive sources are approvable."""

    USER_INTERFACE = "USER_INTERFACE"
    USER_API = "USER_API"


#: Sources a human can plausibly operate. Anything else (MODEL, SYSTEM,
#: BACKGROUND_TASK, SERVICE) is refused at approval time so no model or daemon
#: can self-approve a forget.
HUMAN_REQUEST_SOURCES = frozenset(
    {item.value for item in ForgetRequestSource}
)

#: Actor labels that unambiguously identify a non-human caller.
NON_HUMAN_ACTOR_TOKENS = frozenset(
    {
        "model",
        "llm",
        "ai",
        "agent",
        "assistant",
        "system",
        "service",
        "svc",
        "background",
        "bg-task",
        "job",
        "cron",
        "scheduler",
        "daemon",
        "worker",
        "pipeline",
    }
)


class ForgetRequestCreate(BaseModel):
    """Body of ``POST /api/v1/memories/{memory_id}/forget-requests``.

    Deliberately contains NO ``approved``/``confirm``/``execute`` field. There
    is nothing a client can set here that causes a deletion - that was the exact
    flaw of the previous ``/forget?confirm=true`` interface.
    """

    model_config = {"extra": "forbid"}

    reason: Optional[str] = Field(default=None, max_length=1000)
    #: Client CLAIM about who is asking. Recorded as a claim, never trusted as
    #: identity. The server writes its own label separately.
    requester_label: Optional[str] = Field(default=None, max_length=128)
    request_source: ForgetRequestSource = ForgetRequestSource.USER_INTERFACE

    @field_validator("requester_label")
    @classmethod
    def _reserve_agent_label(cls, value):
        if value and value.strip().lower().startswith("agent:"):
            raise ValueError("the 'agent:' prefix is reserved for the agent facade")
        return value

    @field_validator("reason", "requester_label")
    @classmethod
    def _reject_credential(cls, value, info):
        validate_client_value(value, field=info.field_name)
        return value


class ForgetApprovalRequest(BaseModel):
    """Body of ``POST /api/v1/forget-requests/{request_id}/approve``.

    The one-time confirmation token is issued by the server when the request is
    created and returned exactly once. Approval without it is refused, except for
    agent-originated requests: their token is withheld from the requester, so only
    the owner can decide them. A guessed request id is never enough to delete.
    """

    model_config = {"extra": "forbid"}

    confirmation_token: Optional[str] = Field(default=None, min_length=16, max_length=256)
    actor_label: Optional[str] = Field(default=None, max_length=128)


class ForgetRejectionRequest(BaseModel):
    """Body of ``POST /api/v1/forget-requests/{request_id}/reject``."""

    model_config = {"extra": "forbid"}

    confirmation_token: Optional[str] = Field(default=None, min_length=16, max_length=256)
    reason: Optional[str] = Field(default=None, max_length=1000)
    actor_label: Optional[str] = Field(default=None, max_length=128)

    @field_validator("reason", "actor_label")
    @classmethod
    def _reject_credential(cls, value, info):
        validate_client_value(value, field=info.field_name)
        return value


class ForgetRequestResponse(BaseModel):
    """Public shape of one forget request.

    ``memory_forgotten`` and ``execution_confirmed`` exist so a client can never
    mistake "request accepted" for "memory deleted": they are only true for an
    EXECUTED request.
    """

    id: str
    memory_id: str
    status: ForgetRequestStatus
    request_source: str
    requester_label: str = ""
    reason: Optional[str] = None

    # Truthful identity accounting. Never upgraded without real verification.
    authentication_level: str = "API_TOKEN_SHARED"
    authorization_verification_result: str = "NOT_VERIFIED"

    requested_at: str
    confirmation_expires_at: Optional[str] = None
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_source: Optional[str] = None
    executed_at: Optional[str] = None
    execution_result: Optional[str] = None
    failure_reason: Optional[str] = None
    updated_at: str

    #: True only when the request still awaits a user decision.
    awaiting_user_decision: bool = False
    #: True only after the memory was actually soft-deleted in this request.
    memory_forgotten: bool = False


class ForgetRequestCreateResponse(ForgetRequestResponse):
    """Create response: the ONLY response that carries the plaintext token.

    ``reused_existing`` is true when an existing PENDING request was returned
    instead of creating a duplicate; the token is rotated in that case, so the
    previously issued token stops working.
    """

    confirmation_token: str
    reused_existing: bool = False


class ForgetRequestListResponse(ListEnvelope[ForgetRequestResponse]):
    pass


class ForgetRequestListQuery(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[ForgetRequestStatus] = None
    memory_id: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


def status_to_response(row) -> ForgetRequestResponse:  # pragma: no cover - thin
    """Convert a mapping row produced by the repository into the public model."""
    return ForgetRequestResponse(**dict(row))


__all__ = [
    "HUMAN_REQUEST_SOURCES",
    "NON_HUMAN_ACTOR_TOKENS",
    "ForgetApprovalRequest",
    "ForgetRejectionRequest",
    "ForgetRequestCreate",
    "ForgetRequestCreateResponse",
    "ForgetRequestListQuery",
    "ForgetRequestListResponse",
    "ForgetRequestResponse",
    "ForgetRequestSource",
    "ForgetRequestStatus",
    "status_to_response",
]
