"""Versioned SQLite schema migrations（第一阶段：只读检视 + 全量阻断）。

MIGRATIONS 以纯数据形式保存 v1..v6 的全部 SQL，供后续阶段与审计使用；
本阶段不执行其中任何语句。``schema_migrations`` 仍是版本事实来源，
但 ``current_version()`` 只读、绝不建表。

第一阶段对外行为：
- ``inspect_schema()`` 以只读 URI 打开库文件做只读检视，不写任何 SQL。
- ``apply_pending()`` / ``initialize()`` / ``rollback_v6()`` /
  ``initialize_database()`` 一律抛出 ``MigrationBlocked``。拒绝判断在调用栈
  最外层完成，**拒绝路径不调用 inspect_schema、不建立任何数据库连接、
  不执行 SQL/DDL/DML、不产生 -wal/-shm 副作用**。不提供可执行审批机制，
  不自动 DROP/重建，不删除或改写任何 memory/candidate 行。
- ``plan_pending()`` 是离线诊断入口，保留原只读检视 + partial_v6 判定；
  它不在 ``apply_pending()`` 的调用链上，因此不影响拒绝路径的零访问属性。
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from .connection import DatabaseManager

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: Tuple[str, ...]
    # FTS5 virtual tables are optional: some SQLite builds lack the module.
    fts5_statements: Tuple[str, ...] = field(default=())


MIGRATIONS: Tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE memory_sources (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                uri TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE memory_records (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                importance REAL NOT NULL DEFAULT 5.0,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                source_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                accessed_at TEXT,
                expires_at TEXT,
                metadata_json TEXT,
                FOREIGN KEY (source_id) REFERENCES memory_sources(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE memory_audit_logs (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT,
                detail_json TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_memory_records_type ON memory_records(memory_type)",
            "CREATE INDEX idx_memory_records_status ON memory_records(status)",
            "CREATE INDEX idx_memory_records_created_at ON memory_records(created_at)",
            "CREATE INDEX idx_memory_records_updated_at ON memory_records(updated_at)",
            "CREATE INDEX idx_memory_records_source ON memory_records(source_id)",
            "CREATE INDEX idx_memory_audit_memory ON memory_audit_logs(memory_id)",
        ),
        fts5_statements=(
            """
            CREATE VIRTUAL TABLE memory_records_fts USING fts5(
                content,
                summary,
                content='memory_records',
                content_rowid='rowid'
            )
            """,
            """
            CREATE TRIGGER memory_records_ai AFTER INSERT ON memory_records BEGIN
                INSERT INTO memory_records_fts(rowid, content, summary)
                VALUES (new.rowid, new.content, new.summary);
            END
            """,
            """
            CREATE TRIGGER memory_records_ad AFTER DELETE ON memory_records BEGIN
                INSERT INTO memory_records_fts(memory_records_fts, rowid, content, summary)
                VALUES ('delete', old.rowid, old.content, old.summary);
            END
            """,
            """
            CREATE TRIGGER memory_records_au AFTER UPDATE ON memory_records BEGIN
                INSERT INTO memory_records_fts(memory_records_fts, rowid, content, summary)
                VALUES ('delete', old.rowid, old.content, old.summary);
                INSERT INTO memory_records_fts(rowid, content, summary)
                VALUES (new.rowid, new.content, new.summary);
            END
            """,
        ),
    ),
    Migration(
        version=2,
        name="search_index_and_sources",
        statements=(
            # Segmented text maintained by the service layer; the FTS table
            # below indexes it so Chinese substring queries work.
            "ALTER TABLE memory_records ADD COLUMN search_text TEXT",
            "CREATE INDEX idx_memory_sources_type ON memory_sources(source_type)",
            "CREATE INDEX idx_memory_sources_name ON memory_sources(source_name)",
            "CREATE INDEX idx_memory_audit_created_at ON memory_audit_logs(created_at)",
        ),
        fts5_statements=(
            """
            CREATE VIRTUAL TABLE memory_records_fts_zh USING fts5(
                search_text,
                content='memory_records',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            )
            """,
            """
            CREATE TRIGGER memory_records_zh_ai AFTER INSERT ON memory_records BEGIN
                INSERT INTO memory_records_fts_zh(rowid, search_text)
                VALUES (new.rowid, new.search_text);
            END
            """,
            """
            CREATE TRIGGER memory_records_zh_ad AFTER DELETE ON memory_records BEGIN
                INSERT INTO memory_records_fts_zh(memory_records_fts_zh, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
            END
            """,
            """
            CREATE TRIGGER memory_records_zh_au AFTER UPDATE ON memory_records BEGIN
                INSERT INTO memory_records_fts_zh(memory_records_fts_zh, rowid, search_text)
                VALUES ('delete', old.rowid, old.search_text);
                INSERT INTO memory_records_fts_zh(rowid, search_text)
                VALUES (new.rowid, new.search_text);
            END
            """,
        ),
    ),
    Migration(
        version=3,
        name="memory_candidates",
        statements=(
            """
            CREATE TABLE memory_candidates (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                source_id TEXT,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                importance REAL NOT NULL DEFAULT 5.0,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'PENDING',
                duplicate_of TEXT,
                conflicts_with TEXT,
                extraction_engine TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                reject_reason TEXT,
                metadata_json TEXT,
                FOREIGN KEY (source_id) REFERENCES memory_sources(id) ON DELETE SET NULL
            )
            """,
            "CREATE INDEX idx_candidates_status ON memory_candidates(status)",
            "CREATE INDEX idx_candidates_conversation ON memory_candidates(conversation_id)",
            "CREATE INDEX idx_candidates_type ON memory_candidates(memory_type)",
        ),
    ),
    Migration(
        version=4,
        name="hybrid_vector_index",
        statements=(
            "ALTER TABLE memory_records ADD COLUMN vector_status TEXT NOT NULL DEFAULT 'PENDING'",
            "ALTER TABLE memory_records ADD COLUMN vector_updated_at TEXT",
            "CREATE INDEX idx_memory_records_vector_status ON memory_records(vector_status)",
            """
            CREATE TABLE IF NOT EXISTS vector_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=5,
        name="memory_lifecycle",
        statements=(
            # JME-5.1 summaries / reflection candidates
            """
            CREATE TABLE memory_summaries (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                summary_type TEXT NOT NULL,
                memory_type TEXT,
                memory_ids_json TEXT NOT NULL,
                source_ids_json TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                confidence REAL NOT NULL DEFAULT 0.5,
                engine TEXT NOT NULL DEFAULT 'local',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_by TEXT,
                rejected_at TEXT,
                rejected_by TEXT,
                reject_reason TEXT,
                metadata_json TEXT
            )
            """,
            "CREATE INDEX idx_memory_summaries_status ON memory_summaries(status)",
            "CREATE INDEX idx_memory_summaries_type ON memory_summaries(summary_type)",
            "CREATE INDEX idx_memory_summaries_memory_type ON memory_summaries(memory_type)",
            # JME-5.2 corrections / conflict records
            """
            CREATE TABLE memory_corrections (
                id TEXT PRIMARY KEY,
                memory_id_a TEXT NOT NULL,
                memory_id_b TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                chosen_memory_id TEXT,
                corrected_content TEXT,
                reason TEXT,
                source TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                before_state_json TEXT,
                after_state_json TEXT,
                metadata_json TEXT
            )
            """,
            "CREATE INDEX idx_memory_corrections_status ON memory_corrections(status)",
            "CREATE INDEX idx_memory_corrections_a ON memory_corrections(memory_id_a)",
            "CREATE INDEX idx_memory_corrections_b ON memory_corrections(memory_id_b)",
            # JME-5.3 merge groups
            """
            CREATE TABLE memory_merges (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'PENDING',
                target_memory_id TEXT,
                merged_memory_ids_json TEXT NOT NULL,
                new_content TEXT,
                new_summary TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                metadata_json TEXT
            )
            """,
            "CREATE INDEX idx_memory_merges_status ON memory_merges(status)",
            # JME-5.5 archive event log (one row per archive/restore cycle)
            """
            CREATE TABLE memory_archive_records (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                reason TEXT,
                source TEXT,
                previous_status TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                restored_at TEXT,
                restored_by TEXT,
                metadata_json TEXT
            )
            """,
            "CREATE INDEX idx_memory_archive_records_memory ON memory_archive_records(memory_id)",
            "CREATE INDEX idx_memory_archive_records_archived_at ON memory_archive_records(archived_at)",
            # JME-5.4 expiry reviews / lifecycle events
            """
            CREATE TABLE memory_lifecycle_reviews (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                review_type TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_expires_at TEXT,
                new_expires_at TEXT,
                note TEXT,
                actor TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_memory_lifecycle_reviews_memory ON memory_lifecycle_reviews(memory_id)",
            # JME-5.6 forget bookkeeping on the record itself
            "ALTER TABLE memory_records ADD COLUMN forgotten_at TEXT",
            "ALTER TABLE memory_records ADD COLUMN forget_reason TEXT",
            # JME-5.3 merge bookkeeping: originals are kept, flagged as MERGED
            "ALTER TABLE memory_records ADD COLUMN merged_into TEXT",
        ),
    ),
    Migration(
        version=6,
        name="memory_security_gate",
        statements=(
            # --- 003-B: formal-memory uniqueness -------------------------------
            # Historical rows keep NULL: their authorization info is NEVER
            # backfilled automatically (no modification of historical memories).
            "ALTER TABLE memory_records ADD COLUMN source_candidate_id TEXT",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_candidate
               ON memory_records(source_candidate_id)
               WHERE source_candidate_id IS NOT NULL""",
            # --- candidate security state -------------------------------------
            # Existing rows become LEGACY_UNSCANNED and must never be treated
            # as authorized. All new columns are nullable/defaulted so the
            # ALTER never rewrites or drops existing rows.
            "ALTER TABLE memory_candidates ADD COLUMN scan_status TEXT NOT NULL DEFAULT 'LEGACY_UNSCANNED'",
            "ALTER TABLE memory_candidates ADD COLUMN decision_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE memory_candidates ADD COLUMN content_fingerprint TEXT",
            "ALTER TABLE memory_candidates ADD COLUMN ruleset_version TEXT",
            "ALTER TABLE memory_candidates ADD COLUMN required_grants_json TEXT",
            "ALTER TABLE memory_candidates ADD COLUMN message_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE memory_candidates ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE memory_candidates ADD COLUMN app_decision_reported TEXT",
            "ALTER TABLE memory_candidates ADD COLUMN server_security_result TEXT",
            "ALTER TABLE memory_candidates ADD COLUMN final_result TEXT",
            "CREATE INDEX IF NOT EXISTS idx_candidates_scan_status ON memory_candidates(scan_status)",
            # --- BLOCK events (BLOCK only) -------------------------------------
            # Every UNIQUE participant is NOT NULL DEFAULT '' so SQLite's
            # "NULLs are distinct" behaviour cannot defeat idempotency.
            """CREATE TABLE IF NOT EXISTS block_events (
                block_event_id  TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL DEFAULT '',
                message_id      TEXT NOT NULL DEFAULT '',
                turn_id         TEXT NOT NULL DEFAULT '',
                category        TEXT NOT NULL,
                hit_length      INTEGER NOT NULL,
                ruleset_version TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_block_events_turn
               ON block_events(conversation_id, message_id, turn_id, category)""",
            "CREATE INDEX IF NOT EXISTS idx_block_events_message ON block_events(message_id)",
            # --- non-candidate extraction events ------------------------------
            # IGNORE / DUPLICATE / CONFLICT / LOW_VALUE / EXPIRED / EMPTY only.
            # BLOCK is NEVER written here.
            """CREATE TABLE IF NOT EXISTS extraction_events (
                event_id                TEXT PRIMARY KEY,
                conversation_id         TEXT NOT NULL DEFAULT '',
                message_id              TEXT NOT NULL DEFAULT '',
                turn_id                 TEXT NOT NULL DEFAULT '',
                event_type              TEXT NOT NULL,
                ignore_reason_enum      TEXT NOT NULL DEFAULT '',
                app_decision_reported   TEXT,
                server_security_result  TEXT,
                final_result            TEXT,
                ruleset_version         TEXT NOT NULL,
                created_at              TEXT NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_extraction_events_turn
               ON extraction_events(conversation_id, message_id, turn_id,
                                    event_type, ignore_reason_enum)""",
            "CREATE INDEX IF NOT EXISTS idx_extraction_events_conversation ON extraction_events(conversation_id, created_at)",
            # --- approval ledger (atomicity + UNKNOWN recovery) ---------------
            # candidate_id is NOT NULL and has NO `ON DELETE SET NULL` FK:
            # combining those two would be a structural contradiction.
            """CREATE TABLE IF NOT EXISTS approval_events (
                event_id                          TEXT PRIMARY KEY,
                candidate_id                      TEXT NOT NULL,
                decision                          TEXT NOT NULL,
                decision_version                  INTEGER NOT NULL DEFAULT 1,
                content_fingerprint               TEXT NOT NULL DEFAULT '',
                ruleset_version                   TEXT NOT NULL DEFAULT '',
                idempotency_key                   TEXT NOT NULL,
                memory_id                         TEXT,
                actor_label                       TEXT NOT NULL DEFAULT '',
                client_actor_claim                TEXT NOT NULL DEFAULT '',
                authentication_level              TEXT NOT NULL DEFAULT 'API_TOKEN_SHARED',
                device_id                         TEXT,
                interaction_id                    TEXT,
                authorization_verification_result TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
                transaction_result                TEXT NOT NULL DEFAULT 'UNKNOWN',
                operation_result                  TEXT NOT NULL DEFAULT 'INDETERMINATE',
                error_code                        TEXT,
                created_at                        TEXT NOT NULL,
                confirmed_at                      TEXT
            )""",
            # One successful/indeterminate attempt per candidate. A confirmed
            # rollback frees the key so a legitimate retry can proceed.
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_success
               ON approval_events(candidate_id)
               WHERE operation_result IN ('SUCCEEDED', 'INDETERMINATE')
                 AND transaction_result <> 'ROLLED_BACK'""",
            "CREATE INDEX IF NOT EXISTS idx_approval_unknown ON approval_events(transaction_result, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_approval_idem ON approval_events(idempotency_key)",
            # --- reserved for 003-C (Grant / device / key lifecycle) ----------
            # candidate_id is nullable HERE, so `ON DELETE SET NULL` is valid.
            """CREATE TABLE IF NOT EXISTS grants (
                grant_id            TEXT PRIMARY KEY,
                grant_type          TEXT NOT NULL,
                candidate_id        TEXT,
                decision_version    INTEGER,
                content_fingerprint TEXT,
                ruleset_version     TEXT NOT NULL DEFAULT '',
                device_id           TEXT NOT NULL DEFAULT '',
                key_version         INTEGER,
                nonce               TEXT NOT NULL UNIQUE,
                signature           TEXT NOT NULL DEFAULT '',
                pii_categories_json TEXT,
                disclosure_version  TEXT,
                policy_id           TEXT,
                scope_json          TEXT,
                policy_version      TEXT,
                issued_at           TEXT NOT NULL,
                expires_at          TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'ISSUED',
                consumed_at         TEXT,
                revoked_at          TEXT,
                invalidated_reason  TEXT,
                interaction_id      TEXT,
                FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE SET NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_grants_candidate ON grants(candidate_id, status)",
            """CREATE TABLE IF NOT EXISTS device_registry (
                device_id   TEXT NOT NULL,
                key_version INTEGER NOT NULL,
                public_key  TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                platform    TEXT NOT NULL DEFAULT '',
                build_type  TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'ACTIVE',
                paired_at   TEXT NOT NULL,
                paired_by   TEXT,
                revoked_at  TEXT,
                PRIMARY KEY (device_id, key_version)
            )""",
            """CREATE TABLE IF NOT EXISTS pairing_codes (
                code_hash   TEXT PRIMARY KEY,
                device_id   TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                consumed_at TEXT,
                created_at  TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS memory_rejections (
                dedup_key    TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL DEFAULT '',
                reason_code  TEXT NOT NULL,
                rejected_at  TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS secret_key_versions (
                key_version TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'ACTIVE',
                rotated_at  TEXT,
                retired_at  TEXT
            )""",
        ),
    ),
)

