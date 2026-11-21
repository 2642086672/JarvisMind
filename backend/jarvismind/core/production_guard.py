"""P0-03A-R4 · Central runtime guard for debug / probe / scratch access.

This module is the single choke point that temporary inspection scripts must
use before touching a SQLite file, a token value, or an HTTP API.  It is a
*runtime* defence, independent of the test-phase AST blacklist in
``tests/conftest.py``: a plain ``python scratch_check.py`` process goes through
the same path checks.

Scope boundary (must be reported, never claimed as complete):
    This guard only controls code paths that call it.  An arbitrary Python
    process can still do ``import sqlite3; sqlite3.connect(<any path>)``.
    Process-level denial requires OS-level controls (file ACL / read-only
    mount / dedicated low-privilege account / moving the production data dir
    out of the working tree).  See the remediation risk register.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# jme/app/core/production_guard.py -> parents[2] == jme/
_JME_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DATA_DIR = (_JME_ROOT / "data").resolve()

ENV_DEBUG_ROOT = "JARVISMIND_DEBUG_TMP_ROOT"
ENV_DEBUG_API_ALLOW = "JARVISMIND_DEBUG_API_ALLOW"
ENV_PRODUCTION_DB_ALLOW = "JARVISMIND_ALLOW_PRODUCTION_DB"

# Default service port from jarvismind.config.Settings.port.  A debug probe must not
# silently hit the live service on this port.
DEFAULT_PRODUCTION_PORT = 8777

_BANNED_TOKEN_MARKERS = (".jme" + "_token",)


class ProductionAccessBlocked(RuntimeError):
    """Raised when a debug/probe entry point targets a production resource."""


def _resolve(path: Any) -> Path:
    return Path(str(path)).expanduser().resolve()


def debug_root(explicit: Optional[Any] = None) -> Path:
    """Return the explicitly declared temporary sandbox root.

    No implicit default.  A scratch script that forgets to declare a sandbox
    fails closed instead of falling back to the production data directory.
    """
    raw = explicit if explicit is not None else os.environ.get(ENV_DEBUG_ROOT, "")
    if not raw:
        raise ProductionAccessBlocked(
            "调试/探针未声明临时沙箱目录：必须显式设置 "
            + ENV_DEBUG_ROOT
            + " 或传入 debug_root；不提供默认值，拒绝访问任何数据库"
        )
    root = _resolve(raw)
    if not root.is_dir():
        raise ProductionAccessBlocked(
            f"调试沙箱目录不存在或不是目录，拒绝访问：{root}"
        )
    # A declared sandbox must never be the production data dir itself, nor a
    # parent of it (which would make every production file "inside" it).
    if PRODUCTION_DATA_DIR == root or PRODUCTION_DATA_DIR.is_relative_to(root):
        raise ProductionAccessBlocked(
            "调试沙箱目录覆盖生产数据目录，拒绝接受该声明"
        )
    return root


def assert_debug_db_path(db_path: Any, *, explicit_root: Optional[Any] = None) -> Path:
    """Validate a debug database path; raise instead of opening a connection.

    Rules: path must be inside the declared sandbox, must not be inside the
    production data dir, and must not be one of the production triplet files.
    Pure path normalization only -- no ``exists()`` probe of production files.
    """
    root = debug_root(explicit_root)
    resolved = _resolve(db_path)
    if not resolved.is_relative_to(root):
        raise ProductionAccessBlocked(
            f"数据库路径不在声明的调试沙箱内，拒绝访问：{resolved} (sandbox={root})"
        )
    if resolved.is_relative_to(PRODUCTION_DATA_DIR):
        raise ProductionAccessBlocked(
            f"数据库路径落在生产数据目录内，拒绝访问：{resolved}"
        )
    return resolved


def open_debug_db_readonly(db_path: Any, *, explicit_root: Optional[Any] = None):
    """Open a SQLite connection in read-only mode after the path check.

    ``mode=ro`` prevents DDL/DML and prevents the creation of a new database
    file.  All scratch readers must go through here instead of calling
    ``sqlite3.connect`` with a hardcoded path.
    """
    resolved = assert_debug_db_path(db_path, explicit_root=explicit_root)
    posix = "/" + resolved.as_posix().lstrip("/")
    posix = posix.replace("?", "%3F").replace("#", "%23")
    uri = f"file:{posix}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def assert_debug_token(token: Any) -> str:
    """Accept only an explicitly supplied token *value*, never a file path.

    Reading the production token file is forbidden for debug probes.  A value
    containing a path separator or the token filename is rejected.
    """
    if not isinstance(token, str) or not token.strip():
        raise ProductionAccessBlocked(
            "调试探针必须显式传入 token 值（环境变量或参数）；禁止自动读取凭证文件"
        )
    lowered = token.lower()
    if "/" in token or "\\" in token or any(m in lowered for m in _BANNED_TOKEN_MARKERS):
        raise ProductionAccessBlocked(
            "token 参数看起来是文件路径，拒绝：调试探针不得读取凭证文件"
        )
    return token.strip()


def assert_debug_api_url(url: Any, *, port: Optional[int] = None) -> str:
    """Validate a debug API URL; reject the live service endpoint.

    Requires an explicit ``JARVISMIND_DEBUG_API_ALLOW=1`` opt-in and rejects the
    default production port / loopback production host.
    """
    if not isinstance(url, str) or not url.strip():
        raise ProductionAccessBlocked(
            "调试探针未显式提供 API 地址，拒绝自动调用任何服务"
        )
    if os.environ.get(ENV_DEBUG_API_ALLOW, "") != "1":
        raise ProductionAccessBlocked(
            "调用调试 API 需要显式设置 " + ENV_DEBUG_API_ALLOW + "=1"
        )
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProductionAccessBlocked(f"调试 API 地址不合法：{url!r}")
    effective_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    production_port = port if port is not None else DEFAULT_PRODUCTION_PORT
    if effective_port == production_port and parsed.hostname in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise ProductionAccessBlocked(
            "调试 API 地址指向本机生产服务端口，拒绝自动调用生产 API"
        )
    return url.strip()


def build_debug_request(url: Any, token: Any, *, port: Optional[int] = None):
    """Construct (but do not send) an API request after both checks.

    Returning a Request object keeps network I/O explicit at the call site:
    importing or running a probe never fires a request by itself.
    """
    import urllib.request

    checked_url = assert_debug_api_url(url, port=port)
    checked_token = assert_debug_token(token)
    return urllib.request.Request(
        checked_url, headers={"Authorization": "Bearer " + checked_token}
    )


# ---------------------------------------------------------------------------
# Optional centralized connect interception for debug entry points.
# ---------------------------------------------------------------------------
_ORIGINAL_SQLITE_CONNECT = sqlite3.connect


def guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any):
    """Wrapper refusing any connect() that targets the production data dir.

    Installed explicitly via :func:`install_connect_guard`.  It is not
    installed globally by import, so production startup is unaffected.
    """
    raw = str(database)
    if raw.startswith("file:"):
        path_part = raw[5:].split("?", 1)[0]
    else:
        path_part = raw
    if path_part and path_part != ":memory:":
        try:
            resolved = _resolve(path_part.lstrip("/") if raw.startswith("file:") else path_part)
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and resolved.is_relative_to(PRODUCTION_DATA_DIR):
            if os.environ.get(ENV_PRODUCTION_DB_ALLOW, "") != "1":
                raise ProductionAccessBlocked(
                    "集中连接守卫拒绝访问生产数据库："
                    + str(resolved)
                    + "（调试/探针进程不得连接生产库）"
                )
    return _ORIGINAL_SQLITE_CONNECT(database, *args, **kwargs)


def install_connect_guard() -> None:
    """Install the centralized guard for the current process."""
    if sqlite3.connect is not guarded_sqlite_connect:
        sqlite3.connect = guarded_sqlite_connect  # type: ignore[assignment]


def uninstall_connect_guard() -> None:
    """Restore the original connect (used by tests for exact cleanup)."""
    if sqlite3.connect is guarded_sqlite_connect:
        sqlite3.connect = _ORIGINAL_SQLITE_CONNECT  # type: ignore[assignment]
