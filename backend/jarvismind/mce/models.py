"""Pure domain objects for MCE-01. No database, no network, no I/O.

The authoritative memory row has no ``project_id`` column (JME schema v6 is
frozen). MCE therefore treats **explicit metadata or an explicit marker** as
the only proof that a memory belongs to a project:

* ``metadata.project_id`` / ``metadata.project``;
* a ``project:<id>`` tag in ``metadata.tags``;
* a ``[project:<id>]`` marker in the content (first 200 characters).

A memory that merely *mentions* a project name is never attributed to it. That
conservatism is what makes cross-project isolation testable: absence of proof
means exclusion, not a guess.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MAX_PROJECT_ID_LENGTH = 64
MAX_METADATA_CHARS = 20_000
MAX_SUPERSEDES = 20
_PROJECT_MARKER = re.compile(r"\[project:([^\]\s]{1,64})\]", re.IGNORECASE)
_TAG_PREFIX = "project:"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DecisionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    TENTATIVE = "TENTATIVE"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


class ContextKind(str, Enum):
    STAGE = "STAGE"
    COMPLETED = "COMPLETED"
    DECISION = "DECISION"
    TENTATIVE = "TENTATIVE"
    RISK = "RISK"
    NEXT_STEP = "NEXT_STEP"
    NOTE = "NOTE"


#: Render order is also the default budget priority (see ``budgeter``).
KIND_PRIORITY: Mapping[ContextKind, int] = {
    ContextKind.STAGE: 0,
    ContextKind.DECISION: 1,
    ContextKind.NEXT_STEP: 2,
    ContextKind.RISK: 3,
    ContextKind.TENTATIVE: 4,
    ContextKind.COMPLETED: 5,
    ContextKind.NOTE: 6,
}

_KIND_PREFIXES: Tuple[Tuple[Tuple[str, ...], ContextKind], ...] = (
    (("已推翻", "已废弃", "旧决策", "已替代", "superseded:"), ContextKind.DECISION),
    (("待定", "待确认", "候选方案", "未定", "tentative:"), ContextKind.TENTATIVE),
    (("项目阶段", "当前阶段", "阶段：", "阶段:", "stage:"), ContextKind.STAGE),
    (("项目状态", "status:"), ContextKind.STAGE),
    (("已完成", "完成项", "已交付", "completed:"), ContextKind.COMPLETED),
    (("下一步", "待办", "后续行动", "next:", "todo:"), ContextKind.NEXT_STEP),
    (("风险", "未解决", "阻塞", "问题：", "问题:", "risk:"), ContextKind.RISK),
    (("决策", "已确认", "决定", "已定", "decision:"), ContextKind.DECISION),
)

_DECISION_PREFIXES = ("决策", "已确认", "决定", "已定", "decision:")
_TENTATIVE_PREFIXES = ("待定", "待确认", "候选方案", "未定", "tentative:")
_SUPERSEDED_PREFIXES = ("已推翻", "已废弃", "旧决策", "已替代", "superseded:")

_STATUS_ALIASES = {
    "confirmed": DecisionStatus.CONFIRMED,
    "decided": DecisionStatus.CONFIRMED,
    "accepted": DecisionStatus.CONFIRMED,
    "active": DecisionStatus.CONFIRMED,
    "tentative": DecisionStatus.TENTATIVE,
    "pending": DecisionStatus.TENTATIVE,
    "proposed": DecisionStatus.TENTATIVE,
    "candidate": DecisionStatus.TENTATIVE,
    "superseded": DecisionStatus.SUPERSEDED,
    "replaced": DecisionStatus.SUPERSEDED,
    "deprecated": DecisionStatus.DEPRECATED,
    "abandoned": DecisionStatus.DEPRECATED,
    "obsolete": DecisionStatus.DEPRECATED,
}

_KIND_ALIASES = {
    "stage": ContextKind.STAGE,
    "phase": ContextKind.STAGE,
    "completed": ContextKind.COMPLETED,
    "done": ContextKind.COMPLETED,
    "decision": ContextKind.DECISION,
    "confirmed": ContextKind.DECISION,
    "tentative": ContextKind.TENTATIVE,
    "pending": ContextKind.TENTATIVE,
    "risk": ContextKind.RISK,
    "issue": ContextKind.RISK,
    "next_step": ContextKind.NEXT_STEP,
    "next": ContextKind.NEXT_STEP,
    "todo": ContextKind.NEXT_STEP,
    "note": ContextKind.NOTE,
    "background": ContextKind.NOTE,
}


def normalize_project_id(value: Any) -> Optional[str]:
    """Return a safe project id, or ``None`` when the value is unusable.

    Only ASCII letters are case-folded. Deliberately **no** NFKC or Unicode
    casefold: those map distinct explicit ids onto the same key (for example
    ``①`` vs ``1``, ``ß`` vs ``ss``), which would weaken the cross-project
    isolation boundary. Non-ASCII ids are preserved verbatim after trimming.
    Control characters and whitespace-only values are rejected rather than
    silently rewritten: a bad id must not collapse two projects into one.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_PROJECT_ID_LENGTH:
        return None
    if _CONTROL.search(text):
        return None
    return "".join(char.lower() if "A" <= char <= "Z" else char for char in text)


