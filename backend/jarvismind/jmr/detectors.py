"""Deterministic, offline JMR detectors (pure functions, no DB / no IO).

Design v1.2 decisions implemented here:

* Thresholds are REAL parameters. ``jme.app.core.dedup`` is reused for
  normalisation / tokenisation / Jaccard, but the comparisons are implemented
  locally so ``JMRConfig`` is not a set of dead configuration values
  (``local.detect_*`` reads module-level constants and accepts no arguments).
* Priority chain ``MERGE > STATUS_CHANGE > CONFLICT``: a status change across
  two timestamps also satisfies the conflict predicate (same subject, low
  similarity), so STATUS_CHANGE must claim the pair first; otherwise the
  status-update acceptance criterion becomes unreachable.
* Status words are matched as ORIGINAL TEXT SUBSTRINGS. ``segment_for_index``
  only emits CJK unigrams/bigrams, so three-character words like ``进行中``
  can never appear as a token, and ``dedup._STOPWORDS`` contains ``进行`` /
  ``完成`` / ``项目``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Sequence, Tuple

from ..core import dedup
from ..services.reflection import local
from .records import SourceRecord

# Longest match first: "已完成" before "完成", "进行中" before "进行".
STATUS_TOKENS: Tuple[Tuple[str, str], ...] = (
    ("已完成", "DONE"),
    ("完成", "DONE"),
    ("进行中", "IN_PROGRESS"),
    ("进行", "IN_PROGRESS"),
    ("已上线", "LIVE"),
    ("上线", "LIVE"),
    ("下线", "OFFLINE"),
    ("暂停", "PAUSED"),
    ("取消", "CANCELLED"),
    ("在职", "ACTIVE"),
    ("离职", "INACTIVE"),
    ("有效", "ACTIVE"),
    ("失效", "VOID"),
    ("作废", "VOID"),
)

# ``dedup.token_set`` contains single CJK characters, so unrelated short
# Chinese sentences share most of their tokens and ``dedup.similarity`` reads
# ~0.87 for "Atlas 项目预算为 100 万元" vs "...250 万元". Similarity is therefore
# computed over SALIENT tokens only. A lower bound stops two memories that
# merely share one generic word from being reported as a conflict.
_MIN_SUBJECT_SIMILARITY = 0.10

_ASCII_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_GENERIC_ASCII = frozenset(
    {
        "the", "and", "for", "with", "project", "status", "current", "user",
        "data", "system", "test", "note", "memory", "gateway", "state",
    }
)


@dataclass(frozen=True)
class DuplicateCluster:
    memory_ids: Tuple[str, ...]
    reason: str
    confidence: float


@dataclass(frozen=True)
class ConflictPair:
    memory_id_a: str
    memory_id_b: str
    reason: str
    confidence: float
    conflict_class: str = "WORDING_DIFF"


@dataclass(frozen=True)
class StatusChange:
    memory_id: str
    earlier_memory_id: str
    subject: str
    before_status: str
    after_status: str
    reason: str


@dataclass(frozen=True)
class RelatedGroup:
    subject: str
    memory_ids: Tuple[str, ...]
    reason: str


# ---------------------------------------------------------------------------
# Pair helpers
# ---------------------------------------------------------------------------
def _norm_pair(left: str, right: str) -> Tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def all_pairs(rows: Sequence[SourceRecord]) -> Tuple[Tuple[str, str], ...]:
    ids = sorted(row.id for row in rows)
    return tuple(
        (ids[i], ids[j])
        for i in range(len(ids))
        for j in range(i + 1, len(ids))
    )


def cluster_pairs(clusters: Iterable[DuplicateCluster]) -> FrozenSet[Tuple[str, str]]:
    pairs: set = set()
    for cluster in clusters:
        ids = sorted(cluster.memory_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.add((ids[i], ids[j]))
    return frozenset(pairs)


def pair_set(pairs: Iterable[Any]) -> FrozenSet[Tuple[str, str]]:
    """Normalise ConflictPair / StatusChange / raw tuples into a pair set."""
    out: set = set()
    for item in pairs:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            left, right = str(item[0]), str(item[1])
        elif hasattr(item, "memory_id_a"):
            left, right = item.memory_id_a, item.memory_id_b
        elif hasattr(item, "earlier_memory_id"):
            left, right = item.earlier_memory_id, item.memory_id
        else:
            continue
        out.add(_norm_pair(left, right))
    return frozenset(out)


def status_pairs(changes: Iterable[StatusChange]) -> FrozenSet[Tuple[str, str]]:
    return pair_set(changes)


# ---------------------------------------------------------------------------
# Subject grouping
# ---------------------------------------------------------------------------
def _subject_key(row: SourceRecord) -> str:
    """Prefer a stable ASCII identifier; fall back to shared salient tokens.

    ``项目 A 进行中`` cannot be grouped by CJK tokens: ``项目`` / ``进行`` are
    stopwords and ``A`` is a single character. A stable ASCII name such as
    ``Phoenix`` is required for deterministic state-change grouping.
    """
    for match in _ASCII_IDENTIFIER.finditer(row.content or ""):
        word = match.group(0).lower()
        if word not in _GENERIC_ASCII:
            return word
    tokens = sorted(dedup._subject_tokens(row.content or ""))
    return "|".join(tokens[:2]) if tokens else ""


def _status_token(text: str) -> str:
    for needle, value in STATUS_TOKENS:
        if needle in (text or ""):
            return value
    return ""


def subject_similarity(left_text: str, right_text: str) -> float:
    """Jaccard over SALIENT tokens (no CJK unigrams, no stopwords).

    Exported so acceptance tests can choose thresholds against the SAME metric
    the detectors actually use, instead of ``dedup.similarity`` (which counts
    CJK unigrams and reads ~0.87 for unrelated short Chinese sentences).
    """
    left = dedup._subject_tokens(left_text)
    right = dedup._subject_tokens(right_text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# Internal alias kept for readability inside this module.
_subject_similarity = subject_similarity


# ---------------------------------------------------------------------------
# Comparisons (threshold-parameterised)
# ---------------------------------------------------------------------------
def _is_duplicate(
    left_text: str, right_text: str, *, threshold: float, subsumption: float
) -> bool:
    if not left_text or not right_text:
        return False
    left, right = dedup.normalize(left_text), dedup.normalize(right_text)
    if left == right:
        return True
    # Subsumption: one normalised text is contained in the other. The overlap
    # is measured over SALIENT tokens (not character-length ratio and not
    # ``dedup.token_set``, which contains CJK unigrams). A character ratio of
    # 0.65 wrongly rejected "Bifrost 网关采用 PostgreSQL 主从架构" against its
    # longer extension (ratio 0.6364); a single shared word cannot reach the
    # ``smaller >= 2`` guard below.
    if left and right and (left in right or right in left):
        ls = dedup._subject_tokens(left_text)
        rs = dedup._subject_tokens(right_text)
        if not ls or not rs:
            return False
        smaller = min(len(ls), len(rs))
        return smaller >= 2 and len(ls & rs) / smaller >= subsumption
    if _subject_similarity(left_text, right_text) >= threshold:
        return True
    return False


def _is_conflict(
    left_text: str, right_text: str, *, max_similarity: float
) -> bool:
    if not left_text or not right_text:
        return False
    left_subjects = dedup._subject_tokens(left_text)
    right_subjects = dedup._subject_tokens(right_text)
    shared = left_subjects & right_subjects
    if not shared:
        return False
    distinctive = {token for token in shared if token.isascii() and len(token) >= 3}
    if len(shared) < 2 and len(distinctive) < 1:
        return False
    if len(left_subjects) < 3 and len(distinctive) < 1:
        return False
    similarity = _subject_similarity(left_text, right_text)
    return _MIN_SUBJECT_SIMILARITY <= similarity <= max_similarity


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
def find_duplicate_clusters(
    rows: Sequence[SourceRecord],
    *,
    threshold: float = 0.82,
    subsumption: float = 0.65,
) -> Tuple[DuplicateCluster, ...]:
    """Union-find over the duplicate relation. O(n^2), n <= max_memories."""
    items = list(rows)
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _is_duplicate(
                items[i].content,
                items[j].content,
                threshold=threshold,
                subsumption=subsumption,
            ):
                union(i, j)

    groups: Dict[int, List[str]] = {}
    for index, row in enumerate(items):
        groups.setdefault(find(index), []).append(row.id)
    clusters = [
        DuplicateCluster(
            memory_ids=tuple(sorted(ids)),
            reason="内容规范化相同或高度相似（含子串包含），合并可减少检索噪声",
            confidence=0.82,
        )
        for ids in groups.values()
        if len(ids) > 1
    ]
    return tuple(sorted(clusters, key=lambda c: c.memory_ids))


def find_conflict_pairs(
    rows: Sequence[SourceRecord],
    *,
    max_similarity: float = 0.80,
    exclude_pairs: FrozenSet[Tuple[str, str]] = frozenset(),
) -> Tuple[ConflictPair, ...]:
    items = sorted(rows, key=lambda r: r.id)
    found: List[ConflictPair] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left, right = items[i], items[j]
            if _norm_pair(left.id, right.id) in exclude_pairs:
                continue
            if _is_conflict(
                left.content, right.content, max_similarity=max_similarity
            ):
                found.append(
                    ConflictPair(
                        memory_id_a=left.id,
                        memory_id_b=right.id,
                        reason="同一主题但表述不一致，需要人工确认哪一条正确",
                        confidence=0.55,
                        conflict_class="WORDING_DIFF",
                    )
                )
    return tuple(found)


def find_status_changes(
    rows: Sequence[SourceRecord],
    *,
    min_group: int = 2,
    exclude_pairs: FrozenSet[Tuple[str, str]] = frozenset(),
) -> Tuple[StatusChange, ...]:
    """Group by subject, order by created_at, compare ADJACENT state tokens.

    An A -> B -> A sequence yields two adjacent changes, never a collapsed
    A -> A result; intermediate states are not skipped.
    """
    groups: Dict[str, List[SourceRecord]] = {}
    for row in rows:
        # Only state-bearing memories participate. Otherwise a same-subject
        # memory without a status word (e.g. "Phoenix 网关采用 PostgreSQL")
        # sits between two state records in created_at order and swallows the
        # adjacent comparison that defines a state change.
        if not _status_token(row.content):
            continue
        key = _subject_key(row)
        if not key:
            continue
        groups.setdefault(key, []).append(row)

    found: List[StatusChange] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda r: (r.created_at, r.id))
        if len(members) < max(2, min_group):
            continue
        for earlier, later in zip(members, members[1:]):
            if _norm_pair(earlier.id, later.id) in exclude_pairs:
                continue
            before = _status_token(earlier.content)
            after = _status_token(later.content)
            if not before or not after or before == after:
                continue
            found.append(
                StatusChange(
                    memory_id=later.id,
                    earlier_memory_id=earlier.id,
                    subject=key,
                    before_status=before,
                    after_status=after,
                    reason=(
                        f"主体 {key} 的状态由 {before} 变为 {after}"
                        f"（{earlier.created_at} -> {later.created_at}），"
                        "仅为状态更新建议，需人工审核"
                    ),
                )
            )
    return tuple(sorted(found, key=lambda c: (c.subject, c.memory_id)))


def group_related(rows: Sequence[SourceRecord]) -> Tuple[RelatedGroup, ...]:
    """Shared-subject groups; structured candidate input for a later JKG batch.

    This is NOT a JKG implementation: no four predicates, no graph tables.
    """
    groups: Dict[str, List[str]] = {}
    for row in rows:
        key = _subject_key(row)
        if key:
            groups.setdefault(key, []).append(row.id)
    return tuple(
        RelatedGroup(
            subject=key,
            memory_ids=tuple(sorted(set(ids))),
            reason="共享主体标识，可作为后续 JKG 的关联候选输入（本批次不建图谱）",
        )
        for key, ids in sorted(groups.items())
        if len(set(ids)) >= 2
    )


def build_summary_draft(
    rows: Sequence[SourceRecord], *, summary_type: str = "REFLECTION"
) -> Tuple[str, str, float]:
    """Reuse the existing offline deterministic summary builder."""
    draft = local.build_summary(rows, summary_type=summary_type, memory_type=None)
    return (
        str(draft.get("title") or "记忆反思总结"),
        str(draft.get("content") or ""),
        float(draft.get("confidence") or 0.0),
    )
