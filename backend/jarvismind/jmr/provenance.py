"""Source lineage and provenance contract for JMR derived rows.

The three derived tables have no ``source_memory_id`` column and no FK to
``memory_records`` (``migrations.py:232-296``). Traceability is therefore a
runtime contract on ``metadata_json``: every proposal must carry the exact
original memory ids, the lineage injected by the read projection, and enough
evidence for the review bridge to prove the source has not changed since the
proposal was generated.

The provenance keys written here are the approved JMR-04 contract:

``source_memory_ids`` / ``lineage`` / ``task_id`` / ``run_id`` / ``rule`` /
``algorithm_version`` / ``projection_digest`` / ``engine`` / ``rule_version`` /
``model_provider`` / ``model_version`` / ``prompt_version`` / ``confidence`` /
``claim_type`` / ``evidence`` / ``expires_at`` / ``generation`` / ``review`` /
``sensitivity``.

``evidence`` contains hashes and timestamps only, never memory text. It is the
review-time revalidation material and the input to ``source_fingerprint``.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping as ABCMapping
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .errors import JMRProvenanceError
from .records import SourceRecord

RULES = ("MERGE", "CONFLICT", "STATUS_CHANGE", "SUMMARY")

#: Fixed provenance values for the offline deterministic JMR engine. They are
#: records of what actually ran, not claims of an online model call.
ENGINE = "local"
MODEL_PROVIDER = "local"
MODEL_VERSION = "deterministic"
PROMPT_VERSION = "jmr-prompt-v1"
RULE_VERSION = "1"

_CLAIM_TYPES = {
    "MERGE": "FACT",
    "CONFLICT": "FACT",
    "STATUS_CHANGE": "FACT",
    "SUMMARY": "INFERENCE",
}


def content_sha256(text: str) -> str:
    """Stable content digest used by review-time source revalidation."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def evidence_for(rows: Sequence[SourceRecord]) -> List[Dict[str, Any]]:
    """Value-free evidence for every source row: id, status, timestamps, hash."""
    ordered = sorted(rows, key=lambda row: row.id)
    return [
        {
            "memory_id": row.id,
            "status": row.status,
            "updated_at": row.updated_at,
            "created_at": row.created_at,
            "content_sha256": content_sha256(row.content),
        }
        for row in ordered
    ]


def source_watermark(evidence: Sequence[Mapping[str, Any]]) -> str:
    """Latest source timestamp in the evidence set.

    Used by ``DerivedStore`` to reject a stale in-flight run: an incoming write
    whose watermark is older than the stored one may not replace a newer
    proposal, even when the deterministic id is the same.
    """
    values = [str(item.get("updated_at") or "") for item in evidence]
    return max(values, default="")


def source_fingerprint(evidence: Sequence[Mapping[str, Any]]) -> str:
    material = json.dumps(
        list(evidence), ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_expires_at(ttl_days: int, *, now: Optional[datetime] = None) -> str:
    if ttl_days <= 0:
        raise JMRProvenanceError("result TTL must be positive")
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=int(ttl_days))).isoformat()


