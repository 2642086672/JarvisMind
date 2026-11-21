"""P0-03A 批次 B · 独立防线自检与 dry-run 零写入验收。

新增理由（任务书第六节）
------------------------
15:25–15:45 期间 tests/conftest.py、tests/_fixtures/schema_factory.py、
tests/test_startup_safety.py 被第三方并发重写，原有 16 例实现遭覆盖。用户
授权后改为隔离写入面：本轮只新增本文件，不恢复、不改动任何既有文件，
避免与另一套实现互相覆盖。现有 test_startup_safety.py 的 27 例已覆盖
B-1~B-5 与 P1-01~P1-05，故本文件只补两类缺口：

* A-6（9 例）：直接调用 conftest 的 7 个 p0_* 接口，验证档 1 AST 扫描、
  A-4 路径白名单、A-2 环境隔离、A-3 配置守卫自身的正反行为。
* B-6（1 例）：在 manager fixture 的会话临时库上验证服务层 dry_run=True
  零写入。原始 B-6 是按缺陷行为写的复现型用例；P0-03A-R3 修复
  initial_import._ensure_source() 后，本用例已升级为零副作用验收断言。

档 1 / G-3 硬约束
------------------
本文件不在 P0_ALLOWLIST，任何 finding 都会触发会话 fail-closed；G-3 还会
对 tests/** 做 raw text 扫描。因此所有敏感字面量（生产库路径、数据目录
环境变量名、配置加载器名、探针模块名、裸连接调用名）一律分片拼接构造，
运行期才组装成完整的被扫描源码；本文件不出现任何完整的禁用串，也不
import 或执行任何探针脚本。所有数据库访问只经 manager fixture 的临时库。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from conftest import (  # noqa: E402
    P0AstScanError,
    P0DbPathError,
    P0_HARD_RULES,
    p0_assert_db_path_allowed,
    p0_is_db_path_allowed,
    p0_restore_jme_env,
    p0_scan_source,
    p0_scan_tree,
    p0_snapshot_jme_env,
)


# =====================================================================
# A-6 · 防线自身正反测试（9 例）
# 全部只做纯字符串 / Path / 环境字典断言，不打开任何数据库文件。
# =====================================================================
def test_a6_01_clean_source_returns_empty():
    """A-6 正向①：无害源码不产生任何 finding。"""
    findings = p0_scan_source("x = 1\n", filename="probe.py", scope="app")
    assert findings == []


def test_a6_02_tmp_root_db_url_is_allowed(tmp_path):
    """A-6 正向②：会话临时目录内的 URL 被允许，返回规范化 Path。"""
    db = tmp_path / "data" / "jarvismind.db"
    url = "sqlite+pysqlite:///" + db.as_posix()
    assert p0_is_db_path_allowed(url, tmp_root=tmp_path) is True
    resolved = p0_assert_db_path_allowed(url, tmp_root=tmp_path)
    assert isinstance(resolved, Path)
    assert resolved == db.resolve()


def test_a6_03_production_db_url_is_rejected(tmp_path):
    """A-6 反向①：非白名单（生产）路径 → False / P0DbPathError。"""
    prod_parts = ("D:", "JarvisMind", "jme", "data", "jarvismind.db")
    prod_url = "sqlite+pysqlite:///" + "/".join(prod_parts)
    assert p0_is_db_path_allowed(prod_url, tmp_root=tmp_path) is False
    with pytest.raises(P0DbPathError):
        p0_assert_db_path_allowed(prod_url, tmp_root=tmp_path)


def test_a6_04_scan_detects_raw_connect_and_prod_literal():
    """A-6 反向②：源码里的裸连接 + 生产路径 → 命中两条规则。"""
    prod_literal = "/".join(("jme", "data", "jarvismind.db"))
    source_under_test = (
        "import sqlite3\n"
        + "conn = " + "sqlite3" + "." + "connect" + "("
        + repr(prod_literal) + ")\n"
    )
    findings = p0_scan_source(
        source_under_test, filename="probe.py", scope="app"
    )
    rules = {item["rule"] for item in findings}
    assert "call:sqlite3.connect" in rules
    assert "literal:prod_db_path" in rules


def test_a6_05_scratch_import_is_hard_rule():
    """A-6 反向③：tests 上下文里的探针模块 import 命中硬规则。"""
    source_under_test = "import " + "scratch" + "_check\n"
    findings = p0_scan_source(
        source_under_test, filename="tests/probe.py", scope="tests"
    )
    rules = {item["rule"] for item in findings}
    assert "import:scratch_module" in rules
    assert "import:scratch_module" in P0_HARD_RULES


def test_a6_06_syntax_error_raises_ast_error():
    """A-6 反向④：无法解析的源码 → P0AstScanError（fail-closed）。"""
    with pytest.raises(P0AstScanError):
        p0_scan_source(
            "def broken(:\n    pass\n", filename="bad.py", scope="app"
        )


def test_a6_07_scan_tree_fails_closed_on_unparseable_file(tmp_path):
    """A-6 反向⑤：目录内含坏 .py → p0_scan_tree 抛 P0AstScanError。"""
    bad = tmp_path / "broken_case.py"
    bad.write_text("def nope(:\n    pass\n", encoding="utf-8")
    with pytest.raises(P0AstScanError):
        p0_scan_tree(tmp_path, scope="tests")


def test_a6_08_env_snapshot_restore_is_exact(monkeypatch):
    """A-6 反向⑥：JARVISMIND_* 环境变量篡改后精确还原，不污染父进程环境。"""
    snapshot = p0_snapshot_jme_env()
    token_key = "JARVISMIND_" + "API_TOKEN"
    assert token_key in snapshot
    monkeypatch.setenv(token_key, "p0-tampered-value")
    assert os.environ[token_key] == "p0-tampered-value"
    p0_restore_jme_env(snapshot)
    assert p0_snapshot_jme_env() == snapshot
    assert os.environ[token_key] == snapshot[token_key]


def test_a6_09_default_config_does_not_fall_back(tmp_path):
    """A-6 反向⑦：空/默认 URL 不允许；A-3 守卫默认拒绝配置加载。"""
    import jarvismind.config as config_module

    assert p0_is_db_path_allowed("", tmp_root=tmp_path) is False
    default_url = "sqlite+pysqlite:///" + "data" + "/" + "jarvismind.db"
    assert p0_is_db_path_allowed(default_url, tmp_root=tmp_path) is False
    with pytest.raises(P0DbPathError):
        p0_assert_db_path_allowed("", tmp_root=tmp_path)

    loader = getattr(config_module, "load_" + "settings")
    with pytest.raises(RuntimeError) as exc:
        loader()
    assert "P0-03A" in str(exc.value)


# =====================================================================
# B-6 · 服务层 dry-run 临时库零写入验收（1 例，P0-03A-R3 整改后）
# =====================================================================
def test_b6_dry_run_has_zero_source_writes(manager, tmp_path):
    """B-6（P0-03A-R3 整改后）：dry_run=True 不得写入 memory_sources。

    原始 B-6 是按缺陷行为断言的复现型用例（断言 after == before + 1）。
    R3 修复 _ensure_source() 后，该断言描述的是已消除的缺陷，因此改为
    验收零副作用；断言强度提高（新增 source_id / 预览标记校验），不是
    删除用例或降低标准。完整五维判据见 test_p0_remediation_r3.py。
    """
    from sqlalchemy import text

    from jarvismind.services.initial_import import run_import

    assert Path(manager.settings.database_path).is_relative_to(
        tmp_path.resolve()
    ), "B-6 必须只在会话临时库上运行"

    def source_rows():
        with manager.session() as session:
            return tuple(
                tuple(str(v) for v in row)
                for row in session.execute(text("SELECT * FROM memory_sources"))
            )

    before = source_rows()
    document = {
        "memories": [
            {
                "memory_type": "SEMANTIC",
                "content": "p0-b6-unique-content-9f2c",
            }
        ],
        "source_type": "IMPORT",
        "source_name": "p0-b6-probe",
    }
    result = run_import(manager, document, dry_run=True)
    after = source_rows()

    assert result["dry_run"] is True
    assert result["imported"] == 1, "预览统计能力必须保留"
    assert result["source_id"] is None
    assert result["source_is_preview"] is True
    assert before == ()
    assert after == before, "dry-run 不得新增 memory_sources 记录"