def parse_metadata(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse ``metadata_json`` safely. ``None`` means absent or unusable."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    if len(raw) > MAX_METADATA_CHARS:
        return None
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _string(value: Any, *, limit: int = 256) -> Optional[str]:
    if isinstance(value, str):
        # Labels and keys are rendered into a single prompt line; collapsing
        # whitespace here is a prompt-injection boundary, not cosmetic tidying.
        text = re.sub(r"\s+", " ", value).strip()
        return text[:limit] if text else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def metadata_tags(metadata: Mapping[str, Any]) -> Tuple[str, ...]:
    """Collect tags from the common spellings, bounded and deduplicated."""
    raw: List[Any] = []
    for key in ("tags", "project_tags", "labels"):
        value = metadata.get(key)
        if isinstance(value, str):
            raw.extend(part for part in re.split(r"[,;，；]", value))
        elif isinstance(value, (list, tuple)):
            raw.extend(value)
    tags: List[str] = []
    for item in raw:
        text = _string(item, limit=128)
        if text and text not in tags:
            tags.append(text)
    return tuple(tags[:50])


def metadata_project_id(metadata: Mapping[str, Any], content: str = "") -> Optional[str]:
    """Resolve an explicit project id from metadata, tags or a content marker."""
    for key in ("project_id", "projectId", "project"):
        candidate = normalize_project_id(metadata.get(key))
        if candidate is not None:
            return candidate
    for tag in metadata_tags(metadata):
        normalized = normalize_project_id(tag)
        if normalized is not None and normalized.startswith(_TAG_PREFIX):
            candidate = normalize_project_id(normalized[len(_TAG_PREFIX):])
            if candidate is not None:
                return candidate
    for marker in _PROJECT_MARKER.finditer((content or "")[:2000]):
        candidate = normalize_project_id(marker.group(1))
        if candidate is not None:
            return candidate
    return None


def metadata_project_name(metadata: Mapping[str, Any]) -> Optional[str]:
    for key in ("project_name", "projectName", "project_title"):
        value = _string(metadata.get(key), limit=120)
        if value:
            return value
    return None


def classify_kind(
    *,
    metadata: Mapping[str, Any],
    memory_type: str,
    content: str,
) -> ContextKind:
    """Classify one memory deterministically.

    Metadata wins. For historical rows, a recognised leading label is the
    fallback. Everything else is a NOTE and is rendered last (or dropped by the
    budget before any decision).
    """
    raw_kind = metadata.get("context_kind") or metadata.get("kind") or metadata.get(
        "category"
    )
    if isinstance(raw_kind, str):
        mapped = _KIND_ALIASES.get(raw_kind.strip().casefold())
        if mapped is not None:
            return mapped
    if metadata.get("stage") is not None or metadata.get("phase") is not None:
        return ContextKind.STAGE
    if metadata.get("decision_key") is not None or metadata.get(
        "decision_status"
    ) is not None:
        return ContextKind.DECISION

    head = (content or "").lstrip()[:80]
    for prefixes, kind in _KIND_PREFIXES:
        if head.startswith(prefixes):
            return kind

    if memory_type == "PROJECT":
        return ContextKind.NOTE
    return ContextKind.NOTE


def decision_status_for(
    *,
    metadata: Mapping[str, Any],
    kind: ContextKind,
    content: str,
) -> DecisionStatus:
    raw = (
        metadata.get("decision_status")
        or metadata.get("decisionStatus")
        or metadata.get("status")
    )
    if isinstance(raw, str):
        mapped = _STATUS_ALIASES.get(raw.strip().casefold())
        if mapped is not None:
            return mapped
    head = (content or "").lstrip()[:80]
    if head.startswith(_SUPERSEDED_PREFIXES):
        return DecisionStatus.SUPERSEDED
    if head.startswith(_TENTATIVE_PREFIXES):
        return DecisionStatus.TENTATIVE
    if head.startswith(_DECISION_PREFIXES):
        return DecisionStatus.CONFIRMED
    if kind == ContextKind.TENTATIVE:
        return DecisionStatus.TENTATIVE
    if kind == ContextKind.DECISION:
        return DecisionStatus.CONFIRMED
    return DecisionStatus.UNKNOWN


def metadata_decision_key(metadata: Mapping[str, Any]) -> Optional[str]:
    # Only the namespaced fields are accepted. A generic ``key`` is often an
    # idempotency/cache key unrelated to decisions and would cause unintended
    # same-key suppression.
    for key in ("decision_key", "decisionKey"):
        value = _string(metadata.get(key), limit=128)
        if value:
            return value.casefold()
    return None


def metadata_supersedes(metadata: Mapping[str, Any]) -> Tuple[str, ...]:
    """Collect explicit "this replaces that" memory ids, bounded and stable."""
    raw: List[Any] = []
    for key in ("supersedes_memory_id", "supersedes", "replaces", "replaced_memory_id"):
        value = metadata.get(key)
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, (list, tuple)):
            raw.extend(value)
    ids: List[str] = []
    for item in raw:
        text = _string(item, limit=128)
        if text and text not in ids:
            ids.append(text)
    return tuple(ids[:MAX_SUPERSEDES])


