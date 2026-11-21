"""Server-side AI enrichment for the Memory Overview.

Why server-side: the authoritative memories live in the JME SQLite database on
this host. Sending excerpts to a model from the phone means every device needs
its own provider key and every refresh costs the phone a round trip. Here the
overview is enriched once, next to the data, using the same ``JARVISMIND_LLM_*``
configuration the reflection engine already uses.

Safety rules are identical to the client-side organizer, enforced server-side:

* the model only ever sees excerpts that already passed the sensitivity gate;
* every returned ``memory_id`` must exist in the category it claims to describe;
* a summary may not contain a multi-digit number that is absent from its sources;
* over-long or empty summaries are dropped;
* any failure (no provider, timeout, bad JSON, rejected output) returns the
  deterministic payload unchanged — the endpoint never fails because of AI.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..config import Settings
from ..core.sensitive import redact
from .llm import LLMError, LLMProvider
from .llm.factory import build_provider
from .summary_fact_check import unsupported_reason

logger = logging.getLogger("jme.memory_overview.enricher")

MAX_SUMMARY_CHARS = 220
MAX_PROMPT_EXCERPT_CHARS = 120
MAX_PROMPT_ITEMS = 40

SYSTEM_PROMPT = (
    "你是记忆整理助手，任务是把用户已有的记忆摘录写成分类的连贯说明。\n"
    "硬性规则：\n"
    "1. 只能使用提供的摘录内容，不得补充任何用户没有表达过的个人事实。\n"
    "2. 不得出现摘录中没有的数字、日期、金额、姓名、地点或机构名称。\n"
    "3. 不得把推测写成确定结论；信息不足时省略，不要臆测。\n"
    "4. 只输出 JSON，不要解释、不要 Markdown 代码块。\n"
    '5. 结构：{"categories":[{"key":"分类键","summary":"说明","memory_ids":["引用的记忆ID"]}]}\n'
    "6. memory_ids 只能取自该分类给出的 id。\n"
    "7. 不得提升职级或职务（如总监、经理、主管），除非摘录中已明确写出。\n"
    "8. 不得使用中文数字表示数量或时间（如「五年前」「三万」）。\n"
    "9. 不得预测未来（如「一定会」「未来将」「必将」）。\n"
)


def _digits(text: str) -> List[str]:
    """Multi-digit runs; single digits are too noisy to police."""
    out: List[str] = []
    current = ""
    for char in text:
        if char.isdigit():
            current += char
        else:
            if len(current) >= 2:
                out.append(current)
            current = ""
    if len(current) >= 2:
        out.append(current)
    return out


def has_novel_numbers(summary: str, sources: Iterable[str]) -> bool:
    allowed = set()
    for source in sources:
        allowed.update(_digits(source or ""))
    return any(token not in allowed for token in _digits(summary))


def _excerpt(text: str) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= MAX_PROMPT_EXCERPT_CHARS:
        return clean
    return clean[:MAX_PROMPT_EXCERPT_CHARS].rstrip() + "…"


def build_prompt(payload: Dict[str, Any]) -> str:
    categories = []
    for category in payload.get("categories", []):
        items = category.get("items") or []
        if not items:
            continue
        categories.append(
            {
                "key": category.get("key"),
                "title": category.get("title"),
                "item_count": category.get("item_count", len(items)),
                "memories": [
                    {
                        "memory_id": item.get("memory_id"),
                        "excerpt": _excerpt(str(item.get("excerpt", ""))),
                    }
                    for item in items[:MAX_PROMPT_ITEMS]
                ],
            }
        )
    return (
        "以下是用户已确认的记忆摘录，按分类给出。请为每个分类写一段简洁的中文说明。\n"
        "只使用下面出现的信息。不要新增事实。不要输出摘录里没有的数字。\n\n"
        + json.dumps({"categories": categories}, ensure_ascii=False, indent=2)
    )


def parse_output(text: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the model reply, tolerating code fences and surrounding prose."""
    if not text or not text.strip():
        return None
    body = text.strip()
    fenced = body.find("```")
    if fenced >= 0:
        inner_start = body.find("\n", fenced)
        inner_end = body.rfind("```")
        if inner_start >= 0 and inner_end > inner_start:
            body = body[inner_start + 1 : inner_end].strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    rows = parsed.get("categories")
    if not isinstance(rows, list) or not rows:
        return None
    result: List[Dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            return None
        key = entry.get("key") or entry.get("category")
        summary = entry.get("summary") or entry.get("text") or entry.get("content")
        if not isinstance(key, str) or not isinstance(summary, str):
            return None
        ids = entry.get("memory_ids") or entry.get("memoryIds") or []
        if not isinstance(ids, list):
            ids = []
        result.append(
            {
                "key": key,
                "summary": summary,
                "memory_ids": [item for item in ids if isinstance(item, str)],
            }
        )
    return result


def validate_row(row: Dict[str, Any], category: Dict[str, Any]) -> Optional[str]:
    """Return the accepted summary, or ``None`` when the row must be dropped.

    A summary survives only when it is traceable (every cited id belongs to the
    category), short enough, free of invented quantities, free of undeserved
    seniority claims, and free of predictions.
    """
    summary = (row.get("summary") or "").strip()
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        return None
    allowed_ids = {
        str(item.get("memory_id")) for item in (category.get("items") or [])
    }
    cited = row.get("memory_ids") or []
    if not cited or any(str(item) not in allowed_ids for item in cited):
        return None
    sources = [str(item.get("excerpt", "")) for item in (category.get("items") or [])]
    sources.append(str(category.get("title", "")))
    if has_novel_numbers(summary, sources):
        return None
    # R2/A2: Chinese numerals, promotion claims and predictions.
    if unsupported_reason(summary, sources) is not None:
        return None
    return summary


def build_overview_enricher(
    settings: Settings,
    *,
    provider: Optional[LLMProvider] = None,
    logger_: Optional[logging.Logger] = None,
) -> Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]:
    """Return an enricher, or ``None`` when no model is configured.

    ``None`` means the endpoint serves the deterministic overview — it never
    pretends an AI pass happened.
    """
    active = provider if provider is not None else build_provider(settings)
    if active is None:
        return None

    log = logger_ or logger
    # Never let a live secret reach the provider in the prompt text.
    tokens: Sequence[str] = tuple(
        token
        for token in (settings.api_token, settings.llm_api_key)
        if token
    )

    def enrich(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not payload.get("categories"):
            return payload

        prompt = redact(build_prompt(payload), extra_tokens=tokens)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            result = active.complete(
                messages,
                timeout=float(getattr(settings, "llm_timeout", 30.0) or 30.0),
                max_tokens=1200,
                temperature=0.2,
            )
        except LLMError as exc:
            # The message is built by the provider and never contains memory
            # text, so it is safe to log and useful when a model returns nothing.
            log.warning("memory overview enrichment skipped: %s", exc)
            return payload
        except Exception as exc:  # noqa: BLE001 - AI must never break the endpoint
            log.warning("memory overview enrichment failed: %s", type(exc).__name__)
            return payload

        rows = parse_output(result.text)
        if rows is None:
            log.info("memory overview enrichment produced no usable JSON")
            return payload

        by_key = {row["key"]: row for row in rows}
        accepted = 0
        categories = []
        for category in payload["categories"]:
            raw = by_key.get(category.get("key"))
            if raw is None:
                categories.append(category)
                continue
            summary = validate_row(raw, category)
            if summary is None:
                categories.append(category)
                continue
            accepted += 1
            categories.append({**category, "ai_summary": summary})

        if accepted == 0:
            log.info("memory overview enrichment rejected every category")
            return payload

        log.info(
            "memory overview enriched %d/%d categories model=%s",
            accepted,
            len(categories),
            getattr(result, "model", "") or getattr(active, "name", ""),
        )
        return {
            **payload,
            "categories": categories,
            "ai_enhanced": True,
            "ai_model": getattr(result, "model", "") or getattr(active, "name", ""),
        }

    return enrich


__all__ = [
    "build_overview_enricher",
    "build_prompt",
    "has_novel_numbers",
    "parse_output",
    "validate_row",
]
