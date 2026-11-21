"""P2 JMR · 纯函数验收单测（无 DB / 无 IO）。

对齐 JarvisMind-2.0-P2-JMR-Architecture-Design.md v1.2：
  §5 公共 API 契约、§12 十四条验收标准、§20 契约对齐点。

本文件只测 §5 冻结签名的纯函数：detectors / provenance / records / config。
所有断言都是可证伪的强断言：不写恒真式、不自比、不 mock 核心能力。
数据集全部在用例内构造，按 memory_type 分组隔离以便确定性触发。
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from jarvismind.jmr.config import JMRConfig
from jarvismind.jmr.errors import JMRProvenanceError
from jarvismind.jmr.provenance import (
    RULES,
    build_metadata,
    deterministic_id,
    validate_sources,
)
from jarvismind.jmr.records import SourceRecord, digest_of_records
from jarvismind.jmr.detectors import (
    all_pairs,
    build_summary_draft,
    cluster_pairs,
    find_conflict_pairs,
    find_duplicate_clusters,
    find_status_changes,
    group_related,
    pair_set,
    status_pairs,
    subject_similarity,
)

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"
TS3 = "2026-03-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# 构造辅助：SourceRecord.from_projection 从 MappingProxyType 构造（§5 / D-4）
# 14 字段与 projection.py:45-49 _READ_COLUMNS 逐字对应 + lineage
# --------------------------------------------------------------------------
def _projection_row(memory_id, content, memory_type, created_at, *, lineage=None):
    data = {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "summary": None,
        "importance": 5.0,
        "confidence": 0.9,
        "status": "ACTIVE",
        "source_id": None,
        "source_candidate_id": None,
        "created_at": created_at,
        "updated_at": created_at,
        "expires_at": None,
        "metadata_json": None,
        "vector_status": "PENDING",
        "lineage": lineage
        if lineage is not None
        else MappingProxyType(
            {
                "authority": "JME",
                "table": "memory_records",
                "memory_id": memory_id,
                "source_candidate_id": None,
            }
        ),
    }
    return MappingProxyType(data)


def _rec(memory_id, content, memory_type="JMR_DUP", created_at=TS1, *, lineage=None):
    return SourceRecord.from_projection(
        _projection_row(memory_id, content, memory_type, created_at, lineage=lineage)
    )


def _dup_rows():
    return (
        _rec("M-DUP-A", "Bifrost 网关采用 PostgreSQL 主从架构", "JMR_DUP", TS1),
        _rec(
            "M-DUP-B",
            "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
            "JMR_DUP",
            TS2,
        ),
    )


def _status_rows():
    return (
        _rec("M-ST-1", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS1),
        _rec("M-ST-2", "Phoenix 项目当前状态为已完成", "JMR_STATUS", TS2),
    )


def _conflict_rows():
    return (
        _rec("M-CF-1", "Nebula 缓存层使用 Redis Cluster 三主三从部署", "JMR_CONFLICT", TS1),
        _rec("M-CF-2", "Nebula 缓存层改用 Memcached 单机方案降低成本", "JMR_CONFLICT", TS2),
    )


def _related_rows():
    return (
        _rec("M-RL-1", "Atlas 日志平台采集 Nginx 访问日志", "JMR_RELATED", TS1),
        _rec("M-RL-2", "Atlas 日志平台采集应用错误日志", "JMR_RELATED", TS2),
    )


# ==========================================================================
# 标准 1 · 重复记忆识别（detector 层）
# ==========================================================================
def test_std1_duplicate_cluster_identifies_near_duplicate_pair():
    clusters = find_duplicate_clusters(_dup_rows())
    assert len(clusters) == 1
    assert set(clusters[0].memory_ids) == {"M-DUP-A", "M-DUP-B"}
    assert clusters[0].reason
    assert 0.0 < clusters[0].confidence <= 1.0


def test_std1_duplicate_threshold_is_effectively_applied():
    """阈值必须真正外置：同一对内容随 threshold 变化产生不同判定（D-5 / P1-3）。

    用 detectors.subject_similarity 实测值动态选两个阈值，避免猜数值；subsumption 置为
    不可达值以关闭包含度路径，迫使唯一生效的是 Jaccard >= threshold 分支。
    """
    a = "Redwood 服务使用 MySQL 存储"
    b = "Redwood 服务使用 MongoDB 缓存"
    rows = (_rec("M-TH-A", a, "JMR_DUP"), _rec("M-TH-B", b, "JMR_DUP"))
    sim = subject_similarity(a, b)
    assert 0.0 < sim < 1.0  # 非平凡：不是完全相同也不是零交集
    strict = find_duplicate_clusters(
        rows, threshold=min(1.0, sim + 0.10), subsumption=1.01
    )
    loose = find_duplicate_clusters(
        rows, threshold=max(0.0, sim - 0.10), subsumption=1.01
    )
    target = {"M-TH-A", "M-TH-B"}
    assert not any(set(c.memory_ids) == target for c in strict)
    assert any(set(c.memory_ids) == target for c in loose)


def test_std1_subsumption_parameter_has_default_and_is_callable():
    """§20.1：subsumption 有默认值，只传 threshold 合法（不抛 TypeError）。"""
    result = find_duplicate_clusters(_dup_rows(), threshold=0.9)
    assert isinstance(result, tuple)
    assert len(result) == 1


# ==========================================================================
# 标准 2 · 不同时间的状态变化识别（detector 层）
# ==========================================================================
def test_std2_status_change_before_after_tokens():
    """§8 最小数据：稳定 ASCII 标识 + 状态词原文子串 + 不同 created_at。"""
    changes = find_status_changes(_status_rows(), min_group=2)
    assert len(changes) == 1
    change = changes[0]
    assert change.before_status == "IN_PROGRESS"
    assert change.after_status == "DONE"
    assert change.earlier_memory_id == "M-ST-1"
    assert change.memory_id == "M-ST-2"
    assert change.subject  # 稳定标识分组键
    assert change.reason


def test_std2_aba_counterexample_only_adjacent_changes():
    """A→B→A 只产出相邻两条变化（A→B、B→A），不合并、不跳过中间态。"""
    rows = (
        _rec("M-ABA-1", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS1),
        _rec("M-ABA-2", "Phoenix 项目当前状态为已完成", "JMR_STATUS", TS2),
        _rec("M-ABA-3", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS3),
    )
    changes = find_status_changes(rows, min_group=2)
    assert len(changes) == 2
    ordered = sorted(
        (c.earlier_memory_id, c.memory_id, c.before_status, c.after_status)
        for c in changes
    )
    assert ordered == [
        ("M-ABA-1", "M-ABA-2", "IN_PROGRESS", "DONE"),
        ("M-ABA-2", "M-ABA-3", "DONE", "IN_PROGRESS"),
    ]


# ==========================================================================
# 标准 3 · 冲突记忆识别 + 优先级链（detector 层）
# ==========================================================================
def test_std3_conflict_pair_detects_same_subject_disagreement():
    pairs = find_conflict_pairs(_conflict_rows())
    assert len(pairs) >= 1
    assert any(
        {p.memory_id_a, p.memory_id_b} == {"M-CF-1", "M-CF-2"} for p in pairs
    )
    assert all(p.reason and 0.0 < p.confidence <= 1.0 for p in pairs)
    assert all(p.conflict_class == "WORDING_DIFF" for p in pairs)
    assert all(p.conflict_class != "FACT_CONFLICT" for p in pairs)
    assert all(p.conflict_class != "TIME_UPDATE" for p in pairs)
    assert all(p.conflict_class != "INSUFFICIENT_INFO" for p in pairs)


def test_priority_merge_excludes_cluster_pairs_from_conflict():
    """MERGE > CONFLICT：cluster_pairs(clusters) 必须把去重对从冲突里排除。"""
    rows = _dup_rows()
    clusters = find_duplicate_clusters(rows)
    excluded = cluster_pairs(clusters)
    assert excluded  # 至少 1 个对
    conflicts = find_conflict_pairs(rows, exclude_pairs=excluded)
    assert not any(
        pair_set(((p.memory_id_a, p.memory_id_b),)) & excluded for p in conflicts
    )


def test_priority_status_change_precedes_conflict():
    """v1.2 核心：STATUS_CHANGE 优先于 CONFLICT（D-11 / 标准 2 可达性）。

    状态变化对必然同时满足冲突条件；status_pairs(status) 排除后，
    find_conflict_pairs 不得再把它报为事实冲突。
    """
    rows = _status_rows()
    status = find_status_changes(rows, min_group=2)
    assert len(status) == 1
    excluded = status_pairs(status)
    assert excluded  # 该对被登记
    conflicts = find_conflict_pairs(rows, exclude_pairs=excluded)
    assert conflicts == ()


def test_find_conflict_pairs_respects_exclude_pairs():
    rows = _conflict_rows()
    raw = find_conflict_pairs(rows)
    assert raw  # 先证明不加排除时确实能检出
    blocked = frozenset({(p.memory_id_a, p.memory_id_b) for p in raw})
    blocked |= frozenset({(p.memory_id_b, p.memory_id_a) for p in raw})
    assert find_conflict_pairs(rows, exclude_pairs=blocked) == ()


def test_find_status_changes_respects_exclude_pairs():
    rows = _status_rows()
    assert find_status_changes(rows, min_group=2)
    excluded = frozenset({("M-ST-1", "M-ST-2")})
    assert find_status_changes(rows, min_group=2, exclude_pairs=excluded) == ()


def test_find_conflict_pairs_and_status_changes_have_default_kwargs():
    """§20.1：max_similarity / min_group 有默认值，exclude_pairs 可选。"""
    assert isinstance(find_conflict_pairs(_conflict_rows()), tuple)
    assert isinstance(find_status_changes(_status_rows()), tuple)


# ==========================================================================
# 辅助纯函数
# ==========================================================================
def test_all_pairs_is_sorted_complete_tuple():
    rows = (
        _rec("id-c", "c", "JMR_DUP"),
        _rec("id-a", "a", "JMR_DUP"),
        _rec("id-b", "b", "JMR_DUP"),
    )
    pairs = all_pairs(rows)
    assert isinstance(pairs, tuple)
    assert pairs == (("id-a", "id-b"), ("id-a", "id-c"), ("id-b", "id-c"))
    assert all(a <= b for a, b in pairs)


def test_pair_set_normalizes_order_and_dedups():
    result = pair_set((("b", "a"), ("a", "b"), ("d", "c")))
    assert isinstance(result, frozenset)
    assert result == frozenset({("a", "b"), ("c", "d")})


def test_cluster_pairs_and_status_pairs_helpers():
    from jarvismind.jmr.detectors import DuplicateCluster, StatusChange

    clusters = (DuplicateCluster(("x", "y", "z"), "dup", 0.9),)
    cps = cluster_pairs(clusters)
    assert cps == frozenset({("x", "y"), ("x", "z"), ("y", "z")})

    changes = (
        StatusChange("late", "early", "Phoenix", "IN_PROGRESS", "DONE", "r"),
    )
    assert status_pairs(changes) == frozenset({("early", "late")})

    # ConflictPair 只用于确认 pair_set 能处理 (a,b) 对象序列
    assert pair_set((("m", "n"),)) == frozenset({("m", "n")})


def test_group_related_groups_shared_subject():
    groups = group_related(_related_rows())
    assert groups
    assert any(
        {"M-RL-1", "M-RL-2"}.issubset(set(g.memory_ids))
        for g in groups
    )
    assert all(g.subject and g.reason for g in groups)


def test_build_summary_draft_returns_triplet():
    draft = build_summary_draft(_dup_rows())
    assert isinstance(draft, tuple) and len(draft) == 3
    title, content, confidence = draft
    assert isinstance(title, str) and title
    assert isinstance(content, str) and content
    assert "Bifrost" in content  # 复用 local.build_summary 的内容预览
    assert isinstance(confidence, float) and 0.0 <= confidence <= 1.0
    custom = build_summary_draft(_dup_rows(), summary_type="COLLECTION")
    assert custom[0] != title  # summary_type 真正影响标题


# ==========================================================================
# provenance / records / config
# ==========================================================================
def test_rules_vocabulary_is_frozen():
    assert RULES == ("MERGE", "CONFLICT", "STATUS_CHANGE", "SUMMARY")
    assert "CORRECTION" not in RULES
    assert "RELATED" not in RULES


def test_deterministic_id_format_and_stability():
    pid = deterministic_id(
        rule="MERGE", source_memory_ids=["M-B", "M-A"], algorithm_version="jmr-v1"
    )
    assert pid.startswith("jmr-MERGE-")
    suffix = pid[len("jmr-MERGE-") :]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)
    # 排序无关 + 幂等
    assert pid == deterministic_id(
        rule="MERGE", source_memory_ids=["M-A", "M-B"], algorithm_version="jmr-v1"
    )


def test_deterministic_id_varies_by_rule_and_version():
    base = dict(source_memory_ids=["M-A", "M-B"], algorithm_version="jmr-v1")
    merge = deterministic_id(rule="MERGE", **base)
    conflict = deterministic_id(rule="CONFLICT", **base)
    v2 = deterministic_id(rule="MERGE", source_memory_ids=["M-A", "M-B"], algorithm_version="jmr-v2")
    assert merge != conflict
    assert merge != v2


def test_build_metadata_fixed_keys_and_extra():
    raw = build_metadata(
        source_memory_ids=["M-A", "M-B"],
        lineage={"authority": "JME", "table": "memory_records"},
        task_id="T-1",
        run_id="R-1",
        rule="MERGE",
        algorithm_version="jmr-v1",
        projection_digest="deadbeef",
        extra={"note": "x"},
    )
    assert isinstance(raw, str)
    data = json.loads(raw)
    for key in (
        "source_memory_ids",
        "lineage",
        "task_id",
        "run_id",
        "rule",
        "algorithm_version",
        "projection_digest",
    ):
        assert key in data
    assert data["source_memory_ids"] == ["M-A", "M-B"]
    assert data["rule"] == "MERGE"
    assert data["extra"] == {"note": "x"}


def test_validate_sources_accepts_valid_rows():
    rows = _dup_rows()
    validate_sources(rows, source_memory_ids=["M-DUP-A", "M-DUP-B"])
    # 未抛异常即通过；再构造一个「少一个 id」的合法子集
    validate_sources(rows, source_memory_ids=["M-DUP-A"])


def test_validate_sources_rejects_unknown_source_id():
    """标准 4：篡改源 id（引用投影窗口外记忆）→ JMRProvenanceError。"""
    rows = _dup_rows()
    with pytest.raises(JMRProvenanceError):
        validate_sources(rows, source_memory_ids=["M-DUP-A", "NOT-IN-WINDOW"])


def test_validate_sources_is_case_sensitive_and_lineage_strict():
    rows = (_rec("M-CASE", "content", "JMR_DUP"),)
    with pytest.raises(JMRProvenanceError):
        validate_sources(rows, source_memory_ids=["m-case"])  # 大小写敏感精确匹配

    bad_lineage = MappingProxyType(
        {
            "authority": "NOT-JME",
            "table": "memory_records",
            "memory_id": "M-LINEAGE",
            "source_candidate_id": None,
        }
    )
    bad = replace(
        _rec("M-LINEAGE", "content", "JMR_DUP"), lineage=bad_lineage
    )
    with pytest.raises(JMRProvenanceError):
        validate_sources((bad,), source_memory_ids=["M-LINEAGE"])


def test_digest_of_records_is_content_sensitive():
    """证伪恒真：内容变化必须改变摘要；同一快照必须稳定。"""
    rows = _dup_rows()
    first = digest_of_records(rows)
    assert isinstance(first, str) and len(first) == 64
    assert first == digest_of_records(rows)
    changed = (_rec("M-DUP-A", "完全不同的内容", "JMR_DUP", TS1), rows[1])
    assert digest_of_records(changed) != first


def test_source_record_from_projection_maps_14_fields_plus_lineage():
    rec = _rec("M-F", "content", "JMR_STATUS", TS2)
    assert rec.id == "M-F"
    assert rec.memory_type == "JMR_STATUS"
    assert rec.content == "content"
    assert rec.status == "ACTIVE"
    assert rec.created_at == TS2
    assert rec.lineage["authority"] == "JME"
    assert rec.lineage["table"] == "memory_records"
    assert rec.lineage["memory_id"] == "M-F"
    for attr in (
        "summary",
        "importance",
        "confidence",
        "source_id",
        "source_candidate_id",
        "updated_at",
        "expires_at",
        "metadata_json",
        "vector_status",
    ):
        assert hasattr(rec, attr)


def test_jmr_config_defaults_match_frozen_contract():
    cfg = JMRConfig()
    assert cfg.page_size == 100
    assert cfg.max_memories == 200
    assert cfg.duplicate_threshold == 0.82
    assert cfg.conflict_max_similarity == 0.80
    assert cfg.subsumption_threshold == 0.65
    assert cfg.status_min_group == 2
    assert cfg.algorithm_version == "jmr-v1"
    assert cfg.expected_cost_cny == 0.0
    assert cfg.expected_tokens == 0
    assert cfg.model_calls_per_stage == 1


def test_jmr_config_from_env_reads_jme_jmr_vars(monkeypatch):
    """JARVISMIND_* 只能在用例内 setenv（conftest sessionstart 会清空所有 JARVISMIND_*）。"""
    monkeypatch.setenv("JARVISMIND_JMR_PAGE_SIZE", "7")
    monkeypatch.setenv("JARVISMIND_JMR_DUPLICATE_THRESHOLD", "0.55")
    monkeypatch.setenv("JARVISMIND_JMR_ALGORITHM_VERSION", "jmr-v2")
    cfg = JMRConfig.from_env()
    assert cfg.page_size == 7
    assert cfg.duplicate_threshold == 0.55
    assert cfg.algorithm_version == "jmr-v2"


def test_jmr_config_from_env_fail_fast_on_invalid(monkeypatch):
    monkeypatch.setenv("JARVISMIND_JMR_PAGE_SIZE", "not-an-int")
    with pytest.raises(ValueError):
        JMRConfig.from_env()