# Manual, human-executed rollback for v6. It is intentionally conservative:
# it drops only objects introduced by v6 and never touches historical rows.
# SQLite builds without DROP COLUMN must rebuild the two altered tables instead
# (documented in the 003-B report); no automatic table rebuild is performed.
MIGRATION_V6_ROLLBACK_SQL: Tuple[str, ...] = (
    "DROP INDEX IF EXISTS uq_memory_records_candidate",
    "DROP INDEX IF EXISTS idx_candidates_scan_status",
    "DROP INDEX IF EXISTS uq_block_events_turn",
    "DROP INDEX IF EXISTS idx_block_events_message",
    "DROP INDEX IF EXISTS uq_extraction_events_turn",
    "DROP INDEX IF EXISTS idx_extraction_events_conversation",
    "DROP INDEX IF EXISTS uq_approval_success",
    "DROP INDEX IF EXISTS idx_approval_unknown",
    "DROP INDEX IF EXISTS idx_approval_idem",
    "DROP INDEX IF EXISTS idx_grants_candidate",
    "DROP TABLE IF EXISTS block_events",
    "DROP TABLE IF EXISTS extraction_events",
    "DROP TABLE IF EXISTS approval_events",
    "DROP TABLE IF EXISTS grants",
    "DROP TABLE IF EXISTS device_registry",
    "DROP TABLE IF EXISTS pairing_codes",
    "DROP TABLE IF EXISTS memory_rejections",
    "DROP TABLE IF EXISTS secret_key_versions",
    "DELETE FROM schema_migrations WHERE version = 6",
)


