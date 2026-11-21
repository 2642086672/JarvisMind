"""P1 real application integration: authorised context for EVERY service write.

Problem this solves
-------------------
The connection-level gate (P0-2 fix) blocks any write to memory_records /
memory_candidates that is not inside an ``authorized_write`` context. If that is
the only change, enabling governance would BREAK the existing API: POST
/memories, /candidates/{id}/approve and the import path all write without a
context. Independent review P2-1 predicted exactly this.

Solution
--------
``authorized_operation()`` is a context manager installed at the service
boundary. When governance is active it:

  1. mints a SERVER-side TrustedPrincipal (never from a client label),
  2. issues a one-shot SignedGrant bound to this operation/object,
  3. runs WriteGovernor.authorize() (identity + signature + cost + audit),
  4. enters the authorized_write context so ORM and connection gates allow the
     statements produced inside,
  5. records SUCCEEDED/FAILED on the AuditBus, updates CostGovernor and
     consumes the grant exactly once.

When governance is inactive it is a no-op, so the 382 legacy regression cases
keep their exact behaviour. When a caller is ALREADY inside an authorised
context (e.g. GovernedMemoryGateway), it reuses that decision instead of
double-authorising. Every existing write service is wrapped with it.

Complexity: O(1) per operation; one HMAC + a few indexed SQLite writes.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..p1_gate import (
    active_governor,
    authorized_write,
    current_write_context,
    is_active,
)
from .identity import (
    CAP_MEMORY_READ,
    CAP_MEMORY_WRITE,
    CAP_TASK_MANAGE,
    TrustedPrincipal,
)

SERVICE_SUBJECT = "svc:jme-governed-write"
OP_MEMORY_CREATE = "MEMORY_CREATE"
OP_MEMORY_UPDATE = "MEMORY_UPDATE"
OP_MEMORY_DELETE = "MEMORY_DELETE"
OP_CANDIDATE_APPROVE = "CANDIDATE_APPROVE"


def service_principal(manager) -> TrustedPrincipal:
    """Server-issued identity. No client field is consulted.

    The key version MUST come from the same GovernanceConfig used by
    WriteGovernor.authorize(); hardcoding 1 broke every write when an operator
    set JARVISMIND_P1_GRANT_KEY_VERSION=2 (independent review P1 finding A5).
    """
    governor = active_governor()
    key_version = int(
        getattr(getattr(governor, "config", None), "grant_key_version", 1)
    )
    return TrustedPrincipal.issue(
        manager.settings,
        subject_id=SERVICE_SUBJECT,
        principal_type="SERVICE",
        grants=(CAP_MEMORY_READ, CAP_MEMORY_WRITE, CAP_TASK_MANAGE),
        key_version=key_version,
    )


@contextmanager
def authorized_operation(
    *,
    manager,
    operation: str,
    object_ref: str,
    trigger: str,
    task_id: str = "",
    run_id: str = "",
) -> Iterator[Optional[Any]]:
    existing = current_write_context()
    if existing is not None:
        # Already authorised by Gateway / an outer operation: do not double-sign.
        yield existing
        return
    governor = active_governor()
    if governor is None or not is_active():
        yield None
        return

    principal = service_principal(manager)
    grant = governor.grant_authority.issue(
        principal=principal,
        operation=operation,
        object_ref=object_ref,
        grants=(CAP_MEMORY_WRITE,),
        task_id=task_id,
        run_id=run_id,
    )
    decision = governor.authorize(
        principal=principal,
        grant=grant,
        operation=operation,
        object_ref=object_ref,
        trigger=trigger,
        task_id=task_id,
        run_id=run_id,
    )
    try:
        with authorized_write(decision.context):
            yield decision.context
    except Exception as exc:
        governor.audit_bus.record(
            principal_id=principal.subject_id,
            principal_type=principal.principal_type,
            operation=operation,
            object_ref=object_ref,
            trigger=trigger,
            status="FAILED",
            failure_reason=f"{type(exc).__name__}: {exc}",
            task_id=task_id,
            run_id=run_id,
            grant_id=grant.grant_id,
        )
        raise
    else:
        governor.audit_bus.record(
            principal_id=principal.subject_id,
            principal_type=principal.principal_type,
            operation=operation,
            object_ref=object_ref,
            trigger=trigger,
            status="SUCCEEDED",
            task_id=task_id,
            run_id=run_id,
            grant_id=grant.grant_id,
        )
        governor.cost_governor.record(
            task_id=task_id or principal.subject_id,
            run_id=run_id,
            model_calls=1,
        )
        governor.grant_authority.consume(grant.grant_id)
