"""Shared pytest fixtures, the P0-03 session guard, and the P0-03A test-isolation
defence line (batch A).

Fixtures build every database under ``tmp_path`` and never open the on-disk
production database.  An autouse fixture redirects the (blocked) production
migration entry points to the test-only schema factory so legacy tests keep
working; cases marked ``real_migration_gate`` skip that redirect and exercise
the real first-stage gate.

P0-03A additions (this file only):

* A-1  Two-tier ``ast.parse`` scan of every project ``*.py`` under ``jme/``.
       Tier 1 (``tests/**``) is fail-closed; tier 2 (``jme/`` root, ``app/**``,
       ``scripts/**``) is recorded as a known-risk list and printed, but never
       fails the session merely because a risky file exists.  The fail-closed
       part of tier 2 is the *import ban*: any test module importing the two
       scratch probes aborts the session.
* A-2  Snapshot / isolate / restore every ``JARVISMIND_*`` process environment value.
* A-3  Session-level guard on the configuration loader with an explicit
       ``allow_load_settings`` opt-in marker.
* A-4  Session-level path whitelist on ``DatabaseManager.__init__``.

IMPORTANT SCOPE STATEMENT
-------------------------
The AST blacklist in this file is a **test-phase defence only**.  It inspects
source text; it does not sandbox the interpreter.  A normal Python process that
does ``import scratch_check`` still executes that module and can still open the
production database.  Static scanning cannot prevent that -- it can only make
the *test suite* fail-closed when a test module contains such an import, and
record the risk for human review.  Likewise, dynamic calls built through
``getattr`` / ``importlib`` / string concatenation / ``exec`` are outside the
detection boundary and are explicitly listed as such in ``P0_RULES`` below.
No claim of complete coverage is made.

P0-03A-R1 整改后：收集门禁无条件强制（不存在 forced=False / 豁免路径），增加关键
用例名单、执行期 outcome 对账与跳过检测，新增 test_p0_remediation_*.py。

批次 B 已知未达项（本次仍未实现，登记于风险登记册）：

* 类级 ``Engine "connect"`` 事件路径检查：未实现。
* 档2 已知风险改 fail-closed：未实现（档2 仍只记录、不阻断会话）。
* ``--noconftest`` 使本文件根本不加载，代码层无法自防（进程级边界，见 R4）。

``--noconftest`` 会让 conftest 根本不加载，代码层面无法自防；R-4 的
检查只覆盖 confcutdir 部分，以及 PYTEST_ADDOPTS / PYTEST_PLUGINS 中
显式注入该开关的可观测痕迹，不能覆盖「命令行直接传 --noconftest」。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import jarvismind.config as _p0_config_module  # noqa: E402
from jarvismind.config import Settings  # noqa: E402
from jarvismind.database.connection import DatabaseManager  # noqa: E402
from jarvismind.main import create_app  # noqa: E402
from _fixtures import schema_factory  # noqa: E402

TEST_TOKEN = "test-token-0123456789"

# R-2: 安全专项的用例总数（计数判定，不是 nodeid 白名单）。
# 构成：G-1 8 参数化 + auto_drop 2 参数化 + G-2 / G-3 / G-4 各 1
#       + 批次 B 新增 14 例（P1-01 2 / P1-02 2 / P1-03 2 / P1-04 4 / P1-05 4）
#       = 8 + 2 + 3 + 14 = 27
_P0_SAFETY_CASE_COUNT = 27

# P0-03A-R1: 关键安全用例名单。仅靠数量下限无法发现「删掉一个关键用例、
# 补一个空壳用例凑数」，因此额外要求下列函数名全部出现在收集结果里。
# 参数化用例的 [param] 后缀会在比较前剥离。
_P0_CRITICAL_SAFETY_TESTS = frozenset(
    {
        "test_g1_real_gate_blocks_all_writes",
        "test_auto_drop_is_blocked",
        "test_g2_create_app_opens_no_connection",
        "test_g3_static_isolation",
        "test_g4_session_guard_enabled_and_stable",
        "test_init_db_main_exits_2_with_policy_text",
        "test_init_db_main_has_zero_file_side_effects",
        "test_import_script_dry_run_five_way_zero_side_effects",
        "test_import_script_dry_run_never_reaches_config_or_db",
        "test_export_script_never_initializes_or_rebuilds",
        "test_export_script_runs_only_on_tmp_db",
        "test_health_connect_and_session_call_counts",
        "test_health_does_not_create_schema_migrations",
        "test_health_fires_global_connect_pragmas",
        "test_health_degraded_still_opens_session",
        "test_g5_lazy_import_resolves_to_stand_in",
        "test_g5_new_module_imported_after_setup_is_covered",
        "test_g5_alias_binding_boundary_is_documented_and_banned",
        "test_g5_autouse_fixture_precedes_lazy_fixture",
    }
)

# P0-03A-R1: 门禁无条件强制，不提供任何豁免开关。任何环境变量、命令行
# 参数或单文件调试都不能关闭它；一旦关闭就构成「人为绕过」，与验收标准
# 直接冲突。单文件调试请使用 -k 保留安全套件，或独立进程 + --noconftest
# （后者是已登记的进程级边界，见风险登记册）。

# P0-03A-R1: exact collected count of the new remediation regression cases.
# Any deletion / deselection of one of them fails the gate.
_P0_REMEDIATION_CASE_COUNT = 40

# ===========================================================================
# A-1 · AST scan: constants, rules, allowlist, pure scanning functions
# ===========================================================================

# Environment variable prefix and the individual literal fragments.  Keeping
# the fragments separate (rather than writing the concatenated name) is
# deliberate: the tier-1 scan also reads THIS file, and G-3 in
# test_startup_safety.py rejects the concatenated literal anywhere in tests/**.
_P0_ENV_PREFIX = "JARVISMIND_"
_P0_ENV_DATA_DIR = "JARVISMIND_" + "DATA_DIR"
_P0_ENV_API_TOKEN = "JARVISMIND_" + "API_TOKEN"
_P0_TOKEN_LITERAL = ".jme" + "_token"
_P0_API_HOSTPORT = "127.0.0.1" + ":" + "8777"
_P0_LOAD_SETTINGS_NAME = "load_" + "settings"
_P0_GUARD_MESSAGE = "P0-03A: 测试默认禁止 " + _P0_LOAD_SETTINGS_NAME

# Hardcoded production path markers.  Built from fragments so this file does
# not itself contain any banned production-path literal (G-3 checks raw text).
_P0_PROD_PATH_MARKERS = tuple(
    dict.fromkeys(
        (
            "/".join(("jme", "data", "jarvismind.db")),
            os.sep.join(("jme", "data", "jarvismind.db")),
            "/".join(("JarvisMind", "jme", "data", "jarvismind.db")),
            os.sep.join(("JarvisMind", "jme", "data", "jarvismind.db")),
            "/".join(("JarvisMind", "jme", "data")) + "/",
            os.sep.join(("JarvisMind", "jme", "data")) + os.sep,
        )
    )
)

# Directories pruned while walking the tree.  These are third-party / cache
# trees, NOT a detection allowlist: every project source file (jme/ root,
# app/**, scripts/**, tests/**, tests/_fixtures/**) is still scanned.  Pruning
# is required because jme/.venv contains thousands of installed-package .py
# files that are not project source.
P0_SCAN_EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "site-packages",
        "node_modules",
        ".git",
        ".pytest_cache",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "build",
        "dist",
    }
)

# Rule catalogue.  ``boundary`` states explicitly what the rule cannot see.
# Detection is name/AST based only: getattr(...), importlib.import_module(...),
# dynamically assembled paths, exec/eval and alias imports are NOT covered.
P0_RULES: tuple[dict[str, str], ...] = (
    {
        "id": "call:sqlite3.connect",
        "desc": "直接 sqlite3.connect(...) 调用（绕开 DatabaseManager）",
        "boundary": "仅识别 sqlite3.connect 与 from sqlite3 import connect 的直接名字；"
        "getattr/importlib/别名重绑定/字符串拼接的目标不覆盖",
    },
    {
        "id": "call:create_engine",
        "desc": "直接 create_engine(...) 调用（另一个 Engine 实例）",
        "boundary": "仅识别名字为 create_engine 的调用；动态导入不覆盖",
    },
    {
        "id": "call:db_manager",
        "desc": "DatabaseManager(...) 构造（连接工厂调用点）",
        "boundary": "仅识别名字为 DatabaseManager 的调用；运行时路径由 A-4 白名单强制校验",
    },
    {
        "id": "call:load_settings",
        "desc": "调用默认配置加载器（可能读到生产目录 / 写 token 文件）",
        "boundary": "仅识别名字为 load_settings 的调用；getattr/importlib 不覆盖",
    },
    {
        "id": "literal:prod_db_path",
        "desc": "硬编码生产库 / 生产数据目录路径字面量",
        "boundary": "仅扫描字符串常量；路径拼接、Path(...)/... 分段构造、环境变量展开不覆盖",
    },
    {
        "id": "literal:jme_data_dir",
        "desc": "数据目录环境变量名字面量",
        "boundary": "仅扫描字符串常量；拼接构造（如 'JARVISMIND_'+'DATA_DIR'）不覆盖",
    },
    {
        "id": "literal:jme_token",
        "desc": "token 文件名字面量",
        "boundary": "仅扫描字符串常量；拼接构造不覆盖",
    },
    {
        "id": "literal:prod_api_url",
        "desc": "生产 API 地址字面量（本机服务 + 端口）",
        "boundary": "仅扫描字符串常量与 f-string 常量片段；动态拼 host/port 不覆盖",
    },
    {
        "id": "module_level_db_call",
        "desc": "模块级（非函数体内）执行的数据库调用",
        "boundary": "仅识别 AST 顶层语句中的 Call；装饰器/默认参数内的调用按函数内处理，"
        "if/try 内的顶层语句仍可识别；动态调用不覆盖",
    },
    {
        "id": "call:dangerous_file",
        "desc": "危险文件写入 / 删除调用",
        "boundary": "仅在「模块级执行」或「参数含生产路径标记」时命中；"
        "用例体内对 tmp_path 的正常写入属受制裁行为，不算违规",
    },
    {
        "id": "import:subprocess",
        "desc": "测试代码引入子进程 / 多进程能力（可能继承生产环境变量）",
        "boundary": "仅 tests 上下文生效；仅识别 import / from-import / os.system / os.popen / "
        "subprocess.* / multiprocessing.* 名字；动态导入不覆盖",
    },
    {
        "id": "import:scratch_module",
        "desc": "测试代码 import 了会直连生产库或读取生产凭证的 scratch 探针",
        "boundary": "仅 tests 上下文生效；硬规则，不可白名单豁免；"
        "通过 importlib / 字符串模块名导入不覆盖",
    },
)

P0_RULE_IDS = frozenset(rule["id"] for rule in P0_RULES)
# Tests-only rules: never evaluated outside a tests context.
P0_TESTS_ONLY_RULES = frozenset({"import:subprocess", "import:scratch_module"})
# Hard rules: the allowlist can never exempt these.
P0_HARD_RULES = frozenset({"import:scratch_module"})

_P0_SCRATCH_MODULES = frozenset({"scratch_check", "scratch_probe"})
_P0_SUBPROCESS_MODULES = frozenset({"subprocess", "multiprocessing"})
_P0_DANGEROUS_FILE_CALLS = frozenset(
    {"unlink", "write_text", "write_bytes", "mkdir", "rmtree", "remove"}
)
_P0_DB_CALL_TAILS = frozenset(
    {
        "connect", "create_engine", "DatabaseManager", "load_settings",
        "execute", "executemany", "session", "cursor", "commit",
        "rollback", "begin", "dispose", "verify_connection",
    }
)


class P0AstScanError(RuntimeError):
    """Raised when a source file cannot be read or parsed (fail-closed)."""


class P0DbPathError(RuntimeError):
    """Raised when a database URL does not resolve under the session tmp root."""


# Whitelist: one entry per (concrete file, concrete rule).  No directory-wide
# exemptions.  ``status`` is either "exempt" (tier-1 finding suppressed) or
# "known_risk" (documented, still reported in the known-risk list, NOT exempt).
#
# R-1: entries may carry an OPTIONAL ``symbols`` field.  When present, the
# (file, rule) exemption is further narrowed to the named enclosing functions;
# a finding in any other function is NOT exempt (strict fail-closed, no
# ``continue`` fallback).  Entries without ``symbols`` keep the original
# (file, rule) granularity.
P0_ALLOWLIST: tuple[dict[str, Any], ...] = (
    {
        "file": "tests/_fixtures/schema_factory.py",
        "rule": "call:sqlite3.connect",
        "status": "exempt",
        "symbols": (
            "_connect",
            "_p0_ro_conn",
            "p0_safe_ro_connect",
            "p0_make_ro_snapshot_copy",
        ),
        "reason": "仅放行上述具名函数内的裸 sqlite3.connect：临时库工厂连接 "
        "(_connect)、只读 URI 连接 (_p0_ro_conn / p0_safe_ro_connect)、"
        "tmp root 内只读快照复制 (p0_make_ro_snapshot_copy)；"
        "其余函数若新增裸连接一律 fail-closed 拒绝",
    },
    {
        "file": "tests/test_startup_safety.py",
        "rule": "call:sqlite3.connect",
        "status": "exempt",
        "symbols": ("_ro_conn",),
        "reason": "仅允许 _ro_conn 薄封装内的 file:...?mode=ro 只读 URI 连接，"
        "用于采集临时库 sqlite_master / schema_version 快照；"
        "其它函数新增裸连接一律 fail-closed 拒绝",
    },
    {
        "file": "tests/test_backup_restore.py",
        "rule": "call:sqlite3.connect",
        "status": "exempt",
        "reason": "在 tmp_path 下构造无关空库，用于验证 verify_backup 拒绝损坏 / 缺表文件",
    },
    {
        "file": "tests/conftest.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "manager fixture 用 settings fixture 的 tmp_path 库；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_db.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "全部基于 settings fixture 的 tmp_path 库；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_startup_safety.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "8 场景各自 tmp_path 独立库；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_index_lifecycle.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "自建 tmp_path Settings；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_hybrid_search.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "自建 tmp_path Settings；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_security_fix_b1.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "dataclasses.replace(settings) 改到 tmp_path 子目录；A-4 运行时校验路径",
    },
    {
        "file": "tests/test_lan_test_page.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "reason": "lan_client fixture 自建 tmp_path/lan-data；A-4 运行时校验路径",
    },
    {
        # P0-03A-R1: A-4 端到端反向用例必须真实构造 DatabaseManager 才能
        # 验证会话级路径守卫在真实 __init__ 之前拦截。仅放行这一个具名
        # 函数，symbols 精确收窄；该文件其它函数新增构造一律 fail-closed。
        "file": "tests/test_p0_remediation_gate.py",
        "rule": "call:db_manager",
        "status": "exempt",
        "symbols": (
            "test_a4_end_to_end_production_path_manager_is_refused_before_init",
        ),
        "reason": "A-4 端到端反向用例：真实构造指向会话白名单外的 DatabaseManager，"
        "断言抛 P0DbPathError 且真实 __init__/create_engine/mkdir 零触发；"
        "不建立任何数据库连接，不触碰生产库",
    },
    {
        # JKG-INT-01（主代理复核整改三）：全新解释器导入用例必须真的启动子进程
        # 才能证明「导入 app.main / app.container / app.jkg 不建 JKG 对象」。
        # 本条只绑定那一个具名测试函数：该文件里其它任何函数新增 subprocess
        # 调用仍然 fail-closed 拒绝。subprocess 也只在函数体内局部导入，因此
        # 模块级不存在 subprocess 导入可被连带放行。子进程的 env 已剥除全部
        # JARVISMIND_* 变量、cwd 固定为 jme/、数据目录指向 tmp_path、不继承生产配置。
        "file": "tests/test_upgrade_to_jkg.py",
        "rule": "import:subprocess",
        "status": "exempt",
        "symbols": ("test_fresh_interpreter_import_creates_no_jkg_object",),
        "reason": "JKG-INT-01 全新解释器导入用例：subprocess 只用于以 "
        "sys.executable -c 导入 app 包并断言零 JKG 对象；env 剥除全部 JARVISMIND_* 变量、"
        "cwd=jme/、timeout=300，目标数据目录为 tmp_path；不连接生产库",
    },
    {
        "file": "app/core/production_guard.py",
        "rule": "call:sqlite3.connect",
        "status": "known_risk",
        "reason": "P0-03A-R4 集中运行时守卫：唯一允许打开调试只读库的裸连接点，"
        "打开前强制 assert_debug_db_path() 校验临时沙箱并拒绝生产数据目录，"
        "且以 mode=ro 打开；标为已知风险，不豁免",
    },
    {
        "file": "app/database/migrations.py",
        "rule": "call:sqlite3.connect",
        "status": "known_risk",
        "reason": "inspect_schema() 用 file:...?mode=ro 只读 URI 检视；"
        "已知会打开库文件且可能触碰 -shm，标为已知风险，不豁免",
    },
    {
        "file": "app/database/backup.py",
        "rule": "call:sqlite3.connect",
        "status": "known_risk",
        "reason": "备份 / 恢复链路既有裸连接（可写打开源库）；"
        "P0-03A 批次 C 排除项，标为已知风险，不豁免",
    },
)


def _p0_call_name(node: ast.AST) -> str:
    """Best-effort dotted name for a Call's func (name / attribute chain only)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _p0_call_name(node.value)
        return (base + "." + node.attr) if base else node.attr
    return ""