def _supports_fts5(session: Session) -> bool:
    try:
        rows = session.execute(
            text("PRAGMA compile_options")
        ).scalars().all()
    except Exception:
        return False
    return any(str(row).upper() == "ENABLE_FTS5" for row in rows)


def _ensure_migrations_table(session: Session) -> None:
    session.execute(text(SCHEMA_MIGRATIONS_TABLE))


def _applied_versions(session: Session) -> set:
    return {
        int(row)
        for row in session.execute(text("SELECT version FROM schema_migrations")).scalars()
    }


_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
)


def _column_already_present(session: Session, statement: str) -> bool:
    """003-B: make ADD COLUMN idempotent after a partially applied migration.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``. A failed v6 migration can leave
    a column behind even after the transaction rolls back on some drivers, so
    re-running must not abort with ``duplicate column name``.
    """
    match = _ADD_COLUMN.match(statement)
    if match is None:
        return False
    table, column = match.group(1), match.group(2)
    rows = session.execute(text(f"PRAGMA table_info({table})")).all()
    return any(str(row[1]).lower() == column.lower() for row in rows)


# Objects introduced exclusively by v6. A partially applied v6 can leave any
# subset of these behind (e.g. an interrupted process or an external tool).
_V6_OBJECT_NAMES: Tuple[str, ...] = (
    "block_events", "extraction_events", "approval_events", "grants",
    "device_registry", "pairing_codes", "memory_rejections",
    "secret_key_versions", "uq_memory_records_candidate",
    "idx_candidates_scan_status", "uq_block_events_turn",
    "idx_block_events_message", "uq_extraction_events_turn",
    "idx_extraction_events_conversation", "uq_approval_success",
    "idx_approval_unknown", "idx_approval_idem", "idx_grants_candidate",
)


