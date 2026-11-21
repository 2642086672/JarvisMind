"""Owner-controlled automatic JMR execution settings (R3).

Reads and writes the ``jmr.auto_execute.*`` keys in the existing
``system_settings`` table. No schema change.

The API surface is deliberately small and every mutation is audited:

* ``GET  /api/v1/jmr/auto-execute``          — current state + risk text
* ``POST /api/v1/jmr/auto-execute/enable``   — enable with explicit acknowledgement
* ``POST /api/v1/jmr/auto-execute/disable``  — turn off (always allowed)
* ``POST /api/v1/jmr/auto-execute/run``      — run one bounded pass now

Enabling requires ``acknowledge_risk: true`` **and** a non-empty rule list. There
is no "enable everything" shortcut.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ...container import Container
from ...governance.audit_bus import AuditBus
from ...core.timeutils import utc_now_iso
from ...jmr.auto_execute import (
    KEY_ACKNOWLEDGED_AT,
    KEY_ENABLED,
    KEY_LAST_RESULT,
    KEY_LAST_RUN_AT,
    KEY_RULES,
    KEY_UPDATED_AT,
    MAX_BATCH,
    RULES_LOW_TO_HIGH,
    RULE_RISK,
    AutoExecuteState,
    enable_payload,
    normalize_rules,
    read_state,
)
from ...jmr.auto_execute_runner import JmrAutoExecutor
from ...schemas.common import DataEnvelope
from ..deps import get_container

logger = logging.getLogger("jme.jmr.auto_execute_api")

router = APIRouter(tags=["jmr"])


def _read(container: Container, key: str, default: Any = None) -> Any:
    with container.manager.session() as session:
        raw = session.execute(
            text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
        ).scalar()
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _write(container: Container, key: str, value: Any) -> None:
    with container.manager.session() as session:
        session.execute(
            text(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES (:k, :v, :u) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at"
            ),
            {
                "k": key,
                "v": json.dumps(value, ensure_ascii=False, sort_keys=True),
                "u": utc_now_iso(),
            },
        )
        session.commit()


def _state(container: Container) -> AutoExecuteState:
    return read_state(lambda key, default=None: _read(container, key, default))


# --------------------------------------------------------------------- models
class AutoExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    rules: List[str]
    acknowledged_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_result: Optional[Dict[str, Any]] = None
    max_batch: int = MAX_BATCH
    available_rules: List[str] = Field(default_factory=lambda: list(RULES_LOW_TO_HIGH))
    rule_risk: Dict[str, str] = Field(default_factory=lambda: dict(RULE_RISK))
    #: 界面上必须原样展示的风险说明。
    risk_notice: str = (
        "开启后，所选类型的整理建议会被自动应用到记忆（无需逐条确认）。"
        "自动执行仍会做来源重校验、敏感信息拦截和审计留痕，"
        "但不会再有第二次人工确认。请仅勾选你确实愿意放手的类型。"
    )


class EnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: List[str] = Field(default_factory=list)
    acknowledge_risk: bool = False


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ran: bool
    attempted: int = 0
    approved: int = 0
    skipped: List[Dict[str, str]] = Field(default_factory=list)
    message: str = ""


# --------------------------------------------------------------------- routes
@router.get("/auto-execute", response_model=DataEnvelope[AutoExecuteResponse])
def get_auto_execute(
    container: Container = Depends(get_container),
) -> DataEnvelope[AutoExecuteResponse]:
    state = _state(container)
    return DataEnvelope(data=AutoExecuteResponse(**state.to_dict()))


@router.post(
    "/auto-execute/enable", response_model=DataEnvelope[AutoExecuteResponse]
)
def enable_auto_execute(
    payload: EnableRequest,
    container: Container = Depends(get_container),
) -> DataEnvelope[AutoExecuteResponse]:
    ok, rules, error = enable_payload(
        rules=payload.rules, acknowledged=payload.acknowledge_risk
    )
    if not ok:
        # Fail closed: a rejected enable leaves the previous state untouched.
        logger.info("auto-execute enable rejected: %s", error)
        state = _state(container)
        response = AutoExecuteResponse(**state.to_dict())
        response.risk_notice = error
        return DataEnvelope(data=response)

    timestamp = utc_now_iso()
    _write(container, KEY_ACKNOWLEDGED_AT, timestamp)
    _write(container, KEY_RULES, rules)
    _write(container, KEY_UPDATED_AT, timestamp)
    _write(container, KEY_ENABLED, True)

    try:
        AuditBus(container.manager).record(
            principal_id="svc:jmr-auto-execute",
            principal_type="SERVICE",
            operation="JMR_AUTO_EXECUTE_ENABLE",
            object_ref="JMR",
            trigger="JMR",
            status="SUCCEEDED",
            extra={
                "rules": list(rules),
                "acknowledged_at": timestamp,
                "risk_acknowledged": True,
            },
        )
    except Exception:
        # Fail closed: an unaudited enable must not take effect.
        _write(container, KEY_ENABLED, False)
        raise
    logger.info("auto-execute enabled for rules=%s", ",".join(rules))
    return DataEnvelope(data=AutoExecuteResponse(**_state(container).to_dict()))


@router.post(
    "/auto-execute/disable", response_model=DataEnvelope[AutoExecuteResponse]
)
def disable_auto_execute(
    container: Container = Depends(get_container),
) -> DataEnvelope[AutoExecuteResponse]:
    timestamp = utc_now_iso()
    _write(container, KEY_ENABLED, False)
    _write(container, KEY_UPDATED_AT, timestamp)
    try:
        AuditBus(container.manager).record(
            principal_id="svc:jmr-auto-execute",
            principal_type="SERVICE",
            operation="JMR_AUTO_EXECUTE_DISABLE",
            object_ref="JMR",
            trigger="JMR",
            status="SUCCEEDED",
            extra={"disabled_at": timestamp},
        )
    except Exception:
        # Disable is the safe direction; keep it effective and surface the gap.
        logger.exception("auto-execute disable audit failed")
    logger.info("auto-execute disabled")
    return DataEnvelope(data=AutoExecuteResponse(**_state(container).to_dict()))


@router.post("/auto-execute/run", response_model=DataEnvelope[RunResponse])
def run_auto_execute(
    container: Container = Depends(get_container),
) -> DataEnvelope[RunResponse]:
    state = _state(container)
    if not state.enabled:
        return DataEnvelope(
            data=RunResponse(ran=False, message="自动执行未开启。")
        )

    executor = JmrAutoExecutor(
        bridge=container.jmr_review_bridge,
        query=container.jmr_query_service,
        limit=MAX_BATCH,
    )
    # P1 任务表缺失时无法读取待处理建议。返回明确状态，不当作错误。
    try:
        result = executor.run(rules=state.rules)
    except Exception as exc:  # noqa: BLE001 - schema/CAS refusal, not a crash
        code = getattr(exc, "code", "") or type(exc).__name__
        logger.info("auto-execute run unavailable: %s", code)
        return DataEnvelope(
            data=RunResponse(
                ran=False,
                message=f"暂时无法读取整理建议（{code}）。自动执行未做任何变更。",
            )
        )

    timestamp = utc_now_iso()
    _write(container, KEY_LAST_RUN_AT, timestamp)
    _write(container, KEY_LAST_RESULT, result.to_dict())

    message = (
        f"已自动处理 {result.attempted} 条建议，"
        f"成功 {len(result.approved)} 条，跳过 {len(result.skipped)} 条。"
    )
    return DataEnvelope(
        data=RunResponse(
            ran=True,
            attempted=result.attempted,
            approved=len(result.approved),
            skipped=result.skipped,
            message=message,
        )
    )