def _p0_tail(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _p0_is_tests_context(filename: str, scope: str) -> bool:
    """True when a finding belongs to the tier-1 (tests) context."""
    normalised = filename.replace("\\", "/")
    return scope == "tests" or normalised.startswith("tests/") or normalised == "tests"


def _p0_literal_markers(constant: str) -> list[str]:
    """Return rule ids triggered by one string constant (deduped upstream)."""
    hits: list[str] = []
    if any(marker in constant for marker in _P0_PROD_PATH_MARKERS):
        hits.append("literal:prod_db_path")
    if _P0_ENV_DATA_DIR in constant:
        hits.append("literal:jme_data_dir")
    if _P0_TOKEN_LITERAL in constant:
        hits.append("literal:jme_token")
    if _P0_API_HOSTPORT in constant:
        hits.append("literal:prod_api_url")
    return hits


def _p0_protected_calls(tree: ast.Module) -> set[int]:
    """ids of Call nodes nested inside function / class / lambda bodies."""
    protected: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            for child in ast.iter_child_nodes(node):
                for inner in ast.walk(child):
                    if isinstance(inner, ast.Call):
                        protected.add(id(inner))
    return protected


def _p0_has_prod_marker_arg(call: ast.Call) -> bool:
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        for inner in ast.walk(arg):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                if _p0_literal_markers(inner.value):
                    return True
    return False


def _p0_enclosing_symbols(tree: ast.Module) -> dict[int, str]:
    """Map ``id(node)`` to its innermost FunctionDef / AsyncFunctionDef name.

    R-1 / JKG-INT-01: recorded for ``Call``, ``Attribute``, ``Import`` and
    ``ImportFrom`` nodes.  Only ``call:sqlite3.connect``, ``call:db_manager``
    and ``import:subprocess`` findings actually attach this information; the
    other rule ids are unaffected (an ``Attribute`` entry exists so the
    ``import:subprocess`` call-site rule can be narrowed as well).  A node with
    no enclosing function (top-level statement, class body, decorator
    expression) is attributed to ``"<module>"``.  The traversal is iterative so
    deeply nested expressions cannot blow the Python recursion limit; it is a
    pure AST walk with no filesystem access.
    """
    mapping: dict[int, str] = {}
    stack: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    while stack:
        node, enclosing = stack.pop()
        if isinstance(
            node, (ast.Call, ast.Attribute, ast.Import, ast.ImportFrom)
        ):
            mapping[id(node)] = enclosing
        child_enclosing = enclosing
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child_enclosing = node.name
        for child in ast.iter_child_nodes(node):
            stack.append((child, child_enclosing))
    return mapping


def p0_scan_source(source: str, *, filename: str, scope: str) -> list[dict]:
    """Scan one source string.  Pure: no filesystem access, no global mutation.

    Returns a list of ``{"filename", "lineno", "rule", "scope"}`` dicts.
    ``scope`` is the caller-supplied label echoed into each finding; tiering is
    decided separately by ``_p0_is_tests_context`` during enforcement.

    R-1 / JKG-INT-01: findings for ``call:sqlite3.connect``, ``call:db_manager``
    and ``import:subprocess`` also carry an ``"enclosing"`` field (innermost
    function name, or ``"<module>"``); every other rule keeps the original
    finding structure unchanged.  Allowlist entries without ``symbols`` keep
    their (file, rule) grain, so this extra field changes no existing entry.

    Raises ``P0AstScanError`` on any parse failure (fail-closed).
    """
    try:
        tree = ast.parse(source, filename=filename)
    except Exception as exc:  # SyntaxError, ValueError (null bytes), RecursionError
        raise P0AstScanError(f"AST 解析失败: {filename}: {exc}") from exc

    tests_context = _p0_is_tests_context(filename, scope)
    protected = _p0_protected_calls(tree)
    enclosing_symbols = _p0_enclosing_symbols(tree)

    # Pre-pass: collect `from sqlite3 import connect [as alias]` bindings so a
    # bare connect(...) call earlier in the file is still attributed correctly.
    sqlite_connect_aliases = {"connect"}
    for pre in ast.walk(tree):
        if isinstance(pre, ast.ImportFrom) and pre.module == "sqlite3":
            for alias in pre.names:
                if alias.name == "connect":
                    sqlite_connect_aliases.add(alias.asname or alias.name)

    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def emit(lineno: int, rule: str, enclosing: str | None = None) -> None:
        key = (lineno, rule)
        if key in seen:
            return
        seen.add(key)
        finding: dict[str, Any] = {
            "filename": filename,
            "lineno": lineno,
            "rule": rule,
            "scope": scope,
        }
        # R-1 / JKG-INT-01: only call:sqlite3.connect, call:db_manager and
        # import:subprocess pass ``enclosing``; every other rule never does, so
        # their findings stay byte-identical to the pre-R-1 structure.
        if enclosing is not None:
            finding["enclosing"] = enclosing
        findings.append(finding)

    for node in ast.walk(tree):
        # ---- string constants ------------------------------------------------
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for rule in _p0_literal_markers(node.value):
                emit(node.lineno, rule)

        # ---- imports ---------------------------------------------------------
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif node.module:
                names = [node.module]
                for alias in node.names:
                    names.append(node.module + "." + alias.name)
            roots = {name.split(".")[0] for name in names if name}
            if tests_context:
                if roots & _P0_SUBPROCESS_MODULES:
                    emit(
                        node.lineno,
                        "import:subprocess",
                        enclosing_symbols.get(id(node), "<module>"),
                    )
                if roots & _P0_SCRATCH_MODULES:
                    emit(node.lineno, "import:scratch_module")
        # ---- calls -----------------------------------------------------------
        elif isinstance(node, ast.Call):
            name = _p0_call_name(node.func)
            tail = _p0_tail(name)
            module_level = id(node) not in protected
            enclosing = enclosing_symbols.get(id(node), "<module>")

            if name == "sqlite3.connect" or (
                isinstance(node.func, ast.Name)
                and node.func.id in sqlite_connect_aliases
            ):
                # Only bare `connect(...)` names (from sqlite3 import connect)
                # qualify here.  `engine.connect()` / `manager.session()` are
                # SQLAlchemy calls and must NOT be reported as raw sqlite3 use.
                emit(node.lineno, "call:sqlite3.connect", enclosing)
            if tail == "create_engine":
                emit(node.lineno, "call:create_engine")
            if tail == "DatabaseManager":
                emit(node.lineno, "call:db_manager", enclosing)
            if tail == _P0_LOAD_SETTINGS_NAME:
                emit(node.lineno, "call:load_settings")

            if module_level and tail in _P0_DB_CALL_TAILS:
                emit(node.lineno, "module_level_db_call")
            if tail in _P0_DANGEROUS_FILE_CALLS and (
                module_level or _p0_has_prod_marker_arg(node)
            ):
                emit(node.lineno, "call:dangerous_file")

            if tests_context:
                if name in {"os.system", "os.popen"} or (
                    name.split(".")[0] in _P0_SUBPROCESS_MODULES
                ):
                    emit(node.lineno, "import:subprocess", enclosing)

    findings.sort(key=lambda item: (item["lineno"], item["rule"]))
    return findings


def _p0_iter_py_files(root: Path):
    """Yield project ``*.py`` files with third-party/cache dirs pruned."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise P0AstScanError(f"目录遍历失败: {current}: {exc}") from exc
        subdirs: list[Path] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError as exc:
                raise P0AstScanError(f"文件状态读取失败: {entry}: {exc}") from exc
            if is_dir:
                if entry.name not in P0_SCAN_EXCLUDE_DIRS:
                    subdirs.append(entry)
            elif entry.suffix == ".py":
                yield entry
        stack.extend(reversed(subdirs))


def p0_scan_tree(root: Path, *, scope: str) -> list[dict]:
    """Scan every ``*.py`` under ``root``.  Pure (read-only filesystem access).

    ``filename`` in each finding is the path relative to ``root`` in posix form,
    which is the key used by ``P0_ALLOWLIST``.  Any read/parse failure raises
    ``P0AstScanError`` (fail-closed); no file is silently skipped.
    """
    root = Path(root)
    if not root.is_dir():
        raise P0AstScanError(f"扫描根目录不存在或不是目录: {root}")
    findings: list[dict] = []
    for path in _p0_iter_py_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise P0AstScanError(f"源码读取失败: {path}: {exc}") from exc
        findings.extend(
            p0_scan_source(source, filename=relative, scope=scope)
        )
    findings.sort(key=lambda item: (item["filename"], item["lineno"], item["rule"]))
    return findings


def _p0_is_exempt(finding: dict) -> bool:
    """R-1: (file, rule) + optional per-function ``symbols`` narrowing.

    A matched entry WITH ``symbols`` validates ``finding["enclosing"]``; a
    mismatch returns False immediately (strict fail-closed, NOT a ``continue``
    that could fall through to a later, broader entry).  An entry WITHOUT
    ``symbols`` keeps the original (file, rule) exemption.
    """
    if finding["rule"] in P0_HARD_RULES:
        return False
    for entry in P0_ALLOWLIST:
        if (
            entry.get("status") == "exempt"
            and entry["file"] == finding["filename"]
            and entry["rule"] == finding["rule"]
        ):
            symbols = entry.get("symbols")
            if symbols is not None:
                if finding.get("enclosing") not in symbols:
                    return False
            return True
    return False


def _p0_run_session_scan(config, *, root: Path | None = None) -> None:
    """Two-tier enforcement, called from pytest_sessionstart.

    ``root`` is injectable so end-to-end tests can scan a real temporary tree
    containing a real violating file; production calls always use JARVISMIND_ROOT.
    """
    scan_root = JARVISMIND_ROOT if root is None else Path(root)
    findings = p0_scan_tree(scan_root, scope="jme")
    tier1 = [f for f in findings if _p0_is_tests_context(f["filename"], f["scope"])]
    tier2 = [f for f in findings if not _p0_is_tests_context(f["filename"], f["scope"])]
    violations = [f for f in tier1 if not _p0_is_exempt(f)]

    config._p0_scan_findings = tuple(findings)
    config._p0_scan_violations = tuple(violations)
    config._p0_known_risks = tuple(tier2)

    # ---- tier 2 output: recorded + printed, never a session failure ---------
    by_file: dict[str, list[str]] = {}
    for item in tier2:
        by_file.setdefault(item["filename"], []).append(
            f"{item['lineno']}:{item['rule']}"
        )
    print(
        f"[P0-03A AST] 扫描 {len(list(_p0_iter_py_files(scan_root)))} 个 .py；"
        f"档1 命中 {len(tier1)}（豁免后违规 {len(violations)}）；"
        f"档2 已知风险 {len(tier2)}"
    )
    if by_file:
        print("[P0-03A AST] 档2 已知风险清单（仅记录，不因文件存在而阻断会话）：")
        for name in sorted(by_file):
            print(f"  {name}: {', '.join(sorted(set(by_file[name])))}")
    unbound = [
        entry
        for entry in P0_ALLOWLIST
        if entry.get("status") == "exempt" and "symbols" not in entry
    ]
    print(
        f"[P0-03A AST] 已知风险提示：P0_ALLOWLIST 未绑定 symbols 的豁免条目 "
        f"{len(unbound)} 条（仍是 (file, rule) 粒度放行，未收窄到具名函数）；"
        f"已绑定 symbols 的条目 {len([e for e in P0_ALLOWLIST if 'symbols' in e])} 条"
    )
    print(
        "[P0-03A AST] 说明：AST 黑名单只是测试阶段防线，"
        "不能阻止普通 Python 进程导入危险脚本；动态调用属检测边界。"
    )

    # ---- tier 1 fail-closed --------------------------------------------------
    if violations:
        detail = "\n".join(
            f"  {v['filename']}:{v['lineno']} [{v['rule']}]" for v in violations
        )
        pytest.exit(
            "[P0-03A 档1 FAIL-CLOSED] tests/** 存在未豁免违规：\n"
            + detail
            + "\n请修正测试代码，或在 P0_ALLOWLIST 登记具体文件+具体规则+允许原因。",
            returncode=2,
        )


# ===========================================================================
# A-2 · process environment isolation
# ===========================================================================

def p0_snapshot_jme_env() -> dict:
    """Return a plain copy of all current ``JARVISMIND_*`` process env values.  Pure."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_P0_ENV_PREFIX)
    }