def build_metadata(
    *,
    source_memory_ids: Sequence[str],
    lineage: Iterable[Mapping[str, Any]],
    task_id: str,
    run_id: str,
    rule: str,
    algorithm_version: str,
    projection_digest: str,
    extra: Mapping[str, Any] | None = None,
    engine: str = ENGINE,
    rule_version: str = RULE_VERSION,
    model_provider: str = MODEL_PROVIDER,
    model_version: str = MODEL_VERSION,
    prompt_version: str = PROMPT_VERSION,
    confidence: Optional[float] = None,
    claim_type: Optional[str] = None,
    evidence: Optional[Sequence[Mapping[str, Any]]] = None,
    expires_at: Optional[str] = None,
    generation: int = 1,
    review: Optional[Mapping[str, Any]] = None,
    sensitivity: Optional[str] = None,
) -> str:
    """Serialise the fixed provenance keys into ``metadata_json``.

    ``extra`` is kept BOTH as a nested object and expanded to the top level.
    The nested copy is the frozen test contract; the top-level expansion is the
    existing consumer contract (``confidence`` / ``conflict_class`` /
    ``related_memory_ids`` are read by the query facade and by downstream JME
    code). Keeping both is additive and lets neither contract silently break.
    """
    if rule not in RULES:
        raise JMRProvenanceError(f"unknown derived rule: {rule}")
    evidence_items = [dict(item) for item in (evidence or ())]
    lineage_items = (
        [dict(lineage)] if isinstance(lineage, ABCMapping)
        else [dict(item) for item in lineage]
    )
    claim = claim_type or _CLAIM_TYPES[rule]
    if claim not in {"FACT", "INFERENCE", "SPECULATION"}:
        raise JMRProvenanceError(f"unknown claim_type: {claim!r}")
    payload: Dict[str, Any] = {
        "source_memory_ids": list(source_memory_ids),
        "lineage": lineage_items,
        "task_id": task_id,
        "run_id": run_id,
        "rule": rule,
        "algorithm_version": algorithm_version,
        "projection_digest": projection_digest,
        "engine": engine,
        "rule_version": rule_version,
        "model_provider": model_provider,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "confidence": confidence,
        "claim_type": claim,
        "evidence": evidence_items,
        "expires_at": expires_at,
        "generation": int(generation),
        "review": dict(
            review
            or {
                "status": "PENDING",
                "authoritative": False,
                "source": "system_generated",
                "reviewed_at": None,
            }
        ),
        "sensitivity": sensitivity,
        "source_fingerprint": source_fingerprint(evidence_items),
        "source_watermark": source_watermark(evidence_items),
    }
    if extra:
        payload["extra"] = dict(extra)
        payload.update(dict(extra))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def validate_sources(
    rows: Sequence[SourceRecord], *, source_memory_ids: Sequence[str]
) -> None:
    """Every referenced id must be an authorised projection row of this run.

    SQLite TEXT primary keys are case-sensitive; matching is exact and never
    normalised, so a case mismatch fails closed instead of silently resolving
    to a different memory.
    """
    by_id = {row.id: row for row in rows}
    if not source_memory_ids:
        raise JMRProvenanceError("a derived proposal has no source memory ids")
    for memory_id in source_memory_ids:
        row = by_id.get(memory_id)
        if row is None:
            raise JMRProvenanceError(
                f"source {memory_id!r} is absent from this run's read projection"
            )
        lineage = dict(row.lineage)
        if (
            lineage.get("authority") != "JME"
            or lineage.get("table") != "memory_records"
            or lineage.get("memory_id") != row.id
        ):
            raise JMRProvenanceError(
                f"source {memory_id!r} has an invalid JME lineage"
            )


def deterministic_id(
    *, rule: str, source_memory_ids: Sequence[str], algorithm_version: str
) -> str:
    """Stable id: identical inputs produce the same proposal id (idempotency).

    The 64-bit truncation is a theoretical collision risk, registered in the
    design report; the rule prefix and sorted ids keep same-run collisions
    negligible.
    """
    if rule not in RULES:
        raise JMRProvenanceError(f"unknown derived rule: {rule}")
    ordered = sorted(set(str(item) for item in source_memory_ids))
    material = "\x1f".join((rule, *ordered, algorithm_version))
    return f"jmr-{rule}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def rule_of(metadata_json: str | None) -> str:
    """Read the rule from a stored ``metadata_json`` without trusting callers.

    Only a JSON object can carry a rule. Any other valid JSON (array, string,
    number, bool, null) is treated like unparseable JSON and yields ``""``, so
    the row is classified as corrupt instead of crashing the whole query (F-1).
    """
    if not metadata_json:
        return ""
    try:
        payload = json.loads(metadata_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    rule = payload.get("rule")
    # A non-string rule (e.g. a list) must not be stringified into a value that
    # looks like a rule name; it is unknown, so the row stays in ``corrupt``.
    return rule if isinstance(rule, str) else ""
