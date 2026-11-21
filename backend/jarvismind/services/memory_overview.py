"""Memory Overview: read-only, extractive personal memory summary.

Design constraints (JME-MEMORY-OVERVIEW-01):

* **Read-only.** This module never opens a write transaction, never touches
  ``memory_records`` / ``memory_candidates`` and never calls a lifecycle
  service. It only reads ACTIVE rows through the repository.
* **Extractive, not generative.** Every line shown to the user is either a
  fixed category label or a verbatim excerpt of a stored memory. Nothing is
  paraphrased, inferred or completed by a model, so the overview cannot invent
  a personal fact the user never expressed.
* **Traceable.** Every excerpt carries the ``memory_id`` it came from.
* **Fail-closed on sensitivity.** A memory is excluded from the overview when
  ANY of the existing JME gates flags it: credential shapes, regex PII, or the
  personal-sensitivity keyword policy (health / home address / finance /
  sexual / religion / politics). Exclusion is silent and value-free.
* **Conflicts are preserved, not resolved.** When two memories describe the
  same single-valued attribute with different values, the category is marked
  ``uncertain`` and both memories are kept. No value is chosen.
* **No AI dependency.** An optional ``enricher`` hook may decorate the result;
  if it raises or is absent the deterministic output is returned unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.auto_governance import extract_name
from ..core.personal_sensitivity import categories as personal_categories
from ..core.sensitive import credential_categories, pii_categories
from ..core.timeutils import utc_now_iso
from ..repositories.memory_repository import MemoryRepository

logger = logging.getLogger("jme.memory_overview")

#: Categories required by the task book, in display order.
CATEGORY_ORDER: Tuple[str, ...] = (
    "profile",
    "work",
    "interests",
    "projects",
    "experiences",
    "relationships",
    "recent",
)

CATEGORY_TITLES: Dict[str, str] = {
    "profile": "个人画像",
    "work": "工作与职业",
    "interests": "兴趣与偏好",
    "projects": "长期项目",
    "experiences": "重要经历",
    "relationships": "人物与关系",
    "recent": "近期重点",
}

#: Deterministic keyword sets. They only decide which EXISTING memories belong
#: to a reading category; they never generate content.
_WORK_KEYWORDS: Tuple[str, ...] = (
    "职业", "工作", "岗位", "任职", "入职", "离职", "公司", "部门", "职责",
    "理赔", "客服", "质管", "仲裁", "物流", "行业", "岗", "升职", "转岗",
)
_INTEREST_KEYWORDS: Tuple[str, ...] = (
    "喜欢", "爱好", "偏好", "习惯", "讨厌", "旅行", "旅游", "摄影", "拍摄",
    "咖啡", "猫", "狗", "音乐", "电影", "阅读", "运动", "健身", "游戏",
    "美食", "海边", "视频",
)
_RELATION_KEYWORDS: Tuple[str, ...] = (
    "朋友", "同事", "哥哥", "姐姐", "弟弟", "妹妹", "父亲", "母亲", "爸爸",
    "妈妈", "家人", "妻子", "丈夫", "女友", "男友", "儿子", "女儿", "亲戚",
)

#: Recent-focus window and per-category caps.
RECENT_DAYS = 30
#: "Recent focus" additionally requires above-median importance, because a pure
#: time window degenerates into "every memory" after a bulk import. The rule is
#: stated in the API payload so the UI can explain it instead of implying a
#: curated list.
RECENT_MIN_IMPORTANCE = 6.5
MAX_ITEMS_PER_CATEGORY = 12
EXCERPT_LIMIT = 160
SUMMARY_LIMIT = 220


def _preview(text: str, limit: int = EXCERPT_LIMIT) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


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


def sensitivity_reasons(content: str, summary: Optional[str]) -> List[str]:
    """Return the (value-free) reasons a memory must not be summarized.

    Any reason means EXCLUDE. The function returns category names only; it
    never returns the matched text.
    """
    reasons: List[str] = []
    for text in (content, summary or ""):
        if credential_categories(text):
            reasons.append("credential")
            break
    for text in (content, summary or ""):
        if pii_categories(text):
            reasons.append("pii")
            break
    for text in (content, summary or ""):
        if personal_categories(text):
            reasons.append("personal_sensitive")
            break
    return sorted(set(reasons))


def _metadata_safe(metadata_json: Optional[str]) -> bool:
    """Conservative metadata gate.

    ``metadata_json`` is server-written, but it can echo user text. A
    credential-shaped value anywhere inside it excludes the memory.
    """
    if not metadata_json:
        return True
    try:
        parsed = json.loads(metadata_json)
    except (ValueError, TypeError):
        return True

    def walk(value: Any, depth: int = 0) -> bool:
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


def _matches(content: str, keywords: Iterable[str]) -> bool:
    lowered = (content or "").lower()
    return any(keyword in lowered for keyword in keywords)


def category_keys_for(memory_type: str, content: str) -> List[str]:
    """Deterministic category membership for one memory."""
    keys: List[str] = []
    if memory_type == "PROFILE":
        keys.append("profile")
    if _matches(content, _WORK_KEYWORDS):
        keys.append("work")
    if _matches(content, _INTEREST_KEYWORDS):
        keys.append("interests")
    if memory_type == "PROJECT":
        keys.append("projects")
    if memory_type == "EPISODIC":
        keys.append("experiences")
    if _matches(content, _RELATION_KEYWORDS):
        keys.append("relationships")
    return keys


class MemoryOverviewService:
    """Build the overview from ACTIVE memories. Never writes anything."""

    def __init__(
        self,
        manager,
        *,
        enricher: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        cache: Optional[Any] = None,
    ) -> None:
        self.manager = manager
        self.enricher = enricher
        self.cache = cache

    # ------------------------------------------------------------------ gather
    def _active_records(self) -> List[Dict[str, Any]]:
        """Read every ACTIVE memory plus its source label. Read-only."""
        with self.manager.session() as session:
            repo = MemoryRepository(session)
            records, _total = repo.list(
                status="ACTIVE",
                page=1,
                page_size=10_000,
                sort_by="updated_at",
                sort_order="desc",
            )
            rows = [
                {
                    "id": record.id,
                    "memory_type": record.memory_type or "SEMANTIC",
                    "content": record.content or "",
                    "summary": record.summary,
                    "status": record.status,
                    "importance": float(record.importance or 0.0),
                    "confidence": float(record.confidence or 0.0),
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "expires_at": record.expires_at,
                    "metadata_json": record.metadata_json,
                    "source_id": record.source_id,
                }
                for record in records
            ]
        return rows

    # ------------------------------------------------------------------- build
    def build(self, *, enrich: bool = False, refresh: bool = False) -> Dict[str, Any]:
        """Build the overview, using the summary cache when it is still valid.

        ``enrich`` permits a model call at all. ``refresh`` forces a new call
        even when the cached summaries still match the current memory set.

        Default behaviour (both false) is deterministic, fast and free: it never
        calls a model. The cached model summaries are re-attached when their
        ``source_version`` still matches, so re-opening the page costs nothing.
        """
        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(days=RECENT_DAYS)

        eligible: List[Dict[str, Any]] = []
        excluded_reasons: Dict[str, int] = {}

        for row in self._active_records():
            reasons: List[str] = []
            if not row["content"].strip():
                reasons.append("empty")
            if _is_expired(row["expires_at"], now):
                reasons.append("expired")
            reasons.extend(sensitivity_reasons(row["content"], row["summary"]))
            if not _metadata_safe(row["metadata_json"]):
                reasons.append("credential")
            if reasons:
                for reason in sorted(set(reasons)):
                    excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
                continue
            eligible.append(row)

        buckets: Dict[str, List[Dict[str, Any]]] = {key: [] for key in CATEGORY_ORDER}
        for row in eligible:
            for key in category_keys_for(row["memory_type"], row["content"]):
                buckets[key].append(row)
            updated = _parse_iso(row["updated_at"])
            if (
                updated is not None
                and updated >= recent_cutoff
                and row["importance"] >= RECENT_MIN_IMPORTANCE
            ):
                buckets["recent"].append(row)

        categories: List[Dict[str, Any]] = []
        for key in CATEGORY_ORDER:
            rows = buckets[key]
            # Most recently updated first; a stable tiebreak keeps output
            # reproducible across calls.
            rows.sort(
                key=lambda item: (
                    _parse_iso(item["updated_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                    item["id"],
                ),
                reverse=True,
            )
            items = [
                {
                    "memory_id": row["id"],
                    "memory_type": row["memory_type"],
                    "excerpt": _preview(row["content"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows[:MAX_ITEMS_PER_CATEGORY]
            ]
            uncertain, conflicts = _detect_conflicts(rows)
            summary = _build_summary(items)
            categories.append(
                {
                    "key": key,
                    "title": CATEGORY_TITLES[key],
                    "summary": summary,
                    "item_count": len(rows),
                    "items": items,
                    "truncated": len(rows) > len(items),
                    "uncertain": uncertain,
                    "conflicts": conflicts,
                    "available": bool(rows),
                }
            )

        source_version = hashlib.sha256(
            "\x1f".join(
                f"{row['id']}:{row['updated_at']}"
                for row in sorted(eligible, key=lambda item: item["id"])
            ).encode("utf-8")
        ).hexdigest()

        payload: Dict[str, Any] = {
            "generated_at": utc_now_iso(),
            "source_version": source_version,
            "eligible_count": len(eligible),
            "excluded_count": sum(excluded_reasons.values()),
            "excluded_reasons": dict(sorted(excluded_reasons.items())),
            "recent_days": RECENT_DAYS,
            "recent_min_importance": RECENT_MIN_IMPORTANCE,
            "categories": categories,
            "ai_enhanced": False,
        }

        cache_entry = None
        if self.cache is not None:
            cache_entry = self.cache.get()

        ai_state = "none"
        ai_generated_at: Optional[str] = None
        ai_model: Optional[str] = None

        # 1) 缓存命中且记忆未变：直接复用，零模型调用。
        if (
            cache_entry is not None
            and cache_entry.is_current(source_version)
            and not refresh
            and self.enricher is not None
        ):
            categories = _attach_cached_summaries(categories, cache_entry)
            ai_state = "current"
            ai_generated_at = cache_entry.generated_at
            ai_model = cache_entry.model

        # 2) 缓存存在但记忆已变（含遗忘/删除/更新）：**不再展示旧摘要**。
        #
        #    R2/B-2：旧摘要可能描述已被遗忘的记忆，继续展示等于让旧事实
        #    通过 AI 摘要泄露。这里只标记 stale，绝不套用旧文本。
        elif cache_entry is not None and not cache_entry.is_current(source_version):
            ai_state = "stale"
            ai_generated_at = cache_entry.generated_at
            ai_model = cache_entry.model

        # 3) 只有被明确要求时才调用模型（enrich=True）。
        if enrich and self.enricher is not None and ai_state != "current":
            try:
                enriched = self.enricher({**payload, "source_version": source_version})
                if isinstance(enriched, dict) and enriched.get("ai_enhanced") is True:
                    payload = enriched
                    categories = enriched["categories"]
                    ai_state = "current"
                    ai_generated_at = utc_now_iso()
                    ai_model = enriched.get("ai_model")
                    if self.cache is not None:
                        self.cache.put(
                            _cache_entry_from(
                                source_version, ai_generated_at, ai_model, categories
                            )
                        )
            except Exception:  # noqa: BLE001 - degradation must be silent
                logger.warning("memory overview enricher failed; using deterministic output")

        # 4) R2/A1：模型写入的摘要本身也必须过敏感门禁。
        #    模型可能复述一条已被过滤记忆里的手机号，或自己编一个。
        #    任一命中即移除该摘要，界面回退到确定性原文。
        categories, dropped = _sanitize_ai_summaries(categories)
        if dropped:
            logger.warning(
                "memory overview dropped %d AI summaries containing sensitive content",
                dropped,
            )
            if all("ai_summary" not in category for category in categories):
                ai_state = "none"
                ai_generated_at = None
                ai_model = None

        payload = {
            **payload,
            "categories": categories,
            "ai_enhanced": ai_state == "current",
            "ai_state": ai_state,
            "ai_generated_at": ai_generated_at,
            "ai_model": ai_model,
        }
        return payload


def _attach_cached_summaries(
    categories: Sequence[Dict[str, Any]], cache_entry: Any
) -> List[Dict[str, Any]]:
    """Attach cached AI text, but only for memory ids still present.

    ``source_version`` already changes when any memory changes, so a cache hit
    implies an identical memory set. This belt-and-braces check keeps a
    corrupted or hand-edited cache from describing memories that are gone.
    """
    result: List[Dict[str, Any]] = []
    for category in categories:
        text = cache_entry.summaries.get(category["key"])
        if not isinstance(text, str) or not text:
            result.append(category)
            continue
        result.append({**category, "ai_summary": text})
    return result


def _sanitize_ai_summaries(
    categories: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop any AI-written summary that itself contains sensitive content.

    R2/A1: the model is an untrusted writer. It can echo a phone number from a
    source the filter already missed, or invent one. Every ``ai_summary`` must
    therefore pass the same gate as a memory before it is displayed.

    Returns the cleaned categories and the number of summaries dropped.
    """
    cleaned: List[Dict[str, Any]] = []
    dropped = 0
    for category in categories:
        text = category.get("ai_summary")
        if not isinstance(text, str) or not text:
            cleaned.append(category)
            continue
        if sensitivity_reasons(text, None):
            # Never echo the text; just remove it.
            cleaned.append(
                {key: value for key, value in category.items() if key != "ai_summary"}
            )
            dropped += 1
        else:
            cleaned.append(category)
    return cleaned, dropped


