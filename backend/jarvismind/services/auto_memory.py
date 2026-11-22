"""JEA-01 / JME-AUTO-GOVERNANCE-01: automatic memory governance.

The service is the single entry point for turning one user message into a
governed memory decision:

    fact filter -> credential gate -> sensitivity policy -> relation to
    existing memories -> write / update / merge / hold / deny

Hard rules that shape the implementation:

* ``memory_records`` is never written with raw SQL. Every authoritative write
  goes through ``ApprovalService`` (new fact), ``LifecycleService.supersede``
  (replaces a value) or ``LifecycleService.auto_merge_duplicates`` (merge).
* A credential is DENIED before a candidate row exists.
* A sensitive fact becomes a ``PENDING_AUTHORIZATION`` candidate; it is never
  auto-written.
* A value difference without an explicit update marker is CONFLICT: the old,
  possibly-correct memory is not overwritten.
* Nothing is reported as saved unless the authoritative row really exists.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text

from ..core.auto_governance import (
    Relation,
    best_match,
    is_fact_like,
    mergeable_group,
    relation,
    scored_matches,
)
from ..core.dedup import find_matches
from ..core.fingerprint import content_fingerprint
from ..core.personal_sensitivity import categories as personal_categories
from ..core.security_gate import (
    RULESET_VERSION,
    assert_writable,
    classify_candidate,
    required_grants_for,
)
from ..core.sensitive import redact
from ..core.timeutils import utc_now_iso
from ..database.models import MemoryCandidate
from ..repositories.candidate_repository import CandidateRepository
from ..repositories.memory_repository import MemoryRepository
from ..schemas.extraction import AutoRememberResponse, CandidateStatus
from ..schemas.memory import MemoryType
from .approval_service import ApprovalService
from .lifecycle_service import LifecycleService
from .memory_service import MemoryService

_ACTOR = "auto:non-sensitive"
_MAX_FACT_LENGTH = 4000
_MAX_GOVERNED_PAIRS = 400

# Explicit memory requests. These are phrases, not a single magic keyword:
# each pattern can appear as a prefix ("记住，我...") or around a clause
# ("我叫..., 记住我的名字").
_REQUEST_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:请|帮我|麻烦)?记住(?:一下|住)?(?:我的)?(?:名字|信息)?[：:，, ]*"),
    re.compile(r"(?:请|帮我)?保存到(?:我的)?(?:长期)?记忆[：:，, ]*"),
    re.compile(r"以后(?:都)?记得[：:，, ]*"),
    re.compile(r"以后(?:都)?(?:按照|按)这个习惯[：:，, ]*"),
    re.compile(r"更新(?:一下)?我的个人信息[：:，, ]*"),
    re.compile(r"(?:把|将)(?:这个|这条|它)?(?:加入|存入|保存到)(?:我的)?(?:长期)?记忆[：:，, ]*"),
)

_NEGATION = re.compile(r"(?:不要|别|不用|无需)(?:再)?(?:记住|保存|记)")

#: Dated career facts ("我 2022 年起在省区客服质管部任品效改善岗") are explicitly
#: in scope for长期记忆. They never start with one of the short prefixes above,
#: so they get their own conservative pattern: a year + an employment verb.
_CAREER_PATTERN = re.compile(
    r"^我\s*(?:19|20)\d{2}\s*年?.*(?:任|担任|入职|加入|从事|负责|工作|任职)"
)

# Strong first-person profile patterns. They are ONLY used when the user did
# not explicitly say "remember"; this keeps ordinary chat out of the auto path.
_PROFILE_PREFIXES = (
    "我叫", "我的名字", "我的职业", "我从事", "我的工作", "我使用", "我用",
    "我长期使用", "我平时使用", "我的爱好", "我喜欢", "我习惯", "我通常",
    "我不喜欢", "我住", "我住在", "我在", "我从事",
    "以后我", "我现在", "我最近", "我目前", "我更喜欢", "我不太",
    "我的项目", "我平时", "我主要", "我已经", "我已经不",
)

#: Sentences that are questions or instructions ABOUT memory rather than a
#: statement of fact. They must not become memories.
_NON_FACT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:你|助手|贾维斯)?(?:还记得|记得|知道|能记住)"),
    re.compile(r"(?:记住|保存)(?:了)?(?:吗|么|没有)"),
)

#: Pure social noise. A message whose *entire* content is one of these carries
#: no fact about the user, so it must never become a memory in either mode.
#:
#: The check is deliberately whole-string and small on purpose: a substring rule
#: would reject "谢谢，另外我叫年糕" (a real fact) and a broad "anything short is
#: noise" rule would reject short but real facts such as "我叫年糕". "嗯" and
#: "好" are already caught by the two-character minimum; these are the ones that
#: are two characters or longer and still say nothing.
_SOCIAL_ONLY_PHRASES = frozenset({
    "谢谢", "多谢", "感谢", "谢谢你", "多谢了", "感谢你",
    "好的", "好", "嗯嗯", "是的", "对的", "收到", "明白了", "了解",
    "辛苦了", "辛苦了谢谢", "不错", "很好", "太棒了",
    "ok", "okay", "thanks", "thank you", "thx", "got it", "sure",
})

#: Trailing punctuation and whitespace that carries no meaning of its own.
_TRAILING_PUNCT = "。！？~～.!,? \t\n\r，,、"


def is_social_only(text: str) -> bool:
    """True when the whole message is a pleasantry rather than a statement.

    Only the complete, normalised message is compared: any extra clause makes
    this false, so "谢谢，我叫年糕" is still a legitimate memory.
    """
    normalized = " ".join((text or "").split()).strip().strip(_TRAILING_PUNCT).lower()
    if not normalized:
        return True
    return normalized in _SOCIAL_ONLY_PHRASES


@dataclass(frozen=True)
class _Intent:
    is_memory: bool
    fact: str


def detect_memory_intent(text: str) -> _Intent:
    """Extract a single candidate fact from one user message.

    An explicit memory marker wins; otherwise only a strong first-person
    profile prefix qualifies. Questions, hypotheticals and third-party claims
    are rejected by the caller via ``is_fact_like``.
    """
    original = " ".join((text or "").split())
    if not original or len(original) > _MAX_FACT_LENGTH:
        return _Intent(False, "")
    if _NEGATION.search(original):
        return _Intent(False, "")
    for pattern in _NON_FACT_PATTERNS:
        if pattern.search(original):
            return _Intent(False, "")

    for pattern in _REQUEST_PATTERNS:
        match = pattern.search(original)
        if match is None:
            continue
        before = original[: match.start()].strip(" ，,。；;：:")
        after = original[match.end() :].strip(" ，,。；;：:")
        # Prefer the clause carrying the actual fact. "我叫梅凯龙，记住我的名字"
        # yields the clause before the marker; "记住我喜欢咖啡" yields after.
        fact = before or after
        if not fact:
            fact = after or before
        fact = re.sub(
            r"(?:，|,)?\s*(?:请|帮我)?记住(?:我的)?(?:名字|信息)?\s*$",
            "",
            fact,
        ).strip(" ，,。；;：:")
        return _Intent(bool(fact), fact)

    if any(original.startswith(prefix) for prefix in _PROFILE_PREFIXES):
        return _Intent(True, original)
    if _CAREER_PATTERN.search(original):
        return _Intent(True, original)
    return _Intent(False, "")


#: Project/status statements legitimately have no "我" prefix
#: ("JarvisMind 项目已完成"). They are only accepted when an existing ACTIVE
#: memory shares the same subject, so ordinary chat cannot create new memories
#: through this weaker shape.
_STATUS_STATEMENT = re.compile(
    r"(?:项目|进度|状态|阶段|版本).*(?:已完成|完成|进行中|暂停|上线|延期|取消|发布)"
    r"|(?:已完成|已暂停|已上线|已延期|已取消|已发布)"
)
_STATUS_SUBJECT_STOP = {
    "项目", "进度", "状态", "阶段", "版本", "当前", "已经", "完成", "进行中",
    "暂停", "上线", "延期", "取消", "发布", "的", "了", "是", "为",
}


def looks_like_status_statement(text: str) -> bool:
    source = " ".join((text or "").split())
    if not source or len(source) > 200:
        return False
    return _STATUS_STATEMENT.search(source) is not None


def status_subject_tokens(text: str) -> set:
    """Subject tokens of a status statement, minus the status words themselves."""
    from ..core.dedup import token_set

    return {
        token
        for token in token_set(text)
        if token not in _STATUS_SUBJECT_STOP and len(token) >= 2
    }


def _infer_memory_type(fact: str) -> MemoryType:
    lowered = fact.lower()
    if any(
        token in lowered
        for token in (
            "我叫", "我的名字", "称呼", "叫我", "职业", "工作", "我从事",
            "爱好", "喜欢", "习惯", "使用", "我用", "住",
        )
    ):
        return MemoryType.PROFILE
    if any(token in lowered for token in ("项目", "jarvis", "jme", "代码", "仓库", "技术栈")):
        return MemoryType.PROJECT
    if any(token in lowered for token in ("昨天", "上周", "那次", "去了", "参加")):
        return MemoryType.EPISODIC
    return MemoryType.SEMANTIC


def _importance_for(memory_type: MemoryType) -> Tuple[float, float]:
    if memory_type == MemoryType.PROFILE:
        return 8.0, 0.9
    if memory_type == MemoryType.PROJECT:
        return 7.5, 0.82
    if memory_type == MemoryType.EPISODIC:
        return 6.0, 0.75
    return 6.5, 0.8


def _receipt(
    status: str,
    code: str,
    message: str,
    *,
    candidate_id: Optional[str] = None,
    memory_id: Optional[str] = None,
    content: Optional[str] = None,
    memory_type: Optional[MemoryType] = None,
    sensitive_categories: Optional[Sequence[str]] = None,
    merged_ids: Optional[Sequence[str]] = None,
    error_type: Optional[str] = None,
) -> AutoRememberResponse:
    return AutoRememberResponse(
        status=status,
        code=code,
        message=message,
        candidate_id=candidate_id,
        memory_id=memory_id,
        content=content,
        memory_type=memory_type,
        sensitive_categories=list(sensitive_categories or []),
        merged_ids=list(merged_ids or []),
        error_type=error_type,
    )


class AutoMemoryService:
    def __init__(
        self, manager, *, memory_service: Optional[MemoryService] = None
    ) -> None:
        self.manager = manager
        # C-1: the wired MemoryService (embedding + vector_store) supplied by
        # the composition root. Without it, every LifecycleService built below
        # would fall back to a bare MemoryService whose vector hooks silently
        # no-op, leaving MERGED/ARCHIVED orphan points in the vector store.
        # ``None`` keeps the historical DB-only behaviour for direct callers.
        self._memory_service = memory_service

    def _lifecycle(self) -> LifecycleService:
        """A lifecycle entry that shares the wired vector backend (C-1)."""
        return LifecycleService(self.manager, memory_service=self._memory_service)

    # ------------------------------------------------------------------ helpers
    def _exact_tokens(self) -> Tuple[str, ...]:
        settings = self.manager.settings
        return tuple(
            token for token in (settings.api_token, settings.llm_api_key) if token
        )

    def _active_memories(self, limit: int = _MAX_GOVERNED_PAIRS) -> List[Tuple[str, str]]:
        """Active memories, newest first, bounded so one message stays cheap."""
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT id, content FROM memory_records "
                    "WHERE status='ACTIVE' ORDER BY updated_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
        return [(str(row.id), row.content or "") for row in rows]

    def _memory_id_for_candidate(self, candidate_id: str) -> Optional[str]:
        with self.manager.session() as session:
            row = session.execute(
                text(
                    "SELECT id FROM memory_records "
                    "WHERE source_candidate_id=:cid LIMIT 1"
                ),
                {"cid": candidate_id},
            ).scalar()
        return str(row) if row else None

    def _candidate_exists(self, candidate_id: str) -> bool:
        with self.manager.session() as session:
            row = session.execute(
                text("SELECT 1 FROM memory_candidates WHERE id=:cid"),
                {"cid": candidate_id},
            ).scalar()
        return bool(row)

    def _create_candidate(
        self,
        *,
        candidate_id: str,
        conversation_id: str,
        stored_fact: str,
        summary: str,
        memory_type: MemoryType,
        importance: float,
        confidence: float,
        source_id: Optional[str],
        message_id: Optional[str],
        turn_id: Optional[str],
        pii_categories: Sequence[str],
        credential_categories: Sequence[str],
        conflicts_with: Optional[str],
        engine: str = "auto-memory",
        intent_label: str = "explicit",
    ) -> None:
        now = utc_now_iso()
        metadata = json.dumps(
            {
                "auto_memory": True,
                "auto_governance": True,
                "intent": intent_label,
                "ruleset_version": RULESET_VERSION,
                "message_id": message_id or "",
                "turn_id": turn_id or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprint = content_fingerprint(
            self.manager.settings,
            content=stored_fact,
            summary=summary,
            metadata_json=metadata,
            decision_version=1,
        )
        required = required_grants_for(pii_categories, credential_categories)
        status = (
            CandidateStatus.PENDING_AUTHORIZATION.value
            if pii_categories
            else CandidateStatus.PENDING.value
        )
        candidate = MemoryCandidate(
            id=candidate_id,
            conversation_id=conversation_id,
            source_id=source_id,
            memory_type=memory_type.value,
            content=stored_fact,
            summary=summary,
            importance=importance,
            confidence=confidence,
            status=status,
            duplicate_of=None,
            conflicts_with=conflicts_with,
            extraction_engine=engine,
            extracted_at=now,
            decided_at=None,
            decided_by=None,
            reject_reason=None,
            metadata_json=metadata,
            scan_status="SCANNED",
            decision_version=1,
            content_fingerprint=fingerprint,
            ruleset_version=RULESET_VERSION,
            required_grants_json=json.dumps(required),
            message_id=message_id or "",
            turn_id=turn_id or "",
            app_decision_reported=(
                "AUTO_WRITE_ELIGIBLE" if not pii_categories else "PII_HOLD"
            ),
            server_security_result=(
                "ALLOWED" if not pii_categories else "PII_REQUIRES_GRANTS"
            ),
            final_result=None,
        )
        with self.manager.session() as session:
            CandidateRepository(session).create(candidate)

    # -------------------------------------------------------------------- main
    def auto_remember(
        self,
        *,
        text_value: str,
        conversation_id: str,
        message_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        source_id: Optional[str] = None,
        mode: str = "auto",
    ) -> AutoRememberResponse:
        """Turn one statement into a governed memory write.

        ``mode="auto"`` is the original behavior: the statement must look like
        a memory request before it is considered, which is why a plain
        ``用户平时在 Mac 上开发 JarvisMind。`` is refused with ``NO_INTENT``.

        ``mode="direct"`` is for a caller that has *already* judged the
        statement worth remembering (the DSH agent, after reading the turn). It
        skips **only** the two intent heuristics: the trigger-word scan and
        ``is_fact_like``. Both exist to guess whether a raw user sentence is a
        memory request, and both misfire on a memory the agent distilled itself
        ("用户说他最近压力很大，我陪他聊了很久" is rejected as third-party).
        Everything that makes a write safe still runs in the same order: the
        credential gate, the JMR sensitivity hold, conflict/duplicate detection,
        and the single authoritative write through ``ApprovalService``. A direct
        write is not a privileged write; it is a differently-addressed one.
        """
        if mode not in ("auto", "direct"):
            return _receipt("NO_MEMORY", "invalid_mode", "未知的记忆写入模式。")

        existing = self._active_memories()

        fact = ""
        status_statement = False
        if mode == "direct":
            # No trigger word is required, so the whole statement is the fact.
            fact = " ".join((text_value or "").split()).strip()
        else:
            intent = detect_memory_intent(text_value)
            if intent.is_memory and intent.fact:
                fact = intent.fact.strip()
            elif looks_like_status_statement(text_value):
                # A project/status update does not start with "我". It is accepted
                # ONLY when an existing ACTIVE memory shares its subject, so plain
                # chat cannot create memories through this weaker shape.
                subject = status_subject_tokens(text_value)
                if subject and any(
                    subject & status_subject_tokens(content)
                    for _id, content in existing
                ):
                    fact = " ".join(text_value.split()).strip()
                    status_statement = True

        if not fact:
            if mode == "direct":
                # Nothing was written, and no intent word was involved to blame.
                return _receipt("NO_MEMORY", "fact_empty", "没有可保存的内容。")
            return _receipt("NO_INTENT", "no_memory_intent", "没有识别到明确的记忆请求。")
        if len(fact) < 2:
            # Applies in both modes: "嗯" / "好" are statements, not memories.
            return _receipt("NO_MEMORY", "fact_empty", "记忆请求中没有可保存的事实。")
        if is_social_only(fact):
            # Applies in both modes too: a pleasantry states no fact. In `direct`
            # mode no intent heuristic runs, so this is what stops the agent's
            # own judgement from filing "谢谢" as a memory.
            return _receipt("NO_MEMORY", "social_only", "这句话只是寒暄，没有可保存的事实。")
        if mode == "auto" and not status_statement and not is_fact_like(fact):
            return _receipt(
                "NO_MEMORY",
                "not_a_fact",
                "这句话是提问、假设或转述，未作为用户事实保存。",
            )

        # Credential gate first: a credential must never become a candidate.
        try:
            assert_writable(
                fact,
                metadata={"source": "auto_memory"},
                exact_tokens=self._exact_tokens(),
                field="memory_records",
            )
        except Exception as exc:
            detail = getattr(exc, "detail", {}) or {}
            categories = list(detail.get("categories", [])) if isinstance(detail, dict) else []
            return _receipt(
                "DENIED",
                "credential_blocked",
                "该内容包含凭据，已拒绝写入长期记忆。",
                sensitive_categories=categories,
            )

        blocked, credential_categories, detected_pii = classify_candidate(fact)
        if blocked:
            return _receipt(
                "DENIED",
                "credential_blocked",
                "该内容包含凭据，已拒绝写入长期记忆。",
                sensitive_categories=credential_categories,
            )

        policy_categories = personal_categories(fact)
        pii_categories = sorted(set(detected_pii) | set(policy_categories))
        stored_fact = redact(fact, self._exact_tokens()) if pii_categories else fact
        memory_type = _infer_memory_type(fact)
        importance, confidence = _importance_for(memory_type)
        candidate_id = uuid.uuid4().hex
        summary = stored_fact[:60]

        # Sensitive: keep the existing HOLD behaviour, nothing is written.
        if pii_categories:
            self._create_candidate(
                candidate_id=candidate_id,
                conversation_id=conversation_id,
                stored_fact=stored_fact,
                summary=summary,
                memory_type=memory_type,
                importance=importance,
                confidence=confidence,
                source_id=source_id,
                message_id=message_id,
                turn_id=turn_id,
                pii_categories=pii_categories,
                credential_categories=credential_categories,
                conflicts_with=None,
                intent_label="explicit",
            )
            return _receipt(
                "PROTECTED",
                "sensitive_hold",
                "这是敏感信息，已进入保护流程，需用户明确授权后才能写入。",
                candidate_id=candidate_id,
                content=stored_fact,
                memory_type=memory_type,
                sensitive_categories=pii_categories,
            )

        # Non-sensitive: decide the relation to existing memories.
        scored = scored_matches(stored_fact, existing)
        match = best_match(stored_fact, existing)

        if match is None and scored:
            # More than one plausible target: refuse to pick, hold for review.
            top = scored[0]
            self._create_candidate(
                candidate_id=candidate_id,
                conversation_id=conversation_id,
                stored_fact=stored_fact,
                summary=summary,
                memory_type=memory_type,
                importance=importance,
                confidence=confidence,
                source_id=source_id,
                message_id=message_id,
                turn_id=turn_id,
                pii_categories=[],
                credential_categories=credential_categories,
                conflicts_with=top[0],
                intent_label="ambiguous",
            )
            return _receipt(
                "CONFLICT",
                "conflict_ambiguous",
                "这条内容与多条现有记忆相关，无法安全判断应更新哪一条，已保留等待确认。",
                candidate_id=candidate_id,
                content=stored_fact,
                memory_type=memory_type,
            )

        if match is not None:
            target_id, target_content, rel, _score = match

            if rel == Relation.DUPLICATE:
                return _receipt(
                    "DUPLICATE",
                    "duplicate_of_existing",
                    "这条内容已经存在，未重复保存。",
                    memory_id=target_id,
                    content=target_content,
                )

            if rel == Relation.UPDATE:
                return self._apply_update(
                    target_id=target_id,
                    target_content=target_content,
                    new_fact=stored_fact,
                    memory_type=memory_type,
                )

            if rel == Relation.CONFLICT:
                self._create_candidate(
                    candidate_id=candidate_id,
                    conversation_id=conversation_id,
                    stored_fact=stored_fact,
                    summary=summary,
                    memory_type=memory_type,
                    importance=importance,
                    confidence=confidence,
                    source_id=source_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    pii_categories=[],
                    credential_categories=credential_categories,
                    conflicts_with=target_id,
                    intent_label="explicit",
                )
                return _receipt(
                    "CONFLICT",
                    "conflict_pending",
                    "这条内容与现有记忆冲突，已保留候选等待确认。",
                    candidate_id=candidate_id,
                    content=stored_fact,
                    memory_type=memory_type,
                )

        # Genuinely new fact: create the candidate, then let ApprovalService
        # perform the only authoritative write.
        return self._create_new(
            candidate_id=candidate_id,
            conversation_id=conversation_id,
            stored_fact=stored_fact,
            summary=summary,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_id=source_id,
            message_id=message_id,
            turn_id=turn_id,
            credential_categories=credential_categories,
        )

    # ------------------------------------------------------------------- paths
    def _create_new(
        self,
        *,
        candidate_id: str,
        conversation_id: str,
        stored_fact: str,
        summary: str,
        memory_type: MemoryType,
        importance: float,
        confidence: float,
        source_id: Optional[str],
        message_id: Optional[str],
        turn_id: Optional[str],
        credential_categories: Sequence[str],
    ) -> AutoRememberResponse:
        self._create_candidate(
            candidate_id=candidate_id,
            conversation_id=conversation_id,
            stored_fact=stored_fact,
            summary=summary,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_id=source_id,
            message_id=message_id,
            turn_id=turn_id,
            pii_categories=[],
            credential_categories=credential_categories,
            conflicts_with=None,
        )
        try:
            ApprovalService(self.manager).review(
                candidate_id,
                decision="approve",
                reason="auto:non-sensitive",
                actor_label=_ACTOR,
            )
        except Exception as exc:
            return _receipt(
                "FAILED",
                "write_failed",
                "自动写入失败，未产生权威记忆。",
                candidate_id=candidate_id,
                content=stored_fact,
                memory_type=memory_type,
                sensitive_categories=credential_categories,
                error_type=type(exc).__name__,
            )
        memory_id = self._memory_id_for_candidate(candidate_id)
        if memory_id is None:
            return _receipt(
                "FAILED",
                "memory_receipt_missing",
                "审批服务未返回权威记忆回执，不计为成功。",
                candidate_id=candidate_id,
                content=stored_fact,
                memory_type=memory_type,
            )
        return _receipt(
            "SAVED",
            "auto_saved",
            "已保存到长期记忆。",
            candidate_id=candidate_id,
            memory_id=memory_id,
            content=stored_fact,
            memory_type=memory_type,
        )

    def _apply_update(
        self,
        *,
        target_id: str,
        target_content: str,
        new_fact: str,
        memory_type: MemoryType,
    ) -> AutoRememberResponse:
        """Replace an outdated value through the governed lifecycle entry."""
        # Keep the sentence the user actually said, not a paraphrase of it: the
        # updated memory must remain a user statement.
        try:
            self._lifecycle().supersede_memory(
                target_id,
                new_content=new_fact,
                reason="auto:user stated a newer value",
                source="auto_governance",
            )
        except Exception as exc:
            return _receipt(
                "FAILED",
                "update_failed",
                "自动更新失败，原记忆保持不变。",
                memory_id=target_id,
                content=target_content,
                memory_type=memory_type,
                error_type=type(exc).__name__,
            )
        return _receipt(
            "UPDATED",
            "auto_updated",
            "已更新这条长期记忆。",
            memory_id=target_id,
            content=new_fact,
            memory_type=memory_type,
        )

    # -------------------------------------------------------------------- merge
    def auto_merge(self, *, dry_run: bool = False) -> Dict[str, object]:
        """Merge verified-equivalent ACTIVE memories.

        Only clusters proven equivalent by ``mergeable_group`` are considered.
        The caller of the HTTP endpoint controls whether this actually writes;
        ``dry_run`` reports what would merge without changing anything.
        """
        merged: List[Dict[str, object]] = []
        skipped: List[Dict[str, object]] = []
        existing = self._active_memories(limit=_MAX_GOVERNED_PAIRS)

        # Build clusters from the transitive duplicate relation, then verify
        # each cluster is pair-wise equivalent before touching anything.
        clusters: List[List[Tuple[str, str]]] = []
        visited: set = set()
        for memory_id, content in existing:
            if memory_id in visited:
                continue
            group = [(memory_id, content)]
            visited.add(memory_id)
            for other_id, other_content in existing:
                if other_id in visited:
                    continue
                if relation(other_content, content) == Relation.DUPLICATE:
                    group.append((other_id, other_content))
                    visited.add(other_id)
            if len(group) > 1:
                clusters.append(group)

        lifecycle = self._lifecycle()
        for group in clusters:
            contents = [content for _id, content in group]
            if not mergeable_group(contents):
                skipped.append(
                    {
                        "memory_ids": [item[0] for item in group],
                        "reason": "not_equivalent_enough",
                    }
                )
                continue
            # The merged memory keeps the most complete wording, which preserves
            # every fact the shorter variants carried.
            merged_content = max(contents, key=len)
            ids = [item[0] for item in group]
            if dry_run:
                merged.append(
                    {
                        "memory_ids": ids,
                        "merged_content": merged_content,
                        "dry_run": True,
                    }
                )
                continue
            try:
                result = lifecycle.auto_merge_duplicates(
                    ids,
                    new_content=merged_content,
                    reason="多条记忆内容高度等价，自动整理为一条",
                )
            except Exception as exc:
                skipped.append(
                    {
                        "memory_ids": ids,
                        "reason": f"merge_failed:{type(exc).__name__}",
                    }
                )
                continue
            merged.append(
                {
                    "memory_ids": ids,
                    "memory_id": result.target_memory_id,
                    "merged_content": merged_content,
                }
            )

        return {
            "status": "DRY_RUN" if dry_run else "MERGED",
            "clusters": len(clusters),
            "merged": merged,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------- expiry
    def auto_expire(self, *, dry_run: bool = False) -> Dict[str, object]:
        """Archive ACTIVE memories whose own explicit expiry has passed.

        Rows without ``expires_at`` are never touched: elapsed time is not
        evidence that a fact became false.
        """
        now = utc_now_iso()
        with self.manager.session() as session:
            rows = session.execute(
                text(
                    "SELECT id, content, expires_at FROM memory_records "
                    "WHERE status='ACTIVE' AND expires_at IS NOT NULL "
                    "AND expires_at <= :now ORDER BY expires_at ASC"
                ),
                {"now": now},
            ).all()

        items: List[Dict[str, object]] = []
        archived = 0
        lifecycle = self._lifecycle()
        for row in rows:
            entry: Dict[str, object] = {
                "memory_id": str(row.id),
                "expires_at": row.expires_at,
                "content": (row.content or "")[:120],
            }
            if dry_run:
                entry["dry_run"] = True
                items.append(entry)
                continue
            try:
                changed = lifecycle.auto_archive_expired(str(row.id), now=now)
            except Exception as exc:
                entry["error"] = type(exc).__name__
                items.append(entry)
                continue
            entry["archived"] = changed
            archived += 1 if changed else 0
            items.append(entry)

        return {
            "status": "DRY_RUN" if dry_run else "EXPIRED",
            "candidates": len(rows),
            "archived": archived,
            "items": items,
        }

    # ------------------------------------------------------------------ summary
    def governance_summary(self) -> Dict[str, object]:
        """Real counts for the iOS memory page. No static or demo values."""
        with self.manager.session() as session:
            counts = {
                str(row.status): int(row.count)
                for row in session.execute(
                    text(
                        "SELECT status, COUNT(*) AS count FROM memory_records "
                        "GROUP BY status"
                    )
                )
            }
            auto_written = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM memory_records "
                        "WHERE status='ACTIVE' AND metadata_json LIKE '%auto-memory%'"
                    )
                ).scalar()
                or 0
            )
            pending_sensitive = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM memory_candidates "
                        "WHERE status='PENDING_AUTHORIZATION'"
                    )
                ).scalar()
                or 0
            )
            pending_conflicts = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM memory_candidates "
                        "WHERE status='PENDING' AND conflicts_with IS NOT NULL"
                    )
                ).scalar()
                or 0
            )
            expiring = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM memory_records "
                        "WHERE status='ACTIVE' AND expires_at IS NOT NULL "
                        "AND expires_at > :now"
                    ),
                    {"now": utc_now_iso()},
                ).scalar()
                or 0
            )
            expired = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM memory_records "
                        "WHERE status='ACTIVE' AND expires_at IS NOT NULL "
                        "AND expires_at <= :now"
                    ),
                    {"now": utc_now_iso()},
                ).scalar()
                or 0
            )
            recent = session.execute(
                text(
                    "SELECT memory_id, action, detail_json, created_at "
                    "FROM memory_audit_logs "
                    "WHERE action IN ('AUTO_UPDATE','AUTO_MERGE_CONFIRM',"
                    "'AUTO_MERGE_SOURCE','AUTO_EXPIRE','CREATE') "
                    "ORDER BY created_at DESC LIMIT 10"
                )
            ).all()

        events: List[Dict[str, object]] = []
        for row in recent:
            detail: Dict[str, object] = {}
            try:
                detail = json.loads(row.detail_json or "{}")
            except (ValueError, TypeError):
                detail = {}
            events.append(
                {
                    "memory_id": str(row.memory_id),
                    "action": str(row.action),
                    "created_at": row.created_at,
                    "engine": detail.get("engine") or detail.get("source") or "",
                }
            )

        return {
            "active": counts.get("ACTIVE", 0),
            "archived": counts.get("ARCHIVED", 0),
            "merged": counts.get("MERGED", 0),
            "deleted": counts.get("DELETED", 0),
            "auto_written": auto_written,
            "pending_sensitive": pending_sensitive,
            "pending_conflicts": pending_conflicts,
            "expiring": expiring,
            "expired_pending": expired,
            "recent_events": events,
        }