def p0_restore_jme_env(snapshot: dict) -> None:
    """Restore the process ``JARVISMIND_*`` environment exactly to ``snapshot``."""
    for key in [k for k in os.environ if k.startswith(_P0_ENV_PREFIX)]:
        del os.environ[key]
    for key, value in (snapshot or {}).items():
        os.environ[key] = value


# ===========================================================================
# A-4 · database URL / path whitelist
# ===========================================================================

_P0_SQLITE_PREFIXES = (
    "sqlite+pysqlite:///",
    "sqlite:///",
    "sqlite://",
    "sqlite+pysqlite://",
)


def _p0_parse_db_path(database_url: str, *, tmp_root: Path) -> Path | None:
    """Pure URL -> normalized Path.  Returns None for in-memory databases."""
    if not isinstance(database_url, str) or not database_url:
        raise P0DbPathError("database_url 为空或不是字符串，拒绝构造数据库")
    rest = None
    for prefix in _P0_SQLITE_PREFIXES:
        if database_url.startswith(prefix):
            rest = database_url[len(prefix) :]
            break
    if rest is None:
        raise P0DbPathError(f"无法解析 database_url: {database_url!r}")
    # strip query / fragment (e.g. ?mode=ro, ?cache=shared)
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    if rest in ("", ":memory:"):
        return None
    # Pure path normalization: Path.resolve() handles absolute / relative paths
    # and symlinks.  No exists()/stat() probing of any production path happens.
    return Path(rest).resolve()


