"""P0-03A-R1 · collection-gate regression + A-4 / A-6 end-to-end tests.

These tests exercise the real gate hook and the real AST scan on real files;
they do not assert source strings, function names or mocked return values.

All sensitive literals are assembled from fragments at runtime so that this
file itself stays clean under the tier-1 AST blacklist and the G-3 raw-text
isolation scan.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import conftest as gate  # noqa: E402
from jarvismind.database.connection import DatabaseManager  # noqa: E402


def _nodeids(request):
    return [item.nodeid for item in request.session.items]


# ---------------------------------------------------------------------------
# R1 · gate decision on the REAL collected item list
# ---------------------------------------------------------------------------
def test_gate_decision_passes_on_real_collection(request):
    """真实收集结果满足全部门槛。"""
    decision = gate.p0_gate_decide(
        _nodeids(request),
        required=gate._P0_SAFETY_CASE_COUNT,
        critical=gate._P0_CRITICAL_SAFETY_TESTS,
        remediation_required=gate._P0_REMEDIATION_CASE_COUNT,
    )
    assert decision.ok, decision.failures
    assert decision.collected == gate._P0_SAFETY_CASE_COUNT
    assert decision.remediation_collected >= gate._P0_REMEDIATION_CASE_COUNT
    assert decision.missing_critical == ()


def test_gate_decision_fails_when_safety_count_is_reduced(request):
    """人为减少安全专项用例数量 → 门禁必须失败。"""
    nodes = _nodeids(request)
    safety = [n for n in nodes if "test_startup_safety.py" in n]
    reduced = [n for n in nodes if "test_startup_safety.py" not in n] + safety[:5]
    decision = gate.p0_gate_decide(
        reduced,
        required=gate._P0_SAFETY_CASE_COUNT,
        critical=gate._P0_CRITICAL_SAFETY_TESTS,
        remediation_required=0,
    )
    assert not decision.ok
    assert any("安全专项用例不足" in f for f in decision.failures)
    assert decision.collected == 5


def test_gate_decision_detects_critical_case_renamed_with_count_kept(request):
    """删掉关键用例、用空壳凑数保持总数 → 名单检查必须发现。"""
    target = "test_g1_real_gate_blocks_all_writes"
    renamed = [n.replace(target, "test_placeholder") for n in _nodeids(request)]
    decision = gate.p0_gate_decide(
        renamed,
        required=gate._P0_SAFETY_CASE_COUNT,
        critical=gate._P0_CRITICAL_SAFETY_TESTS,
        remediation_required=0,
    )
    assert not decision.ok
    assert target in decision.missing_critical
    assert any("关键安全用例缺失" in f for f in decision.failures)


def test_gate_decision_fails_when_remediation_cases_are_dropped(request):
    nodes = [n for n in _nodeids(request) if "test_p0_remediation_" not in n]
    decision = gate.p0_gate_decide(
        nodes,
        required=gate._P0_SAFETY_CASE_COUNT,
        critical=gate._P0_CRITICAL_SAFETY_TESTS,
        remediation_required=gate._P0_REMEDIATION_CASE_COUNT,
    )
    assert not decision.ok
    assert any("整改专项用例不足" in f for f in decision.failures)


def test_gate_decision_fails_on_skip_marker(request):
    decision = gate.p0_gate_decide(
        _nodeids(request),
        required=gate._P0_SAFETY_CASE_COUNT,
        critical=gate._P0_CRITICAL_SAFETY_TESTS,
        remediation_required=0,
        skipped_markers=["tests/test_startup_safety.py::test_g2_create_app_opens_no_connection"],
    )
    assert not decision.ok
    assert decision.skipped_markers


def test_gate_is_forced_without_any_special_argument(pytestconfig, monkeypatch):
    """普通全量命令无需任何参数即强制启用门禁，且不存在可关闭它的开关。"""
    assert gate.p0_gate_is_forced(pytestconfig) is True
    # 任何试图通过环境变量绕过门禁的做法都必须无效。
    for name in ("JARVISMIND_P0_GATE_DEBUG", "PYTEST_P0_GATE_DEBUG", "P0_GATE_OFF"):
        monkeypatch.setenv(name, "1")
    assert gate.p0_gate_is_forced(pytestconfig) is True
    assert not hasattr(gate, "_P0_GATE_DEBUG_ENV"), "不得存在调试豁免开关"


def test_gate_state_reports_real_collected_numbers(pytestconfig):
    state = getattr(pytestconfig, "_p0_gate_state", None)
    assert state is not None, "收集期门禁未写入会话状态"
    assert state["forced"] is True
    assert state["collected"] == gate._P0_SAFETY_CASE_COUNT
    assert state["remediation_collected"] >= gate._P0_REMEDIATION_CASE_COUNT
    assert state["decision"].ok


def test_gate_hook_really_exits_on_filtered_items(monkeypatch):
    """真实 hook（不是纯函数）在 0 个安全用例时确实调用 pytest.exit(2)。

    使用独立 stub config，避免污染真实会话的门禁状态。
    """
    calls = []
    monkeypatch.setattr(
        gate.pytest, "exit",
        lambda msg, returncode=None, **kw: calls.append((msg, returncode)),
    )
    stub_config = SimpleNamespace(
        args=["tests"],
        getoption=lambda name, default=None: default,
        _p0_scan_violations=(),
    )
    saved_state = dict(gate._P0_GATE_STATE)
    saved_exec = dict(gate._P0_GATE_EXEC)
    try:
        gate.pytest_collection_modifyitems(
            None,
            stub_config,
            [SimpleNamespace(
                nodeid="tests/test_db.py::test_probe",
                get_closest_marker=lambda name: None,
            )],
        )
    finally:
        gate._P0_GATE_STATE.clear()
        gate._P0_GATE_STATE.update(saved_state)
        gate._P0_GATE_EXEC.clear()
        gate._P0_GATE_EXEC.update(saved_exec)
    assert calls, "门禁没有触发 pytest.exit"
    assert calls[0][1] == 2
    assert "GATE FAIL" in calls[0][0]


def test_gate_hook_detects_skip_marker_on_safety_items(monkeypatch):
    """真实 hook 检测关键安全用例被静态 skip 禁用（不是只测纯函数）。"""
    calls = []
    monkeypatch.setattr(
        gate.pytest, "exit",
        lambda msg, returncode=None, **kw: calls.append((msg, returncode)),
    )
    nodeids = [
        "tests/test_startup_safety.py::" + name
        for name in sorted(gate._P0_CRITICAL_SAFETY_TESTS)
    ]
    # G-1 / auto_drop 的参数化用例补齐到 27 例，只给其中一例挂 skip marker。
    nodeids += [
        "tests/test_startup_safety.py::test_g1_real_gate_blocks_all_writes[" + s + "]"
        for s in ("empty", "v5", "v6", "partial_v6", "full_v6_missing_marker",
                  "missing_middle", "version_too_new", "no_migrations_table")
    ]
    nodeids += [
        "tests/test_p0_remediation_gate.py::test_extra_" + str(i)
        for i in range(gate._P0_REMEDIATION_CASE_COUNT)
    ]

    class _Item:
        def __init__(self, nodeid, skipped=False):
            self.nodeid = nodeid
            self._skipped = skipped

        def get_closest_marker(self, name):
            if name == "skip" and self._skipped:
                return object()
            return None

    items = [
        _Item(n, skipped=(n.endswith("test_g2_create_app_opens_no_connection")))
        for n in nodeids
    ]
    stub_config = SimpleNamespace(
        args=["tests"],
        getoption=lambda name, default=None: default,
        _p0_scan_violations=(),
    )
    saved_state = dict(gate._P0_GATE_STATE)
    saved_exec = dict(gate._P0_GATE_EXEC)
    try:
        gate.pytest_collection_modifyitems(None, stub_config, items)
    finally:
        gate._P0_GATE_STATE.clear()
        gate._P0_GATE_STATE.update(saved_state)
        gate._P0_GATE_EXEC.clear()
        gate._P0_GATE_EXEC.update(saved_exec)
    assert calls, "skip 标记没有被门禁发现"
    assert calls[0][1] == 2
    assert "静态跳过" in calls[0][0]


# ---------------------------------------------------------------------------
# A-4 · end-to-end: real DatabaseManager construction with a non-sandbox URL
# ---------------------------------------------------------------------------
def test_a4_end_to_end_production_path_manager_is_refused_before_init(
    settings, monkeypatch
):
    """真实构造 DatabaseManager 指向生产样式路径 → 在真实 __init__ 之前被拒绝。

    断言的是行为：抛 P0DbPathError、真实 __init__ / create_engine / mkdir
    一次都没被调用，且没有异常被吞掉伪装成拒绝成功。
    """
    prod_url = "sqlite+pysqlite:///" + "/".join(
        ("D:", "JarvisMind", "jme", "data", "jarvismind.db")
    )
    settings_outside = replace(settings, database_url=prod_url)

    counter = {"real_init": 0, "create_engine": 0, "mkdir": 0}

    def boom(*args, **kwargs):
        raise AssertionError("禁止的调用被触发")

    import jarvismind.database.connection as connection_module

    def boom_init(*args, **kwargs):
        counter["real_init"] += 1
        raise AssertionError("真实 __init__ 被调用")

    monkeypatch.setattr(gate, "_P0_REAL_DBM_INIT", boom_init)
    monkeypatch.setattr(connection_module, "create_engine", boom)
    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(gate.P0DbPathError) as exc:
        DatabaseManager(settings_outside)
    assert "白名单" in str(exc.value)
    assert counter == {"real_init": 0, "create_engine": 0, "mkdir": 0}


# ---------------------------------------------------------------------------
# A-6 · end-to-end: a REAL violating file is caught by the real session scan
# ---------------------------------------------------------------------------
def test_a6_end_to_end_real_violation_file_is_fail_closed(tmp_path, monkeypatch):
    """真实写入违规 .py → 真实 p0_scan_tree → 真实豁免判定 → 会话 fail-closed。"""
    tree = tmp_path / "scan_tree"
    tests_dir = tree / "tests"
    tests_dir.mkdir(parents=True)
    violating = tests_dir / "smuggled_probe.py"
    violating.write_text("import " + "scratch" + "_check\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        gate.pytest, "exit",
        lambda msg, returncode=None, **kw: calls.append((msg, returncode)),
    )
    config = SimpleNamespace()
    gate._p0_run_session_scan(config, root=tree)

    assert calls, "真实违规文件没有被会话扫描拦截"
    assert calls[0][1] == 2
    assert "smuggled_probe.py" in calls[0][0]
    rules = {v["rule"] for v in config._p0_scan_violations}
    assert "import:" + "scratch_module" in rules
    assert not gate._p0_is_exempt(
        {"filename": "tests/smuggled_probe.py",
         "rule": "import:" + "scratch_module"}
    ), "硬规则绝不可被白名单豁免"


def test_a6_end_to_end_clean_tree_does_not_fail_session(tmp_path, monkeypatch):
    """对照组：干净真实文件不会触发 fail-closed（防止门禁无条件乱杀）。"""
    tree = tmp_path / "clean_tree"
    tests_dir = tree / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "harmless.py").write_text("value = 1\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        gate.pytest, "exit",
        lambda msg, returncode=None, **kw: calls.append((msg, returncode)),
    )
    config = SimpleNamespace()
    gate._p0_run_session_scan(config, root=tree)
    assert calls == []
    assert config._p0_scan_violations == ()
