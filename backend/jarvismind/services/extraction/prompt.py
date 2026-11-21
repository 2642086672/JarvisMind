"""LLM prompt construction and response parsing for extraction."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from ...core.sensitive import redact
from .local import LocalCandidate, classify

SYSTEM_PROMPT = """你是一个记忆提取器，负责从用户与助手的对话中识别值得长期记住的信息。

规则：
1. 只提取长期有效的内容：用户偏好、稳定事实、项目背景与决策、重要经历。
2. 短期任务、待办、闲聊、重复内容不要提取。
3. 绝对不要提取密码、API Key、Token、身份证号、银行卡号等敏感凭证。
4. 不要猜测。信息不足时降低 confidence，不要编造。
5. memory_type 只能是：PROFILE、SEMANTIC、EPISODIC、PROJECT、WORKING。
6. importance 取值 0.0-10.0，confidence 取值 0.0-1.0。
7. 每条内容独立成条，不要合并多个事实。

只输出 JSON，格式如下，不要输出任何其他文字：
{"candidates":[{"memory_type":"SEMANTIC","content":"...","summary":"...","importance":7.0,"confidence":0.9}]}

如果没有值得提取的内容，返回 {"candidates":[]}。"""

USER_TEMPLATE = """对话内容：
{conversation}
{context_block}
请提取记忆候选，只输出 JSON。"""


def build_messages(conversation: str, context: str | None, extra_redactions=()) -> List[Dict[str, str]]:
    safe_conversation = redact(conversation, extra_redactions)
    context_block = f"\n补充上下文：\n{redact(context, extra_redactions)}\n" if context else ""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            conversation=safe_conversation, context_block=context_block
        )},
    ]


def parse_response(text: str) -> List[Dict[str, Any]]:
    """Extract and validate the candidate list from an LLM reply."""
    if not text:
        return []
    candidate_json = _extract_json(text)
    if candidate_json is None:
        raise ValueError("response contained no JSON object")
    payload = json.loads(candidate_json)
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("candidates")
        if items is None and "memory_type" in payload:
            items = [payload]
    else:
        raise ValueError("unexpected JSON shape")

    if not isinstance(items, list):
        raise ValueError("candidates is not a list")

    results: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        memory_type = str(item.get("memory_type", "")).upper()
        content = str(item.get("content", "")).strip()
        if memory_type not in {"PROFILE", "SEMANTIC", "EPISODIC", "PROJECT", "WORKING"}:
            continue
        if not content:
            continue
        results.append(
            {
                "memory_type": memory_type,
                "content": content,
                "summary": str(item.get("summary", ""))[:200] or None,
                "importance": _clamp(float(item.get("importance", 5.0)), 0.0, 10.0),
                "confidence": _clamp(float(item.get("confidence", 0.5)), 0.0, 1.0),
            }
        )
    return results


def _extract_json(text: str) -> str | None:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        return fenced.group(1).strip()
    brace = re.search(r"\{[\s\S]*\}", text)
    return brace.group(0) if brace else None


def _clamp(value: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, value))
    except (TypeError, ValueError):
        return low


def local_fallback(text: str) -> List[LocalCandidate]:
    """Used when the LLM reply cannot be parsed."""
    candidates = []
    for statement in text.splitlines():
        candidate = classify(statement.strip())
        if candidate:
            candidates.append(candidate)
    return candidates