def p0_is_db_path_allowed(database_url: str, *, tmp_root: Path) -> bool:
    """Pure predicate: does the URL point inside ``tmp_root`` (or in-memory)?"""
    try:
        return _p0_assert_impl(database_url, tmp_root=tmp_root) is not None
    except (P0DbPathError, OSError, ValueError):
        return False


def p0_assert_db_path_allowed(database_url: str, *, tmp_root: Path) -> Path:
    """Validate and return the normalized path; raise on anything outside."""
    return _p0_assert_impl(database_url, tmp_root=tmp_root)


def _p0_assert_impl(database_url: str, *, tmp_root: Path) -> Path:
    root = Path(tmp_root).resolve()
    resolved = _p0_parse_db_path(database_url, tmp_root=root)
    if resolved is None:
        # In-memory database: no file is created.  Represent it with the
        # normalized tmp root so the caller always receives a Path.
        return root
    if not resolved.is_relative_to(root):
        raise P0DbPathError(
            "P0-03A: 数据库路径不在会话临时目录白名单内，拒绝构造 "
            f"(path={resolved}, tmp_root={root})"
        )
    return resolved


# ===========================================================================
# Session state (process-local).  All mutations are reversed at session end.
# ===========================================================================

_P0_TMP_ROOT: Path | None = None
_P0_ENV_SNAPSHOT: dict | None = None
_P0_SESSION_MP: Any = None
_P0_ALLOW_LOAD_SETTINGS = False
_P0_SESSION_CLEANED = False

