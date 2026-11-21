"""Prompt building and response parsing for LLM-backed reflection.

The model is asked for strict JSON. Parsing tolerates a surrounding code fence
and returns ``None`` when the payload is unusable so the caller can fall back to
the deterministic local strategy.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

MAX_MEMORY_CHARS = 1200

SUMMARY_SYSTEM = (
    "你是个人长期记忆引擎的反思模块。你的任务是把多条记忆整理成一条高质量的"
    "总结候选。要求：保留事实、去重、按主题归类、不臆造。只输出 JSON，格式为 "
    '{"title": string, "content": string, "confidence": number}。'
)
CONFLICT_SYSTEM = (
    "你是个人长期记忆引擎的冲突检测模块。判断给出的记忆中哪些对同一事实表述"
    "不一致。不要臆造冲突；只有主题相同且语义矛盾才算冲突。只输出 JSON，格式为 "
    '{"conflicts": [{"memory_id_a": string, "memory_id_b": string, '
    '"reason": string, "confidence": number}]}。'
)
MERGE_SYSTEM = (
    "你是个人长期记忆引擎的合并模块。把给出的多条重复或相关记忆合并为一条"
    "更完整、无冗余的记忆。保留所有独有事实，不要臆造。只输出 JSON，格式为 "
    '{"new_content": string, "new_summary": string, "reason": string}。'
)


def _format_records(records: Sequence) -> str:
    lines: List[str] = []
    for index, record in enumerate(records):
        content = " ".join((record.content or "").split())[:MAX_MEMORY_CHARS]
        lines.append(
            f"[{index}] id={record.id} type={record.memory_type} "
            f"status={record.status} source={record.source_id or '-'}\n"
            f"content: {content}"
        )
    return "\n\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else raw
    # Trim to the outermost braces when the model added prose.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_summary_messages(
    records: Sequence, *, summary_type: str, memory_type: Optional[str] = None
) -> List[Dict[str, str]]:
    label = {
        "COLLECTION": "指定记忆集合",
        "CATEGORY": f"{memory_type or '指定类别'} 类别",
        "PROJECT": "项目经历",
        "PROFILE": "长期用户画像",
        "REFLECTION": "记忆反思",
    }.get(summary_type, "记忆集合")
    return [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"请为以下{label}生成一条总结候选。\n\n"
                f"{_format_records(records)}"
            ),
        },
    ]


def parse_summary_response(text: str) -> Optional[Dict[str, Any]]:
    parsed = _extract_json(text)
    if not parsed:
        return None
    title = parsed.get("title")
    content = parsed.get("content")
    if not isinstance(title, str) or not isinstance(content, str):
        return None
    if not title.strip() or not content.strip():
        return None
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "title": title.strip(),
        "content": content.strip(),
        "confidence": max(0.0, min(1.0, confidence)),
    }


def build_conflict_messages(records: Sequence) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": CONFLICT_SYSTEM},
        {"role": "user", "content": _format_records(records)},
    ]


def parse_conflict_response(text: str) -> List[Dict[str, Any]]:
    parsed = _extract_json(text)
    if not parsed or not isinstance(parsed.get("conflicts"), list):
        return []
    results: List[Dict[str, Any]] = []
    for item in parsed["conflicts"]:
        if not isinstance(item, dict):
            continue
        a = item.get("memory_id_a")
        b = item.get("memory_id_b")
        if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        results.append(
            {
                "memory_id_a": a,
                "memory_id_b": b,
                "reason": str(item.get("reason") or "表述不一致"),
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
    return results


def build_merge_messages(records: Sequence) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": MERGE_SYSTEM},
        {"role": "user", "content": _format_records(records)},
    ]


def parse_merge_response(text: str) -> Optional[Dict[str, Any]]:
    parsed = _extract_json(text)
    if not parsed:
        return None
    content = parsed.get("new_content")
    if not isinstance(content, str) or not content.strip():
        return None
    summary = parsed.get("new_summary")
    return {
        "new_content": content.strip(),
        "new_summary": summary.strip() if isinstance(summary, str) else None,
        "reason": str(parsed.get("reason") or "多条记忆高度重复，建议合并"),
    }