def _v6_partial_objects(session: Session) -> bool:
    placeholders = ", ".join(f":n{i}" for i in range(len(_V6_OBJECT_NAMES)))
    params = {f"n{i}": name for i, name in enumerate(_V6_OBJECT_NAMES)}
    row = session.execute(
        text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ("
            + placeholders
            + ")"
        ),
        params,
    ).scalar()
    return int(row or 0) > 0


class MigrationBlocked(RuntimeError):
    """第一阶段硬阻断：任何会写入 schema 的迁移入口一律拒绝。"""


class PartialMigrationDetected(RuntimeError):
    """v6 部分对象残留且版本未标记，必须人工介入，禁止自动恢复。"""


@dataclass(frozen=True)
class SchemaInspection:
    """inspect_schema 的只读结果快照（不可变）。"""

    schema_migrations_exists: bool
    applied_versions: Tuple[int, ...]
    max_version: int
    code_version: int
    v6_objects_present: Tuple[str, ...]
    partial_v6: bool
    status: str


def _readonly_uri(db_path: Path) -> str:
    """把物理路径转成 sqlite3 只读 URI：file:/D:/...?mode=ro。"""
    # sqlite URI 只认正斜杠；Windows 反斜杠先统一替换
    posix = str(db_path).replace("\\", "/")
    # 盘符绝对路径（D:/...）前补 '/'，否则会被当成相对/UNC 路径误解析
    if not posix.startswith("/"):
        posix = "/" + posix
    # 文件名里的 '?' '#' 必须 %HH 编码，避免被当成 query/fragment 分隔符；
    # 替换在拼接 ?mode=ro 之前完成，查询串自身的 '?' 不受影响
    posix = posix.replace("?", "%3F").replace("#", "%23")
    return f"file:{posix}?mode=ro"