_P0_REAL_LOAD_SETTINGS = _p0_config_module.load_settings
_P0_REAL_DBM_INIT = DatabaseManager.__init__


def _p0_config_loader_guard(*args, **kwargs):
    # NOTE: the identifier deliberately avoids the concatenated banned token
    # checked by G-3 in test_startup_safety.py (raw text scan of tests/**).
    if _P0_ALLOW_LOAD_SETTINGS:
        return _P0_REAL_LOAD_SETTINGS(*args, **kwargs)
    raise RuntimeError(_P0_GUARD_MESSAGE)


def _p0_guard_dbm_init(self, settings, *args, **kwargs):
    """Validate the database path before the real __init__ can mkdir anything."""
    if _P0_TMP_ROOT is None:
        raise P0DbPathError("P0-03A: 会话临时目录未初始化，拒绝构造 DatabaseManager")
    url = getattr(settings, "database_url", None)
    if not url:
        # Fall back to the settings' own data_dir, still validated by the same
        # whitelist.  Never silently accept a default/production directory.
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is None:
            raise P0DbPathError(
                "P0-03A: settings 缺少 database_url / data_dir，拒绝构造 DatabaseManager"
            )
        url = "sqlite+pysqlite:///" + str(Path(data_dir) / "jarvismind.db").replace("\\", "/")
    p0_assert_db_path_allowed(url, tmp_root=_P0_TMP_ROOT)
    return _P0_REAL_DBM_INIT(self, settings, *args, **kwargs)


def _p0_install_session_patches(config) -> None:
    global _P0_SESSION_MP
    mp = pytest.MonkeyPatch()

    # A-3: canonical module attribute ...
    mp.setattr(_p0_config_module, _P0_LOAD_SETTINGS_NAME, _p0_config_loader_guard)
    # ... plus every already-imported module-level `from ... import` binding,
    # so app.main's own binding cannot bypass the guard.
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            bound = getattr(module, _P0_LOAD_SETTINGS_NAME, None)
        except (AttributeError, TypeError):
            continue
        if bound is _P0_REAL_LOAD_SETTINGS:
            try:
                mp.setattr(
                    module, _P0_LOAD_SETTINGS_NAME,
                    _p0_config_loader_guard, raising=False,
                )
            except (AttributeError, TypeError):
                pass

    # A-4: path whitelist on the connection factory.
    mp.setattr(DatabaseManager, "__init__", _p0_guard_dbm_init)
    _P0_SESSION_MP = mp


