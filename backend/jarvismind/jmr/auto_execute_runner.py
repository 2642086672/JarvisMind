"""Runs owner-authorized automatic reviews through the existing bridge.

The executor is deliberately thin. It does **not** touch ``memory_records``,
``memory_candidates`` or the derived tables directly. For every eligible
proposal it calls ``JMRReviewBridge.review(..., automated=True)``, which means
the normal chain still applies:

    provenance revalidation -> TTL check -> status check
    -> LifecycleService write -> JMR review audit

If any of those reject the proposal, the executor records the failure and moves
on. A partial run is a normal outcome, not an error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .auto_execute import (
    AUTHENTICATION_LEVEL,
    AUTHORIZATION_VERIFICATION,
    AUTOMATION_ACTOR,
    MAX_BATCH,
    RULE_RISK,
)

logger = logging.getLogger("jme.jmr.auto_execute_runner")


@dataclass
class AutoReviewResult:
    attempted: int = 0
    approved: List[str] = field(default_factory=list)
    #: Proposals that could not be approved automatically, with a value-free
    #: reason code (never proposal content).
    skipped: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "approved": len(self.approved),
            "approved_ids": self.approved,
            "skipped": self.skipped,
        }


class JmrAutoExecutor:
    def __init__(self, *, bridge, query, limit: int = MAX_BATCH) -> None:
        self.bridge = bridge
        self.query = query
        self.limit = min(limit, MAX_BATCH)

    def _pending_ids(self, rules: tuple[str, ...]) -> List[Dict[str, str]]:
        """PENDING proposals limited to the authorized rules.

        Only ids and rule names leave this method; no memory text is read here.
        """
        page = self.query.list_proposals(
            page=1, page_size=min(self.limit, 100), status="PENDING"
        )
        items: List[Dict[str, str]] = []
        for item in page.items:
            rule = str(item.get("rule") or "")
            if rule not in rules:
                continue
            proposal_id = str(item.get("proposal_id") or item.get("id") or "")
            if proposal_id:
                items.append({"id": proposal_id, "rule": rule})
            if len(items) >= self.limit:
                break
        return items

    def run(self, *, rules: tuple[str, ...]) -> AutoReviewResult:
        result = AutoReviewResult()
        if not rules:
            return result

        for entry in self._pending_ids(rules):
            proposal_id = entry["id"]
            rule = entry["rule"]
            result.attempted += 1
            try:
                self.bridge.review(
                    proposal_id,
                    decision="approve",
                    actor=AUTOMATION_ACTOR,
                    reason=f"自动执行（已授权 {rule}）",
                    automated=True,
                    actor_authentication_level=AUTHENTICATION_LEVEL,
                    authorization_verification=AUTHORIZATION_VERIFICATION,
                )
                result.approved.append(proposal_id)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pass
                # Record only the error type and code. Proposal content and
                # memory text never enter this structure.
                code = getattr(exc, "code", "") or type(exc).__name__
                result.skipped.append({"id": proposal_id, "rule": rule, "code": str(code)})
                logger.info(
                    "auto review skipped proposal=%s rule=%s code=%s",
                    proposal_id,
                    rule,
                    code,
                )
        return result


__all__ = ["AutoReviewResult", "JmrAutoExecutor", "RULE_RISK"]
