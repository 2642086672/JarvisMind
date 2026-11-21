"""JKG-INT-02 回归套件：JKG 服务层正式接入组合根。

覆盖范围（对应任务验收清单 A–G）：

* A 配置解析：纯函数语义、硬关优先、fail-closed、上限校验（不连库）；
* B 默认态：未启用且无表时的状态、异常与「访问不建表」；
* C 启用但未装表：报告 ``schema_not_ready`` 且不偷偷安装；
* D 启用且已装表：读写端到端、空图报告、JME 权威表零改动；
* E 关闭时降级：JME HTTP 与健康检查不回归、状态查询异常隔离；
* F 架构边界：静态扫描禁止目录不得 import JKG，组合根只能延迟导入；
* G 启动不建表：走完整 lifespan 后仍无 JKG 对象、adapter 未被触碰。

数据库一律来自共享 ``manager`` fixture 的会话临时库；需要真实 JKG 表结构的
用例只通过 ``tests/_fixtures/jkg_schema_factory.create_jkg_schema`` 安装，
不调用任何迁移入口，也不在生产代码路径上建表。测试里不出现 P0-03A 门禁
禁止的四类构造：裸连接工厂、默认配置加载器、会话构造、子进程。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.container import Container
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import (
    DEFAULT_MAX_PAGE_SIZE,
    JKG_ENABLED_ENV,
    JkgCompositionAdapter,
    JkgConfig,
    JkgDisabledError,
    JkgSchemaNotReadyError,
    JkgState,
    present_jkg_tables,
    resolve_enabled,
)
from jarvismind.jkg.adapter import JkgCompositionAdapter as AdapterFromModule
from jarvismind.jkg.schema import JKG_SCHEMA_VERSION, JKG_TABLES
from jarvismind.main import create_app

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, 5.0, 0.9, 'ACTIVE', :now, :now"
    ")"
)

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


# ---------------------------------------------------------------------------
# 辅助函数（全部在函数体内使用 manager，避免模块级数据库调用）
# ---------------------------------------------------------------------------
def _set_setting(manager, key: str, value: str) -> None:
    """写入一条 system_settings 行（存在则覆盖）。"""
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING, {"key": key, "value": value, "now": utc_now_iso()}
        )


def _read_setting(manager, key: str):
    """读取一条 system_settings 行；不存在返回 None。"""
    with manager.session() as session:
        return session.execute(
            text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}
        ).scalar()


def _insert_memory(manager, content: str = "INT-02 memory") -> str:
    """直接插入一条 ACTIVE 记忆（等价于既有测试的最小写入方式）。"""
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {"id": memory_id, "content": content, "now": utc_now_iso()},
        )
    return memory_id


def _jkg_table_names(manager) -> list:
    """当前库里存在的 JKG 表（只读探测，不建表）。"""
    with manager.session() as session:
        return present_jkg_tables(session)


def _memory_digest(manager):
    """memory_records 的行数与有序内容摘要，用于证明 JKG 未改权威表。"""
    with manager.session() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM memory_records")
        ).scalar()
        rows = session.execute(
            text("SELECT id, status FROM memory_records ORDER BY id")
        ).all()
    return int(count or 0), tuple((str(r[0]), str(r[1])) for r in rows)


def _enabled_only_schema_missing(manager) -> JkgCompositionAdapter:
    """构造一个「已启用但没装表」的 adapter。"""
    _set_setting(manager, "jkg.enabled", "1")
    return JkgCompositionAdapter(manager)


# ===========================================================================
# A · 配置解析（纯函数，不连库）
# ===========================================================================
def test_resolve_enabled_default_is_off_when_nothing_is_set():
    """两个来源都没设置时 JKG 默认关闭，来源标记为 default。"""
    assert resolve_enabled(setting_value=None, env_value=None) == (False, "default")
    assert resolve_enabled(setting_value="", env_value="   ") == (False, "default")


def test_resolve_enabled_reads_the_setting_row():
    """数据库行的 "1"/"0" 决定开关，来源标记为 setting。"""
    assert resolve_enabled(setting_value="1", env_value=None) == (True, "setting")
    assert resolve_enabled(setting_value="0", env_value=None) == (False, "setting")


def test_resolve_enabled_environment_off_overrides_an_enabled_setting():
    """环境变量显式关闭是硬关：数据库行写 "1" 也不能覆盖它。"""
    assert resolve_enabled(setting_value="1", env_value="0") == (
        False,
        "env_disabled",
    )
    assert resolve_enabled(setting_value="true", env_value="OFF") == (
        False,
        "env_disabled",
    )


def test_resolve_enabled_environment_on_without_setting():
    """没有数据库行时，环境变量显式开启即可启用，来源标记为 env。"""
    assert resolve_enabled(setting_value=None, env_value="1") == (True, "env")
    assert resolve_enabled(setting_value="", env_value="yes") == (True, "env")


def test_resolve_enabled_unparseable_setting_fails_closed():
    """无法解析的数据库行关闭 JKG 而不是抛异常（fail-closed）。"""
    assert resolve_enabled(setting_value="maybe", env_value=None) == (
        False,
        "setting_invalid",
    )
    assert resolve_enabled(setting_value="maybe", env_value="1") == (
        False,
        "setting_invalid",
    )


def test_resolve_enabled_tolerates_case_and_surrounding_spaces():
    """大小写与首尾空格不影响解析结果。"""
    assert resolve_enabled(setting_value=" TRUE ", env_value=None) == (
        True,
        "setting",
    )
    assert resolve_enabled(setting_value="On", env_value=None) == (True, "setting")
    assert resolve_enabled(setting_value=None, env_value=" Off ") == (
        False,
        "env_disabled",
    )
    assert resolve_enabled(setting_value=None, env_value=" YES ") == (True, "env")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": 0},
        {"max_page_size": 101},
        {"max_scan": 0},
        {"reconcile_batch_limit": 5001},
        {"max_scan": 1001},
        {"page_size": 60, "max_page_size": 55},
    ],
)
def test_jkg_config_rejects_out_of_range_bounds(kwargs):
    """构造期拒绝越界上限，避免出现比查询层更宽的扫描边界。"""
    with pytest.raises(ValueError):
        JkgConfig(**kwargs)


def test_jkg_config_from_values_uses_environment_when_none_is_passed(monkeypatch):
    """from_values 在未显式传参时读取 JARVISMIND_* 环境变量，非法整数抛 ValueError。"""
    monkeypatch.setenv(JKG_ENABLED_ENV, "1")
    cfg = JkgConfig.from_values(setting_value=None, env_value=None)
    assert (cfg.enabled, cfg.enabled_source) == (True, "env")
    assert cfg.max_page_size == DEFAULT_MAX_PAGE_SIZE

    monkeypatch.setenv("JARVISMIND_JKG_PAGE_SIZE", "not-a-number")
    with pytest.raises(ValueError):
        JkgConfig.from_values(setting_value=None, env_value="0")


# ===========================================================================
# B · 默认态：未启用、无表
# ===========================================================================
def test_default_state_is_disabled_and_reports_the_default_source(app):
    """默认设置下状态为 disabled，enabled 为 False，来源为 default。"""
    adapter = app.state.container.jkg_adapter
    assert adapter.state() is JkgState.DISABLED
    status = adapter.status()
    assert status["enabled"] is False
    assert status["enabled_source"] == "default"
    assert status["state"] == "disabled"
    assert status["schema_ready"] is False
    assert status["schema_version_expected"] == JKG_SCHEMA_VERSION


def test_default_state_services_raise_jkg_disabled(app):
    """未启用时两个服务入口都抛 JkgDisabledError（503 / jkg_disabled）。"""
    container = app.state.container
    with pytest.raises(JkgDisabledError) as query_exc:
        _ = container.jkg_query_service
    assert query_exc.value.code == "jkg_disabled"
    assert query_exc.value.status_code == 503
    assert query_exc.value.detail["state"] == "disabled"

    with pytest.raises(JkgDisabledError) as write_exc:
        _ = container.jkg_service
    assert write_exc.value.code == "jkg_disabled"
    assert write_exc.value.status_code == 503


def test_reading_jkg_status_creates_no_table(manager, app):
    """读取状态与拒绝服务都不允许创建任何 jkg_ 对象。"""
    adapter = app.state.container.jkg_adapter
    _ = adapter.status()
    _ = adapter.health()
    _ = adapter.schema_status()
    with pytest.raises(JkgDisabledError):
        adapter.query_service()
    assert _jkg_table_names(manager) == []


# ===========================================================================
# C · 启用但未装表
# ===========================================================================
def test_enabled_without_schema_reports_schema_not_ready(manager):
    """jkg.enabled=1 但没装表时状态为 schema_not_ready 并列出全部缺表。"""
    adapter = _enabled_only_schema_missing(manager)
    assert adapter.state() is JkgState.SCHEMA_NOT_READY
    status = adapter.status()
    assert status["enabled"] is True
    assert status["enabled_source"] == "setting"
    assert status["schema_ready"] is False
    assert status["missing_tables"] == list(JKG_TABLES)
    assert status["present_tables"] == []
    assert adapter.health()["usable"] is False


def test_enabled_without_schema_raises_schema_not_ready_with_missing_tables(manager):
    """服务入口抛 JkgSchemaNotReadyError，detail 里带缺失表清单。"""
    adapter = _enabled_only_schema_missing(manager)
    with pytest.raises(JkgSchemaNotReadyError) as excinfo:
        adapter.query_service()
    assert excinfo.value.code == "jkg_schema_not_ready"
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["missing"] == list(JKG_TABLES)

    with pytest.raises(JkgSchemaNotReadyError):
        adapter.write_service()


def test_reporting_schema_not_ready_never_installs_the_schema(manager):
    """报告 schema_not_ready 不等于偷偷建表：库内 jkg_ 对象仍为零。"""
    adapter = _enabled_only_schema_missing(manager)
    assert adapter.state() is JkgState.SCHEMA_NOT_READY
    with pytest.raises(JkgSchemaNotReadyError):
        adapter.query_service()
    assert _jkg_table_names(manager) == []


# ===========================================================================
# D · 启用且已装表（授权临时库）
# ===========================================================================
def test_ready_state_after_the_authorised_test_schema_install(manager):
    """测试工厂装表 + 启用后状态为 ready。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "1")
    adapter = JkgCompositionAdapter(manager)
    assert adapter.state() is JkgState.READY
    status = adapter.status()
    assert status["schema_ready"] is True
    assert status["missing_tables"] == []
    assert sorted(status["present_tables"]) == sorted(JKG_TABLES)
    assert adapter.health()["usable"] is True