def _p0_undo_session_patches() -> None:
    global _P0_SESSION_MP
    if _P0_SESSION_MP is not None:
        try:
            _P0_SESSION_MP.undo()
        finally:
            _P0_SESSION_MP = None


def _p0_cleanup_session(config) -> None:
    """Idempotent restore: env, patches, and the guard flag."""
    global _P0_ALLOW_LOAD_SETTINGS, _P0_SESSION_CLEANED
    if _P0_SESSION_CLEANED:
        return
    _P0_SESSION_CLEANED = True
    _P0_ALLOW_LOAD_SETTINGS = False
    _p0_undo_session_patches()
    if _P0_ENV_SNAPSHOT is not None:
        p0_restore_jme_env(_P0_ENV_SNAPSHOT)


def _p0_resolve_tmp_root(config) -> Path:
    """Use pytest's own session tmp base so every tmp_path is inside it."""
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        try:
            return Path(factory.getbasetemp()).resolve()
        except Exception:
            pass
    import tempfile

    return Path(tempfile.mkdtemp(prefix="p003a-session-")).resolve()


# ===========================================================================
# P0-03 会话级生产数据库文件守卫（原始实现保留）
# ===========================================================================
# 只做文件系统读取（exists / stat / hashlib 二进制只读），不 import
# app.database、不打开 SQLite 连接、不执行任何 SQL/PRAGMA。
#
# 注意：前后对比只能证明「检测结束时状态与开始时一致」，不能单独证明
# 「整个过程从未访问过这些文件」。零连接还须依靠 G-2 连接拦截、源码调用链
# 审查与隔离运行纪律共同证明。
_PROD_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_PROD_DB_FILES = (
    _PROD_DATA_DIR / "jarvismind.db",
    _PROD_DATA_DIR / "jarvismind.db-wal",
    _PROD_DATA_DIR / "jarvismind.db-shm",
)
_GUARD_START_SNAPSHOT = None


