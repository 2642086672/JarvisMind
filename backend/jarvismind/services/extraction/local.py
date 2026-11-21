"""Deterministic extraction strategy.

This is the default engine when no LLM provider is configured. It is a real
rule-based classifier, not a stub: it segments a conversation, classifies each
statement by lexical rules, scores confidence and importance, and filters
chitchat. It is intentionally conservative — anything it cannot classify with
confidence is left to the user as a low-confidence candidate rather than being
written as a fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...core.sensitive import redact, scan

# (type, patterns, base confidence, base importance)
_TYPE_RULES: Tuple[Tuple[str, Tuple[str, ...], float, float], ...] = (
    (
        "PROFILE",
        (
            "称呼我", "叫我", "叫我先生", "怎么用称呼", "prefer to be called",
            "用中文", "中文交流", "语言偏好", "回答风格", "简洁", "正式",
            "初学者", "小白", "direct answers", "communication style",
            "professional style", "jarvis",
        ),
        0.85,
        8.0,
    ),
    (
        "PROJECT",
        (
            "项目", " JarvisMind", "JarvisMind", "JME", "代码", "仓库", "技术栈",
            "react native", "expo", "typescript", "开发环境", "部署", "build",
            "roadmap", "路线", "计划", "预算", "商业化",
        ),
        0.80,
        7.5,
    ),
    (
        "SEMANTIC",
        (
            "我从事", "我的工作", "负责", "我在", "我养", "我喜欢", "我居住",
            "我的行业", "工作内容", "日常需要", "经常需要", "重视", "习惯",
            "i work in", "i live in", "i have", "i prefer", "my job",
        ),
        0.72,
        6.5,
    ),
    (
        "EPISODIC",
        (
            "昨天", "上周", "上个月", "去年", "前几天", "那次", "记得",
            "参加了", "去了", "拜访", "会议", "yesterday", "last week",
            "last year", "i visited", "we went",
        ),
        0.78,
        6.0,
    ),
    (
        "WORKING",
        (
            "今天要做", "待办", "任务", "周报", "安排一下", "跟进", "处理一下",
            "todo", "task for", "need to finish", "deadline",
        ),
        0.62,
        4.0,
    ),
)

_CHITCHAT = (
    "你好", "您好", "谢谢", "多谢", "好的", "收到", "明白", "嗯嗯", "哈哈",
    "再见", "拜拜", "请稍等", "hello", "hi", "thanks", "thank you", "ok",
    "okay", "got it", "bye",
)

# Statements containing these markers are treated as durable rather than
# transient, even when they otherwise look like task talk.
_DURABLE_MARKERS = ("一直", "长期", "通常", "始终", "always", "usually", "every")

_SENTENCE_SPLIT = re.compile(r"[。！？；\n\r]+|\.(?=\s)|(?<=[一-鿿])\.(?=\s)")
_MAX_CANDIDATES = 12
_MIN_STATEMENT_LENGTH = 8


@dataclass
class LocalCandidate:
    memory_type: str
    content: str
    summary: str
    importance: float
    confidence: float
    matched_rules: List[str] = field(default_factory=list)
    unit_index: int = -1


@dataclass
class ExtractionUnit:
    """003-B1: one processing unit with its position in the ORIGINAL text.

    ``start``/``end`` let the engine map a credential hit that spans several
    segments (PEM blocks, DB URIs) back to the units it blocks.
    """

    index: int
    statement: str
    redacted: str
    start: int
    end: int
    candidate: Optional[LocalCandidate]
    credential_flags: List[str]
    pii_flags: List[str]
    empty: bool = False


def segment_conversation(conversation: str) -> List[str]:
    parts = _SENTENCE_SPLIT.split(conversation)
    return [part.strip(" \t-,.，, ") for part in parts if part and part.strip()]


def classify(statement: str) -> Optional[LocalCandidate]:
    """Classify one statement. Returns None when it carries no memory value."""
    lowered = statement.lower()
    if len(statement) < _MIN_STATEMENT_LENGTH:
        return None
    if any(mark in lowered for mark in _CHITCHAT) and len(statement) < 20:
        return None

    best: Optional[Tuple[str, float, float, List[str], int]] = None
    for memory_type, patterns, base_conf, base_importance in _TYPE_RULES:
        hits = [pattern for pattern in patterns if pattern.lower() in lowered]
        if not hits:
            continue
        strength = len(hits)
        candidate = (
            memory_type,
            base_conf + 0.03 * min(strength - 1, 3),
            base_importance,
            hits,
            strength,
        )
        if best is None or candidate[4] > best[4]:
            best = candidate

    if best is None:
        return None

    memory_type, confidence, importance, hits, _ = best
    confidence = min(confidence, 0.95)
    if len(statement) < 20:
        confidence -= 0.08
    if any(marker in lowered for marker in _DURABLE_MARKERS):
        importance = min(importance + 1.0, 10.0)
        confidence = min(confidence + 0.05, 0.95)
    if any(word in lowered for word in ("重要", "必须", "一定", "关键", "critical", "must")):
        importance = min(importance + 1.5, 10.0)

    summary = statement[:60]
    return LocalCandidate(
        memory_type=memory_type,
        content=statement,
        summary=summary,
        importance=round(importance, 2),
        confidence=round(max(confidence, 0.1), 2),
        matched_rules=hits,
    )


def extract_units_with_security(
    conversation: str, extra_redactions=()
) -> Tuple[List[ExtractionUnit], List[str]]:
    """003-B1: evaluate EVERY segment, including the ones with no memory value.

    The 003-B variant dropped low-value segments silently, which made honest
    per-unit accounting impossible. Here each segment produces one unit with
    its pre-redaction flags and its offsets in the original text.
    """
    from ...core.sensitive import CREDENTIAL_CATEGORIES, PII_CATEGORIES

    units: List[ExtractionUnit] = []
    categories = set()
    cursor = 0
    for index, statement in enumerate(segment_conversation(conversation)):
        start = conversation.find(statement, cursor)
        if start < 0:
            start = cursor
        end = start + len(statement)
        cursor = end
        hits = scan(statement)
        statement_flags = sorted({hit.category for hit in hits})
        if hits:
            categories.update(hit.category for hit in hits)
        safe_statement = redact(statement, extra_redactions)
        candidate = classify(safe_statement)
        if candidate is None and hits:
            # Sensitive statements must reach the security gate instead of
            # being silently discarded by the value classifier.
            candidate = LocalCandidate(
                memory_type="SEMANTIC",
                content=safe_statement,
                summary=None,
                importance=5.0,
                confidence=0.3,
                matched_rules=[],
            )
        if candidate is not None:
            candidate.unit_index = index
        units.append(
            ExtractionUnit(
                index=index,
                statement=statement,
                redacted=safe_statement,
                start=start,
                end=end,
                candidate=candidate,
                credential_flags=[
                    c for c in statement_flags if c in CREDENTIAL_CATEGORIES
                ],
                pii_flags=[c for c in statement_flags if c in PII_CATEGORIES],
            )
        )
    return units, sorted(categories)


def extract_locally_with_security(
    conversation: str, extra_redactions=()
) -> Tuple[List[LocalCandidate], List[str], List[List[str]]]:
    """003-B compatibility wrapper: candidate list + per-candidate flags."""
    units, categories = extract_units_with_security(conversation, extra_redactions)
    candidates: List[LocalCandidate] = []
    flags: List[List[str]] = []
    for unit in units:
        if unit.candidate is None:
            continue
        candidates.append(unit.candidate)
        flags.append(unit.credential_flags + unit.pii_flags)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return candidates, categories, flags


def extract_locally(conversation: str, extra_redactions=()) -> Tuple[List[LocalCandidate], List[str]]:
    """Run the deterministic strategy over a whole conversation.

    Kept for JME-1..JME-6 compatibility: the third return value of the
    security-aware variant is intentionally discarded here.
    """
    candidates, categories, _ = extract_locally_with_security(
        conversation, extra_redactions
    )
    return candidates, sorted(categories)