def test_ready_graph_lists_no_nodes(manager):
    """空图上的 list_nodes 返回空页而不是报错。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "1")
    page = JkgCompositionAdapter(manager).query_service().list_nodes()
    assert page["items"] == []
    assert page["total"] == 0
    assert page["limit"] == 50
    assert page["offset"] == 0


def test_ready_graph_integrity_report_is_all_zero_and_not_truncated(manager):
    """空图的完整性报告各项计数为 0，且未被截断。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "1")
    report = JkgCompositionAdapter(manager).query_service().integrity_report()
    assert report["counts"] == {
        "nodes": 0,
        "edges": 0,
        "sources": 0,
        "invalidated_sources": 0,
    }
    assert report["truncated"] is False
    assert report["orphan_edges"] == []


def test_end_to_end_write_then_read_through_the_adapter(manager):
    """经 adapter 端到端写入实体/关系后，查询端能看到 1 条有效边。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "1")
    memory_id = _insert_memory(manager, "INT-02 end-to-end source")
    adapter = JkgCompositionAdapter(manager)

    writer = adapter.write_service()
    person = writer.upsert_node(
        entity_type="PERSON", name="Alice", source_memory_id=memory_id
    )
    project = writer.upsert_node(
        entity_type="PROJECT", name="JarvisMind", source_memory_id=memory_id
    )
    edge = writer.create_edge(
        subject_id=person["node_id"],
        predicate="WORKS_ON",
        object_id=project["node_id"],
        source_memory_ids=[memory_id],
    )
    assert edge["valid_source_count"] == 1

    page = adapter.query_service().list_edges()
    assert len(page["items"]) == 1
    assert page["items"][0]["edge_id"] == edge["edge_id"]
    assert page["items"][0]["valid_source_count"] == 1
    assert page["truncated"] is False


def test_jkg_writes_leave_memory_records_untouched(manager):
    """JKG 写入前后 memory_records 的行数与内容摘要完全一致。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "1")
    memory_id = _insert_memory(manager, "INT-02 read-only authority")
    before = _memory_digest(manager)

    writer = JkgCompositionAdapter(manager).write_service()
    person = writer.upsert_node(
        entity_type="PERSON", name="Bob", source_memory_id=memory_id
    )
    project = writer.upsert_node(
        entity_type="PROJECT", name="JarvisMind", source_memory_id=memory_id
    )
    writer.create_edge(
        subject_id=person["node_id"],
        predicate="WORKS_ON",
        object_id=project["node_id"],
        source_memory_ids=[memory_id],
    )

    assert _memory_digest(manager) == before