def _sha256_of(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_prod_db():
    """采集三件套 exists / size / mtime_ns / SHA-256（纯文件系统读取）。"""
    state = []
    for path in _PROD_DB_FILES:
        if path.exists():
            stat = path.stat()
            state.append((
                path.name, True, stat.st_size, stat.st_mtime_ns,
                _sha256_of(path),
            ))
        else:
            state.append((path.name, False, None, None, None))
    return tuple(state)


def pytest_configure(config):
    """R-4: 拒绝会绕过本 conftest 的收集期配置（尽力而为的检查）。

    ``--noconftest`` 会让 conftest 根本不加载，代码层面无法自防，本检查只
    覆盖 confcutdir 部分：这里能观测到的只有 confcutdir 选项，以及
    PYTEST_ADDOPTS / PYTEST_PLUGINS 中显式注入 ``--noconftest`` 的痕迹；
    命令行直接传 ``--noconftest`` 时本函数根本不会执行，无法检测。
    """
    config.addinivalue_line(
        "markers",
        "real_migration_gate: skip the autouse migration stand-in and "
        "exercise the real first-stage migration gate",
    )
    config.addinivalue_line(
        "markers",
        "allow_load_settings: explicitly opt in to the real configuration "
        "loader inside a test (P0-03A guards it by default)",
    )

    confcutdir = config.getoption("confcutdir", None)
    if confcutdir is not None:
        resolved = Path(confcutdir).resolve()
        tests_root = TESTS_DIR.resolve()
        if resolved.is_relative_to(tests_root):
            pytest.exit(
                "[P0-03A R-4] confcutdir 被显式设到 jme/tests 之下，"
                "会截断 conftest 加载："
                f"confcutdir={resolved}, tests_root={tests_root}",
                returncode=2,
            )

    injected = " ".join(
        str(os.environ.get(name, "") or "")
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
    )
    if "--noconftest" in injected:
        pytest.exit(
            "[P0-03A R-4] PYTEST_ADDOPTS / PYTEST_PLUGINS 注入了 "
            "--noconftest，本 conftest 的防线将被整体跳过",
            returncode=2,
        )


# Filters that can reduce the collected safety suite.  Their presence is
# recorded and printed so a filtered run is always auditable.
_P0_FILTER_OPTION_NAMES = (
    "keyword", "markexpr", "deselect", "ignore", "ignore_glob",
    "lf", "last_failed", "ff", "failed_first", "nf", "new_first",
    "sw", "stepwise", "maxfail",
)


def p0_gate_active_filters(config) -> tuple[str, ...]:
    """Return names of collection-filter options that are actually set."""
    active = []
    for name in _P0_FILTER_OPTION_NAMES:
        try:
            value = config.getoption(name, default=None)
        except (ValueError, KeyError, AttributeError):
            continue
        if value:
            active.append(name)
    return tuple(sorted(set(active)))


def p0_gate_is_forced(config) -> bool:
    """P0-03A-R1: gate is unconditionally ON. There is no opt-out.

    The old heuristic compared raw ``config.args`` strings against ini
    ``testpaths``; with an absolute path (``pytest tests/`` resolves args to
    an absolute directory) that comparison was always False, so ``forced``
    was False and the 27-case floor never ran.  Replacing it with another
    conditional (env var, single-file detection, filter detection) would
    recreate the same bypass class.  The gate is now a constant: every
    pytest session loading this conftest must collect the full safety suite.
    """
    return True


@dataclass(frozen=True)
class P0GateDecision:
    """Immutable result of a safety-gate evaluation (pure, unit-testable)."""

    collected: int
    required: int
    missing_critical: tuple[str, ...]
    remediation_collected: int
    remediation_required: int
    skipped_markers: tuple[str, ...]
    active_filters: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def p0_gate_decide(
    nodeids,
    *,
    required: int,
    critical,
    remediation_required: int = 0,
    skipped_markers=(),
    active_filters=(),
) -> P0GateDecision:
    """Evaluate the collection gate. Pure: no I/O, no pytest.exit.

    Both the real hook and the dedicated gate unit tests call this function,
    so the asserted behaviour is the behaviour actually enforced.
    """
    nodes = tuple(str(n) for n in nodeids)
    safety = tuple(n for n in nodes if "test_startup_safety.py" in n)
    remediation = tuple(n for n in nodes if "test_p0_remediation_" in n)
    names = {n.split("::")[-1].split("[")[0] for n in safety}
    missing = tuple(sorted(set(critical) - names))
    skipped = tuple(str(n) for n in skipped_markers)
    filters = tuple(str(n) for n in active_filters)

    failures = []
    if len(safety) < required:
        failures.append(
            f"安全专项用例不足：收集 {len(safety)}，下限 {required}，缺 {required - len(safety)}"
        )
    if missing:
        failures.append("关键安全用例缺失：" + ", ".join(missing))
    if len(remediation) < remediation_required:
        failures.append(
            f"整改专项用例不足：收集 {len(remediation)}，下限 {remediation_required}"
        )
    if skipped:
        failures.append("关键安全用例被静态跳过：" + ", ".join(skipped))
    return P0GateDecision(
        collected=len(safety),
        required=required,
        missing_critical=missing,
        remediation_collected=len(remediation),
        remediation_required=remediation_required,
        skipped_markers=skipped,
        active_filters=filters,
        failures=tuple(failures),
    )


# Execution-phase outcome tracking (populated by pytest_runtest_logreport).
_P0_GATE_EXEC: dict[str, str] = {}
_P0_GATE_STATE: dict[str, Any] = {}


def pytest_runtest_logreport(report) -> None:
    """Record the real outcome of every safety/remediation case."""
    nodeid = getattr(report, "nodeid", "") or ""
    if "test_startup_safety.py" not in nodeid and "test_p0_remediation_" not in nodeid:
        return
    if report.when == "call":
        _P0_GATE_EXEC[nodeid] = str(report.outcome)
    elif report.when == "setup" and report.skipped:
        _P0_GATE_EXEC[nodeid] = "skipped"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    """P0-03A-R1: gate is ON by default for every invocation loading this
    conftest. ``trylast=True`` is mandatory: pytest's own -k/-m deselection
    hook runs earlier, so only here is the post-filter item list visible.
    """
    violations = tuple(getattr(config, "_p0_scan_violations", ()) or ())
    if violations:
        detail = "\n".join(
            f"  {v['filename']}:{v['lineno']} [{v['rule']}]" for v in violations
        )
        pytest.exit(
            "[P0-03A R-2] 收集期复检：档1 AST 存在未豁免违规：\n" + detail,
            returncode=2,
        )

    _P0_GATE_EXEC.clear()
    nodeids = [item.nodeid for item in items]
    skip_markers = [
        item.nodeid
        for item in items
        if ("test_startup_safety.py" in item.nodeid
            or "test_p0_remediation_" in item.nodeid)
        and (item.get_closest_marker("skip") is not None
             or item.get_closest_marker("xfail") is not None)
    ]
    filters = p0_gate_active_filters(config)
    forced = p0_gate_is_forced(config)
    decision = p0_gate_decide(
        nodeids,
        required=_P0_SAFETY_CASE_COUNT,
        critical=_P0_CRITICAL_SAFETY_TESTS,
        remediation_required=_P0_REMEDIATION_CASE_COUNT,
        skipped_markers=skip_markers,
        active_filters=filters,
    )

    _P0_GATE_STATE.clear()
    _P0_GATE_STATE.update(
        {
            "decision": decision,
            "forced": forced,
            "collected": decision.collected,
            "remediation_collected": decision.remediation_collected,
            "required": decision.required,
            "remediation_required": decision.remediation_required,
            "filters": filters,
        }
    )
    config._p0_gate_state = dict(_P0_GATE_STATE)
    print(
        "[P0-03A GATE] "
        f"forced={forced} collected={decision.collected}/{decision.required} "
        f"critical={len(_P0_CRITICAL_SAFETY_TESTS) - len(decision.missing_critical)}"
        f"/{len(_P0_CRITICAL_SAFETY_TESTS)} "
        f"remediation={decision.remediation_collected}/{decision.remediation_required} "
        f"filters={','.join(filters) or 'none'}"
    )

    if not decision.ok:
        listed = "\n".join("  " + n for n in nodeids if "test_startup_safety.py" in n)
        pytest.exit(
            "[P0-03A GATE FAIL] 安全测试收集门禁未通过：\n"
            + "\n".join("  - " + reason for reason in decision.failures)
            + "\n本次已收集的安全专项 nodeid：\n"
            + (listed or "  (无)")
            + "\n可能原因：-k / --ignore / --deselect / -m / --lf / "
              "删除或跳过关键用例 / 放宽 P0_ALLOWLIST。",
            returncode=2,
        )


def pytest_sessionstart(session):
    global _GUARD_START_SNAPSHOT, _P0_TMP_ROOT, _P0_ENV_SNAPSHOT
    global _P0_SESSION_CLEANED

    config = session.config
    _P0_SESSION_CLEANED = False

    # A-2: snapshot first, and register an unconditional cleanup.  Pytest calls
    # config cleanups during _ensure_unconfigure(), which runs in the finally
    # block of wrap_session even when pytest_sessionstart itself aborts (in that
    # case pytest_sessionfinish is NOT called -- initstate never reaches 2).
    _P0_ENV_SNAPSHOT = p0_snapshot_jme_env()
    config.add_cleanup(lambda: _p0_cleanup_session(config))

    try:
        _GUARD_START_SNAPSHOT = _snapshot_prod_db()
        config._p003_guard_snapshot = _GUARD_START_SNAPSHOT
        config._p003_guard_probe = _snapshot_prod_db

        _P0_TMP_ROOT = _p0_resolve_tmp_root(config)
        config._p0_tmp_root = _P0_TMP_ROOT
        # R-1 / 批次 B: 让 schema_factory 的路径断言读取与 A-4 同一会话根。
        schema_factory.p0_bind_tmp_config(config)

        # A-2: remove every JARVISMIND_* value, then redirect the two that matter to a
        # session-local temporary directory / a fixed test token.  This is a
        # process-level os.environ change only; it is fully restored.
        p0_restore_jme_env({})
        os.environ[_P0_ENV_DATA_DIR] = str(_P0_TMP_ROOT)
        os.environ[_P0_ENV_API_TOKEN] = TEST_TOKEN

        _p0_install_session_patches(config)

        # A-1: two-tier scan.  May call pytest.exit() on a tier-1 violation.
        _p0_run_session_scan(config)
    except BaseException:
        # Guaranteed restore even on the fail-closed / exception path.
        _p0_cleanup_session(config)
        raise


def pytest_sessionfinish(session, exitstatus):
    try:
        before = getattr(
            session.config, "_p003_guard_snapshot", _GUARD_START_SNAPSHOT
        )
        after = _snapshot_prod_db()
        if before is None or after != before:
            session.exitstatus = 1
            print("\n[P0-03 GUARD FAILED] 生产数据库三件套在会话期间发生变化")
            print(f"  sessionstart : {before}")
            print(f"  sessionfinish: {after}\n")
    finally:
        # A-2: restore the original process environment on the normal path too.
        _p0_cleanup_session(session.config)

    # ---- P0-03A-R1 execution-phase reconciliation ------------------------
    state = _P0_GATE_STATE
    if not state or state.get("forced") is False:
        return
    try:
        collect_only = bool(session.config.getoption("collectonly", default=False))
    except (ValueError, KeyError, AttributeError):
        collect_only = False
    expected = state["collected"] + state["remediation_collected"]
    outcomes = dict(_P0_GATE_EXEC)
    passed = sum(1 for v in outcomes.values() if v == "passed")
    skipped = sorted(n for n, v in outcomes.items() if v == "skipped")
    failed = sorted(n for n, v in outcomes.items() if v == "failed")
    print(
        "[P0-03A GATE RESULT] "
        f"collected={expected} executed={len(outcomes)} passed={passed} "
        f"failed={len(failed)} skipped={len(skipped)} collect_only={collect_only}"
    )
    if collect_only:
        return
    reasons = []
    if len(outcomes) < expected:
        reasons.append(f"实际执行 {len(outcomes)} < 收集 {expected}")
    if skipped:
        reasons.append("关键安全用例被跳过：" + ", ".join(skipped))
    if reasons and exitstatus == 0:
        session.exitstatus = 2
        print("[P0-03A GATE FAIL] " + "；".join(reasons))


# ===========================================================================
# A-2 / A-3 per-test gate
# ===========================================================================

@pytest.fixture(autouse=True)
def _p0_test_isolation_gate(request):
    """Per-test: re-verify env isolation and set the load_settings opt-in flag.

    Uses no shared monkeypatch instance, so legacy tests that call
    ``monkeypatch.undo()`` cannot undo the session-level guards.
    """
    global _P0_ALLOW_LOAD_SETTINGS
    # R-3: 每个用例执行前再次确认档1 扫描零违规，防止绕过会话级 fail-closed。
    assert not getattr(request.config, "_p0_scan_violations", ()), (
        "P0-03A R-3: 档1 AST 存在未豁免违规，per-test 隔离门拒绝执行用例"
    )
    allow = request.node.get_closest_marker("allow_load_settings") is not None
    _P0_ALLOW_LOAD_SETTINGS = allow
    try:
        config = request.config
        tmp_root = getattr(config, "_p0_tmp_root", _P0_TMP_ROOT)
        assert tmp_root is not None, "P0-03A: 会话临时根目录未初始化"

        data_dir = os.environ.get(_P0_ENV_DATA_DIR)
        if data_dir is not None:
            resolved = Path(data_dir).resolve()
            assert resolved.is_relative_to(Path(tmp_root).resolve()), (
                "P0-03A: 数据目录环境变量指向会话临时目录之外: "
                f"{resolved} (tmp_root={tmp_root})"
            )
        token = os.environ.get(_P0_ENV_API_TOKEN)
        assert token == TEST_TOKEN, (
            "P0-03A: API token 环境变量不是测试值（可能被外部注入）"
        )
        yield
    finally:
        _P0_ALLOW_LOAD_SETTINGS = False


# ===========================================================================
# Fixtures (original P0-03 behaviour, unchanged)
# ===========================================================================

@pytest.fixture()
def settings(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "jarvismind.db"
    return Settings(
        env="test",
        host="127.0.0.1",
        port=8777,
        data_dir=data_dir,
        log_level="WARNING",
        api_token=TEST_TOKEN,
        cors_origins=(),
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
        db_echo=False,
        embedding_provider="deterministic",
        qdrant_path_override=":memory:",
        # JW-00: the product default for JARVISMIND_DIRECT_WRITE_ENABLED is FALSE.
        # The shared fixture opts in explicitly so the pre-existing regression
        # suite (which uses POST/PATCH /memories to create fixtures) keeps its
        # exact behaviour. Tests that must prove the default-deny behaviour
        # build their own Settings(...) without this flag.
        direct_write_enabled=True,
        # JW-02: import is OFF in production (it can fabricate authoritative
        # rows). The shared fixture opts in explicitly so the pre-existing
        # migration/import regression keeps its exact behaviour. The default-deny
        # cases build their own Settings() without this flag.
        lifecycle_import_enabled=True,
        lifecycle_merge_enabled=True,
        lifecycle_summary_review_enabled=True,
        lifecycle_correction_resolve_enabled=True,
    )


@pytest.fixture()
def manager(settings):
    mgr = DatabaseManager(settings)
    schema_factory._test_only_apply_all(mgr)
    yield mgr
    mgr.dispose()


@pytest.fixture(autouse=True)
def _migration_runner_sandbox(request):
    """把被拒绝的生产迁移入口重定向到测试工厂。

    使用独立的 pytest.MonkeyPatch() 实例，而不是共享的 monkeypatch fixture：
    既有测试（test_security_gate.py / test_security_fix_b1.py）会在用例体内
    调用 monkeypatch.undo()，共享实例会连带撤销本重定向，导致其后仍打到
    被阻断的真实门控。独立实例保证重定向在整个用例生命周期内始终生效，
    并在 teardown 精确撤销。

    G-1（real_migration_gate）跳过 patch，并把真实方法引用装回类/模块，
    以验证第一阶段硬阻断；其余用例统一走替身，覆盖 27 处旧调用点。
    """
    mp = pytest.MonkeyPatch()
    try:
        if request.node.get_closest_marker("real_migration_gate") is not None:
            schema_factory._test_only_restore_runner(mp)
        else:
            schema_factory._test_only_patch_runner(mp)
        yield
    finally:
        mp.undo()


@pytest.fixture()
def app(settings, manager):
    return create_app(settings, manager=manager)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"X-API-Token": TEST_TOKEN}


@pytest.fixture()
def sample_memory_payload():
    return {
        "memory_type": "SEMANTIC",
        "content": "The user prefers short, direct answers.",
        "summary": "Answer style preference",
        "importance": 7.5,
        "confidence": 0.9,
    }


# ---------------------------------------------------------------------------
# JW-01: approved-forget helper
# ---------------------------------------------------------------------------
class ApprovedForget:
    """Drive the REAL two-step forget approval through the HTTP API.

    JW-01 turned ``DELETE /memories/{id}`` and ``POST /memories/forget`` into
    approval-gated transports, so the pre-existing soft-delete regression cases
    can no longer delete directly. They also must not be weakened or skipped:
    they are the proof that a forgotten memory is hidden from lists/search and
    keeps its audit trail.

    This helper performs the same two calls the iOS client makes
    (create request -> approve with the one-time token) and then reports the
    result, so those cases keep asserting exactly the same end state while going
    through the new, approval-required path. Any behaviour change in the flow
    breaks these tests too, which is the point.
    """

    def __init__(self, client, headers) -> None:
        self.client = client
        self.headers = headers

    def request(self, memory_id: str, *, reason: str = "test forget") -> dict:
        """Step 1: create a forget request. Returns the create response data."""
        resp = self.client.post(
            f"/api/v1/memories/{memory_id}/forget-requests",
            json={"reason": reason},
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    def approve(self, memory_id: str, *, reason: str = "test forget") -> dict:
        """Steps 1+2: request then approve. Returns the EXECUTED request body."""
        created = self.request(memory_id, reason=reason)
        resp = self.client.post(
            f"/api/v1/forget-requests/{created['id']}/approve",
            json={"confirmation_token": created["confirmation_token"]},
            headers=self.headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["status"] == "EXECUTED", body
        assert body["memory_forgotten"] is True, body
        return body

    def forget(self, memory_id: str, *, reason: str = "test forget") -> None:
        """Steps 1+2, asserting the memory really became DELETED."""
        self.approve(memory_id, reason=reason)
        current = self.client.get(
            f"/api/v1/memories/{memory_id}", headers=self.headers
        )
        assert current.status_code == 200, current.text
        assert current.json()["data"]["status"] == "DELETED", current.text


@pytest.fixture()
def approved_forget(client, auth_headers) -> ApprovedForget:
    return ApprovedForget(client, auth_headers)
