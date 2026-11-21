"""Read-only retrieval for chat memory-context injection (JME-MEMORY-CONTEXT-01).

Why this exists
---------------
``/memories/hybrid-search`` is a general search surface. It returns the best
keyword/vector matches without judging whether they belong in a chat prompt.
Chat injection needs stricter rules:

* only ``ACTIVE`` rows, and never an expired row;
* never a credential, regex PII, or personally-sensitive memory;
* a real relevance verdict, so "今天杭州天气怎么样" cannot pull in a memory
  about a relationship or a salary;
* a bounded amount of content per row.

Everything here is a pure read: no INSERT/UPDATE/DELETE, no audit rows, no
access-time bookkeeping, no vector writes, no JMR interaction. The service
never constructs text from a model; every returned string is stored memory
content (possibly truncated).

Relevance model (deterministic, explainable)
--------------------------------------------
1. The query is reduced to *topic tokens*: CJK bigrams (unigrams for a
   one-character run) and ASCII words, after removing question/pronoun/verb
   filler. "我喜欢喝什么咖啡？" becomes the single bigram "咖啡".
2. A candidate's lexical coverage is ``matched topic tokens / all topic
   tokens``. A memory must reach ``min_match_ratio`` or contain the reduced
   query verbatim; otherwise it is dropped as irrelevant.
3. Rank = relevance * recency, where recency decays gently with age so an
   older-but-equally-relevant memory is not silently erased. The reported
   ``relevance_score`` stays purely relevance, so a client cannot mistake a
   decay factor for match strength.
4. Identity questions ("我是谁") are the one case where literal overlap cannot
   work; a fixed name/address expansion is searched and a PROFILE memory that
   actually states a name or salutation is accepted.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from ..core.personal_sensitivity import categories as personal_categories
from ..core.search import strip_query_fillers
from ..core.sensitive import credential_categories, pii_categories
from ..repositories.memory_repository import MemoryRepository
from ..schemas.context import (
    MemoryContextExcluded,
    MemoryContextItem,
    MemoryContextResponse,
)
from ..schemas.hybrid import HybridSearchResultItem
from .hybrid_search_service import HybridSearchService

logger = logging.getLogger("jme.memory_context")

#: Extra filler removed only for topic extraction. Kept narrower than "words
#: that are not nouns": a word is dropped only when it cannot identify a topic.
_TOPIC_FILLERS: Tuple[str, ...] = (
    "怎么样", "如何", "是什么", "有没有", "能不能", "可不可以", "是多少",
    "今天", "昨天", "明天", "现在", "最近", "目前", "刚才", "以后", "之前",
    "请问", "帮我", "告诉我", "你知道", "还记得", "我想知道", "我想问",
    "什么", "哪些", "哪个", "哪里", "怎么", "怎样", "为什么", "多少", "多久",
    "是不是", "是否", "可以", "一下", "一些",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯",
)

#: Verbs/pronouns are dropped when building the topic core, mirroring the
#: existing chat-side normalization. Longest entries first at removal time.
_TOPIC_VERBS: Tuple[str, ...] = (
    "我们", "你们", "他们", "她们", "它们",
    "我", "你", "他", "她", "它",
    "喜欢", "爱好", "爱", "想", "要", "会", "能", "喝", "吃", "用", "使用",
    "记得", "知道", "觉得", "认为", "希望", "需要", "打算", "准备",
    "的", "了", "是", "在", "有", "和", "就", "也", "都", "而", "及", "或",
    # 单字动词/否定："我开什么车" → "开车"、"我喝不喝酒" → "不酒"，
    # 不剥掉的词会变成不存在的主题词，永远匹配不到记忆。
    "不", "没", "没有", "开", "养", "抽", "玩", "穿",
    # 量词/领属/询问残余："我家猫叫什么" 归约后只剩 "家猫叫"，这三个字
    # 会把真正的主题字 "猫" 埋掉。这些词不可能是主题本身。
    "家", "只", "个", "位", "条", "张", "件", "次", "叫", "叫做",
    "名字", "什么名字", "养了", "养着",
)

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]{2,}")

#: Pronouns/filler that carry no topic even as bigrams, so the fallback token
#: set for an all-filler query does not match everything.
_TOPIC_STOP_BIGRAMS: FrozenSet[str] = frozenset(
    {
        "我是", "是我", "我叫", "叫什", "什么", "是谁", "我的", "们的",
        "你是", "你好", "有什", "么事", "可以", "帮我", "请问", "告诉",
    }
)

_IDENTITY_QUERY = re.compile(
    r"(?:我是谁|知道我是谁|记得我是谁|还记得我是谁|我叫什么|我叫啥|"
    r"我的名字(?:是什么|叫什么)?|我叫什么名字)"
)
_IDENTITY_EXPANSION = "姓名 名字 称呼"
_IDENTITY_MARKERS = ("姓名", "名字", "称呼", "叫我", "喊我")
_NAME_PATTERNS = (
    re.compile(r"我叫([^\s，,。；;：:]{1,20})"),
    re.compile(r"(?:我的)?名字(?:是|叫|为)\s*([^\s，,。；;：:]{1,20})"),
    re.compile(r"姓名(?:是|为)\s*([^\s，,。；;：:]{1,20})"),
    re.compile(r"称呼我(?:为|是)?\s*([^\s，,。；;：:]{1,20})"),
)

#: Recency shaping. A memory younger than _RECENT_DAYS keeps full weight;
#: afterwards the rank multiplier slides to _RECENCY_FLOOR at _AGING_DAYS.
_RECENT_DAYS = 30
_AGING_DAYS = 365
_RECENCY_FLOOR = 0.85

#: How much of a stored memory is considered when measuring relevance.
_MATCH_WINDOW_CHARS = 4000

#: A short CJK topic word may be split by a grammatical particle in the stored
#: sentence (query "养狗" vs memory "养了一只狗"). Both characters within this
#: distance counts as the same topic; anything further apart does not.
_CHAR_GAP_WINDOW = 4

#: Frames that make a single-character topic meaningful. "车" alone appears in
#: "卸车" (a logistics task), which is not the user's car; "有…车" / "买…车" is.
#: A one-character token therefore only counts when one of these words appears
#: shortly before it.
_SINGLE_CHAR_FRAMES: Tuple[str, ...] = (
    "有", "没有", "养", "养了", "喜欢", "爱", "喝", "吃", "抽", "玩",
    "穿", "开", "买", "住", "叫", "用", "看", "听", "喝", "抽",
)
_FRAME_WINDOW = 6

_SOURCE_BY_TYPE = {
    "REFLECTION": "reflection",
}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_expired(expires_at: Optional[str], now: datetime) -> bool:
    parsed = _parse_iso(expires_at)
    return parsed is not None and parsed <= now


def _reduce_topic(query: str) -> str:
    """Reduce a question to its topic words, or '' when nothing is left."""
    reduced = query
    for word in sorted(_TOPIC_FILLERS, key=len, reverse=True):
        reduced = reduced.replace(word, "")
    reduced = strip_query_fillers(reduced)
    for word in sorted(_TOPIC_VERBS, key=len, reverse=True):
        reduced = reduced.replace(word, "")
    # Punctuation would otherwise survive into the verbatim-match check.
    reduced = re.sub(r"[\s\u3000-\u303f\uff00-\uff65\u2010-\u2027!-/:-@\[-`{-~]+", " ", reduced)
    return reduced.strip()


def topic_tokens(text: str) -> List[str]:
    """Deterministic topic tokens: CJK bigrams (or unigrams) + ASCII words."""
    if not text:
        return []
    tokens: List[str] = []
    cursor = 0
    for match in _CJK_RUN.finditer(text):
        tokens.extend(
            word.lower() for word in _ASCII_WORD.findall(text[cursor:match.start()])
        )
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        cursor = match.end()
    tokens.extend(word.lower() for word in _ASCII_WORD.findall(text[cursor:]))
    return tokens


def _query_token_set(query: str) -> Set[str]:
    """Tokens that identify what the user is asking about.

    When the whole query is filler ("请问我喜欢喝什么？"), the reduced form is
    empty. Falling back to the raw bigrams minus the pronoun/question stop list
    is still strict: a memory must share 40% of them to pass the relevance
    gate, which a merely generic preference memory does not do.
    """
    reduced = _reduce_topic(query)
    tokens = {
        token
        for token in topic_tokens(reduced)
        if token not in _TOPIC_STOP_BIGRAMS
    }
    if tokens:
        return tokens
    return {
        token
        for token in topic_tokens(query)
        if token not in _TOPIC_STOP_BIGRAMS
    }


def _short_token_in_text(token: str, text: str) -> bool:
    """True when a 2-character CJK topic word appears, allowing one particle.

    Bigrams alone cannot bridge an inserted "了/的/一/只": query "养狗" would
    never match "养了一只狗". Requiring both characters within a 4-character
    window keeps this from matching unrelated text that merely shares one
    character.
    """
    first = token[0]
    second = token[1]
    start = 0
    while True:
        index = text.find(first, start)
        if index < 0:
            return False
        window = text[index + 1 : index + 1 + _CHAR_GAP_WINDOW]
        if second in window:
            return True
        start = index + 1


def _single_char_in_frame(char: str, text: str) -> bool:
    """True when a one-character topic appears in a meaningful frame.

    A bare substring test is too loose for a single character: "车" also occurs
    in "卸车" and "茶" in "茶色". Requiring a possession/preference word in the
    few characters before it keeps "用户有一辆车" and "用户喜欢喝茶" while
    dropping a compound that merely contains the character.
    """
    start = 0
    while True:
        index = text.find(char, start)
        if index < 0:
            return False
        window = text[max(0, index - _FRAME_WINDOW) : index]
        if any(frame in window for frame in _SINGLE_CHAR_FRAMES):
            return True
        start = index + 1


def lexical_match(query: str, content: str, summary: Optional[str] = None) -> float:
    """Fraction of the query's topic tokens present in the memory (0..1).

    A single-character topic ("茶", "猫", "狗", "酒", "车") must match by
    substring: the memory side only produces CJK bigrams, so a one-character
    query token could never intersect that token set. That made the most common
    preference questions score 0.0 and get dropped as irrelevant (audit B-1).
    Two-character topics additionally tolerate one grammatical particle.
    """
    tokens = _query_token_set(query)
    if not tokens:
        return 0.0
    reduced = _reduce_topic(query)
    # Bound the haystack: a stored memory may be very long, and chat retrieval
    # must stay cheap. A topic stated only past this window is out of scope.
    bounded = (content or "")[:_MATCH_WINDOW_CHARS]
    bounded_summary = (summary or "")[:_MATCH_WINDOW_CHARS]
    haystack = " ".join([bounded, bounded_summary])
    if reduced and len(reduced) >= 2 and reduced in bounded:
        return 1.0
    memory_tokens = set(topic_tokens(haystack))
    matched = 0
    for token in tokens:
        if token.isascii():
            if token in memory_tokens:
                matched += 1
            continue
        if len(token) == 1:
            # A real one-character topic: it must sit in a possession or
            # preference frame, not merely appear inside a compound word.
            if _single_char_in_frame(token, haystack):
                matched += 1
            continue
        if token in memory_tokens or _short_token_in_text(token, haystack):
            matched += 1
    return matched / len(tokens)


def _states_name(content: str) -> bool:
    if any(marker in content for marker in _IDENTITY_MARKERS):
        return True
    return any(pattern.search(content) for pattern in _NAME_PATTERNS)


def _metadata_is_safe(metadata_json: Optional[str]) -> bool:
    """Conservative metadata gate: any credential-shaped value excludes the row."""
    if not metadata_json:
        return True
    try:
        parsed = json.loads(metadata_json)
    except (ValueError, TypeError):
        return True

    def walk(value, depth: int = 0) -> bool:
        if depth > 6:
            return False
        if isinstance(value, str):
            return not credential_categories(value)
        if isinstance(value, dict):
            return all(walk(item, depth + 1) for item in value.values())
        if isinstance(value, list):
            return all(walk(item, depth + 1) for item in value)
        return True

    return walk(parsed)


def exclusion_reasons(content: str, summary: Optional[str]) -> List[str]:
    """Value-free reasons a stored memory must never enter a chat prompt."""
    reasons: List[str] = []
    if credential_categories(content) or credential_categories(summary or ""):
        reasons.append("sensitive")
    if pii_categories(content) or pii_categories(summary or ""):
        reasons.append("sensitive")
    if personal_categories(content) or personal_categories(summary or ""):
        reasons.append("sensitive")
    return sorted(set(reasons))


def recency_factor(updated_at: Optional[str], now: datetime) -> float:
    parsed = _parse_iso(updated_at)
    if parsed is None:
        return _RECENCY_FLOOR
    age_days = max((now - parsed).total_seconds() / 86400.0, 0.0)
    if age_days <= _RECENT_DAYS:
        return 1.0
    if age_days >= _AGING_DAYS:
        return _RECENCY_FLOOR
    span = _AGING_DAYS - _RECENT_DAYS
    return 1.0 - (1.0 - _RECENCY_FLOOR) * ((age_days - _RECENT_DAYS) / span)


@dataclass
class _Candidate:
    """One accepted memory plus the signals used for ranking."""

    record: object
    relevance: float
    fusion: float = 0.0


def _dedupe_key(content: str) -> str:
    return " ".join((content or "").lower().split())


class MemoryContextService:
    """Build the chat memory context from authoritative, readable memory rows."""

    def __init__(
        self,
        *,
        manager,
        hybrid_search: HybridSearchService,
        min_match_ratio: float = 0.4,
        candidate_limit: int = 40,
        scan_limit: int = 200,
    ) -> None:
        self.manager = manager
        self.hybrid_search = hybrid_search
        self.min_match_ratio = max(0.0, min(1.0, float(min_match_ratio)))
        self.candidate_limit = max(10, min(100, int(candidate_limit)))
        self.scan_limit = max(50, min(2000, int(scan_limit)))

    # ------------------------------------------------------------------ public
    def retrieve(
        self,
        *,
        query: str,
        limit: int,
        include_categories: Sequence[str] = (),
        exclude_categories: Sequence[str] = (),
        max_content_chars: int = 600,
    ) -> MemoryContextResponse:
        q = (query or "").strip()
        if not q:
            return MemoryContextResponse(
                items=[],
                total=0,
                excluded=MemoryContextExcluded(),
            )

        now = datetime.now(timezone.utc)
        runs: List[Tuple[List[HybridSearchResultItem], bool]] = []
        for variant in self._query_variants(q):
            try:
                items, meta = self.hybrid_search.search(
                    query=variant,
                    status="ACTIVE",
                    page=1,
                    page_size=self.candidate_limit,
                )
            except Exception as exc:  # noqa: BLE001 - degrade, never break chat
                logger.warning(
                    "memory context search failed for one variant: %s",
                    type(exc).__name__,
                )
                continue
            runs.append((list(items), bool(meta.degraded_to_fts)))

        degraded = all(flag for _items, flag in runs) if runs else True
        merged = self._merge(runs)
        # ``merged`` entries are (item, best_rank) tuples, not items.
        records = self._load_records([item.memory_id for item, _rank in merged])
        scores = {item.memory_id: float(item.score or 0.0) for item, _rank in merged}

        # Lexical fallback/supplement: FTS only sees rows whose ``search_text``
        # has been computed. Historically imported or directly seeded rows may
        # lack it, so a bounded scan of ACTIVE rows is merged in. The scan is
        # capped (``scan_limit``) and every row still has to pass the relevance
        # gate, so this can never dump the whole library into a prompt.
        for record in self._scan_active_records():
            if record.id not in records:
                records[record.id] = record
                scores.setdefault(record.id, 0.0)

        include = {value for value in include_categories}
        exclude = {value for value in exclude_categories}
        identity = bool(_IDENTITY_QUERY.search(q))

        excluded = MemoryContextExcluded()
        accepted: List[_Candidate] = []
        seen_keys: Set[str] = set()

        for memory_id, record in records.items():
            if getattr(record, "status", None) != "ACTIVE":
                excluded.filtered += 1
                continue
            if _is_expired(getattr(record, "expires_at", None), now):
                excluded.expired += 1
                continue
            content = getattr(record, "content", "") or ""
            summary = getattr(record, "summary", None)
            if not content.strip():
                excluded.filtered += 1
                continue
            memory_type = getattr(record, "memory_type", "SEMANTIC")
            if include and memory_type not in include:
                excluded.filtered += 1
                continue
            if memory_type in exclude:
                excluded.filtered += 1
                continue
            if exclusion_reasons(content, summary):
                excluded.sensitive += 1
                continue
            if not _metadata_is_safe(getattr(record, "metadata_json", None)):
                excluded.sensitive += 1
                continue

            coverage = lexical_match(q, content, summary)
            identity_hit = identity and memory_type == "PROFILE" and _states_name(content)
            if coverage < self.min_match_ratio and not identity_hit:
                excluded.irrelevant += 1
                continue

            relevance = coverage
            if identity_hit:
                relevance = max(relevance, 0.8)
            key = _dedupe_key(content)
            if key in seen_keys:
                excluded.duplicate += 1
                continue
            seen_keys.add(key)

            accepted.append(
                _Candidate(
                    record=record,
                    relevance=round(min(max(relevance, 0.0), 1.0), 4),
                    fusion=scores.get(memory_id, 0.0),
                )
            )

        accepted.sort(key=lambda entry: self._rank(entry, now), reverse=True)
        selected = accepted[: max(1, min(int(limit), 20))]
        items = [
            self._to_item(entry, max_content_chars=max_content_chars)
            for entry in selected
        ]
        return MemoryContextResponse(
            items=items,
            total=len(items),
            degraded_to_fts=degraded,
            excluded=excluded,
        )

    # ----------------------------------------------------------------- internal
    def _query_variants(self, query: str) -> List[str]:
        variants = [query]
        reduced = _reduce_topic(query)
        if len(reduced) >= 2 and reduced != query:
            variants.append(reduced)
        if _IDENTITY_QUERY.search(query):
            variants.append(_IDENTITY_EXPANSION)
        return variants

    def _merge(
        self,
        runs: Sequence[Tuple[List[HybridSearchResultItem], bool]],
    ) -> List[Tuple[HybridSearchResultItem, int]]:
        """Merge variant results, keeping the best fusion score per memory."""
        best: Dict[str, Tuple[HybridSearchResultItem, int]] = {}
        for items, _degraded in runs:
            for rank, item in enumerate(items):
                existing = best.get(item.memory_id)
                if existing is None or item.score > existing[0].score:
                    best[item.memory_id] = (item, rank)
        return sorted(best.values(), key=lambda entry: entry[0].score, reverse=True)

    def _load_records(self, ids: Sequence[str]):
        if not ids:
            return {}
        with self.manager.session() as session:
            records = MemoryRepository(session).get_by_ids(list(ids))
            return {record.id: record for record in records}

    def _scan_active_records(self):
        """ACTIVE rows that are invisible to FTS, i.e. no search_text yet.

        Bounded and targeted: rows written through the service always carry a
        ``search_text`` and are found by FTS. This exists so a legacy/imported
        row cannot become permanently unreachable to chat. The relevance gate
        still decides whether any of these rows is actually used.
        """
        with self.manager.session() as session:
            return list(
                MemoryRepository(session).list_active_missing_search_text(
                    limit=self.scan_limit
                )
            )

    def _rank(self, candidate: _Candidate, now: datetime) -> float:
        record = candidate.record
        recency = recency_factor(getattr(record, "updated_at", None), now)
        importance = min(max(float(getattr(record, "importance", 5.0) or 0.0) / 10.0, 0.0), 1.0)
        # Relevance dominates; recency, importance and the fusion score only
        # order equally relevant memories. None of them can lift an irrelevant
        # row over the match threshold, because that gate runs before this.
        return (
            candidate.relevance * recency * (0.9 + 0.1 * importance)
            + 0.05 * min(candidate.fusion, 1.0)
        )

    def _to_item(
        self, candidate: _Candidate, *, max_content_chars: int
    ) -> MemoryContextItem:
        record = candidate.record
        content = (getattr(record, "content", "") or "").strip()
        truncated = False
        if len(content) > max_content_chars:
            content = content[:max_content_chars].rstrip() + "…"
            truncated = True
        summary = getattr(record, "summary", None)
        if summary is not None:
            summary = summary.strip() or None
        memory_type = getattr(record, "memory_type", "SEMANTIC")
        return MemoryContextItem(
            memory_id=getattr(record, "id", ""),
            content=content,
            summary=summary,
            memory_type=memory_type,
            importance=float(getattr(record, "importance", 0.0) or 0.0),
            relevance_score=candidate.relevance,
            updated_at=str(getattr(record, "updated_at", "") or ""),
            source=_SOURCE_BY_TYPE.get(memory_type, "user_memory"),
            truncated=truncated,
        )


__all__ = [
    "MemoryContextService",
    "exclusion_reasons",
    "lexical_match",
    "recency_factor",
    "topic_tokens",
]
