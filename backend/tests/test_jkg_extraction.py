"""JKG-INT-04 抽取器回归：纯规则、纯函数、零 I/O。

覆盖范围
--------
* 规则：8 条句式（4 中文 / 4 英文）每条一正一反；
* 类型：只产出 ``PERSON`` / ``PROJECT`` / ``EVENT`` 与 4 个已批准关系；
* 规范化：大小写 / 空白 / 包裹标点 / 长度边界（1 与 64 通过，65 拒绝）；
* 去重：同 ``(entity_type, normalized_key)`` 只留一个节点，重复计数递增；
* 自环：``subject_key == object_key`` 被拒；
* 隐私：整条 DENY / 整条 HOLD / 候选级拒绝只丢该候选；
* B-1 回归：宽容凭证形态必须 DENY；
* 误判：普通中文与英文句子必须 ALLOW 并正常产出；
* 纯函数性：三张 ``jkg_*`` 表行数不变、模块不引入网络/LLM 依赖；
* ``RULE_VERSION`` 写进结果，``to_dict()`` 可 JSON 化且不含正文。

为什么用源码静态断言而不是 ``sys.modules`` 断言（B-2）
----------------------------------------------------
``sys.modules`` 断言只能证明「本次运行到断言那一刻还没有 import」，而测试执行
顺序、其他用例的副作用、以及 Python 的惰性 import 都会让这个结论随时过期。源码
断言直接读 ``extraction.py`` 的文本，锁住的是「这个模块里根本没有出现这些名字」，
这正是 B-2 要的性质：本阶段不构造 prompt、不调 provider、不发网络请求。
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import text

from jarvismind.jkg import extraction
from jarvismind.jkg.extraction import RULE_VERSION, extract_candidates
from jarvismind.jkg.normalization import (
    VALID_SOURCE_STATUSES,
    canonicalize_entity_name,
    normalize_entity_name,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 正文里的独特标记：用来证明预览与拒绝理由都不回传正文。
_MARKER = "MKR" + "-INT-04-EXTRACT-DO-NOT-ECHO"

# 宽容凭证样本：B-1 的三种写法。片段拼接，本文件不出现完整字面量。
_WIDE_CREDENTIAL_BODIES = (
    "密码 " + "hunter2",
    "密码是 " + "hunter2",
    "passwd=" + "abc12345",
)


def _node_keys(result) -> set:
    return {(node.entity_type, node.normalized_key) for node in result.nodes}


def _edge_keys(result) -> set:
    return {(edge.subject_key, edge.predicate, edge.object_key) for edge in result.edges}


def _names(result) -> set:
    return {node.name for node in result.nodes}


def _rejection_codes(result) -> list:
    return [item.reason_code for item in result.rejections]


def _extract(text, memory_id: str = "m-1"):
    return extract_candidates(text, memory_id=memory_id)


# ===========================================================================
# A · 规则：中文四条句式（每条一正一反）
# ===========================================================================
def test_rule_zh_works_on_produces_person_project_and_relation():
    result = _extract("Alice 负责 JarvisMind")
    assert result.eligible is True
    assert _names(result) == {"Alice", "JarvisMind"}
    assert _node_keys(result) == {
        ("PERSON", normalize_entity_name(canonicalize_entity_name("Alice"))),
        ("PROJECT", normalize_entity_name(canonicalize_entity_name("JarvisMind"))),
    }
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.predicate == "WORKS_ON"
    assert edge.subject_type == "PERSON"
    assert edge.object_type == "PROJECT"
    assert edge.source_memory_id == "m-1"


def test_rule_zh_works_on_ignores_a_clause_without_the_keyword():
    result = _extract("Alice 和 JarvisMind 都在这个仓库里")
    assert result.eligible is True
    assert result.nodes == ()
    assert result.edges == ()
    assert result.rejections == ()


def test_rule_zh_attends_produces_person_event():
    result = _extract("张伟 参加 知识图谱评审")
    assert _node_keys(result) == {
        ("PERSON", normalize_entity_name(canonicalize_entity_name("张伟"))),
        ("EVENT", normalize_entity_name(canonicalize_entity_name("知识图谱评审"))),
    }
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("ATTENDS", "PERSON", "EVENT")
    ]


def test_rule_zh_attends_ignores_an_unrelated_clause():
    result = _extract("张伟 昨天没来")
    assert result.nodes == ()
    assert result.edges == ()


def test_rule_zh_belongs_to_produces_event_project():
    result = _extract("知识图谱评审 属于 JarvisMind")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("BELONGS_TO", "EVENT", "PROJECT")
    ]


def test_rule_zh_belongs_to_ignores_an_unrelated_clause():
    result = _extract("这个评审 挺有意思")
    assert result.edges == ()


def test_rule_zh_knows_produces_person_person():
    result = _extract("李雷 认识 韩梅梅")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("KNOWS", "PERSON", "PERSON")
    ]


def test_rule_zh_knows_ignores_a_clause_without_the_keyword():
    result = _extract("李雷 在开会")
    assert result.edges == ()


# ===========================================================================
# B · 规则：英文四条句式（每条一正一反）
# ===========================================================================
def test_rule_en_works_on_produces_the_relation():
    result = _extract("Alice works on JarvisMind")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("WORKS_ON", "PERSON", "PROJECT")
    ]
    assert _names(result) == {"Alice", "JarvisMind"}


def test_rule_en_works_on_does_not_fire_without_the_phrase():
    result = _extract("Alice is thinking about JarvisMind")
    assert result.edges == ()


def test_rule_en_attends_produces_the_relation():
    result = _extract("Bob attends KnowledgeGraph Review")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("ATTENDS", "PERSON", "EVENT")
    ]


def test_rule_en_attends_does_not_fire_without_the_verb():
    result = _extract("Bob skipped the KnowledgeGraph Review")
    assert result.edges == ()


def test_rule_en_belongs_to_produces_the_relation():
    result = _extract("KnowledgeGraph Review belongs to JarvisMind")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("BELONGS_TO", "EVENT", "PROJECT")
    ]


def test_rule_en_belongs_to_does_not_fire_without_the_phrase():
    result = _extract("KnowledgeGraph Review is about JarvisMind")
    assert result.edges == ()


def test_rule_en_knows_produces_the_relation():
    result = _extract("Alice knows Bob")
    assert [(e.predicate, e.subject_type, e.object_type) for e in result.edges] == [
        ("KNOWS", "PERSON", "PERSON")
    ]


def test_rule_en_knows_does_not_fire_inside_a_longer_word():
    """``knows`` 不得在 ``unknowns`` 里命中：ASCII 关键字要求词边界。"""
    result = _extract("Alice unknowns Bob")
    assert result.edges == ()


# ===========================================================================
# C · 类型与非法输入
# ===========================================================================
def test_every_produced_node_type_is_in_the_frozen_vocabulary():
    result = _extract(
        "Alice 负责 JarvisMind。张伟 参加 知识图谱评审。李雷 认识 韩梅梅。"
        "知识图谱评审 属于 JarvisMind"
    )
    assert result.eligible is True
    assert {node.entity_type for node in result.nodes} <= {"PERSON", "PROJECT", "EVENT"}
    assert {edge.predicate for edge in result.edges} <= {
        "WORKS_ON",
        "ATTENDS",
        "BELONGS_TO",
        "KNOWS",
    }


def test_every_produced_edge_endpoint_belongs_to_an_accepted_node():
    """边只能在两个已接受节点之间形成。"""
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    for edge in result.edges:
        assert (edge.subject_type, edge.subject_key) in _node_keys(result)
        assert (edge.object_type, edge.object_key) in _node_keys(result)


def test_invalid_declared_entity_type_is_rejected_not_produced(monkeypatch):
    """规则声明了非法实体类型时必须拒绝，不得产出。

    真实规则表里没有非法类型（有测试锁住这一点），所以这里替换成一条坏规则，
    走的是同一条 ``validate_entity_type`` 校验路径。
    """
    bad = extraction._Rule(
        "zh.bad", "负责", "WORKS_ON", "ROBOT", "PROJECT", 0.5, False
    )
    monkeypatch.setattr(extraction, "_RULES", (bad,))
    monkeypatch.setattr(
        extraction,
        "_KEYWORD_PATTERNS",
        {"zh.bad": extraction._keyword_pattern("负责", ascii_word=False)},
    )
    result = _extract("Alice 负责 JarvisMind")
    assert result.nodes == ()
    assert result.edges == ()
    assert _rejection_codes(result) == ["INVALID_TYPE"]


def test_invalid_declared_relation_type_is_rejected_not_produced(monkeypatch):
    bad = extraction._Rule(
        "zh.bad", "负责", "LIKES", "PERSON", "PROJECT", 0.5, False
    )
    monkeypatch.setattr(extraction, "_RULES", (bad,))
    monkeypatch.setattr(
        extraction,
        "_KEYWORD_PATTERNS",
        {"zh.bad": extraction._keyword_pattern("负责", ascii_word=False)},
    )
    result = _extract("Alice 负责 JarvisMind")
    assert result.nodes == ()
    assert result.edges == ()
    assert _rejection_codes(result) == ["INVALID_TYPE"]


def test_shipped_rules_only_declare_frozen_types():
    """出厂规则表本身只有合法类型：防止有人以后直接改常量绕过校验。"""
    for rule in extraction._RULES:
        assert rule.subject_type in {"PERSON", "PROJECT", "EVENT"}
        assert rule.object_type in {"PERSON", "PROJECT", "EVENT"}
        assert rule.predicate in {"WORKS_ON", "ATTENDS", "BELONGS_TO", "KNOWS"}


# ===========================================================================
# D · 名称规范化
# ===========================================================================
def test_entity_name_keeps_its_display_case_and_folds_only_in_the_key():
    result = _extract("Alice 负责 JarvisMind")
    by_key = {node.normalized_key: node for node in result.nodes}
    assert "Alice" in [node.name for node in result.nodes]
    assert by_key[normalize_entity_name("Alice")].name == "Alice"


def test_name_case_variants_collapse_to_one_node():
    """同一实体的两种大小写写法只产生一个候选，重复计数递增。"""
    result = _extract("Alice 负责 JarvisMind。alice 认识 Bob")
    alice_keys = [
        node for node in result.nodes
        if node.normalized_key == normalize_entity_name(canonicalize_entity_name("Alice"))
    ]
    assert len(alice_keys) == 1
    assert result.duplicates_removed >= 1


def test_surrounding_whitespace_is_trimmed_from_the_name():
    result = _extract("  Alice   负责   JarvisMind  ")
    assert _names(result) == {"Alice", "JarvisMind"}


def test_wrapping_punctuation_is_trimmed_from_the_name():
    result = _extract("「Alice」 负责 (JarvisMind)")
    assert _names(result) == {"Alice", "JarvisMind"}


def test_one_character_name_is_accepted():
    result = _extract("A 负责 JarvisMind")
    assert "A" in _names(result)
    assert result.eligible is True


def test_64_character_name_is_accepted():
    long_name = "P" * 64
    result = _extract(f"Alice 负责 {long_name}")
    assert long_name in _names(result)
    assert "NAME_TOO_LONG" not in _rejection_codes(result)


def test_65_character_name_is_rejected():
    too_long = "P" * 65
    result = _extract(f"Alice 负责 {too_long}")
    assert too_long not in _names(result)
    assert "NAME_TOO_LONG" in _rejection_codes(result)


def test_empty_name_is_rejected_and_counted():
    """关键字后只有空白：右端为空，必须记 EMPTY_NAME 而不是产出一个空节点。"""
    result = _extract("Alice 负责   。")
    assert result.edges == ()
    assert "EMPTY_NAME" in _rejection_codes(result)


def test_pronoun_is_not_an_entity():
    result = _extract("她 负责 JarvisMind")
    assert "她" not in _names(result)
    assert "PRONOUN_NAME" in _rejection_codes(result)


# ===========================================================================
# E · 自环与去重
# ===========================================================================
def test_self_reference_is_rejected():
    result = _extract("Alice 认识 Alice")
    assert result.edges == ()
    assert "SELF_REFERENCE" in _rejection_codes(result)


def test_self_reference_is_rejected_across_case_variants():
    """``Alice`` 与 ``alice`` 规范化后相同键，因此这也是自环。"""
    result = _extract("Alice 认识 alice")
    assert result.edges == ()
    assert "SELF_REFERENCE" in _rejection_codes(result)


def test_duplicate_node_candidate_is_counted_not_duplicated():
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    alice = [
        node for node in result.nodes
        if node.normalized_key == normalize_entity_name(canonicalize_entity_name("Alice"))
    ]
    assert len(alice) == 1
    assert result.duplicates_removed >= 1


def test_duplicate_edge_candidate_is_counted_not_duplicated():
    result = _extract("Alice 负责 JarvisMind。Alice 负责 JarvisMind")
    works_on = [edge for edge in result.edges if edge.predicate == "WORKS_ON"]
    assert len(works_on) == 1
    assert result.duplicates_removed >= 1


def test_repeated_identical_body_is_stable():
    body = "Alice 负责 JarvisMind。Bob 认识 Alice"
    first = _extract(body)
    second = _extract(body)
    assert first.to_dict() == second.to_dict()


# ===========================================================================
# F · 隐私门
# ===========================================================================
@pytest.mark.parametrize("body", _WIDE_CREDENTIAL_BODIES)
def test_wide_credential_shapes_are_denied_and_extract_nothing(body):
    """B-1 回归：宽松的凭证写法必须判 DENY，不得因规则宽而放过。"""
    result = _extract(f"Alice 负责 JarvisMind。{body}")
    assert result.decision.decision == "DENY", result.decision.to_dict()
    assert result.eligible is False
    assert result.nodes == ()
    assert result.edges == ()
    assert "CREDENTIAL_PRESENT" in _rejection_codes(result)


def test_credential_body_leaks_no_matched_value():
    result = _extract("密码 " + "hunter2")
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "hunter2" not in payload


def test_sensitive_body_is_excluded_by_default():
    """D12：PII 默认不入图，整条排除而不是逐候选过滤。"""
    result = _extract("Alice 负责 JarvisMind，联系电话 13800138000")
    assert result.decision.decision == "HOLD", result.decision.to_dict()
    assert result.eligible is False
    assert result.nodes == ()
    assert result.edges == ()


def test_sensitive_rejection_uses_the_dual_auth_code():
    result = _extract("Alice 负责 JarvisMind，联系电话 13800138000")
    assert result.eligible is False
    assert _rejection_codes(result) == ["SENSITIVE_DUAL_AUTH_REQUIRED"]


def test_candidate_level_rejection_does_not_drop_the_whole_memory():
    """一个候选名被判 HOLD 时只丢该候选，同条记忆的其余候选保留。

    构造方式：让整条正文 ALLOW（不含 PII），但把某个候选名替换成一个会被
    单独判 HOLD 的标签。这里直接调 ``_name_verdict`` 所在的真实路径 —— 用
    monkeypatch 把 ``evaluate`` 换成「整条 ALLOW / 单名 HOLD」的版本，从而精确
    命中「候选级拒绝」这条分支，而不是靠猜某种文本恰好触发它。
    """
    real_evaluate = extraction.evaluate

    class _Verdict:
        def __init__(self, decision, reason_code):
            self.decision = decision
            self.reason_code = reason_code
            self.may_auto_proceed = decision == "ALLOW"

    def fake_evaluate(value, *args, **kwargs):
        if isinstance(value, str) and value.strip() == "Bob":
            return _Verdict("HOLD", "SENSITIVE_DUAL_AUTH_REQUIRED")
        return real_evaluate(value, *args, **kwargs)

    extraction.evaluate = fake_evaluate
    try:
        result = _extract("Alice 认识 Bob")
    finally:
        extraction.evaluate = real_evaluate

    assert result.eligible is True
    assert "Alice" in _names(result)
    assert "Bob" not in _names(result)
    assert "SENSITIVE_DUAL_AUTH_REQUIRED" in _rejection_codes(result)
    # 端点被丢掉的边也不得留下。
    assert result.edges == ()
    assert "ENDPOINT_DROPPED" in _rejection_codes(result)


def test_ordinary_chinese_sentence_is_allowed_and_extracts():
    """普通中文不得被误判为敏感：``Alice 负责 JarvisMind`` 必须 ALLOW。"""
    result = _extract("Alice 负责 JarvisMind")
    assert result.decision.decision == "ALLOW"
    assert result.eligible is True
    assert len(result.edges) == 1


def test_plain_topic_word_is_allowed_and_not_mistaken_for_pii():
    result = _extract("知识图谱 是这个项目的核心")
    assert result.decision.decision == "ALLOW"
    assert result.eligible is True


def test_none_text_is_allowed_and_yields_nothing():
    result = extract_candidates(None, memory_id="m-none")
    assert result.eligible is True
    assert result.nodes == ()
    assert result.edges == ()


def test_memory_status_outside_the_valid_set_is_not_eligible():
    for status in ("DELETED", "MERGED"):
        result = extraction.extract_candidates(
            "Alice 负责 JarvisMind", memory_id="m-st", memory_status=status
        )
        assert result.eligible is False, status
        assert result.nodes == ()
        assert _rejection_codes(result) == ["SOURCE_NOT_EXTRACTABLE"]


def test_archived_memory_is_still_extractable():
    """``ARCHIVED`` 是合法来源状态（D9 / D17）：归档不等于删除。"""
    assert "ARCHIVED" in VALID_SOURCE_STATUSES
    result = extraction.extract_candidates(
        "Alice 负责 JarvisMind", memory_id="m-arch", memory_status="ARCHIVED"
    )
    assert result.eligible is True
    assert len(result.edges) == 1
    assert result.source_memory_status == "ARCHIVED"


# ===========================================================================
# G · 拒绝理由不含原文
# ===========================================================================
def test_rejection_records_carry_no_matched_text():
    too_long = "秘密" * 40  # 80 字符，超出上限
    result = _extract(f"Alice 负责 {too_long}")
    assert _rejection_codes(result) == ["NAME_TOO_LONG"]
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert too_long not in payload


def test_rejections_have_a_rule_a_code_and_a_human_reason():
    result = _extract("Alice 认识 Alice")
    assert result.rejections
    for item in result.rejections:
        assert item.rule
        assert item.reason_code
        assert item.human_reason
        assert item.human_reason.endswith(".")


# ===========================================================================
# H · 证据只放标签与区间
# ===========================================================================
def test_evidence_carries_a_rule_label_and_offsets_only():
    body = "Alice 负责 JarvisMind"
    result = _extract(body)
    for node in result.nodes:
        assert node.evidence.rule
        assert 0 <= node.evidence.start <= node.evidence.end <= len(body)
        # 证据本身就是区间：用它切正文能切出这个名字。
        assert body[node.evidence.start : node.evidence.end].strip() == node.name


def test_edge_evidence_span_covers_the_clause():
    body = "Alice 负责 JarvisMind"
    edge = _extract(body).edges[0]
    assert edge.evidence.start == 0
    assert edge.evidence.end == len(body)


# ===========================================================================
# I · RULE_VERSION 与 to_dict
# ===========================================================================
def test_rule_version_exists_and_is_written_into_the_result():
    # The version is a contract with stored metadata: a candidate set is only
    # comparable with one produced by the same version. Pinned as a literal (not
    # just ``result.rule_version == RULE_VERSION``) so a silent bump has to come
    # with a report change, per JKG-EXTRACTION-QUALITY.
    assert RULE_VERSION == "jkg-rules/2"
    result = _extract("Alice 负责 JarvisMind")
    assert result.rule_version == RULE_VERSION
    assert result.to_dict()["rule_version"] == RULE_VERSION


def test_to_dict_is_json_serialisable():
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert json.loads(payload)["memory_id"] == "m-1"


def test_to_dict_does_not_contain_the_body_text():
    body = f"笔记 {_MARKER} 如下。Alice 负责 JarvisMind"
    result = _extract(body)
    assert result.edges, "样本必须真的抽取到候选，否则这条断言没有意义"
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert _MARKER not in payload
    assert "笔记" not in payload
    assert "如下" not in payload


def test_to_dict_reports_counts_consistent_with_the_tuples():
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    payload = result.to_dict()
    assert payload["counts"]["nodes"] == len(result.nodes)
    assert payload["counts"]["edges"] == len(result.edges)
    assert payload["counts"]["rejections"] == len(result.rejections)


def test_result_dataclasses_are_frozen():
    result = _extract("Alice 负责 JarvisMind")
    with pytest.raises(Exception):
        result.eligible = False  # type: ignore[misc]
    with pytest.raises(Exception):
        result.nodes[0].name = "Mallory"  # type: ignore[misc]


# ===========================================================================
# J · 纯函数性：不写库、不联网、不调模型
# ===========================================================================
def test_extraction_writes_no_jkg_rows(manager):
    """C3 边界：调用抽取前后三张 jkg 表的行数完全不变。"""
    from _fixtures.jkg_schema_factory import create_jkg_schema

    create_jkg_schema(manager)
    counts_before = _jkg_row_counts(manager)
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    assert result.nodes
    assert _jkg_row_counts(manager) == counts_before


def test_preview_does_not_write_authority_rows(manager, client, auth_headers):
    """预览只读权威表：记忆行数与正文摘要都不变。"""
    memory_id = _insert_memory(manager, "Alice 负责 JarvisMind")
    before = _memory_digest(manager)
    projection = _projection(manager)
    result = extraction.preview_memory(projection, memory_id)
    assert result.memory_id == memory_id
    assert len(result.edges) == 1
    assert _memory_digest(manager) == before


def test_preview_of_a_missing_memory_raises_not_found(manager):
    projection = _projection(manager)
    with pytest.raises(Exception) as excinfo:
        extraction.preview_memory(projection, "no-such-memory")
    assert getattr(excinfo.value, "status_code", None) == 404


def test_extraction_module_imports_no_network_or_model_dependency():
    """B-2：源码里不存在任何网络/LLM/prompt 构造的名字。

    用源码静态断言而不是 ``sys.modules``：后者只反映「此刻」的导入状态，会被
    测试顺序和惰性 import 影响；前者锁住的是模块文本里根本没有这些调用。
    """
    source = (
        extraction.__file__
        and open(extraction.__file__, encoding="utf-8").read()
    )
    assert source
    for forbidden in (
        "openai",
        "httpx",
        "requests",
        "wrap_untrusted",
        "build_prompt_segments",
        "anthropic",
        "urllib",
    ):
        assert forbidden not in source, forbidden


def test_extraction_module_imports_no_write_service():
    """C3：抽取器不 import ``JkgService``，结构上无法写图。

    用 AST 检查 import 语句，而不是扫全文：模块文档里会提到那个类名（说清楚
    "本模块不依赖它"），扫全文只会把文档自己也判成违规。
    """
    import ast

    tree = ast.parse(open(extraction.__file__, encoding="utf-8").read())
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("service" in name for name in imported), imported
    assert not any(name.startswith("jarvismind.jkg") and "service" in name for name in imported)


def test_extraction_never_opens_a_connection():
    source = open(extraction.__file__, encoding="utf-8").read()
    for forbidden in ("sqlite3", "create_engine", "session()", "manager.session"):
        assert forbidden not in source, forbidden


# ===========================================================================
# K · 歧义保护
# ===========================================================================
def test_a_clause_holding_two_relations_is_rejected_not_guessed():
    """一条子句里有两个不同规则关键字时不猜：整条拒绝。"""
    result = _extract("Alice 负责 JarvisMind 并且认识 Bob")
    # 既不放纵成 "JarvisMind 并且" 这种脏名字，也不硬猜方向。
    assert "JarvisMind 并且" not in _names(result)
    assert "AMBIGUOUS_CLAUSE" in _rejection_codes(result) or result.edges == ()


def test_the_same_keyword_twice_in_one_clause_is_rejected():
    result = _extract("Alice 负责 JarvisMind 负责 Bob")
    assert result.edges == ()
    assert "AMBIGUOUS_CLAUSE" in _rejection_codes(result)


def test_sentence_boundaries_split_clauses():
    result = _extract("Alice 负责 JarvisMind。Bob 认识 Alice")
    assert len(result.edges) == 2
    assert _names(result) == {"Alice", "JarvisMind", "Bob"}


def test_a_dotted_version_number_does_not_split_a_clause():
    result = _extract("Alice works on JarvisMind 2.0")
    assert len(result.edges) == 1
    assert "JarvisMind 2.0" in _names(result)


# ===========================================================================
# 辅助
# ===========================================================================
_JKG_TABLES = ("jkg_nodes", "jkg_edges", "jkg_edge_sources")

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES (:id, 'SEMANTIC', :content, 5.0, 0.9, 'ACTIVE', :now, :now)"
)


def _jkg_row_counts(manager) -> dict:
    counts = {}
    with manager.session() as session:
        for table in _JKG_TABLES:
            counts[table] = int(
                session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
            )
    return counts


def _insert_memory(manager, content: str) -> str:
    from jarvismind.core.timeutils import utc_now_iso

    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {"id": memory_id, "content": content, "now": utc_now_iso()},
        )
    return memory_id


def _memory_digest(manager) -> tuple:
    """行数 + 正文排序摘要：任何写入都会改变它。"""
    with manager.session() as session:
        rows = session.execute(
            text("SELECT id, content FROM memory_records ORDER BY id")
        ).all()
    return (len(rows), tuple((str(r[0]), str(r[1])) for r in rows))


def _projection(manager):
    from jarvismind.governance.projection import ReadOnlyProjection

    return ReadOnlyProjection(manager).for_module("JKG")
