"""JW-02: shared authorisation policy for memory lifecycle writes.

Why a shared module
-------------------
Four lifecycle entry points (``/memories/import``, ``/merges/confirm``,
``/summaries/{id}/review``, ``/corrections/{id}/resolve``) all take an ``actor``
from the client and all write authoritative memory. Before JW-02 each one
invented its own answer to "who is acting?", which produced three separate
defects:

* the client could name itself anything (``actor="完全伪造的操作人"``), and that
  string was persisted verbatim into ``memory_audit_logs.actor`` and into
  ``confirmed_by`` / ``resolved_by`` columns;
* nothing distinguished a human from a model or a daemon;
* nothing recorded that the shared API token identifies a *client*, not a person.

This module is the single implementation of that policy. It deliberately does
NOT try to authenticate a human - the project has no such capability yet
(003-C). What it does is refuse to *pretend*: the authoritative identity label is
always server-written, the client's string is kept only as a claim, and the
verification result is always ``NOT_VERIFIED``.

Relationship to JW-01
---------------------
``app.services.forget_service`` implements the same policy inline for the forget
flow. JW-02 must not change JW-01's behaviour, so the constants here are
identical and the keyword set is a superset-checked copy - a test asserts the two
sets agree, so they cannot drift apart silently.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.errors import ForbiddenError

#: Identity accounting. Fixed values, never derived from a request field.
AUTHENTICATION_LEVEL = "API_TOKEN_SHARED"
AUTH_NOT_VERIFIED = "NOT_VERIFIED"
#: Reserved for 003-C. No JW-02 code path may emit it.
AUTH_PASSED_RESERVED = "PASSED"

#: Server-written actor label for lifecycle writes. Mirrors JW-01 / 003-B1 so the
#: audit trail states, in one place, that no human identity was proven.
SERVER_ACTOR_LABEL = "shared-api-token:human-identity-not-verified"

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


class NonHumanActorForbiddenError(ForbiddenError):
    """A model / service / background caller tried to perform a human action."""

    code = "non_human_actor_forbidden"
    message = "This operation must be performed by an explicit user action"


def looks_non_human(label: Optional[str]) -> bool:
    """Conservative check on a CLIENT-SUPPLIED actor label.

    Not an identity check - a claim can always be crafted to pass. It exists so
    an obvious ``actor="model"`` cannot be laundered into a user-visible
    confirmation, and so the intent is stated in code rather than implied.
    """
    if not label:
        return False
    normalized = str(label).strip().lower().replace("_", "-")
    if normalized in NON_HUMAN_ACTOR_TOKENS:
        return True
    tokens = {
        token
        for token in normalized.replace(":", "-").replace("/", "-").split("-")
        if token
    }
    return bool(tokens & NON_HUMAN_ACTOR_TOKENS)


def require_human_actor(label: Optional[str], *, operation: str) -> None:
    """Refuse an obviously non-human actor. Raises 403 otherwise nothing."""
    if looks_non_human(label):
        raise NonHumanActorForbiddenError(
            f"{operation} requires an explicit user action; "
            "model, service and background actors are refused",
            detail={
                "code": "non_human_actor",
                "operation": operation,
                "actor_label": label,
            },
        )


def identity_fields(client_claim: Optional[str]) -> Dict[str, Any]:
    """The identity block every lifecycle audit record carries.

    ``actor`` is NOT included - callers pass ``SERVER_ACTOR_LABEL`` as the actor
    and put the client's string in ``client_actor_claim``. Keeping the two apart
    is what stops a claim from being read back later as a fact.
    """
    return {
        "client_actor_claim": client_claim or "",
        "authentication_level": AUTHENTICATION_LEVEL,
        "authorization_verification_result": AUTH_NOT_VERIFIED,
    }


def actor_column(server_actor: bool = True) -> str:
    """Value to store in an ``actor`` / ``*_by`` column.

    Lifecycle writes always use the server label; the client's claim goes into a
    JSON detail field instead of a first-class identity column.
    """
    return SERVER_ACTOR_LABEL if server_actor else SERVER_ACTOR_LABEL


__all__ = [
    "AUTHENTICATION_LEVEL",
    "AUTH_NOT_VERIFIED",
    "AUTH_PASSED_RESERVED",
    "NON_HUMAN_ACTOR_TOKENS",
    "NonHumanActorForbiddenError",
    "SERVER_ACTOR_LABEL",
    "actor_column",
    "identity_fields",
    "looks_non_human",
    "require_human_actor",
]
