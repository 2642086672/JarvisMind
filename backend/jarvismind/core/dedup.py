"""Duplicate and conflict detection for extracted memories.

The rules are deliberately explainable: two memories are duplicates when their
normalised token sets are nearly identical, and they are a potential conflict
when they talk about the same subject (high subject overlap) but disagree (low
overall similarity). Thresholds are module-level constants so they can be
tuned or replaced without touching callers.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set, Tuple

from .search import segment_for_index

DUPLICATE_THRESHOLD = 0.82
SUBSUMPTION_THRESHOLD = 0.65
CONFLICT_MIN_SUBJECT_OVERLAP = 0.60
CONFLICT_MAX_SIMILARITY = 0.80
CONFLICT_MIN_TOKENS = 3

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "these", "those",
    "it", "we", "you", "they", "i", "my", "our", "your", "their",
    "它", "他", "她", "我们", "你们", "他们", "我的", "你的", "他的", "她的",
    "的", "了", "和", "与", "在", "是", "有", "就", "也", "都", "而", "及", "或",
    "一个", "一种", "可以", "应该", "需要", "进行", "实现", "支持", "目前",
    "当前", "以后", "未来", "新的", "项目", "使用", "计划", "工作", "开发",
    "数据", "系统", "内容", "用户", "现在", "已经", "开始", "完成", "处理",
}


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    lowered = (text or "").lower()
    cleaned = re.sub(r"[^\w一-鿿]+", " ", lowered)
    return " ".join(cleaned.split())


def token_set(text: str) -> Set[str]:
    """Return the search-oriented token set for a piece of text."""
    return set(segment_for_index(text).split())


def _jaccard(left: Set[str], right: Set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _subject_tokens(text: str) -> Set[str]:
    """Salient tokens only: drop stopwords and single ASCII characters.

    CJK unigrams are dropped too: a single Chinese character is too generic to
    identify a subject, while bigrams and ASCII words are distinctive.
    """
    subjects = set()
    for token in token_set(text):
        if token in _STOPWORDS or len(token) < 2:
            continue
        if re.match(r"^[A-Za-z0-9]+$", token):
            subjects.add(token)
        elif len(token) >= 2 and not re.match(r"^[一-鿿]$", token):
            subjects.add(token)
    return subjects


def similarity(left: str, right: str) -> float:
    return _jaccard(token_set(left), token_set(right))


def is_duplicate(new_content: str, existing_content: str) -> bool:
    """True when two memories carry essentially the same information."""
    if not new_content or not existing_content:
        return False
    if normalize(new_content) == normalize(existing_content):
        return True
    left = normalize(new_content)
    right = normalize(existing_content)
    if left and right and (left in right or right in left):
        return True

    new_tokens = token_set(new_content)
    old_tokens = token_set(existing_content)
    if not new_tokens or not old_tokens:
        return False
    if similarity(new_content, existing_content) >= DUPLICATE_THRESHOLD:
        return True
    # Subsumption: a short memory whose tokens are almost all contained in the
    # new one is a duplicate even when the new one carries extra detail.
    shared = len(new_tokens & old_tokens)
    smaller = min(len(new_tokens), len(old_tokens))
    return smaller >= 4 and shared / smaller >= SUBSUMPTION_THRESHOLD


def is_conflict(new_content: str, existing_content: str) -> bool:
    """True when two memories discuss the same subject but disagree.

    A future plan versus a current state is a conflict candidate, not a
    duplicate: both should be kept, with the relationship recorded. The subject
    is identified by shared salient tokens (bigrams or ASCII words); at least
    two must match, or one distinctive ASCII identifier such as a product name.
    """
    if not new_content or not existing_content:
        return False
    subjects_new = _subject_tokens(new_content)
    subjects_old = _subject_tokens(existing_content)
    shared = subjects_new & subjects_old
    if not shared:
        return False
    distinctive_ascii = {token for token in shared if token.isascii() and len(token) >= 3}
    if len(shared) < 2 and len(distinctive_ascii) < 1:
        return False
    if len(subjects_new) < CONFLICT_MIN_TOKENS and len(distinctive_ascii) < 1:
        return False
    return similarity(new_content, existing_content) <= CONFLICT_MAX_SIMILARITY


def find_matches(
    new_content: str, existing: Sequence[Tuple[str, str]]
) -> Tuple[List[str], List[str]]:
    """Return (duplicate_ids, conflict_ids) for one candidate."""
    duplicates: List[str] = []
    conflicts: List[str] = []
    for record_id, content in existing:
        if is_duplicate(new_content, content):
            duplicates.append(record_id)
        elif is_conflict(new_content, content):
            conflicts.append(record_id)
    return duplicates, conflicts