def metadata_stage(metadata: Mapping[str, Any]) -> Optional[str]:
    for key in ("stage", "phase", "project_stage"):
        value = _string(metadata.get(key), limit=200)
        if value:
            return value
    return None


def content_preview(content: str, limit: int) -> Tuple[str, bool]:
    """Collapse whitespace and control characters for prompt-safe insertion."""
    text = unicodedata.normalize("NFKC", content or "")
    text = _CONTROL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text, False
    return text[: max(1, limit - 1)].rstrip() + "…", True


@dataclass(frozen=True)
class ContextItem:
    """One reconstructed context entry, always traceable to a memory id."""

    memory_id: str
    kind: ContextKind
    content: str
    summary: Optional[str]
    importance: float
    confidence: float
    created_at: str
    updated_at: str
    relevance: float = 0.0
    decision_key: Optional[str] = None
    decision_status: Optional[DecisionStatus] = None
    supersedes: Tuple[str, ...] = ()
    superseded_by: Optional[str] = None
    decision_reasons: Tuple[str, ...] = ()
    injected: bool = True
    truncated: bool = False


@dataclass(frozen=True)
class RelatedKnowledge:
    """One valid one-hop JKG relation attached to a reconstructed memory."""

    node_id: str
    entity_type: str
    name: str
    predicate: str
    direction: str
    neighbor_id: str
    neighbor_name: str
    neighbor_entity_type: str
    confidence: float
    valid_source_count: int
    source_memory_ids: Tuple[str, ...]


@dataclass(frozen=True)
class SourceRef:
    memory_id: str
    kind: ContextKind
    updated_at: str
    injected: bool = True


@dataclass
class ExcludedCounts:
    """Value-free diagnostics. Counts only: no content, no categories."""

    scanned: int = 0
    other_project: int = 0
    no_project: int = 0
    expired: int = 0
    sensitive: int = 0
    filtered: int = 0
    irrelevant: int = 0
    duplicate: int = 0
    superseded: int = 0
    budget_dropped: int = 0
    cap_dropped: int = 0
    jkg_nodes_scanned: int = 0
    jkg_edges_rejected: int = 0


@dataclass
class ProjectContextSnapshot:
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    project_status: Optional[str] = None
    stage: Optional[ContextItem] = None
    completed_items: List[ContextItem] = field(default_factory=list)
    confirmed_decisions: List[ContextItem] = field(default_factory=list)
    tentative_items: List[ContextItem] = field(default_factory=list)
    superseded_decisions: List[ContextItem] = field(default_factory=list)
    open_risks: List[ContextItem] = field(default_factory=list)
    next_steps: List[ContextItem] = field(default_factory=list)
    background_items: List[ContextItem] = field(default_factory=list)
    related_knowledge: List[RelatedKnowledge] = field(default_factory=list)
    sources: List[SourceRef] = field(default_factory=list)
    excluded: ExcludedCounts = field(default_factory=ExcludedCounts)
    jkg_state: str = "disabled"
    jkg_enriched: bool = False
    jkg_reason: str = "not_requested"
    generated_at: str = ""
    truncated: bool = False
    scan_truncated: bool = False


@dataclass
class ContextResolutionResult:
    snapshot: ProjectContextSnapshot
    rendered_context: str = ""
    token_estimate: int = 0
    budget_max_tokens: int = 0
    shell_tokens: int = 0
    dropped_by_budget: int = 0
    truncated: bool = False
    resolution_version: str = "mce-v1"
    empty_reason: Optional[str] = None
