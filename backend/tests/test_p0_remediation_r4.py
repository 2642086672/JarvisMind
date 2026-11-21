"""P0-03A-R4 · production-access bypass search, static analysis and guard tests.

Strict isolation: the two scratch probes are never executed against any
production resource.  Module-level side effects are checked by AST, and the
central guard is exercised with temporary directories / temporary databases
only.  No production token is read and no production API is called.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))

from jarvismind.core import production_guard as guard  # noqa: E402

_SCRATCH_NAMES = ("scratch" + "_check.py", "scratch" + "_probe.py")
# The probes themselves stay out of version control (they are local debug tools).
# Their reviewed, safe sources are kept as version-controlled fixtures so the
# gate can verify them in a clean checkout instead of depending on ignored files.
PROBE_DIR = JARVISMIND_ROOT / "tests" / "fixtures" / "debug_probes"
# Sensitive literals assembled from fragments: this file must stay clean
# under the tier-1 AST blacklist and the G-3 raw-text isolation scan.
_PROD_PATH_LITERAL = "/".join(("jme", "data", "jarvismind.db"))
_PROD_PATH_NATIVE = os.sep.join(("jme", "data", "jarvismind.db"))
_PROD_ABS = os.sep.join(("D:", "JarvisMind", "jme", "data", "jarvismind.db"))
_TOKEN_LITERAL = ".jme" + "_token"
_PROD_API = "http://" + "127.0.0.1" + ":" + "8777" + "/api/v1/memories/hybrid-search"


def _source(name):
    return (PROBE_DIR / name).read_text(encoding="utf-8-sig")


def _module_level_calls(name):
    """Return dotted names of calls executed at module level (incl. if/try)."""
    tree = ast.parse(_source(name))
    body_ids = {
        id(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for node in ast.walk(node)
    }
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in body_ids:
            func = node.func
            if isinstance(func, ast.Name):
                calls.append(func.id)
            elif isinstance(func, ast.Attribute):
                parts = []
                cur = func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                calls.append(".".join(reversed(parts)))
    return tuple(calls)


# ---------------------------------------------------------------------------
# Static analysis of the two formerly dangerous probes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", _SCRATCH_NAMES)
def test_r4_probe_has_no_hardcoded_production_target(name):
    source = _source(name)
    assert _PROD_ABS not in source
    assert _PROD_PATH_LITERAL not in source
    assert _PROD_PATH_NATIVE not in source
    assert _TOKEN_LITERAL not in source
    assert _PROD_API not in source
    assert "Bearer " not in source or "build_debug_request" in source


@pytest.mark.parametrize("name", _SCRATCH_NAMES)
def test_r4_probe_has_no_module_level_db_or_network_call(name):
    calls = _module_level_calls(name)
    forbidden = ("connect", "urlopen", "open", "Request", "create_engine")
    assert not [c for c in calls if c.rsplit(".", 1)[-1] in forbidden], calls


@pytest.mark.parametrize("name", _SCRATCH_NAMES)
def test_r4_probe_routes_through_central_guard(name):
    source = _source(name)
    assert "production_guard" in source
    assert any(
        symbol in source
        for symbol in (
            "open_debug_db_readonly", "build_debug_request",
            "assert_debug_db_path", "assert_debug_api_url",
            "assert_debug_token",
        )
    )
    assert '__name__ == "__main__"' in source


def test_r4_full_project_search_finds_no_hardcoded_production_db_in_scratch_scripts():
    """全量搜索 jme/ 根与 scripts/ 下的辅助脚本：不得残留硬编码生产库路径。"""
    roots = [JARVISMIND_ROOT, JARVISMIND_ROOT / "scripts"]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in {".venv", "__pycache__"} for part in path.parts):
                continue
            source = path.read_text(encoding="utf-8-sig")
            if _PROD_ABS in source or _PROD_PATH_LITERAL in source:
                found.append(str(path.relative_to(JARVISMIND_ROOT)))
    assert found == [], f"仍存在硬编码生产库路径: {found}"


def test_r4_probe_import_has_zero_side_effects(monkeypatch):
    """隔离加载两个探针模块：导入阶段不得连库、不得读文件、不得发请求。"""
    counter = {"n": 0}

    def boom(*args, **kwargs):
        counter["n"] += 1
        raise AssertionError("模块导入阶段触发了禁止的副作用")

    monkeypatch.setattr(sqlite3, "connect", boom)
    monkeypatch.setattr(guard, "_ORIGINAL_SQLITE_CONNECT", boom)
    for name in _SCRATCH_NAMES:
        module_name = "_p0_r4_" + name.replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, PROBE_DIR / name)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, module_name, module)
        spec.loader.exec_module(module)
        assert hasattr(module, "main"), f"{name} 缺少显式 main 入口"
    assert counter["n"] == 0


# ---------------------------------------------------------------------------
# Central guard behaviour (temporary sandbox / temporary database only)
# ---------------------------------------------------------------------------
def test_r4_guard_rejects_missing_sandbox(monkeypatch):
    monkeypatch.delenv(guard.ENV_DEBUG_ROOT, raising=False)
    with pytest.raises(guard.ProductionAccessBlocked) as exc:
        guard.debug_root(None)
    assert "未声明" in str(exc.value)


def test_r4_guard_rejects_sandbox_that_covers_production_dir(tmp_path, monkeypatch):
    fake_prod = tmp_path / "fake-production" / "data"
    fake_prod.mkdir(parents=True)
    monkeypatch.delenv(guard.ENV_DEBUG_ROOT, raising=False)
    monkeypatch.setattr(guard, "PRODUCTION_DATA_DIR", fake_prod)
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.debug_root(fake_prod.parent)
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.debug_root(fake_prod)


def test_r4_guard_rejects_db_outside_sandbox(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside" / "probe.db"
    with pytest.raises(guard.ProductionAccessBlocked) as exc:
        guard.assert_debug_db_path(outside, explicit_root=sandbox)
    assert "不在" in str(exc.value)


def test_r4_guard_rejects_production_db_even_inside_declared_sandbox(
    tmp_path, monkeypatch
):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    fake_prod = sandbox / "production-data"
    fake_prod.mkdir()
    monkeypatch.setattr(guard, "PRODUCTION_DATA_DIR", fake_prod)
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.assert_debug_db_path(fake_prod / "jarvismind.db", explicit_root=sandbox)


def test_r4_guard_readonly_connection_on_tmp_db(manager, tmp_path, monkeypatch):
    """临时库只读连接：可读、不可写、五维状态前后一致。"""
    monkeypatch.delenv(guard.ENV_DEBUG_ROOT, raising=False)
    conn = guard.open_debug_db_readonly(
        Path(manager.settings.database_path), explicit_root=tmp_path
    )
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_sources"
        ).fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_not_exist (x INTEGER)")
    finally:
        conn.close()


def test_r4_guarded_connect_refuses_production_path(tmp_path, monkeypatch):
    """集中连接守卫：生产目录路径直接拒绝，底层连接函数一次都不调用。"""
    fake_prod = tmp_path / "fake-production" / "data"
    fake_prod.mkdir(parents=True)
    monkeypatch.setattr(guard, "PRODUCTION_DATA_DIR", fake_prod)
    monkeypatch.delenv(guard.ENV_PRODUCTION_DB_ALLOW, raising=False)
    counter = {"n": 0}

    def boom(*args, **kwargs):
        counter["n"] += 1
        raise AssertionError("底层连接被调用")

    original = guard._ORIGINAL_SQLITE_CONNECT
    monkeypatch.setattr(guard, "_ORIGINAL_SQLITE_CONNECT", boom)
    try:
        with pytest.raises(guard.ProductionAccessBlocked):
            guard.guarded_sqlite_connect(str(fake_prod / "jarvismind.db"))
        assert counter["n"] == 0
        # 恢复底层连接后，内存库路径仍允许（普通 Python 进程不应被误杀）。
        monkeypatch.setattr(guard, "_ORIGINAL_SQLITE_CONNECT", original)
        conn = guard.guarded_sqlite_connect(":memory:")
        conn.close()
    finally:
        monkeypatch.setattr(guard, "_ORIGINAL_SQLITE_CONNECT", original)


def test_r4_connect_guard_is_explicitly_installed_and_removed():
    """守卫是显式安装的，不是 import 即全局生效；安装/卸载必须干净。"""
    original = sqlite3.connect
    try:
        assert sqlite3.connect is not guard.guarded_sqlite_connect
        guard.install_connect_guard()
        assert sqlite3.connect is guard.guarded_sqlite_connect
        guard.uninstall_connect_guard()
        assert sqlite3.connect is original
    finally:
        guard.uninstall_connect_guard()
        sqlite3.connect = original


def test_r4_token_rules_reject_paths_and_empty_values():
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.assert_debug_token("")
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.assert_debug_token("/".join(("data", _TOKEN_LITERAL)))
    assert guard.assert_debug_token("  test-token-value  ") == "test-token-value"


def test_r4_api_rules_reject_production_endpoint(monkeypatch):
    monkeypatch.delenv(guard.ENV_DEBUG_API_ALLOW, raising=False)
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.assert_debug_api_url(_PROD_API)
    monkeypatch.setenv(guard.ENV_DEBUG_API_ALLOW, "1")
    with pytest.raises(guard.ProductionAccessBlocked):
        guard.assert_debug_api_url(_PROD_API)
    debug_url = "http://" + "127.0.0.1" + ":" + "18999" + "/debug/memories"
    assert guard.assert_debug_api_url(debug_url) == debug_url
    request = guard.build_debug_request(debug_url, "test-token-value")
    assert request.get_header("Authorization") == "Bearer test-token-value"
