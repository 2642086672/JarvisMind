"""Deterministic, offline reflection strategies.

These functions never call a model. They produce explainable drafts from the
memory text itself so the lifecycle service works with no network and no API
key. The LLM engine uses them as a fallback when a provider call fails.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from ...core.dedup import is_conflict, is_duplicate

MAX_CONTENT_PREVIEW = 400


def _preview(text: str, limit: int = MAX_CONTENT_PREVIEW) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def _group_by_type(records: Sequence) -> Dict[str, List]:
    grouped: Dict[str, List] = defaultdict(list)
    for record in records:
        grouped[getattr(record, "memory_type", "SEMANTIC")].append(record)
    return grouped


def build_summary(
    records: Sequence,
    *,
    summary_type: str,
    memory_type: Optional[str] = None,
) -> Dict[str, object]:
    """Build a deterministic summary draft for a set of memories."""
    items = list(records)
    if not items:
        return {"title": "", "content": "", "confidence": 0.0}

    grouped = _group_by_type(items)
    lines: List[str] = []
    titles = {
        "COLLECTION": "记忆集合总结",
        "CATEGORY": f"{memory_type or '记忆'} 类别总结",
        "PROJECT": "项目经历总结",
        "PROFILE": "长期用户画像总结",
        "REFLECTION": "记忆反思总结",
    }
    title = titles.get(summary_type, "记忆总结")

    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        f"- 来源记忆：{len(items)} 条；类别：{', '.join(sorted(grouped.keys()))}"
    )
    lines.append("")

    for mtype in sorted(grouped.keys()):
        lines.append(f"## {mtype}")
        for record in grouped[mtype]:
            content = _preview(record.content)
            lines.append(f"- {content}")
        lines.append("")

    # Deduplicate near-identical lines so the summary stays readable.
    seen = set()
    deduped: List[str] = []
    for line in lines:
        key = line.strip()
        if key.startswith("- ") and key in seen:
            continue
        if key.startswith("- "):
            seen.add(key)
        deduped.append(line)

    confidence = 0.6 if len(items) >= 2 else 0.4
    return {
        "title": title,
        "content": "\n".join(deduped).strip(),
        "confidence": confidence,
    }


def detect_conflicts(records: Sequence) -> List[Dict[str, object]]:
    """Pairwise conflict detection using the explainable dedup rules."""
    items = list(records)
    found: List[Dict[str, object]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left = items[i]
            right = items[j]
            if is_conflict(left.content, right.content):
                found.append(
                    {
                        "memory_id_a": left.id,
                        "memory_id_b": right.id,
                        "reason": "同一主题但表述不一致，需要人工确认哪一条正确",
                        "confidence": 0.55,
                    }
                )
    return found


def detect_duplicate_groups(records: Sequence) -> List[List[str]]:
    """Group near-duplicate memories into merge candidate clusters."""
    items = list(records)
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if is_duplicate(items[i].content, items[j].content):
                union(i, j)

    clusters: Dict[int, List[str]] = defaultdict(list)
    for idx, record in enumerate(items):
        clusters[find(idx)].append(record.id)
    return [ids for ids in clusters.values() if len(ids) > 1]


def build_merge_draft(
    records: Sequence,
    *,
    reason: Optional[str] = None,
) -> Dict[str, object]:
    """Combine a cluster of related memories into one draft merged memory."""
    items = sorted(records, key=lambda r: r.created_at or "")
    if not items:
        return {"new_content": "", "new_summary": None, "reason": reason or ""}

    chunks: List[str] = []
    for record in items:
        chunks.append(_preview(record.content, 600))
    new_content = "\n".join(chunks)

    mtype = items[0].memory_type
    new_summary = f"合并了 {len(items)} 条{mtype}记忆"
    return {
        "new_content": new_content,
        "new_summary": new_summary,
        "reason": reason or "多条记忆内容高度重复，合并可减少检索噪声",
        "memory_type": mtype,
        "source_id": items[0].source_id,
    }
