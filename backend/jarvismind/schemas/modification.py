"""JW-03: request/response models for the controlled modification flow."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .memory import MAX_CONTENT_LENGTH, MAX_SUMMARY_LENGTH, PageMeta
from .memory import _validate_optional_iso  # noqa: WPS450  (shared ISO check)


class ModificationRequestCreate(BaseModel):
    """Create a modification request for an approved memory.

    ``proposed_changes`` is required for UPDATE requests and optional for the
    lifecycle kinds (ARCHIVE / RESTORE / EXTEND), where the ``request_kind``
    itself states the transition. The service validates the real rules; this
    model enforces types, lengths and the per-kind presence rule so the error
    surfaces at the HTTP edge with a field-level message.
    """

    model_config = {"extra": "forbid"}

    proposed_changes: Optional[Dict[str, Any]] = None
    request_kind: str = Field(default="UPDATE", max_length=16)
    reason: Optional[str] = Field(default=None, max_length=500)
    requester_label: Optional[str] = Field(default=None, max_length=128)

    @field_validator("request_kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        allowed = {"UPDATE", "ARCHIVE", "RESTORE", "EXTEND"}
        upper = (value or "").strip().upper()
        if upper not in allowed:
            raise ValueError(
                f"request_kind must be one of {', '.join(sorted(allowed))}"
            )
        return upper

    @model_validator(mode="after")
    def _check_presence(self):
        kind = self.request_kind
        changes = self.proposed_changes
        if kind == "UPDATE":
            if not changes:
                raise ValueError(
                    "proposed_changes is required for an UPDATE request"
                )
        elif kind == "EXTEND":
            if not changes or "expires_at" not in changes:
                raise ValueError(
                    "an EXTEND request requires proposed_changes.expires_at"
                )
        return self

    @field_validator("proposed_changes")
    @classmethod
    def _check_proposed(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        # An empty object is not an error here: ARCHIVE / RESTORE legitimately
        # omit every field, and senders commonly serialise that as {}. Whether an
        # empty set is ACCEPTABLE depends on request_kind, which the model-level
        # validator below decides with full context.
        if value is not None and not isinstance(value, dict):
            raise ValueError("proposed_changes must be an object")
        return value
        if "content" in value:
            content = value["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            if len(content) > MAX_CONTENT_LENGTH:
                raise ValueError(
                    f"content must be at most {MAX_CONTENT_LENGTH} characters"
                )
        if "summary" in value:
            summary = value["summary"]
            if summary is not None and (
                not isinstance(summary, str) or len(summary) > MAX_SUMMARY_LENGTH
            ):
                raise ValueError(
                    f"summary must be a string of at most {MAX_SUMMARY_LENGTH} characters"
                )
        if "importance" in value:
            importance = value["importance"]
            if not isinstance(importance, (int, float)) or not 0.0 <= float(importance) <= 10.0:
                raise ValueError("importance must be a number between 0 and 10")
        if "confidence" in value:
            confidence = value["confidence"]
            if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
                raise ValueError("confidence must be a number between 0 and 1")
        if "expires_at" in value:
            _validate_optional_iso(value["expires_at"])
        if "metadata" in value and value["metadata"] is not None:
            if not isinstance(value["metadata"], dict):
                raise ValueError("metadata must be an object")
        return value


class ModificationRequestCreateResponse(BaseModel):
    """The plaintext token is returned exactly once, here."""

    request: Dict[str, Any]
    confirmation_token: str
    confirmation_expires_in_seconds: int
    reused_pending_request: bool
    authentication_level: str
    authorization_verification_result: str


class ModificationRequestApprove(BaseModel):
    model_config = {"extra": "forbid"}

    confirmation_token: str = Field(min_length=1, max_length=512)
    actor_label: Optional[str] = Field(default=None, max_length=128)


class ModificationRequestReject(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Optional[str] = Field(default=None, max_length=500)
    actor_label: Optional[str] = Field(default=None, max_length=128)


class ModificationRequestListResponse(BaseModel):
    # ``meta`` must be the shared PageMeta type, not Dict: the handlers pass the
    # object returned by ``build_page_meta``, and a Dict annotation rejects it.
    data: List[Dict[str, Any]]
    meta: PageMeta


__all__ = [
    "ModificationRequestApprove",
    "ModificationRequestCreate",
    "ModificationRequestCreateResponse",
    "ModificationRequestListResponse",
    "ModificationRequestReject",
]