class MigrationRunner:
    """第一阶段：只读检视 + 硬阻断，不执行任何 schema 写入。"""

    def __init__(self, manager: DatabaseManager) -> None:
        self.manager = manager

    def current_version(self) -> int:
        """纯 SELECT 读版本；绝不调用 _ensure_migrations_table，绝不因查询建表。"""
        with self.manager.session() as session:
            # 先确认表是否存在，避免查询不存在的表报错或隐式建表
            exists = session.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='schema_migrations'"
                )
            ).scalar()
            if exists is None:
                return 0
            row = session.execute(
                text("SELECT MAX(version) FROM schema_migrations")
            ).scalar()
            return int(row or 0)

    def inspect_schema(self) -> SchemaInspection:
        """只读检查 schema 状态，返回不可变快照。

        调用本方法会用 sqlite3 以只读 URI 打开数据库文件（mode=ro）。
        只读 URI 只发 SELECT，不执行任何写 SQL/DDL/DML/索引重建/checkpoint，
        但 SQLite 打开 WAL 库时仍可能触碰 -shm 文件，故不能保证 -shm 字节
        完全不变。文件级「零副作用」必须由调用方用 A/B/C 判据（如打开前后
        对比文件哈希、mtime、-wal/-shm 字节）验证；不得把「普通 SELECT 无
        写入」等同于「整个过程零副作用」。

        第一阶段不得在生产启动入口调用它；仅用于离线/人工检视。
        """
        code_version = MIGRATIONS[-1].version
        db_path = Path(self.manager.settings.database_path)

        # 库文件不存在：直接返回 UNINITIALIZED，不打开连接、不抛异常
        if not db_path.exists():
            return SchemaInspection(
                schema_migrations_exists=False,
                applied_versions=(),
                max_version=0,
                code_version=code_version,
                v6_objects_present=(),
                partial_v6=False,
                status="UNINITIALIZED",
            )

        # 只读 URI 打开；closing 确保连接被 close（sqlite3 的 with 不关连接）
        with closing(sqlite3.connect(_readonly_uri(db_path), uri=True)) as conn:
            # 只读 sqlite_master：精确判断 schema_migrations 表是否存在
            schema_migrations_exists = (
                conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                is not None
            )
            # 只读 sqlite_master 取全部对象名，用于检测 v6 残留（含索引/触发器）
            present_set = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master")
            }
            # v6_objects_present 按 _V6_OBJECT_NAMES 顺序取交集，保证确定性
            v6_objects_present = tuple(
                name for name in _V6_OBJECT_NAMES if name in present_set
            )
            if schema_migrations_exists:
                applied_versions = tuple(
                    int(row[0])
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
            else:
                applied_versions = ()

        max_version = max(applied_versions) if applied_versions else 0
        applied_set = set(applied_versions)
        # partial_v6：存在 v6 对象但版本 6 未标记，说明上次迁移中途失败
        partial_v6 = bool(v6_objects_present) and 6 not in applied_set
        # MISSING_MIDDLE：1..max_version 之间存在缺号
        missing_middle = bool(set(range(1, max_version + 1)) - applied_set)

        # 状态优先级：部分残留最优先暴露，其次版本/缺号异常，最后兼容/待迁移
        if partial_v6:
            status = "PARTIAL_V6"
        elif max_version > code_version:
            status = "VERSION_TOO_NEW"
        elif missing_middle:
            status = "MISSING_MIDDLE"
        elif max_version == code_version and 6 in applied_set:
            status = "COMPATIBLE"
        else:
            status = "PENDING_MIGRATION"

        return SchemaInspection(
            schema_migrations_exists=schema_migrations_exists,
            applied_versions=applied_versions,
            max_version=max_version,
            code_version=code_version,
            v6_objects_present=v6_objects_present,
            partial_v6=partial_v6,
            status=status,
        )

    def apply_pending(self) -> int:
        """P0-03A-R2：拒绝判断先于一切数据库访问，函数体内零 I/O。

        第一阶段不实现任何可执行迁移，也不提供审批 / 布尔 / 凭据绕过。
        拒绝必须在调用栈最外层完成：本函数体不得出现 inspect_schema()、
        sqlite3.connect()、manager.session()、Engine.connect() 或任何 SQL。
        因此拒绝路径不会建立数据库连接、不会执行 SELECT/DDL/DML，也不会
        因打开 WAL 库而触碰 -shm / -wal 文件。

        partial_v6 的人工介入判定已归位到离线诊断入口 plan_pending()：
        诊断需要读 schema，但它与拒绝路径彻底分离，不得被 apply_pending
        间接调用，也不得通过捕获异常把诊断失败伪装成拒绝成功。
        """
        raise MigrationBlocked("第一阶段禁用所有 schema 迁移写入（拒绝前零数据库访问）")

    def plan_pending(self) -> SchemaInspection:
        """离线诊断入口：保留原只读检视 + partial_v6 人工介入判定。

        只由离线/人工检视流程调用；不在 apply_pending() 调用链上。
        正常（允许）诊断路径的行为与整改前一致：先 inspect_schema()，
        若检测到 v6 部分对象残留则抛 PartialMigrationDetected。
        """
        inspection = self.inspect_schema()
        if inspection.partial_v6:
            raise PartialMigrationDetected(
                "检测到 v6 部分对象残留且版本未标记："
                f"{inspection.v6_objects_present}"
            )
        return inspection

    def initialize(self) -> int:
        """无条件阻断；不得间接调用 apply_pending。"""
        raise MigrationBlocked("第一阶段禁用数据库初始化")

    def rollback_v6(self) -> None:
        """无条件阻断；不执行任何 DROP/DELETE。"""
        raise MigrationBlocked("第一阶段禁用 v6 回滚")


def initialize_database(manager: DatabaseManager) -> int:
    """模块级入口无条件阻断；保留签名供上层按契约调用。"""
    raise MigrationBlocked("第一阶段禁用数据库初始化入口")
