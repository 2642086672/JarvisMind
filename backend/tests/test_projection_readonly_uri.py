"""Projection read-only URI construction (macOS POSIX path fix).

The derived-module projection opens JME through a SQLite ``file:`` URI. On
POSIX systems an absolute path must not be prefixed with an extra slash:
``file://srv/...`` makes SQLite read ``srv`` as the URI authority and fail
with ``invalid uri authority``. These tests pin both the URI shape and a real
read-only open so the regression cannot silently return.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from jarvismind.governance.projection import _open_readonly, _readonly_uri


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/srv/jarvismind/x/jarvismind.db", "file:/srv/jarvismind/x/jarvismind.db?mode=ro"),
        ("/var/folders/yx/t/jarvismind.db", "file:/var/folders/yx/t/jarvismind.db?mode=ro"),
        ("/private/tmp/a.db", "file:/private/tmp/a.db?mode=ro"),
    ],
)
def test_posix_absolute_path_keeps_single_leading_slash(path, expected):
    uri = _readonly_uri(path)
    assert uri == expected
    assert not uri.startswith("file://")
    # The authority must be empty/absent, never a path segment.
    assert uri[len("file:"):].startswith("/")


@pytest.mark.parametrize(
    "path",
    [r"C:\srv\jarvismind\jarvismind.db", r"D:\work\jarvis\jarvismind.db", "C:/srv/jarvismind/jarvismind.db"],
)
def test_windows_drive_path_becomes_single_slash_drive(path):
    uri = _readonly_uri(path)
    assert uri.startswith("file:/C:") or uri.startswith("file:/D:")
    assert "\\" not in uri
    assert "?mode=ro" in uri


@pytest.mark.parametrize("path", ["relative/jarvismind.db", "jarvismind.db", "./data/jarvismind.db"])
def test_relative_path_gets_a_leading_slash(path):
    uri = _readonly_uri(path)
    assert uri.startswith("file:/")
    assert uri.endswith("?mode=ro")


def test_uri_reserved_characters_are_percent_encoded():
    uri = _readonly_uri("/tmp/a?b#c.db")
    assert "%3F" in uri
    assert "%23" in uri
    assert "?mode=ro" in uri


def test_real_absolute_path_opens_read_only(manager):
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records("
                "id, memory_type, content, importance, confidence, status, "
                "created_at, updated_at, source_id, metadata_json, search_text"
                ") VALUES ('M-URI', 'NOTE', 'uri probe', 5.0, 0.9, 'ACTIVE', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
                "NULL, NULL, 'uri probe')"
            )
        )
    db_path = str(manager.engine.url.database)
    # The POSIX double-slash bug only applies to POSIX absolute paths; on
    # Windows the session database is a drive path, so only require absolute.
    assert Path(db_path).is_absolute()
    if os.name == "posix":
        assert db_path.startswith("/")

    conn = _open_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT content FROM memory_records WHERE id='M-URI'"
        ).fetchone()
        assert row[0] == "uri probe"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO memory_records(id) VALUES ('M-URI-2')")
    finally:
        conn.close()


def test_nonexistent_absolute_path_fails_as_missing_not_invalid_uri(tmp_path):
    missing = str((tmp_path / "absent.db").resolve())
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        _open_readonly(missing)
    assert "invalid uri" not in str(excinfo.value).lower()
