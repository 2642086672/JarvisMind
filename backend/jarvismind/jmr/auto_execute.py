"""Owner-authorized automatic JMR execution (JME-MEMORY-OVERVIEW-01-R3).

Why this module exists
----------------------
JMR was built so that *nothing* approves itself: ``bridge.review()`` refuses a
non-human actor, and ``scheduler.AUTO_TRIGGER_APPROVED`` is a constant ``False``.
That is the safe default and it stays the default here.

An owner may nevertheless decide that certain proposal kinds are safe enough to
apply without a tap every time. This module implements that decision **without**
pretending the decision came from a human:

* the switch is off unless the owner turns it on *and* acknowledges the risks;
* the acknowledgement timestamp is persisted next to the flag;
* automated reviews carry their own principal and audit marker, so a later
  reader can always tell "the owner authorized automation" apart from
  "a person pressed approve";
* only proposal rules the owner explicitly selected are eligible;
* every proposal still passes the full review bridge: provenance revalidation,
  TTL check, status check, lifecycle write, and its own audit row.

Nothing here writes ``memory_records`` / ``memory_candidates`` directly and
nothing creates schema.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("jme.jmr.auto_execute")

#: Setting keys in ``system_settings`` (existing table; no schema change).
KEY_ENABLED = "jmr.auto_execute.enabled"
KEY_RULES = "jmr.auto_execute.rules"
KEY_ACKNOWLEDGED_AT = "jmr.auto_execute.acknowledged_at"
KEY_UPDATED_AT = "jmr.auto_execute.updated_at"
KEY_LAST_RUN_AT = "jmr.auto_execute.last_run_at"
KEY_LAST_RESULT = "jmr.auto_execute.last_result"

#: Rules the owner may select, ordered least to most destructive.
#:
#: * ``SUMMARY``       — writes a derived summary only.
#: * ``STATUS_CHANGE`` — changes one attribute value on one memory.
#: * ``CONFLICT``      — chooses a value between two conflicting memories.
#: * ``MERGE``         — rewrites memory content and retires the sources.
RULES_LOW_TO_HIGH: Tuple[str, ...] = ("SUMMARY", "STATUS_CHANGE", "CONFLICT", "MERGE")

RULE_RISK: Dict[str, str] = {
    "SUMMARY": "只生成摘要，不改记忆内容。",
    "STATUS_CHANGE": "会修改单条记忆中的一个属性值。",
    "CONFLICT": "会在两条冲突记忆之间取值，另一条被标记。",
    "MERGE": "会合并多条记忆并改写内容，操作最重。",
}

#: The identity used for automated reviews. Deliberately NOT a human-looking
#: label: ``require_human_actor`` must keep refusing anything that claims to be
#: a person.
AUTOMATION_ACTOR = "owner-authorized-automation"
AUTOMATION_PRINCIPAL = "jmr-auto-execute"
AUTHENTICATION_LEVEL = "OWNER_AUTHORIZED_AUTOMATION"
AUTHORIZATION_VERIFICATION = "OWNER_AUTHORIZED_AUTOMATION"

#: Hard cap on how many proposals one automatic pass may touch. A runaway loop
#: must not rewrite the whole library in one go.
MAX_BATCH = 20


@dataclass(frozen=True)
class AutoExecuteState:
    enabled: bool
    rules: Tuple[str, ...]
    acknowledged_at: Optional[str]
    updated_at: Optional[str]
    last_run_at: Optional[str]
    last_result: Optional[Dict[str, Any]] = field(default=None)

    def covers(self, rule: str) -> bool:
        return self.enabled and rule in self.rules

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": list(self.rules),
            "acknowledged_at": self.acknowledged_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "max_batch": MAX_BATCH,
            "rule_risk": RULE_RISK,
        }


def normalize_rules(raw: Any) -> Tuple[str, ...]:
    """Keep only known rules, de-duplicated, in the canonical order."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    if not isinstance(raw, (list, tuple)):
        return ()
    chosen = {str(item) for item in raw if isinstance(item, str)}
    return tuple(rule for rule in RULES_LOW_TO_HIGH if rule in chosen)


def read_state(read_setting) -> AutoExecuteState:
    """Build the current state from a ``read_setting(key, default)`` callable."""
    enabled = read_setting(KEY_ENABLED, False) is True
    acknowledged_at = read_setting(KEY_ACKNOWLEDGED_AT, None)
    rules = normalize_rules(read_setting(KEY_RULES, []))
    last_result = read_setting(KEY_LAST_RESULT, None)
    if not isinstance(last_result, dict):
        last_result = None
    # An enabled flag with no acknowledgement is treated as off: the owner must
    # have seen the risk text, not merely toggled a boolean.
    if enabled and not acknowledged_at:
        enabled = False
    # An enabled flag with no admissible rules would silently do nothing.
    if enabled and not rules:
        enabled = False
    return AutoExecuteState(
        enabled=enabled,
        rules=rules,
        acknowledged_at=acknowledged_at if isinstance(acknowledged_at, str) else None,
        updated_at=read_setting(KEY_UPDATED_AT, None),
        last_run_at=read_setting(KEY_LAST_RUN_AT, None),
        last_result=last_result,
    )


def enable_payload(*, rules: Sequence[str], acknowledged: bool) -> Tuple[bool, List[str], str]:
    """Validate an enable request. Returns (ok, rules, error_message)."""
    if not acknowledged:
        return False, [], "启用自动执行需要明确确认风险。"
    chosen = normalize_rules(list(rules))
    if not chosen:
        return False, [], "至少选择一类允许自动执行的建议。"
    unknown = [rule for rule in rules if rule not in RULES_LOW_TO_HIGH]
    if unknown:
        return False, list(chosen), f"未知的建议类型：{', '.join(unknown)}"
    return True, list(chosen), ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AUTHENTICATION_LEVEL",
    "AUTHORIZATION_VERIFICATION",
    "AUTOMATION_ACTOR",
    "AUTOMATION_PRINCIPAL",
    "AutoExecuteState",
    "KEY_ACKNOWLEDGED_AT",
    "KEY_ENABLED",
    "KEY_LAST_RESULT",
    "KEY_LAST_RUN_AT",
    "KEY_RULES",
    "KEY_UPDATED_AT",
    "MAX_BATCH",
    "RULES_LOW_TO_HIGH",
    "RULE_RISK",
    "enable_payload",
    "normalize_rules",
    "now_iso",
    "read_state",
]