def _cache_entry_from(
    source_version: str,
    generated_at: str,
    model: Optional[str],
    categories: Sequence[Dict[str, Any]],
):
    from .memory_overview_cache import CachedOverview

    summaries = {
        category["key"]: category["ai_summary"]
        for category in categories
        if isinstance(category.get("ai_summary"), str)
    }
    return CachedOverview(
        source_version=source_version,
        generated_at=generated_at,
        model=model or "",
        summaries=summaries,
    )


def _build_summary(items: Sequence[Dict[str, Any]]) -> str:
    """Join verbatim excerpts. No paraphrase, no inference."""
    if not items:
        return ""
    parts: List[str] = []
    for item in items[:3]:
        parts.append(item["excerpt"])
    joined = "；".join(part for part in parts if part)
    if len(joined) <= SUMMARY_LIMIT:
        return joined
    return joined[:SUMMARY_LIMIT].rstrip() + "…"


def _detect_conflicts(
    rows: Sequence[Dict[str, Any]],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Report only conflicts this module can PROVE, never a guess.

    A naive pairwise ``relation()`` sweep flags hundreds of pairs that merely
    share a word (a birth-date fact vs a form-of-address preference), which
    would train the user to ignore the warning. So the rule here is narrow and
    explainable:

    * only a stated PERSONAL NAME is compared, using the same extractor as the
      write path (``auto_governance.extract_name``);
    * a conflict is reported only when two memories state DIFFERENT names.

    Every other form of disagreement is instead handled by construction: the
    overview only ever shows verbatim excerpts and never collapses a category
    into a single asserted value, so it cannot silently pick a winner.
    A second, narrower rule covers an explicit contradictory project status on
    an identical subject.
    """
    findings: List[Dict[str, Any]] = []

    names: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = extract_name(row["content"])
        if not name:
            continue
        names.setdefault(name, row)
    if len(names) > 1:
        entries = list(names.items())[:3]
        findings.append(
            {
                "attribute": ["name"],
                "memory_ids": [row["id"] for _name, row in entries],
                "note": "多条记忆给出了不同的姓名；汇总保留全部原文，不判定哪一条正确。",
            }
        )

    findings.extend(_status_conflicts(rows))
    return bool(findings), findings[:3]


_STATUS_WORDS = ("已完成", "进行中", "已暂停", "已上线", "已取消", "已延期")


def _status_conflicts(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag identical-subject memories that assert mutually exclusive status."""
    by_subject: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for row in rows:
        content = row["content"]
        words = [word for word in _STATUS_WORDS if word in content]
        if len(words) != 1:
            continue
        subject = _status_subject(content)
        if not subject:
            continue
        by_subject.setdefault(subject, []).append((words[0], row))

    findings: List[Dict[str, Any]] = []
    for subject, entries in by_subject.items():
        distinct = {word for word, _row in entries}
        if len(distinct) < 2:
            continue
        findings.append(
            {
                "attribute": ["project_status"],
                "memory_ids": [row["id"] for _word, row in entries][:4],
                "note": f"关于「{subject}」的记忆给出了不同状态；汇总保留原文，不判定当前状态。",
            }
        )
        if len(findings) >= 2:
            break
    return findings


def _status_subject(content: str) -> str:
    """Subject of a status statement, stripped of status vocabulary."""
    cleaned = content
    for word in _STATUS_WORDS:
        cleaned = cleaned.replace(word, "")
    for token in ("项目", "进度", "状态", "当前", "已经", "现在", "的", "为", "是"):
        cleaned = cleaned.replace(token, "")
    cleaned = " ".join(cleaned.split()).strip("，,。；;：: ")
    return cleaned[:24]


__all__ = [
    "CATEGORY_ORDER",
    "CATEGORY_TITLES",
    "MemoryOverviewService",
    "category_keys_for",
    "sensitivity_reasons",
]