# ===========================================================================
# E · 关闭时降级 / JME 不回归
# ===========================================================================
def test_disabled_after_schema_is_installed_degrades_but_jme_stays_alive(manager):
    """已装表但 jkg.enabled=0：状态 disabled、服务拒绝，数据库连接仍正常。"""
    create_jkg_schema(manager)
    _set_setting(manager, "jkg.enabled", "0")
    adapter = JkgCompositionAdapter(manager)
    assert adapter.state() is JkgState.DISABLED
    assert adapter.status()["enabled_source"] == "setting"
    with pytest.raises(JkgDisabledError):
        adapter.query_service()
    assert manager.verify_connection() is True


def test_jme_http_stays_healthy_while_jkg_is_disabled(client, auth_headers, manager):
    """JKG 关闭时 JME 的记忆列表与健康检查都返回 200，且流量不建 JKG 表。"""
    listed = client.get("/api/v1/memories", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    health = client.get("/health", headers=auth_headers)
    assert health.status_code == 200, health.text
    assert _jkg_table_names(manager) == []


def test_status_survives_a_broken_session(monkeypatch, manager):
    """数据库 session 打桩成抛异常后，状态查询仍不抛异常地降级。"""

    def boom(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(manager, "session", boom)
    adapter = JkgCompositionAdapter(manager)
    schema = adapter.schema_status()
    assert schema["schema_ready"] is False
    assert schema["missing_tables"] == list(JKG_TABLES)
    assert schema["present_tables"] == []
    # 读不到设置行 => 走默认（关闭），状态查询依旧不抛异常。
    assert adapter.state() is JkgState.DISABLED
    assert adapter.health()["usable"] is False

    # 显式注入已启用配置后，同一份坏 session 只会得到 schema_not_ready。
    enabled = JkgCompositionAdapter(manager, config=JkgConfig(enabled=True))
    assert enabled.state() is JkgState.SCHEMA_NOT_READY
    with pytest.raises(JkgSchemaNotReadyError):
        enabled.query_service()


# ===========================================================================
# F · 架构边界（静态断言）
# ===========================================================================
_FORBIDDEN_IMPORTS = ("from ..jkg", "from .jkg", "from jarvismind.jkg", "import jarvismind.jkg")
_BOUNDARY_PACKAGES = ("services", "repositories", "database", "core")


def test_no_jme_layer_imports_jkg():
    """services / repositories / database / core 不得出现任何 JKG 导入。"""
    offenders = []
    for package in _BOUNDARY_PACKAGES:
        for path in sorted((JARVISMIND_ROOT / "jarvismind" / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for marker in _FORBIDDEN_IMPORTS:
                if marker in source:
                    offenders.append(f"{path.relative_to(JARVISMIND_ROOT)}: {marker}")
    assert offenders == []


def test_composition_root_imports_jkg_only_lazily():
    """container.py / main.py 的模块级导入区不得出现 jkg（只允许函数体内延迟导入）。"""
    container_source = (JARVISMIND_ROOT / "jarvismind" / "container.py").read_text(encoding="utf-8")
    container_head = container_source.split("@dataclass", 1)[0]
    assert "jkg" not in container_head, "container.py 模块级导入区不允许依赖 JKG"
    assert "from .jkg.adapter import JkgCompositionAdapter" in container_source

    main_source = (JARVISMIND_ROOT / "jarvismind" / "main.py").read_text(encoding="utf-8")
    main_head = main_source.split("def create_app", 1)[0]
    assert "jkg" not in main_head, "main.py 模块级导入区不允许依赖 JKG"


# ===========================================================================
# G · 启动不建表（回归加固）
# ===========================================================================
def test_lifespan_startup_creates_no_jkg_table(manager):
    """走完整 lifespan 后仍无 JKG 对象，也没有 jkg.enabled 行。"""
    settings = manager.settings
    with TestClient(create_app(settings, manager=manager)) as test_client:
        assert test_client.get("/health").status_code == 200
    assert _jkg_table_names(manager) == []
    assert _read_setting(manager, "jkg.enabled") is None


def test_create_app_never_touches_the_jkg_adapter(manager):
    """构造应用后 adapter 字段仍为 None，证明启动链路没有碰它。"""
    app = create_app(manager.settings, manager=manager)
    container = app.state.container
    assert isinstance(container, Container)
    assert container._jkg_adapter is None
    assert container._jkg_adapter is None  # 再读一次也不会被惰性构造
    assert AdapterFromModule is JkgCompositionAdapter
